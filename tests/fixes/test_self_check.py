"""FIX-04: self-check rules and the hook A2.1's `ask_mcp` takes (Tech Spec §7, B4, E8)."""

from __future__ import annotations

import asyncio
import functools
import os
from datetime import date

import pytest
import sqlglot
from sqlglot import exp

from agentic_analytics.fixes import (
    RULE_IDS,
    FixError,
    TableStats,
    judge,
    load_rules,
    make_self_check,
)
from agentic_analytics.fixes.self_check import column_kinds, parse_rules, price_level_column
from agentic_analytics.questions import bind_as_of, load_questions
from tests.backends.fakes import ScriptedModel, ask, final, turn
from tests.gold_check.fakes import BUNDLED, FakeExecutor, rs

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")
RULES = load_rules(BUNDLED)
STATS = TableStats(row_count=30_000_000, min_date=date(1995, 1, 1), max_date=date(2026, 10, 30))
STATS_ROW = rs(["row_count", "min_date", "max_date"], [30_000_000, "1995-01-01", "2026-10-30"])
AS_OF = date(2026, 9, 30)


def rules_hit(sql: str, result, **kw) -> list[str]:
    return [r for r, _ in judge(sql, result, RULES, STATS, **kw)]


def run_hook(executor, sql: str, **kw):
    hook = make_self_check(executor, RULES)

    async def go():
        return await hook(sql, **kw)

    return asyncio.run(go())


def test_rules_file_names_every_rule_with_the_agreed_thresholds():
    assert set(RULES.messages) == set(RULE_IDS)
    assert (RULES.price_range.min, RULES.price_range.max) == (1_000, 10_000_000)
    assert RULES.max_change_pct == 500


def test_plausible_results_pass():
    sql = (
        "SELECT type, quantileExactInclusive(0.5)(price) AS median_price, count() AS sales "
        "FROM uk.uk_price_paid WHERE toYear(date) = 2024 GROUP BY type"
    )
    assert rules_hit(sql, rs(["type", "median_price", "sales"], ["flat", 230_000.0, 90_000])) == []


def test_zero_rows_unless_the_question_expects_empty():
    sql = "SELECT town FROM uk.uk_price_paid WHERE town = 'leeds'"
    assert rules_hit(sql, rs(["town"])) == ["zero_rows"]
    assert rules_hit(sql, rs(["town"]), expects_empty=True) == []


def test_zero_aggregate_when_every_number_is_zero():
    sql = "SELECT count() AS sales, sum(price) AS total FROM uk.uk_price_paid WHERE town = 'leeds'"
    assert rules_hit(sql, rs(["sales", "total"], [0, 0])) == ["zero_aggregate"]
    assert rules_hit(sql, rs(["sales", "total"], [0, 0]), expects_empty=True) == []
    assert rules_hit(sql, rs(["sales", "total"], [0, 5])) == []
    assert rules_hit("SELECT town FROM t", rs(["town"], ["LEEDS"])) == []  # no numbers at all


def test_null_aggregate_but_not_a_null_group_key():
    sql = "SELECT avg(price) AS avg_price FROM uk.uk_price_paid WHERE town = 'NOWHERE'"
    assert rules_hit(sql, rs(["avg_price"], [None])) == ["null_aggregate"]
    sql2 = "SELECT locality, count() AS n FROM uk.uk_price_paid GROUP BY locality"
    assert rules_hit(sql2, rs(["locality", "n"], [None, 5])) == []


@pytest.mark.parametrize("value", [999, 10_000_001, 120.5])
def test_price_level_outside_1k_to_10m(value):
    sql = "SELECT medianExact(price) AS median_price FROM uk.uk_price_paid"
    assert rules_hit(sql, rs(["median_price"], [value])) == ["price_out_of_range"]


def test_price_rule_only_for_names_that_denote_a_price_level():
    assert price_level_column("median_price", RULES)
    assert price_level_column("avg_price", RULES) and price_level_column("price_mean", RULES)
    for name in (
        "m",
        "price",
        "min_price",
        "total_value",
        "median_price_change",
        "avg_price_pct",
        "median_price_ratio",
        "avg_price_k",
        "price_diff_avg",
    ):
        assert not price_level_column(name, RULES), name
    street_1995 = "SELECT quantileExactInclusive(0.5)(price) AS median_price FROM uk.uk_price_paid"
    assert rules_hit(street_1995, rs(["median_price"], [4_500.0])) == []  # 1995 street medians
    unnamed = "SELECT medianExact(price) AS m FROM uk.uk_price_paid"
    assert rules_hit(unnamed, rs(["m"], [5.0])) == []  # the name does not say price level


def test_count_above_the_table_rows():
    sql = "SELECT count() AS sales FROM uk.uk_price_paid"
    assert rules_hit(sql, rs(["sales"], [30_000_001])) == ["count_above_rows"]
    assert rules_hit(sql, rs(["sales"], [29_999_999])) == []


def test_dates_and_years_outside_the_data_range():
    sql = "SELECT toStartOfMonth(date) AS month, count() AS n FROM uk.uk_price_paid GROUP BY month"
    assert rules_hit(sql, rs(["month", "n"], ["1994-12-01", 3])) == ["date_out_of_range"]
    years = "SELECT toYear(date) AS y, count() AS n FROM uk.uk_price_paid GROUP BY y"
    assert rules_hit(years, rs(["y", "n"], [2027, 3])) == ["date_out_of_range"]
    assert rules_hit(years, rs(["y", "n"], [2026, 3])) == []
    price = "SELECT quantileExactInclusive(0.5)(price) AS median_last_year FROM uk.uk_price_paid"
    assert rules_hit(price, rs(["median_last_year"], [300_000])) == []


def test_change_rule_only_for_explicit_pct_change_columns_beyond_500():
    sql = "SELECT 100 * (toFloat64(b) - a) / a AS pct_change FROM s"
    assert rules_hit(sql, rs(["pct_change"], [-501.0])) == ["change_beyond_limit"]
    assert rules_hit(sql, rs(["pct_change"], [450.0])) == []
    assert rules_hit("SELECT x AS yoy_pct FROM s", rs(["yoy_pct"], [900.0])) == [
        "change_beyond_limit"
    ]
    for name in ("growth", "change_points", "change_count", "new_build_pct"):
        assert rules_hit(f"SELECT x AS {name} FROM s", rs([name], [9_000.0])) == [], name


def test_column_kinds_are_plain_when_sql_is_unreadable():
    kinds = column_kinds("not sql at all (", ("a", "b"))
    assert all(not k.aggregate for k in kinds)


def _plausible(name: str) -> object:
    n = name.casefold()
    if n in {"county", "district", "duration", "postcode1", "town", "type"}:
        return {"type": "flat", "duration": "freehold", "postcode1": "SW1A"}.get(n, "LEEDS")
    if n == "year":
        return 2020
    if n in ("month", "quarter"):
        return 3
    if "price" in n or n.startswith(("avg_", "median_")):
        return 250_000.0
    if "pct" in n or "change" in n:
        return 12.5
    return 1_234


def test_no_false_positives_on_the_shape_of_every_shipped_gold_answer():
    """Every answer question's gold, with a plausible result of its own shape, passes."""
    checked = 0
    for q in load_questions(BUNDLED):
        if q.gold_sql is None:
            continue
        sql = bind_as_of(q.gold_sql["clickhouse"], AS_OF)
        root = sqlglot.parse_one(sql, read="clickhouse")
        assert isinstance(root, exp.Query)
        cols = tuple(root.named_selects)
        rows = [] if q.expects_empty else [[_plausible(c) for c in cols]]
        assert rules_hit(sql, rs(cols, *rows), expects_empty=q.expects_empty) == [], q.id
        checked += 1
    assert checked == 100


def test_feedback_message_lists_each_hit_once_with_preamble_and_closing():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW})
    sql = "SELECT avg(price) AS avg_price, count() AS n FROM uk.uk_price_paid WHERE town = 'X'"
    ch.set(sql, rs(["avg_price", "n"], [5.0, 40_000_000], [6.0, 40_000_001]))
    msg = run_hook(ch, sql)
    assert msg is not None
    lines = msg.splitlines()
    assert lines[0] == RULES.preamble.strip()
    assert lines[-1] == RULES.closing.strip()
    assert [ln for ln in lines if ln.startswith("- ")] == [
        "- avg_price is £5, outside the plausible range for an average or median price (£1,000 "
        "to £10,000,000).",
        "- n is 40,000,000, more than the 30,000,000 rows in the whole table.",
    ]


def test_hook_runs_the_sql_read_only_and_fetches_stats_once():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW})
    good = "SELECT count() AS n FROM uk.uk_price_paid WHERE toYear(date) = 2020"
    ch.set(good, rs(["n"], [800_000]))
    hook = make_self_check(ch, RULES)

    async def twice():
        return await hook(good), await hook(good)

    assert asyncio.run(twice()) == (None, None)
    assert ch.sqls().count(RULES.stats_sql) == 1
    assert ch.sqls().count(good) == 2  # the agent's SQL runs once per hook call
    assert {cap for sql, cap in ch.calls if sql == good} == {1_000}  # under the 1,000-row cap
    msg = run_hook(ch, "DELETE FROM uk.uk_price_paid WHERE 1")
    assert msg is not None and "did not run (guard)" in msg


def test_hook_takes_the_questions_expects_empty_flag():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW})
    sql = "SELECT type, count() AS n FROM uk.uk_price_paid WHERE toYear(date) = 1994 GROUP BY type"
    ch.set(sql, rs(["type", "n"]))
    assert "no rows" in (run_hook(ch, sql) or "")
    assert run_hook(ch, sql, expects_empty=True) is None
    bound_hook = functools.partial(make_self_check(ch, RULES), expects_empty=True)

    async def go():
        return await bound_hook(sql)

    assert asyncio.run(go()) is None  # the runner binds the flag per question


def test_sql_errors_give_feedback_but_transport_failures_do_not():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW})
    ch.set("SELECT boom FROM uk.uk_price_paid", "sql_error")
    ch.set("SELECT 1 AS slow", "timeout")
    assert "did not run (sql_error)" in (run_hook(ch, "SELECT boom FROM uk.uk_price_paid") or "")
    assert run_hook(ch, "SELECT 1 AS slow") is None
    assert RULES.stats_sql not in ch.sqls()  # no stats query for SQL that did not run


def test_failed_stats_query_skips_only_the_rules_that_need_it():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: "transport"})
    sql = "SELECT count() AS n FROM uk.uk_price_paid"
    ch.set(sql, rs(["n"], [10**12]))
    assert run_hook(ch, sql) is None
    ch.set("SELECT town FROM uk.uk_price_paid WHERE 0", rs(["town"]))
    assert "no rows" in (run_hook(ch, "SELECT town FROM uk.uk_price_paid WHERE 0") or "")


def test_hook_refuses_an_executor_for_another_engine():
    with pytest.raises(FixError, match="clickhouse executor"):
        make_self_check(FakeExecutor("snowflake"), RULES)


def test_hook_drives_one_feedback_turn_in_the_agent_loop():
    """The A2.1 loop sends the hook's message once, then the next final answer stands."""
    bad = "SELECT avg(price) AS avg_price FROM uk.uk_price_paid WHERE town = 'leeds'"
    good = "SELECT avg(price) AS avg_price FROM uk.uk_price_paid WHERE town = 'LEEDS'"
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW, bad: rs(["avg_price"], [None])})
    ch.set(good, rs(["avg_price"], [260_000.0]))
    model = ScriptedModel(
        [turn(final(bad, "No data.")), turn(final(good, "The average was £260,000."))]
    )
    run, _ = ask(model, self_check=make_self_check(ch, RULES))
    assert run.self_check == "sent" and run.answer.sql == good
    assert len(model.user_texts) == 1 and "avg_price is NULL" in model.user_texts[0]
    assert bad in ch.sqls() and good not in ch.sqls()


@pytest.mark.parametrize(
    "patch, why",
    [
        (("max_change_pct: 500", "max_change_pct: 0"), "greater than 0"),
        (("{min: 1000, max: 10000000}", "{min: 10, max: 5}"), "below"),
        (
            ("zero_rows: It returned no rows.", "zero_rows: '{column} returned no rows.'"),
            "unknown placeholders",
        ),
        (("  execution_failed: 'The query did not run ({error}).'\n", ""), "missing"),
        (("change: '(?i)^(pct_change|yoy_pct)$'", "change: '(unclosed'"), "bad regular expression"),
        (("{min: 1000, max: 10000000}", "{min: 5000, max: 5000}"), "below"),
        (("messages:\n", "messages:\n  no_such_rule: Something.\n"), "unknown|no_such_rule"),
    ],
)
def test_malformed_rules_are_refused(patch, why):
    text = BUNDLED.root.joinpath("fixes/self_check.yaml").read_text(encoding="utf-8")
    old, new = patch
    assert old in text
    with pytest.raises(FixError, match=why):
        parse_rules(text.replace(old, new))


@pytest.mark.live
@LIVE
def test_live_false_positive_rate_over_real_gold_results():  # pragma: no cover - live
    """Every selected answer question's real gold result is a correct answer: the share the
    self-check flags is its false-positive rate (reported; at most 5% passes)."""
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.fixes.self_check import stats_from
    from agentic_analytics.run.gold import load_gold_check, load_gold_results
    from agentic_analytics.warehouse import executors_from_env

    check = load_gold_check(BUNDLED)
    assert check is not None, "run `agentic-analytics gold` first"
    selected = [q for q in load_questions(BUNDLED) if q.id in check.questions]
    gold = load_gold_results(BUNDLED, selected, ("clickhouse",))
    load_dotenv()
    ch, _ = executors_from_env()
    assert ch is not None, "no ClickHouse variables set"
    stats = stats_from(ch.execute(RULES.stats_sql, row_cap=1))  # all eight rules, as live
    assert stats is not None, "the stats query failed"
    print(f"table stats: {stats}")
    flagged: list[str] = []
    for q in selected:
        g = gold.get((q.id, "clickhouse"))
        if g is None or q.gold_sql is None:
            continue
        sql = bind_as_of(q.gold_sql["clickhouse"], check.as_of)
        hits = judge(sql, g.result, RULES, stats, expects_empty=q.expects_empty)
        if hits:
            flagged.append(f"{q.id}: {[r for r, _ in hits]}")
    rate = len(flagged) / max(1, len(gold))
    print(f"self-check false-positive rate on gold: {rate:.1%} ({len(flagged)}/{len(gold)})")
    print("\n".join(flagged))
    assert rate <= 0.05, flagged


@pytest.mark.parametrize("value", [1_000, 10_000_000])
def test_price_level_exactly_at_the_limits_passes(value):
    sql = "SELECT quantileExactInclusive(0.5)(price) AS median_price FROM uk.uk_price_paid"
    assert rules_hit(sql, rs(["median_price"], [value])) == []


def test_count_equal_to_the_table_rows_passes():
    sql = "SELECT count() AS sales FROM uk.uk_price_paid"
    assert rules_hit(sql, rs(["sales"], [30_000_000])) == []


@pytest.mark.parametrize("value", [500.0, -500.0])
def test_change_of_exactly_500_percent_passes(value):
    sql = "SELECT 100 * (toFloat64(b) - a) / a AS pct_change FROM s"
    assert rules_hit(sql, rs(["pct_change"], [value])) == []


def test_dates_and_years_at_the_data_range_edges_pass():
    years = "SELECT toYear(date) AS y, count() AS n FROM uk.uk_price_paid GROUP BY y"
    assert rules_hit(years, rs(["y", "n"], [1995, 3])) == []
    assert rules_hit(years, rs(["y", "n"], [1994, 3])) == ["date_out_of_range"]
    days = "SELECT toDate(date) AS d, count() AS n FROM uk.uk_price_paid GROUP BY d"
    assert rules_hit(days, rs(["d", "n"], ["1995-01-01", 3], ["2026-10-30", 2])) == []
    assert rules_hit(days, rs(["d", "n"], ["2026-10-31", 2])) == ["date_out_of_range"]


def test_a_transport_failure_on_the_agents_sql_gives_no_feedback_and_skips_the_stats():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: STATS_ROW})
    ch.set("SELECT 2 AS lost", "transport")
    assert run_hook(ch, "SELECT 2 AS lost") is None
    assert RULES.stats_sql not in ch.sqls()  # no stats query for a query that did not run


def test_a_failed_stats_query_is_retried_on_the_next_call():
    ch = FakeExecutor("clickhouse", {RULES.stats_sql: ["transport", STATS_ROW]})
    sql = "SELECT count() AS n FROM uk.uk_price_paid"
    ch.set(sql, rs(["n"], [10**12]))
    hook = make_self_check(ch, RULES)

    async def twice():
        return await hook(sql), await hook(sql)

    first, second = asyncio.run(twice())
    assert first is None  # stats failed: the count rule is skipped this time
    assert second is not None and "30,000,000" in second  # retried, then the rule runs
    assert ch.sqls().count(RULES.stats_sql) == 2
