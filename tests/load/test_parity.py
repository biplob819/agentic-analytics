"""LOD-03: parity fingerprints on both engines, compared exactly; a mismatch blocks (US9, H3)."""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.load import (
    ParityError,
    clickhouse_runner,
    executor_runner,
    load_clickhouse,
    load_snowflake,
    require_parity,
    run_parity,
    snowflake_runner,
)
from agentic_analytics.load.parity import QueryRunner, compare, load_parity_spec
from agentic_analytics.packs import load_pack
from agentic_analytics.packs.uk_price_paid.transforms import apply_row
from agentic_analytics.schemas import Engine, Execution, ResultSet
from tests.load.fakes import (
    FakeClickHouse,
    FakeSnowflake,
    apply_nullable,
    fixed_clock,
    fixture_download,
)

SHA = "ab" * 32


@pytest.fixture
def pack():
    return load_pack("uk_price_paid")


def loaded(
    pack, tmp_path: Path, sf_transform=apply_nullable
) -> tuple[FakeClickHouse, FakeSnowflake]:
    ch, sf = FakeClickHouse(pack), FakeSnowflake(pack, transform=sf_transform)
    load_clickhouse(pack, ch)
    load_snowflake(pack, sf, workdir=tmp_path / "snap", download=fixture_download())
    return ch, sf


def parity(pack, ch, sf, tmp_path: Path, **kw: Any):
    runners: dict[Engine, QueryRunner] = {
        "clickhouse": clickhouse_runner(ch),
        "snowflake": snowflake_runner(sf),
    }
    return run_parity(
        pack, runners, out_dir=tmp_path / "m", now=fixed_clock(), snapshot_sha256=SHA, **kw
    )


def test_identical_loads_pass_and_the_report_is_written(pack, tmp_path: Path) -> None:
    ch, sf = loaded(pack, tmp_path)
    report = parity(pack, ch, sf, tmp_path)
    assert report.status == "pass" and report.passed
    assert all(f.match for f in report.fingerprints) and len(report.fingerprints) == 17
    assert report.path == tmp_path / "m" / "parity-2026-11-15.json"
    assert report.path is not None
    data = json.loads(report.path.read_text(encoding="utf-8"))
    assert data["status"] == "pass" and data["passed"] is True
    assert data["snapshot_sha256"] == SHA and data["pack"] == "uk_price_paid@1.0"
    fp = {f["name"]: f for f in data["fingerprints"]}
    assert fp["row_count"]["results"]["clickhouse"]["rows"] == [[30]]
    assert fp["row_count"]["results"]["snowflake"]["columns"] == ["N"]
    assert fp["date_range"]["results"]["clickhouse"]["rows"] == [["1995-01-27", "2026-10-14"]]
    require_parity(report)


def _with(field: str, fn):
    def transform(raw: list[str]) -> dict[str, object]:
        row = apply_row(raw)
        row[field] = fn(row[field], raw)
        return row

    return transform


@pytest.mark.parametrize(
    ("transform", "failing"),
    [
        (_with("is_new", lambda v, raw: 1 - int(str(v))), {"by_is_new"}),
        (
            _with("price", lambda v, raw: int(str(v)) + (1 if raw[1] == "1250000" else 0)),
            {"sum_price"},
        ),
        (_with("type", lambda v, raw: "other" if v == "flat" else v), {"by_type"}),
        (_with("duration", lambda v, raw: v if raw[6] != "U" else "U"), {"by_duration"}),
        (
            _with("date", lambda v, raw: None if raw[2].startswith("2026-10") else v),
            {"date_range", "per_year"},
        ),
        (
            _with("town", lambda v, raw: "LONDON" if v == "ST ALBANS" else v),
            {"distinct_towns", "text_town"},
        ),
    ],
)
def test_a_transform_difference_is_caught_by_its_fingerprint(
    pack, tmp_path: Path, transform, failing: set[str]
) -> None:
    ch, sf = loaded(pack, tmp_path, sf_transform=transform)
    report = parity(pack, ch, sf, tmp_path)
    assert report.status == "fail" and not report.passed
    assert {f.name for f in report.fingerprints if f.match is False} == failing
    with pytest.raises(ParityError, match="parity failed"):
        require_parity(report)
    assert json.loads(report.path.read_text(encoding="utf-8"))["passed"] is False  # type: ignore[union-attr]


def test_a_sum_off_by_one_fails_even_at_benchmark_scale() -> None:
    """Exact means exact: sql_match's default rel_tol would accept this difference."""
    a = ResultSet(columns=("total_price",), rows=((7_912_345_678_901,),))
    b = ResultSet(columns=("TOTAL_PRICE",), rows=((7_912_345_678_902,),))
    ok, reason = compare({"clickhouse": a, "snowflake": b}, ordered=False)
    assert not ok and reason
    assert compare({"clickhouse": a, "snowflake": a}, ordered=False) == (True, None)


def test_a_missing_row_fails_row_count(pack, tmp_path: Path) -> None:
    ch, sf = loaded(pack, tmp_path)
    sf.tables["ANALYTICS.UK.uk_price_paid"].pop()
    report = parity(pack, ch, sf, tmp_path)
    assert "row_count" in {f.name for f in report.fingerprints if f.match is False}


def test_column_names_must_match_not_just_values() -> None:
    a = ResultSet(columns=("tenure", "n"), rows=(("freehold", 1),))
    b = ResultSet(columns=("duration", "n"), rows=(("freehold", 1),))
    ok, reason = compare({"clickhouse": a, "snowflake": b}, ordered=False)
    assert not ok and "column names differ" in (reason or "")


def test_ordered_fingerprints_compare_row_order() -> None:
    a = ResultSet(columns=("y", "n"), rows=((1995, 1), (1996, 2)))
    b = ResultSet(columns=("y", "n"), rows=((1996, 2), (1995, 1)))
    assert compare({"clickhouse": a, "snowflake": b}, ordered=False)[0]
    assert not compare({"clickhouse": a, "snowflake": b}, ordered=True)[0]


def test_a_failing_query_fails_parity_and_is_recorded(pack, tmp_path: Path) -> None:
    ch, _ = loaded(pack, tmp_path)

    def broken(sql: str) -> ResultSet:
        raise RuntimeError("warehouse suspended")

    report = run_parity(
        pack,
        {"clickhouse": clickhouse_runner(ch), "snowflake": broken},
        out_dir=None,
    )
    assert report.status == "fail" and report.path is None
    assert all(
        f.errors == {"snowflake": "RuntimeError: warehouse suspended"} for f in report.fingerprints
    )


def test_single_engine_records_fingerprints_without_blocking(pack, tmp_path: Path) -> None:
    ch, _ = loaded(pack, tmp_path)
    report = run_parity(pack, {"clickhouse": clickhouse_runner(ch)}, out_dir=tmp_path)
    assert report.status == "single_engine" and report.passed
    assert all(f.match is None for f in report.fingerprints)
    require_parity(report)


def test_single_engine_query_failure_still_fails(pack) -> None:
    def broken(sql: str) -> ResultSet:
        raise RuntimeError("down")

    assert run_parity(pack, {"snowflake": broken}, out_dir=None).status == "fail"


def test_runner_and_argument_checks(pack) -> None:
    with pytest.raises(ValueError, match="at least one"):
        run_parity(pack, {}, out_dir=None)
    with pytest.raises(ValueError, match="no engine"):
        run_parity(pack, {"duckdb": lambda s: None}, out_dir=None)  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="sha256"):
        run_parity(pack, {"clickhouse": lambda s: None}, out_dir=None, snapshot_sha256="xyz")  # type: ignore[dict-item,return-value]


def _pack_with_parity(tmp_path: Path, text: str):
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    (dst / "parity.yaml").write_text(text, encoding="utf-8")
    return load_pack(dst)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "version: 1\nfingerprints:\n  - name: x\n    clickhouse: DELETE FROM {database}.t\n"
            "    snowflake: SELECT 1 AS n\n",
            "read-only guard",
        ),
        (
            "version: 1\nfingerprints:\n  - name: x\n"
            "    clickhouse: SELECT * FROM url('http://e.invalid', CSV)\n"
            "    snowflake: SELECT 1 AS n\n",
            "read-only guard",
        ),
        ("version: 1\nfingerprints:\n  - name: x\n    clickhouse: SELECT 1 AS n\n", "no snowflake"),
        (
            "version: 1\nfingerprints:\n  - {name: x, clickhouse: SELECT 1, snowflake: SELECT 1}\n"
            "  - {name: x, clickhouse: SELECT 1, snowflake: SELECT 1}\n",
            "unique",
        ),
        ("version: 1\nfingerprints: []\n", "fingerprints"),
        (
            "version: 1\nfingerprints:\n  - {name: x, clickhouse: SELECT 1, mysql: SELECT 1}\n",
            "mysql",
        ),
    ],
)
def test_bad_parity_specs_are_refused(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ParityError, match=match):
        load_parity_spec(_pack_with_parity(tmp_path, text))


class _Exec:
    engine = "clickhouse"

    def __init__(self, ex: Execution) -> None:
        self.ex = ex

    def execute(self, sql: str) -> Execution:
        return self.ex


def _execution(**kw: Any) -> Execution:
    base: dict[str, Any] = dict(
        engine="clickhouse",
        sql="SELECT 1",
        result=None,
        rows_total=None,
        truncated=False,
        query_id="q",
        elapsed_ms=1,
        error="sql_error",
    )
    base.update(kw)
    return Execution(**base)


def test_executor_runner_returns_results_and_raises_on_errors() -> None:
    rs = ResultSet(columns=("n",), rows=((1,),))
    ok = _execution(result=rs, rows_total=1, error=None)
    assert executor_runner(_Exec(ok))("SELECT 1") == rs
    with pytest.raises(Exception, match="sql_error"):
        executor_runner(_Exec(_execution()))("SELECT 1")


TEXT_COLUMNS = ("postcode1", "postcode2", "addr1", "addr2", "street", "locality", "town",
                "district", "county")  # fmt: skip


def test_a_district_county_swap_fails_parity(pack, tmp_path: Path) -> None:
    def swap(raw: list[Any]) -> dict[str, object]:
        row = apply_row(raw)
        row["district"], row["county"] = row["county"], row["district"]
        return row

    ch, sf = loaded(pack, tmp_path, sf_transform=swap)
    report = parity(pack, ch, sf, tmp_path)
    assert report.status == "fail"
    assert {f.name for f in report.fingerprints if f.match is False} == {
        "text_district",
        "text_county",
    }


@pytest.mark.parametrize("column", TEXT_COLUMNS)
def test_a_change_to_any_text_column_fails_its_fingerprint(
    pack, tmp_path: Path, column: str
) -> None:
    ch, sf = loaded(pack, tmp_path, sf_transform=_with(column, lambda v, raw: str(v)[:-1]))
    report = parity(pack, ch, sf, tmp_path)
    assert f"text_{column}" in {f.name for f in report.fingerprints if f.match is False}


def test_null_if_empty_in_the_snowflake_file_format_fails_parity(tmp_path: Path) -> None:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    f = dst / "load" / "snowflake.sql"
    text = f.read_text(encoding="utf-8")
    assert text.count("    NULL_IF = ()\n") == 1
    f.write_text(text.replace("    NULL_IF = ()\n", "    NULL_IF = ('')\n"), encoding="utf-8")
    pack = load_pack(dst)
    ch, sf = loaded(pack, tmp_path)
    report = parity(pack, ch, sf, tmp_path)
    failing = {f.name for f in report.fingerprints if f.match is False}
    assert report.status == "fail"
    assert {"text_postcode1", "text_postcode2", "text_addr2", "text_locality"} <= failing
