"""LOD-06: the as-of date is the last complete month in the snapshot, written to config (D-10)."""

from __future__ import annotations

import calendar
import datetime as dt
import shutil
from importlib import resources
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentic_analytics.load import (
    ParityError,
    as_of_for,
    clickhouse_runner,
    last_complete_month,
    load_clickhouse,
    load_snowflake,
    max_date_from_parity,
    run_parity,
    snowflake_runner,
    write_as_of,
)
from agentic_analytics.load.parity import FingerprintResult, ParityReport
from agentic_analytics.packs import load_pack
from agentic_analytics.schemas import ResultSet, load_run_config
from tests.load.fakes import FakeClickHouse, FakeSnowflake, fixed_clock, fixture_download

D = dt.date


@pytest.mark.parametrize(
    ("max_date", "as_of"),
    [
        (D(2026, 10, 31), D(2026, 9, 30)),  # D-50: a month ending on its last day still counts
        (D(2026, 10, 30), D(2026, 9, 30)),  # as partly registered; one day apart, same as-of
        (D(2026, 10, 14), D(2026, 9, 30)),
        (D(2026, 10, 1), D(2026, 9, 30)),
        (D(2026, 3, 1), D(2026, 2, 28)),
        (D(2024, 3, 15), D(2024, 2, 29)),
        (D(2024, 2, 29), D(2024, 1, 31)),
        (D(2025, 1, 15), D(2024, 12, 31)),
        (D(2025, 12, 31), D(2025, 11, 30)),
    ],
)
def test_last_complete_month_examples(max_date: dt.date, as_of: dt.date) -> None:
    assert last_complete_month(max_date) == as_of


@given(st.dates(min_value=D(1995, 2, 1), max_value=D(2100, 12, 31)))
def test_last_complete_month_properties(d: dt.date) -> None:
    a = last_complete_month(d)
    assert a.day == calendar.monthrange(a.year, a.month)[1]  # always a month end
    assert a < d.replace(day=1)  # never inside max_date's month
    assert (a.year, a.month) == ((d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12))
    assert last_complete_month(d.replace(day=1)) == a  # any day of the month gives the same


def test_as_of_must_not_be_after_the_download_date() -> None:
    pack = load_pack("uk_price_paid")
    assert as_of_for(pack, D(2026, 10, 14), downloaded_on=D(2026, 11, 15)) == D(2026, 9, 30)
    assert as_of_for(pack, D(2026, 10, 14), downloaded_on=D(2026, 9, 30)) == D(2026, 9, 30)
    with pytest.raises(ValueError, match="after the snapshot download date"):
        as_of_for(pack, D(2026, 12, 5), downloaded_on=D(2026, 11, 15))


def test_datetimes_are_refused() -> None:
    with pytest.raises(TypeError):
        last_complete_month(dt.datetime(2026, 10, 14, 12, 0))  # type: ignore[arg-type]


def test_fixed_rule_uses_the_pack_date(tmp_path: Path) -> None:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    y = dst / "pack.yaml"
    y.write_text(
        y.read_text(encoding="utf-8").replace(
            "as_of_rule: last_complete_month", "as_of_rule: fixed\nas_of: 2025-06-30"
        ),
        encoding="utf-8",
    )
    assert as_of_for(load_pack(dst), D(2026, 10, 14)) == D(2025, 6, 30)
    with pytest.raises(ValueError, match="download date"):
        as_of_for(load_pack(dst), D(2026, 10, 14), downloaded_on=D(2025, 6, 1))
    assert as_of_for(load_pack("uk_price_paid"), D(2026, 10, 14)) == D(2026, 9, 30)


def test_as_of_from_a_passing_parity_run_on_the_fixture(tmp_path: Path) -> None:
    pack = load_pack("uk_price_paid")
    ch, sf = FakeClickHouse(pack), FakeSnowflake(pack)
    load_clickhouse(pack, ch)
    load_snowflake(pack, sf, workdir=tmp_path, download=fixture_download())
    report = run_parity(
        pack,
        {"clickhouse": clickhouse_runner(ch), "snowflake": snowflake_runner(sf)},
        out_dir=None,
        now=fixed_clock(),
    )
    max_date = max_date_from_parity(report)
    assert max_date == D(2026, 10, 14)  # the fixture's latest sale, mid-October
    assert as_of_for(pack, max_date) == D(2026, 9, 30)


def _report(status: str, values: dict[str, object]) -> ParityReport:
    results = {
        e: ResultSet(columns=("MIN_DATE", "MAX_DATE"), rows=(("1995-01-01", v),))
        for e, v in values.items()
    }
    return ParityReport(
        pack="p@1",
        engines=tuple(values),
        status=status,  # type: ignore[arg-type]
        generated_at=dt.datetime(2026, 11, 15, tzinfo=dt.UTC),
        fingerprints=(FingerprintResult("date_range", results, {}, True, None),),
    )


def test_max_date_needs_a_passing_report_and_agreeing_text_dates() -> None:
    assert max_date_from_parity(_report("single_engine", {"snowflake": "2026-10-14"})) == D(
        2026, 10, 14
    )
    with pytest.raises(ParityError, match="parity failed"):
        max_date_from_parity(_report("fail", {"clickhouse": "2026-10-14"}))
    with pytest.raises(ParityError, match="disagree"):
        max_date_from_parity(
            _report("pass", {"clickhouse": "2026-10-14", "snowflake": "2026-10-13"})
        )
    with pytest.raises(ParityError, match="text"):
        max_date_from_parity(_report("pass", {"clickhouse": 20261014}))


def test_write_as_of_creates_and_updates_a_run_config(tmp_path: Path) -> None:
    cfg = tmp_path / "configs" / "run.yaml"
    write_as_of(cfg, D(2026, 9, 30))
    assert "as_of: 2026-09-30" in cfg.read_text(encoding="utf-8")
    cfg.write_text("backends: [cortex_analyst]\ntrials: 3\nas_of: 2020-01-31\n", encoding="utf-8")
    write_as_of(cfg, D(2026, 9, 30))
    loaded = load_run_config(cfg)
    assert loaded.as_of == D(2026, 9, 30) and loaded.trials == 3
    assert loaded.backends == ("cortex_analyst",)


def test_write_as_of_refuses_a_non_mapping_file(tmp_path: Path) -> None:
    cfg = tmp_path / "run.yaml"
    cfg.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        write_as_of(cfg, D(2026, 9, 30))
