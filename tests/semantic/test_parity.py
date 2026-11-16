"""SEM-04: every description and synonym in the view is in the prompt block, and the reverse.

Both sides are parsed back from the generated text, so planted defects in either text, or in
either generator, are caught."""

from __future__ import annotations

import pytest

from agentic_analytics.packs import load_pack
from agentic_analytics.semantic import (
    SemanticParityError,
    descriptions_block,
    load_semantic,
    require_semantic_parity,
    semantic_parity,
    semantic_view_ddl,
)
from agentic_analytics.semantic import prompt as prompt_mod
from agentic_analytics.semantic import snowflake as sf_mod
from tests.semantic.helpers import mini

VIEW, TABLE = "ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC", "ANALYTICS.UK.UK_PRICE_PAID"


def _pair() -> tuple[str, str]:
    m = mini()
    return semantic_view_ddl(m, "DB.SCH.MINI_SV", "DB.SCH.SALES"), descriptions_block(m)


def test_bundled_pack_has_parity() -> None:
    model = load_semantic(load_pack("uk_price_paid"))
    ddl, block = semantic_view_ddl(model, VIEW, TABLE), descriptions_block(model)
    assert semantic_parity(ddl, block) == ()
    require_semantic_parity(ddl, block)


def test_fixture_has_parity() -> None:
    assert semantic_parity(*_pair()) == ()


def _one(issues: tuple[str, ...], *needles: str) -> None:
    assert issues, "planted defect not caught"
    assert any(all(n in i for n in needles) for i in issues), issues


# ---- planted defects in the DDL text -------------------------------------------------------


def test_ddl_synonym_dropped() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("('city', 'buyer''s town')", "('city')")
    _one(semantic_parity(ddl, block), "dimension town", "synonyms", "buyer's town")


def test_ddl_synonym_added() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("('sale price')", "('sale price', 'cost')")
    _one(semantic_parity(ddl, block), "fact price", "synonyms", "cost")


def test_ddl_comment_changed() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("COMMENT = 'Median price, GBP.'", "COMMENT = 'Median price, USD.'")
    _one(semantic_parity(ddl, block), "metric median_price", "comment")


def test_ddl_item_missing() -> None:
    ddl, block = _pair()
    start = ddl.index("    sales.year AS")
    end = ddl.index("    sales.month AS")
    ddl = ddl[:start] + ddl[end:]
    _one(semantic_parity(ddl, block), "dimension year", "only in the prompt block")


def test_ddl_view_comment_changed() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("  COMMENT = 'A tiny model.", "  COMMENT = 'A small model.")
    _one(semantic_parity(ddl, block), "view overview", "comment")


def test_ddl_table_synonyms_changed() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("('deals')", "('trades')")
    _one(semantic_parity(ddl, block), "table sales", "synonyms")


def test_ddl_sample_values_dropped_from_comment() -> None:
    ddl, block = _pair()
    ddl = ddl.replace(" Sample values: LONDON, ST. IVES.", "")
    _one(semantic_parity(ddl, block), "dimension town", "comment")


# ---- planted defects in the prompt block ---------------------------------------------------


def test_block_synonym_dropped() -> None:
    ddl, block = _pair()
    block = block.replace('Synonyms: "middle price"', "Synonyms: none")
    _one(semantic_parity(ddl, block), "metric median_price", "synonyms", "middle price")


def test_block_description_changed() -> None:
    ddl, block = _pair()
    block = block.replace("Description: Price in GBP.", "Description: Price in EUR.")
    _one(semantic_parity(ddl, block), "fact price", "comment")


def test_block_item_missing() -> None:
    ddl, block = _pair()
    start = block.index("- month\n")
    end = block.index("\n\n", start) + 2
    block = block[:start] + block[end:]
    _one(semantic_parity(ddl, block), "dimension month", "only in the view")


def test_block_overview_changed() -> None:
    ddl, block = _pair()
    block = block.replace("A tiny model.", "A tiny model!")
    _one(semantic_parity(ddl, block), "view overview", "comment")


def test_block_column_changed() -> None:
    ddl, block = _pair()
    block = block.replace("- town\n  Column: town\n", "- town\n  Column: type\n")
    _one(semantic_parity(ddl, block), "dimension town", "source")


def test_block_derivation_changed() -> None:
    ddl, block = _pair()
    block = block.replace("Column: date (calendar year)", "Column: date (first day of the month)")
    _one(semantic_parity(ddl, block), "dimension year", "source")


def test_block_aggregation_changed() -> None:
    ddl, block = _pair()
    block = block.replace("Aggregation: median of price", "Aggregation: average of price")
    _one(semantic_parity(ddl, block), "metric median_price", "source")


def test_ddl_expression_changed() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("sales.year AS YEAR(sales.date)", "sales.year AS YEAR(sales.price)")
    _one(semantic_parity(ddl, block), "dimension year", "source")
    ddl2 = _pair()[0].replace("MEDIAN(sales.price)", "AVG(sales.price)")
    _one(semantic_parity(ddl2, block), "metric median_price", "source")


def test_ddl_expression_not_understood() -> None:
    ddl, block = _pair()
    ddl = ddl.replace("sales.town AS sales.town", "sales.town AS UPPER(sales.town)")
    _one(semantic_parity(ddl, block), "dimension town", "source")


# ---- planted generator bugs ----------------------------------------------------------------


def test_generator_that_skips_fact_synonyms_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = sf_mod._entry

    def buggy(head: str, synonyms: tuple[str, ...], comment: str) -> str:
        if head.startswith("sales.price AS"):
            synonyms = ()
        return real(head, synonyms, comment)

    monkeypatch.setattr(sf_mod, "_entry", buggy)
    ddl, block = _pair()
    _one(semantic_parity(ddl, block), "fact price", "synonyms")


def test_prompt_that_drops_sample_values_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_mod, "comment_text", lambda item: item.description)
    ddl, block = _pair()
    _one(semantic_parity(ddl, block), "dimension town", "comment")


def test_require_raises_naming_the_item() -> None:
    ddl, block = _pair()
    block = block.replace("Description: Price in GBP.", "Description: Price in EUR.")
    with pytest.raises(SemanticParityError, match="fact price"):
        require_semantic_parity(ddl, block)
