"""FIX-01 (descriptions = the SEM-03 block) and FIX-02 (five verified examples)."""

from __future__ import annotations

import os

import pytest

from agentic_analytics.fixes import (
    Example,
    FixError,
    descriptions_text,
    examples_block,
    fix_blocks,
    lint_example,
    load_examples,
    parse_examples,
)
from agentic_analytics.fixes.leak import result_leaks_live
from agentic_analytics.questions import context_for, load_questions
from agentic_analytics.semantic import descriptions_block, load_semantic
from agentic_analytics.semantic.prompt import TITLE
from agentic_analytics.warehouse import check_sql
from tests.gold_check.fakes import BUNDLED

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")


# -- FIX-01 -------------------------------------------------------------------------------------


def test_descriptions_fix_is_the_semantic_block():
    block = descriptions_text(BUNDLED)
    assert block == descriptions_block(load_semantic(BUNDLED))
    assert block.startswith(TITLE + "\n")
    assert fix_blocks(BUNDLED)["descriptions"] == block


def test_fix_blocks_cover_every_fix_name():
    blocks = fix_blocks(BUNDLED)
    assert set(blocks) == {"descriptions", "examples", "self_check"}
    assert all(text.strip() for text in blocks.values())
    assert blocks["self_check"] == BUNDLED.root.joinpath("fixes/self_check.yaml").read_text(
        encoding="utf-8"
    )


# -- FIX-02 -------------------------------------------------------------------------------------


def test_bundled_pack_has_five_examples():
    examples = load_examples(BUNDLED)
    assert [e.id for e in examples] == ["ex-1", "ex-2", "ex-3", "ex-4", "ex-5"]
    assert len({e.question for e in examples}) == 5


def test_examples_pass_the_clickhouse_writing_rules():
    ctx = context_for(BUNDLED)
    for ex in load_examples(BUNDLED):
        assert lint_example(ex, ctx) == [], ex.id
        assert check_sql(ex.sql, "clickhouse").ok, ex.id


def test_example_lint_catches_an_approximate_median_and_lowercase_town():
    ctx = context_for(BUNDLED)
    bad = Example(
        id="ex-9",
        question="q",
        sql="SELECT median(price) FROM uk.uk_price_paid WHERE town = 'leeds'",
    )
    rules = {i.rule for i in lint_example(bad, ctx)}
    assert {"approximate_function", "literal_case"} <= rules


def test_examples_are_not_benchmark_gold():
    gold = {
        " ".join(sql.split()).casefold()
        for q in load_questions(BUNDLED)
        if q.gold_sql
        for sql in q.gold_sql.values()
    }
    for ex in load_examples(BUNDLED):
        assert " ".join(ex.sql.split()).casefold() not in gold, ex.id


def test_examples_block_holds_each_pair_in_order():
    examples = load_examples(BUNDLED)
    block = examples_block(examples)
    assert block.startswith("VERIFIED EXAMPLES\n")
    pos = 0
    for n, ex in enumerate(examples, 1):
        for part in (f"Example {n}", ex.question, ex.sql.strip()):
            at = block.index(part, pos)
            pos = at
    assert examples_block(examples) == block  # pure


@pytest.mark.parametrize(
    "text, why",
    [
        ("schema_version: '1.0'\nengine: snowflake\nexamples: []\n", "engine"),
        ("schema_version: '1.0'\nengine: clickhouse\nexamples: []\n", "examples"),
        (
            "schema_version: '1.0'\nengine: clickhouse\nexamples:\n"
            "  - {id: ex-1, question: a, sql: SELECT 1}\n"
            "  - {id: ex-1, question: b, sql: SELECT 2}\n",
            "unique",
        ),
        ("schema_version: '1.0'\nschema_version: '1.0'\n", "duplicate"),
        (
            "schema_version: '1.0'\nengine: clickhouse\nexamples:\n"
            "  - {id: example_1, question: a, sql: SELECT 1}\n",
            "pattern",
        ),
        ("examples: [\n", "invalid YAML"),
        (
            "schema_version: '1.0'\nengine: clickhouse\nexamples:\n"
            "  - {id: ex-1, question: a, sql: SELECT 1, extra: x}\n",
            "extra",
        ),
    ],
)
def test_malformed_examples_file_is_refused(text, why):
    with pytest.raises(FixError, match=why if why != "duplicate" else "found duplicate|duplicate"):
        parse_examples(text)


@pytest.mark.live
@LIVE
def test_live_examples_run_nonempty_and_leak_no_gold():  # pragma: no cover - needs ClickHouse
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.run.gold import load_gold_check, load_gold_results
    from agentic_analytics.warehouse import executors_from_env

    load_dotenv()
    ch, _ = executors_from_env()
    assert ch is not None, "ClickHouse variables are not set"
    check = load_gold_check(BUNDLED)
    assert check is not None, "run `agentic-analytics gold` first"
    selected = [q for q in load_questions(BUNDLED) if q.id in check.questions]
    gold = load_gold_results(BUNDLED, selected, check.engines)
    assert result_leaks_live(ch, load_examples(BUNDLED), gold.values()) == []
