"""LOD-02: the Snowflake loader stages the snapshot, copies it raw and builds the typed table
with the same transforms (one spec), idempotently, on an admin connection."""

from __future__ import annotations

import hashlib
import shutil
from importlib import resources
from pathlib import Path

import pytest

from agentic_analytics.load import LoaderError, load_snowflake
from agentic_analytics.load.snowflake import load_steps
from agentic_analytics.packs import load_pack
from agentic_analytics.packs.uk_price_paid.transforms import apply_row
from tests.load.fakes import FIXTURE, FakeSnowflake, fixed_clock, fixture_download, read_fixture

TYPED = "ANALYTICS.UK.uk_price_paid"
RAW = "ANALYTICS.UK.uk_price_paid_raw"


@pytest.fixture
def pack():
    return load_pack("uk_price_paid")


def heads(statements) -> list[str]:
    return [" ".join(s.split()[:2]).upper() for s in statements]


def test_fresh_load_downloads_stages_copies_and_types(pack, tmp_path: Path) -> None:
    sf = FakeSnowflake(pack)
    dl = fixture_download()
    res = load_snowflake(pack, sf, workdir=tmp_path, download=dl, now=fixed_clock())
    assert res.action == "loaded" and res.target == "ANALYTICS.UK"
    assert len(dl.calls) == 1  # type: ignore[attr-defined]
    assert heads(res.statements) == [
        "CREATE DATABASE", "CREATE SCHEMA", "CREATE OR", "CREATE OR", "CREATE OR",
        "CREATE TABLE", heads(res.statements)[6], "COPY INTO", "TRUNCATE TABLE", "INSERT INTO",
    ]  # fmt: skip
    assert res.statements[6].startswith("PUT 'file://")
    put = next(s for s in res.statements if s.startswith("PUT"))
    assert f"file://{(tmp_path / 'pp-complete.csv.gz').resolve().as_posix()}'" in put
    copy = next(s for s in res.statements if s.startswith("COPY"))
    assert "@ANALYTICS.UK.uk_price_paid_stage/pp-complete.csv.gz" in copy
    expected = [apply_row(r) for r in read_fixture()]
    assert sf.tables[TYPED] == expected and len(sf.tables[RAW]) == 30
    assert dict(res.rows_after) == {TYPED: 30}
    assert res.snapshot["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert sf.cursors_open == 0


def test_load_phase_order(pack, tmp_path: Path) -> None:
    res = load_snowflake(pack, FakeSnowflake(pack), workdir=tmp_path, download=fixture_download())
    kinds = [s.split()[0].upper() for s in res.statements[6:]]
    assert kinds == ["PUT", "COPY", "TRUNCATE", "INSERT"]


def test_second_run_skips_and_does_not_download(pack, tmp_path: Path) -> None:
    sf = FakeSnowflake(pack)
    load_snowflake(pack, sf, workdir=tmp_path, download=fixture_download())
    dl = fixture_download()
    res = load_snowflake(pack, sf, workdir=tmp_path, download=dl)
    assert res.action == "skipped" and dl.calls == []  # type: ignore[attr-defined]
    assert all(s.startswith("CREATE") for s in res.statements)
    assert dict(res.rows_after) == {TYPED: 30} and res.snapshot == {}


def test_force_reloads_without_duplicating_rows(pack, tmp_path: Path) -> None:
    sf = FakeSnowflake(pack)
    load_snowflake(pack, sf, workdir=tmp_path, download=fixture_download())
    res = load_snowflake(pack, sf, workdir=tmp_path, download=fixture_download(), force=True)
    assert res.action == "reloaded"
    assert dict(res.rows_before) == dict(res.rows_after) == {TYPED: 30}


def test_a_given_snapshot_is_reused(pack, tmp_path: Path) -> None:
    from agentic_analytics.load.snapshot import download_snapshot

    snap = download_snapshot("http://h.example/pp.csv", tmp_path, download=fixture_download())
    dl = fixture_download()
    res = load_snowflake(pack, FakeSnowflake(pack), workdir=tmp_path, download=dl, snapshot=snap)
    assert dl.calls == [] and res.snapshot["sha256"] == snap.sha256  # type: ignore[attr-defined]


def test_raw_and_typed_counts_must_agree(pack, tmp_path: Path) -> None:
    class Lossy(FakeSnowflake):
        def execute(self, sql: str):
            out = super().execute(sql)
            if sql.startswith("INSERT INTO") and self.tables.get(TYPED):
                self.tables[TYPED].pop()
            return out

    with pytest.raises(LoaderError, match="raw table has 30"):
        load_snowflake(pack, Lossy(pack), workdir=tmp_path, download=fixture_download())


def test_copy_aborts_on_a_malformed_file(pack, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text('"a","b"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="COPY aborted"):
        load_snowflake(
            pack, FakeSnowflake(pack), workdir=tmp_path / "w", download=fixture_download(bad)
        )


def test_every_statement_is_on_the_allow_list(pack) -> None:
    steps = load_steps(pack, local_file="/tmp/pp.csv.gz", stage_file="pp.csv.gz")
    assert [s.kind for s in steps] == [
        "create_database",
        "create_schema",
        "create_file_format",
        "create_stage",
        "create_table",
        "create_table",
        "put",
        "copy",
        "truncate",
        "insert",
    ]


@pytest.mark.parametrize("bad", ["/tmp/x'; DROP TABLE t; --.gz", "/tmp/$x.gz"])
def test_local_file_paths_cannot_inject_sql(pack, bad: str) -> None:
    with pytest.raises(ValueError, match="SQL literal"):
        load_steps(pack, local_file=bad, stage_file="x.gz")


def test_stage_file_must_be_a_bare_name(pack) -> None:
    with pytest.raises(ValueError, match="bare file name"):
        load_steps(pack, local_file="/tmp/x.gz", stage_file="../x.gz")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("PUT 'file://{local_file}'", "PUT 'file:///home/someone/.ssh/id_rsa'"),
        ("uk_price_paid_stage/{stage_file}", "uk_price_paid_stage/id_rsa"),
    ],
)
def test_put_and_copy_move_only_this_loads_snapshot(tmp_path: Path, old: str, new: str) -> None:
    """Verifier: a pack could PUT any local file into its stage and COPY it into the raw table,
    which the read-only role can select."""
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    f = dst / "load" / "snowflake.sql"
    text = f.read_text(encoding="utf-8")
    assert text.count(old) == 1
    f.write_text(text.replace(old, new), encoding="utf-8")
    pack = load_pack(dst)
    with pytest.raises(LoaderError, match="other than this load's snapshot"):
        load_steps(pack, local_file="/tmp/w/pp-complete.csv.gz", stage_file="pp-complete.csv.gz")
    sf = FakeSnowflake(pack)
    with pytest.raises(LoaderError, match="other than this load's snapshot"):
        load_snowflake(pack, sf, workdir=tmp_path / "w", download=fixture_download())
    assert sf.statements == []


def test_a_pack_cannot_smuggle_a_statement_into_the_snowflake_load(tmp_path: Path) -> None:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    f = dst / "load" / "snowflake.sql"
    f.write_text(
        f.read_text(encoding="utf-8") + "\nGRANT ROLE ACCOUNTADMIN TO USER x;\n", encoding="utf-8"
    )
    pack = load_pack(dst)
    sf = FakeSnowflake(pack)
    with pytest.raises(LoaderError, match="not allowed"):
        load_snowflake(pack, sf, workdir=tmp_path / "w", download=fixture_download())
    assert sf.statements == []


def test_snowflake_database_must_be_database_dot_schema(tmp_path: Path) -> None:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    y = dst / "pack.yaml"
    y.write_text(
        y.read_text(encoding="utf-8").replace("snowflake: ANALYTICS.UK", "snowflake: ANALYTICS"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="2 dot-separated"):
        load_steps(load_pack(dst), local_file="/x.gz", stage_file="x.gz")


def test_file_format_stage_and_raw_table_are_replaced_typed_table_is_checked(pack) -> None:
    steps = {s.targets[0]: s.sql for s in load_steps(pack, local_file="/x.gz", stage_file="x.gz")
             if s.kind.startswith("create_")}  # fmt: skip
    for name in ("uk_price_paid_csv", "uk_price_paid_stage", "uk_price_paid_raw"):
        assert steps[f"ANALYTICS.UK.{name}"].startswith("CREATE OR REPLACE "), name
    assert steps[TYPED].startswith("CREATE TABLE IF NOT EXISTS ")


def test_file_format_keeps_empty_strings(pack) -> None:
    fmt = next(s.sql for s in load_steps(pack, local_file="/x.gz", stage_file="x.gz")
               if s.kind == "create_file_format")  # fmt: skip
    flat = " ".join(fmt.split())
    assert "NULL_IF = ()" in flat and "EMPTY_FIELD_AS_NULL = FALSE" in flat


def _stale(pack) -> FakeSnowflake:
    sf = FakeSnowflake(pack)
    sf.execute(f"CREATE TABLE IF NOT EXISTS {TYPED} (price NUMBER(10, 0), is_new BOOLEAN)")
    return sf


def test_a_typed_table_with_other_columns_is_refused(pack, tmp_path: Path) -> None:
    sf = _stale(pack)
    dl = fixture_download()
    with pytest.raises(LoaderError, match=r"exists with other columns .*--force"):
        load_snowflake(pack, sf, workdir=tmp_path, download=dl)
    assert dl.calls == []  # type: ignore[attr-defined]


def test_force_replaces_a_typed_table_with_other_columns(pack, tmp_path: Path) -> None:
    sf = _stale(pack)
    res = load_snowflake(pack, sf, workdir=tmp_path, download=fixture_download(), force=True)
    assert f"DROP TABLE {TYPED}" in res.statements
    assert res.action == "reloaded" and dict(res.rows_after) == {TYPED: 30}


def test_d11_window_against_the_clickhouse_load(pack, tmp_path: Path) -> None:
    import datetime as dt

    t = dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)  # fixed_clock's time
    ok = load_snowflake(
        pack, FakeSnowflake(pack), workdir=tmp_path / "a", download=fixture_download(),
        clickhouse_loaded_at=t - dt.timedelta(minutes=50), now=fixed_clock(),
    )  # fmt: skip
    assert ok.action == "loaded"
    sf = FakeSnowflake(pack)
    with pytest.raises(LoaderError, match="D-11"):
        load_snowflake(
            pack, sf, workdir=tmp_path / "b", download=fixture_download(),
            clickhouse_loaded_at=t - dt.timedelta(hours=2), now=fixed_clock(),
        )  # fmt: skip
    assert not any(s.startswith("PUT") for s in sf.statements)
