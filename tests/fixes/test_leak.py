"""FIX-03: the leak check (Tech Spec §7, C5): word 3-gram overlap and result-set equality."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from agentic_analytics.fixes import Example, load_examples, overlap, result_leaks, text_leaks
from agentic_analytics.fixes.leak import (
    MAX_OVERLAP,
    execute_examples,
    normalise,
    result_leaks_live,
    same_result,
    skeleton,
    skeleton_overlap,
    structural_leaks,
    trigrams,
)
from agentic_analytics.questions import load_probes, load_questions
from agentic_analytics.schemas import GoldResult, ResultSet
from tests.gold_check.fakes import BUNDLED, FakeExecutor, rs

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")
T0 = datetime(2026, 11, 15, tzinfo=UTC)


def gold(qid: str, result: ResultSet, engine: str = "clickhouse") -> GoldResult:
    return GoldResult(
        schema_version="1.0",
        question_id=qid,
        engine=engine,  # type: ignore[arg-type]
        sql_hash="0" * 64,
        result=result,
        elapsed_ms=1,
        executed_at=T0,
    )


@pytest.mark.gold
def test_bundled_examples_do_not_mirror_any_gold_sql_structure():
    examples = load_examples(BUNDLED)
    assert structural_leaks(examples, load_questions(BUNDLED), load_probes(BUNDLED)) == []


def test_skeleton_strips_literals_identifiers_and_aliases():
    a = skeleton("SELECT count() AS sales FROM uk.uk_price_paid WHERE town = 'LEEDS'")
    b = skeleton("SELECT count() AS n FROM uk.uk_price_paid WHERE county = 'KENT'")
    assert a == b == ("select", "count", "(", ")", "from", "c", ".", "c", "where", "c", "=", "?")
    assert skeleton_overlap(a, b) == 1.0


# The first draft's examples (replaced under D-52) demonstrated scored output formulas.
FIRST_DRAFT = {
    "ex-1": "SELECT avg(price) AS avg_price FROM uk.uk_price_paid WHERE town = 'BRISTOL' "
    "AND type = 'terraced' AND toYear(date) = 2018",
    "ex-3": "SELECT district, quantileExactInclusive(0.5)(price) AS median_price "
    "FROM uk.uk_price_paid WHERE county = 'MERSEYSIDE' AND type = 'semi-detached' "
    "AND toYear(date) = 2017 GROUP BY district ORDER BY district",
    "ex-4": "SELECT street, count() AS sales FROM uk.uk_price_paid WHERE town = 'NORWICH' "
    "AND street != '' AND toYear(date) = 2012 GROUP BY street ORDER BY sales DESC, street ASC "
    "LIMIT 3",
    "ex-5": "SELECT countIf(toYear(date) = 2017) AS sales_2017, countIf(toYear(date) = 2018) "
    "AS sales_2018, 100 * (toFloat64(sales_2018) - sales_2017) / sales_2017 AS pct_change "
    "FROM uk.uk_price_paid WHERE town = 'SHEFFIELD' AND duration = 'leasehold' "
    "AND toYear(date) IN (2017, 2018)",
}


@pytest.mark.parametrize("ex_id", sorted(FIRST_DRAFT))
def test_planted_formula_mirrors_are_structural_leaks(ex_id):
    planted = Example(
        id=ex_id, question="Something unrelated to any benchmark text.", sql=FIRST_DRAFT[ex_id]
    )
    leaks = structural_leaks([planted], load_questions(BUNDLED))
    assert leaks and all(lk.rule == "structure" for lk in leaks)


def test_ambiguous_interpretations_and_probe_gold_are_compared_too():
    q = next(q for q in load_questions(BUNDLED) if q.expected == "clarify")
    interp = q.interpretations[0]["clickhouse"]  # type: ignore[index]
    planted = Example(id="ex-9", question="x", sql=interp.replace("{as_of}", "2020-01-31"))
    hits = {lk.question_id for lk in structural_leaks([planted], [q])}
    assert f"{q.id}/interpretations[0]" in hits
    probe = next(p for p in load_probes(BUNDLED) if p.gold_sql is not None)
    copy = Example(id="ex-9", question="x", sql=probe.gold_sql["clickhouse"])  # type: ignore[index]
    assert {lk.question_id for lk in structural_leaks([copy], [], [probe])} == {probe.id}


@pytest.mark.gold
def test_bundled_examples_do_not_leak_the_benchmark_text():
    examples = load_examples(BUNDLED)
    questions = load_questions(BUNDLED)
    assert text_leaks(examples, questions) == []
    worst = max(overlap(e.question, q.text) for e in examples for q in questions)
    assert worst <= MAX_OVERLAP


def test_normalisation_ignores_case_punctuation_and_symbols():
    assert normalise("Semi-detached, in LONDON: £1,000,000?") == [
        "semi",
        "detached",
        "in",
        "london",
        "1",
        "000",
        "000",
    ]
    assert trigrams("a b") == frozenset()
    assert overlap("How many sales?", "how MANY sales") == 1.0
    assert overlap("one two", "one two three") == 0.0  # no 3-gram in the first


def test_planted_paraphrase_of_a_benchmark_question_is_a_leak():
    q = next(q for q in load_questions(BUNDLED) if q.id == "q-062")  # 5 towns, most sales 2023
    planted = Example(
        id="ex-9",
        question="Which 5 towns recorded the most property sales in 2022?",
        sql="SELECT 1",
    )
    leaks = text_leaks([planted, *load_examples(BUNDLED)], [q])
    assert [(lk.example_id, lk.question_id, lk.rule) for lk in leaks] == [("ex-9", "q-062", "text")]
    assert "overlap" in str(leaks[0])


def test_a_short_question_copied_inside_a_long_example_is_a_leak():
    short = next(q for q in load_questions(BUNDLED) if q.id == "q-113")
    planted = Example(
        id="ex-9",
        question=f"For a report on the market, {short.text} Break it down by nothing else.",
        sql="SELECT 1",
    )
    assert overlap(planted.question, short.text) == 1.0
    assert text_leaks([planted], [short])


def test_overlap_at_exactly_one_half_is_allowed():
    # 4 trigrams each, 2 shared -> 0.5, not above the limit
    a, b = "a b c d e f", "a b c d x y"
    assert overlap(a, b) == pytest.approx(0.5)
    q = next(iter(load_questions(BUNDLED))).model_copy(update={"text": b})
    ex = Example(id="ex-1", question=a, sql="SELECT 1")
    assert text_leaks([ex], [q]) == []  # exactly 0.5 passes
    assert [lk.rule for lk in text_leaks([ex], [q], limit=0.49)] == ["text"]  # above the limit
    # 3 of 4 trigrams shared -> 0.75, a leak at the default limit
    q3 = q.model_copy(update={"text": "a b c d e y"})
    assert [lk.question_id for lk in text_leaks([ex], [q3])] == [q.id]


def test_equal_result_set_is_a_leak_even_with_renamed_columns_and_reordered_rows():
    g = gold("q-004", rs(["type", "median_price"], ["flat", 230000], ["detached", 450000]))
    copied = rs(["kind", "m"], ["detached", 450000], ["flat", 230000])
    leaks = result_leaks({"ex-2": copied}, [g])
    assert [(lk.example_id, lk.question_id, lk.rule) for lk in leaks] == [
        ("ex-2", "q-004", "result")
    ]


def test_different_result_sets_do_not_leak():
    g = gold("q-001", rs(["sales"], [900_000]))
    assert result_leaks({"ex-1": rs(["sales"], [900_001])}, [g]) == []
    assert result_leaks({"ex-1": rs(["sales", "x"], [900_000, 1])}, [g]) == []  # superset
    assert not same_result(rs(["a"], [1], [2]), rs(["a"], [1]))


def test_examples_are_executed_read_only_and_bad_ones_reported():
    examples = load_examples(BUNDLED)
    ch = FakeExecutor("clickhouse", default=rs(["n"], [1]))
    ch.set(examples[1].sql, rs(["n"]))  # empty
    ch.set(examples[2].sql, "sql_error")
    ch.set(examples[3].sql, rs(["n"], *[[i] for i in range(201)]))  # over the cap
    results, problems = execute_examples(ch, examples)
    assert sorted(results) == ["ex-1", "ex-5"]
    assert problems == [
        "ex-2: no rows",
        "ex-3: execution failed (sql_error)",
        "ex-4: more than 200 rows",
    ]
    assert all(cap == 200 for _, cap in ch.calls)


def test_live_shape_flags_a_planted_result_equal_to_gold():
    examples = load_examples(BUNDLED)[:1]
    ch = FakeExecutor("clickhouse", {examples[0].sql: rs(["avg_price"], [312_000.5])})
    g = gold("q-050", rs(["avg_price"], [312_000.5]))
    assert result_leaks_live(ch, examples, [g]) == [
        "ex-1 leaks q-050 (result): same result set as clickhouse gold"
    ]


@pytest.mark.live
@LIVE
def test_live_no_example_result_equals_a_gold_result():  # pragma: no cover - needs ClickHouse
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.run.gold import load_gold_check, load_gold_results
    from agentic_analytics.warehouse import executors_from_env

    load_dotenv()
    ch, _ = executors_from_env()
    check = load_gold_check(BUNDLED)
    assert ch is not None and check is not None
    selected = [q for q in load_questions(BUNDLED) if q.id in check.questions]
    gold_results = load_gold_results(BUNDLED, selected, check.engines)
    results, problems = execute_examples(ch, load_examples(BUNDLED))
    assert problems == []
    assert result_leaks(results, gold_results.values()) == []


def test_skeleton_overlap_divides_by_the_smaller_set():
    short = tuple(f"t{i}" for i in range(7))  # 2 distinct 6-grams
    long = short + tuple(f"u{i}" for i in range(10))  # 12, the short ones included
    assert skeleton_overlap(short, long) == 1.0  # not 2/12
    assert skeleton_overlap(long, short) == 1.0


def test_an_extra_column_is_not_the_same_result():
    two = rs(["n", "n2"], [1, 1], [2, 2])
    one = rs(["n"], [1], [2])
    assert not same_result(two, one) and not same_result(one, two)
