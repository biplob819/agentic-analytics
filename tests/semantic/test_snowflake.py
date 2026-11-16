"""SEM-02: the CREATE SEMANTIC VIEW generator and its reader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_analytics.packs import load_pack
from agentic_analytics.semantic import (
    comment_text,
    default_names,
    load_semantic,
    parse_semantic_view_ddl,
    semantic_view_ddl,
    semantic_view_grant_sql,
)
from agentic_analytics.semantic.snowflake import sql_string
from tests.semantic.helpers import mini

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("AA_UPDATE_GOLDEN") == "1"
VIEW = "ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC"
TABLE = "ANALYTICS.UK.UK_PRICE_PAID"


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if UPDATE:  # pragma: no cover - maintainer helper
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), f"{name} differs; AA_UPDATE_GOLDEN=1 rewrites"


def test_bundled_ddl_equals_golden() -> None:
    model = load_semantic(load_pack("uk_price_paid"))
    _golden("uk_price_paid.sql", semantic_view_ddl(model, VIEW, TABLE))


def test_quoting_ddl_equals_golden() -> None:
    _golden("quoting.sql", semantic_view_ddl(mini(), "DB.SCH.MINI_SV", "DB.SCH.SALES"))


def test_golden_shows_escaping() -> None:
    text = (GOLDEN / "quoting.sql").read_text(encoding="utf-8")
    assert "It''s for tests; a back\\\\slash stays." in text
    assert "'buyer''s town'" in text
    assert "e.g. ''LONDON''" in text


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [("plain", "'plain'"), ("it's", "'it''s'"), ("a\\b", "'a\\\\b'"), ("''", "''''''")],
)
def test_sql_string(raw: str, quoted: str) -> None:
    assert sql_string(raw) == quoted


def test_ddl_shape() -> None:
    ddl = semantic_view_ddl(mini(), "DB.SCH.MINI_SV", "DB.SCH.SALES")
    assert ddl.startswith("CREATE OR REPLACE SEMANTIC VIEW DB.SCH.MINI_SV\n  TABLES (\n")
    assert ddl.endswith(";\n")
    order = [ddl.index(k) for k in ("TABLES (", "FACTS (", "DIMENSIONS (", "METRICS (")]
    assert order == sorted(order)
    assert "    sales AS DB.SCH.SALES\n" in ddl
    assert "sales.year AS YEAR(sales.date)" in ddl
    assert "sales.month AS DATE_TRUNC('MONTH', sales.date)" in ddl
    assert "sales.median_price AS MEDIAN(sales.price)" in ddl
    assert "sales.sales_count AS COUNT(sales.price)" in ddl
    assert ddl.count(";") == 1 + ddl.count("tests;")


def test_reader_recovers_every_comment_and_synonym() -> None:
    m = mini()
    parsed = parse_semantic_view_ddl(semantic_view_ddl(m, "DB.SCH.MINI_SV", "DB.SCH.SALES"))
    assert parsed.name == "DB.SCH.MINI_SV"
    assert parsed.items[("view", "overview")].comment == m.description
    table = parsed.items[("table", "sales")]
    assert (table.comment, table.synonyms) == (m.table.description, m.table.synonyms)
    town = parsed.items[("dimension", "town")]
    assert town.comment == comment_text(m.dimensions[0])
    assert town.synonyms == ("city", "buyer's town")
    assert town.expr == "sales.town"
    assert parsed.items[("dimension", "month")].expr == "DATE_TRUNC('MONTH', sales.date)"
    assert parsed.items[("metric", "sales_count")].synonyms == ()
    kinds = sorted({k for k, _ in parsed.items})
    assert kinds == ["dimension", "fact", "metric", "table", "view"]


def test_reader_on_bundled_ddl_has_every_item() -> None:
    model = load_semantic(load_pack("uk_price_paid"))
    parsed = parse_semantic_view_ddl(semantic_view_ddl(model, VIEW, TABLE))
    names = {(k, n) for k, n in parsed.items if k in ("dimension", "fact", "metric")}
    expected = (
        {("dimension", d.name) for d in model.dimensions}
        | {("fact", f.name) for f in model.facts}
        | {("metric", m.name) for m in model.metrics}
    )
    assert names == expected


@pytest.mark.parametrize(
    "bad",
    ["UK_PRICE_PAID", "A.B", "A.B.C.D", "A.B.C; DROP TABLE X", 'A.B."C"', "A..C", ""],
)
def test_names_must_be_three_identifiers(bad: str) -> None:
    with pytest.raises(ValueError, match=r"DATABASE\.SCHEMA\.NAME"):
        semantic_view_ddl(mini(), bad, "DB.SCH.SALES")
    with pytest.raises(ValueError, match=r"DATABASE\.SCHEMA\.NAME"):
        semantic_view_ddl(mini(), "DB.SCH.V", bad)


def test_default_names_come_from_the_pack() -> None:
    pack = load_pack("uk_price_paid")
    model = load_semantic(pack)
    assert default_names(pack, model) == (
        "ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC",
        "ANALYTICS.UK.UK_PRICE_PAID",
    )


def test_grant_sql() -> None:
    assert semantic_view_grant_sql(VIEW) == (
        "GRANT SELECT ON SEMANTIC VIEW ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC TO ROLE ANALYTICS_RO;"
    )
    with pytest.raises(ValueError):
        semantic_view_grant_sql(VIEW, role="X; DROP")


@pytest.mark.parametrize(
    "ddl",
    [
        "SELECT 1",
        "CREATE OR REPLACE SEMANTIC VIEW A.B.C TABLES ( t AS A.B.T COMMENT = 'x' ",
        "CREATE OR REPLACE SEMANTIC VIEW A.B.C TABLES ( t AS A.B.T COMMENT = 'unterminated )",
        "CREATE OR REPLACE SEMANTIC VIEW A.B.C TABLES ( t AS A.B.T ) BOGUS ( x ) ;",
    ],
)
def test_reader_refuses_malformed_ddl(ddl: str) -> None:
    with pytest.raises(ValueError):
        parse_semantic_view_ddl(ddl)
