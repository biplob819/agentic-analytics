"""LOD-04: `listing_notes` with 10 planted rows on both engines, idempotently (B9, H7)."""

from __future__ import annotations

import re

import pytest

from agentic_analytics.load import load_probes_clickhouse, load_probes_snowflake
from agentic_analytics.load.probes import (
    clickhouse_probes_database,
    clickhouse_steps,
    snowflake_probes_database,
    snowflake_steps,
)
from agentic_analytics.packs import load_pack
from tests.load.fakes import FakeClickHouse, FakeSnowflake

MARKERS = [f"INJ-{i:02d}" for i in range(1, 11)]


def markers_in_notes(rows) -> list[str]:
    """Markers live only inside the note text (D-51: no separate marker column)."""
    assert all(len(r) == 4 for r in rows)
    return [re.findall(r"INJ-\d\d", r[3])[0] for r in rows]


@pytest.fixture
def pack():
    return load_pack("uk_price_paid")


def test_probes_database_names_follow_the_role_sql(pack) -> None:
    assert clickhouse_probes_database(pack) == "uk_probes"
    assert snowflake_probes_database(pack) == ("ANALYTICS", "ANALYTICS.UK_PROBES")


def test_clickhouse_probes_load_and_reload(pack) -> None:
    ch = FakeClickHouse(pack)
    res = load_probes_clickhouse(pack, ch)
    assert dict(res.rows_after) == {"uk_probes.listing_notes": 10}
    rows = ch.tables["uk_probes.listing_notes"]
    assert markers_in_notes(rows) == MARKERS
    again = load_probes_clickhouse(pack, ch)  # CREATE OR REPLACE, then insert
    assert dict(again.rows_after) == {"uk_probes.listing_notes": 10}
    assert [s.kind for s in clickhouse_steps(pack)] == [
        "create_database",
        "create_table",
        "insert",
    ]


def test_snowflake_probes_load_and_reload(pack) -> None:
    sf = FakeSnowflake(pack)
    load_probes_snowflake(pack, sf)
    res = load_probes_snowflake(pack, sf)
    assert dict(res.rows_after) == {"ANALYTICS.UK_PROBES.listing_notes": 10}
    assert markers_in_notes(sf.tables["ANALYTICS.UK_PROBES.listing_notes"]) == MARKERS
    assert [s.kind for s in snowflake_steps(pack)] == [
        "create_database",
        "create_schema",
        "create_table",
        "insert",
    ]
    assert sf.cursors_open == 0


def test_both_engines_hold_the_same_planted_rows(pack) -> None:
    ch, sf = FakeClickHouse(pack), FakeSnowflake(pack)
    load_probes_clickhouse(pack, ch)
    load_probes_snowflake(pack, sf)
    assert ch.tables["uk_probes.listing_notes"] == sf.tables["ANALYTICS.UK_PROBES.listing_notes"]


def test_probes_never_touch_the_benchmark_table(pack) -> None:
    for st in clickhouse_steps(pack) + snowflake_steps(pack):
        assert all("_probes" in t.lower() or t == "ANALYTICS" for t in st.targets), st.targets


@pytest.mark.parametrize(
    ("engine", "stmt"),
    [
        ("clickhouse", "INSERT INTO uk.uk_price_paid (price) VALUES (1);"),
        (
            "clickhouse",
            "CREATE OR REPLACE TABLE uk.uk_price_paid (price UInt32) ENGINE = MergeTree "
            "ORDER BY price;",
        ),
        ("snowflake", "INSERT INTO ANALYTICS.UK.uk_price_paid (price) VALUES (1);"),
    ],
)
def test_a_probes_file_aimed_at_the_benchmark_table_is_refused(
    tmp_path, engine: str, stmt: str
) -> None:
    """Verifier: the probes allow-list, not only the bundled SQL, keeps the benchmark table out."""
    import shutil
    from importlib import resources

    from agentic_analytics.load import LoaderError

    src = resources.files("agentic_analytics.packs") / "uk_price_paid"
    pack_dir = tmp_path / "pack"
    with resources.as_file(src) as p:
        shutil.copytree(p, pack_dir, ignore=shutil.ignore_patterns("__pycache__"))
    f = pack_dir / "probes" / f"{engine}.sql"
    f.write_text(f.read_text(encoding="utf-8") + "\n" + stmt + "\n", encoding="utf-8")
    steps = clickhouse_steps if engine == "clickhouse" else snowflake_steps
    with pytest.raises(LoaderError, match="not allowed"):
        steps(load_pack(pack_dir))
