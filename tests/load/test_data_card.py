"""LOD-05: DATA_CARD.md covers what NFR6 and H2 require, and its snapshot facts are the ones the
N1.3 live run recorded in docs/measurements/ (EK L-16: nothing claimed that no record backs)."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from agentic_analytics.load.asof import last_complete_month
from agentic_analytics.packs import load_pack
from agentic_analytics.packs.uk_price_paid.transforms import COLUMNS, SOURCE_CSV_URL

ROOT = Path(__file__).resolve().parents[2]
CARD = ROOT / "DATA_CARD.md"
MEASUREMENTS = ROOT / "docs" / "measurements"


@pytest.fixture(scope="module")
def card() -> str:
    return CARD.read_text(encoding="utf-8")


def test_source_and_licence(card: str) -> None:
    m = load_pack("uk_price_paid").manifest
    assert m.source_url in card and SOURCE_CSV_URL in card
    assert m.attribution in card
    assert "Open Government Licence v3.0" in card
    assert re.search(r"checked the attribution statement word for word .* on 2026-11-16", card)
    assert "not a reading by the owner" in card  # an automated fetch, said plainly


def _newest(kind: str) -> tuple[Path, dict]:
    paths = sorted(MEASUREMENTS.glob(f"{kind}-*.json"))
    assert paths, f"no {kind} record in {MEASUREMENTS}"
    records = [(json.loads(p.read_text(encoding="utf-8")), p) for p in paths]
    key = "downloaded_at" if kind == "snapshot" else "generated_at"
    data, path = max(records, key=lambda r: r[0][key])
    return path, data


def _row(section: str, label: str) -> str:
    return next(ln for ln in section.splitlines() if ln.startswith(f"| {label}"))


def test_snapshot_facts_match_the_live_records(card: str) -> None:
    """Every snapshot fact in the card equals the newest snapshot record and its passing parity
    report; the as-of date follows D-50 from the agreed max_date."""
    section = card.split("## Snapshot", 1)[1].split("\n## ", 1)[0]
    assert "not yet" not in section
    snap_path, snap = _newest("snapshot")
    parity_path, parity = _newest("parity")
    assert parity["status"] == "pass" and parity["snapshot_sha256"] == snap["sha256"]
    assert parity["engines"] == ["clickhouse", "snowflake"]
    shas = set(re.findall(r"\b[0-9a-f]{64}\b", card))
    assert shas == {snap["sha256"]}, shas
    assert snap_path.name in _row(section, "sha256")
    started = dt.datetime.fromisoformat(snap["downloaded_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert started in _row(section, "Download time")
    fps = {f["name"]: f["results"] for f in parity["fingerprints"]}
    counts = {e: r["rows"][0][0] for e, r in fps["row_count"].items()}
    assert len(set(counts.values())) == 1
    rows = counts["clickhouse"]
    assert f"{rows:,}" in _row(section, "Rows loaded") and rows == snap["lines"]
    min_date, max_date = fps["date_range"]["clickhouse"]["rows"][0]
    assert min_date in _row(section, "Rows loaded")
    assert _row(section, "Latest sale").count(max_date) == 1
    as_of = last_complete_month(dt.date.fromisoformat(max_date)).isoformat()
    assert as_of in _row(section, "As-of date")
    assert parity_path.name in _row(section, "Parity report")
    assert f"all {len(parity['fingerprints'])} fingerprints" in _row(section, "Parity report")


def test_as_of_rule_and_registration_lag(card: str) -> None:
    assert "last_complete_month" in card
    assert "2026-10-14 gives 2026-09-30" in card
    assert "2026-10-31 gives 2026-09-30" in card and "D-50" in card
    assert "download date" in card
    assert "## Registration lag" in card and "under-counted" in card


def test_transforms_table_lists_every_column(card: str) -> None:
    section = card.split("## Columns and transforms", 1)[1].split("\n## ", 1)[0]
    for col in COLUMNS:
        assert f"| `{col.name}` |" in section, col.name
    assert "EMPTY_FIELD_AS_NULL = FALSE" in section


def test_enum_order_note(card: str) -> None:
    section = card.split("## Enum ordering", 1)[1].split("\n## ", 1)[0]
    assert "Enum8" in section and "alphabetically" in section
    assert "min or max" in section


def test_probes_table_lists_every_marker(card: str) -> None:
    section = card.split("## Probes table", 1)[1]
    for i in range(1, 11):
        assert f"| INJ-{i:02d} |" in section
    assert "uk_probes.listing_notes" in section


def test_planned_behaviour_is_not_stated_as_fact(card: str) -> None:
    """EK L-16: no present-tense claims about what later nodes will enforce."""
    for claim in ("The sha256 goes on every run record", "or nothing downstream runs",
                  "\nEvery question goes to every backend"):  # fmt: skip
        assert claim not in card
    assert "RUN-04" in card and "not built yet" in card


def test_text_fingerprints_and_stale_objects_are_described(card: str) -> None:
    assert "for every text column" in card
    assert "refused unless the load is forced" in card
