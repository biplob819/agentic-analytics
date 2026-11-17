"""GLD-03 (top-N tie check through a LIMIT n+1 rewrite) and GLD-04 (the exact-function lint and
the run-twice determinism check)."""

from __future__ import annotations

import pytest

from agentic_analytics.gold import check_question, identical, ranking_index, rewrite_limit
from agentic_analytics.questions import load_questions
from tests.gold_check.fakes import AS_OF, BUNDLED, bound, rs
from tests.gold_check.world import TOP4, make_world


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


# -- GLD-03 -------------------------------------------------------------------------------------


def test_rewrite_changes_only_the_outermost_limit_literal():
    sql = (
        "WITH t AS (SELECT town FROM uk.uk_price_paid LIMIT 10) "
        "SELECT town, count() AS n FROM t GROUP BY town ORDER BY n DESC, town LIMIT 10 -- top"
    )
    out = rewrite_limit(sql, "clickhouse", 10)
    assert out == sql[: sql.rindex("LIMIT 10")] + "LIMIT 11 -- top"
    assert rewrite_limit("SELECT a FROM t ORDER BY a DESC LIMIT 5;", "snowflake", 5).endswith(
        "LIMIT 6;"
    )


@pytest.mark.parametrize(
    "sql, why",
    [
        ("SELECT a FROM t ORDER BY a", "no outermost LIMIT"),
        ("SELECT a FROM t ORDER BY a LIMIT 4", "not top_n 5"),
        ("SELECT * FROM (SELECT a FROM t LIMIT 5) AS s", "no outermost LIMIT"),
    ],
)
def test_rewrite_refuses_a_missing_or_different_limit(sql, why):
    with pytest.raises(ValueError, match=why):
        rewrite_limit(sql, "clickhouse", 5)


def test_ranking_index_is_the_first_order_by_key():
    by_alias = "SELECT town, count() AS n FROM t GROUP BY town ORDER BY n DESC, town LIMIT 3"
    assert ranking_index(by_alias, "clickhouse") == 1
    by_expr = "SELECT town, count() FROM t GROUP BY town ORDER BY count() DESC LIMIT 3"
    assert ranking_index(by_expr, "clickhouse") == 1
    hidden = "SELECT town FROM t GROUP BY town ORDER BY count() DESC LIMIT 3"
    with pytest.raises(ValueError, match="not an output column"):
        ranking_index(hidden, "clickhouse")


@pytest.mark.gold
def test_every_shipped_top_n_question_can_be_tie_checked_on_both_engines():
    tops = [q for q in load_questions(BUNDLED) if q.top_n is not None]
    assert len(tops) == 20
    for q in tops:
        for engine in ("clickhouse", "snowflake"):
            sql = bound(q, engine)  # type: ignore[arg-type]
            wider = rewrite_limit(sql, engine, q.top_n)  # type: ignore[arg-type]
            assert wider.endswith(f"LIMIT {q.top_n + 1}"), q.id  # type: ignore[operator]
            assert ranking_index(sql, engine) == 1, q.id  # type: ignore[arg-type]


def test_distinct_values_at_the_cut_pass(world):
    out = check_question(world.q("q-071"), world.executors(), as_of=AS_OF)
    assert out.passed
    assert world.top_sql() in world.ch.sqls() and world.top_sql("snowflake") not in world.sf.sqls()


def test_planted_tie_at_the_cut_fails(world):
    tied = rs(["type", "sales"], *[list(r) for r in TOP4.rows[:3]], ["detached", 200_000])
    world.ch.set(world.top_sql(), tied)
    out = check_question(world.q("q-071"), world.executors(), as_of=AS_OF)
    assert out.failures == (
        "q-071 [clickhouse]: tie at the LIMIT cut (sales 200000 and 200000 at rows 3 and 4 "
        "are within tolerance)",
    )


def median_top(world, rows, *, ordered=True, n=3):
    """q-061-like: a top-n by median price (0.5% tolerance on median_price)."""
    from agentic_analytics.schemas import Tolerance

    base = world.q("q-071")
    q = base.model_copy(
        update={
            "ordered": ordered,
            "tolerance": Tolerance(rel=0.005, columns=("median_price",)),
        }
    )
    top = rs(["town", "median_price"], *rows[:n])
    world.ch.set(bound(q, "clickhouse"), top)
    world.sf.set(bound(q, "snowflake"), rs(["TOWN", "MEDIAN_PRICE"], *rows[:n]))
    world.ch.set(world.top_sql(), rs(["town", "median_price"], *rows))
    return q


def test_values_at_the_cut_within_tolerance_are_a_tie(world):
    rows = [["A", 900_000.0], ["B", 800_000.0], ["C", 700_000.0], ["D", 698_000.0]]  # 0.29%
    q = median_top(world, rows)
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert len(out.failures) == 1 and "tie at the LIMIT cut" in out.failures[0]
    rows[3][1] = 690_000.0  # 1.4% apart: separated
    assert check_question(median_top(world, rows), world.executors(), as_of=AS_OF).passed


def test_ordered_top_n_needs_separated_neighbours_inside_the_list(world):
    rows = [["A", 900_000.0], ["B", 898_000.0], ["C", 700_000.0], ["D", 600_000.0]]
    q = median_top(world, rows)
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert out.failures == (
        "q-071 [clickhouse]: ambiguous order inside the top 3 (median_price 900000.0 and "
        "898000.0 at rows 1 and 2 are within tolerance)",
    )
    # unordered top-N: only the cut matters
    unordered = median_top(world, rows, ordered=False)
    assert check_question(unordered, world.executors(), as_of=AS_OF).passed


def test_uncovered_ranking_column_is_compared_exactly(world):
    from agentic_analytics.gold import separated
    from agentic_analytics.schemas import Tolerance

    assert separated(100, 101, None) and not separated(100, 100, None)
    assert not separated(1000.0, 1004.0, Tolerance(rel=0.005))
    assert separated(1000.0, 1006.0, Tolerance(rel=0.005))
    assert separated("a", "b", Tolerance(rel=0.5)) and not separated("a", "a", Tolerance())
    rows = [["A", 900_000], ["B", 800_000], ["C", 700_000], ["D", 699_999]]
    q = median_top(world, rows).model_copy(
        update={"tolerance": world.q("q-002").tolerance.model_copy(update={"columns": ("x",)})}
    )
    assert check_question(q, world.executors(), as_of=AS_OF).passed  # exact: 1 apart is enough


def test_fewer_than_n_plus_one_rows_cannot_tie(world):
    world.ch.set(world.top_sql(), rs(["type", "sales"], *[list(r) for r in TOP4.rows[:3]]))
    assert check_question(world.q("q-071"), world.executors(), as_of=AS_OF).passed


def test_tie_query_failure_fails(world):
    world.ch.set(world.top_sql(), "timeout")
    out = check_question(world.q("q-071"), world.executors(), as_of=AS_OF)
    assert out.failures == ("q-071 [clickhouse]: tie check query failed (timeout)",)


def test_tie_check_runs_on_snowflake_when_it_is_the_only_engine(world):
    world.sf.set(world.top_sql("snowflake"), rs(["TYPE", "SALES"], *[list(r) for r in TOP4.rows]))
    out = check_question(world.q("q-071"), world.executors("snowflake"), as_of=AS_OF)
    assert out.passed and world.top_sql("snowflake") in world.sf.sqls()


# -- GLD-04 -------------------------------------------------------------------------------------


def test_clickhouse_runs_twice_and_snowflake_once(world):
    q = world.q("q-001")
    check_question(q, world.executors(), as_of=AS_OF)
    assert world.ch.sqls().count(bound(q, "clickhouse")) == 2
    assert world.sf.sqls().count(bound(q, "snowflake")) == 1


def test_determinism_respects_ordered(world):
    q = world.q("q-071")  # ordered
    rows = [list(r) for r in TOP4.rows[:3]]
    first, again = rs(["type", "sales"], *rows), rs(["type", "sales"], *reversed(rows))
    world.ch.set(bound(q, "clickhouse"), [first, again])
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert "q-071 [clickhouse]: not deterministic (two runs differ)" in out.failures
    unordered = q.model_copy(update={"ordered": False})
    world.ch._served.clear()
    assert check_question(unordered, world.executors(), as_of=AS_OF).passed


def test_planted_non_deterministic_result_fails(world):
    q = world.q("q-002")
    world.ch.set(
        bound(q, "clickhouse"),
        [rs(["median_price"], [290_000.0]), rs(["median_price"], [290_001.0])],
    )
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert out.failures == ("q-002 [clickhouse]: not deterministic (two runs differ)",)


def test_row_order_counts_only_for_ordered_questions():
    a = rs(["t", "n"], ["x", 1], ["y", 2])
    b = rs(["t", "n"], ["y", 2], ["x", 1])
    assert identical(a, b, ordered=False) and not identical(a, b, ordered=True)
    assert not identical(a, rs(["T", "n"], ["x", 1], ["y", 2]), ordered=False)
    assert not identical(rs(["n"], [1], [1]), rs(["n"], [1]), ordered=False)


def test_second_run_failure_fails(world):
    q = world.q("q-001")
    world.ch.set(bound(q, "clickhouse"), [rs(["sales"], [1_000_000]), "transport"])
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert out.failures == ("q-001 [clickhouse]: second run failed (transport)",)


def test_exact_function_lint_fails_the_question(world, tmp_path):
    from agentic_analytics.gold import run_gold_check
    from tests.gold_check.fakes import SNAPSHOT, write_pack

    q = world.q("q-002")
    approx = q.model_copy(
        update={
            "gold_sql": {
                "clickhouse": q.gold_sql["clickhouse"].replace(
                    "quantileExactInclusive(0.5)", "median"
                ),  # type: ignore[index]
                "snowflake": q.gold_sql["snowflake"],  # type: ignore[index]
            }
        }
    )
    qs = [approx if x.id == "q-002" else x for x in world.questions]
    root = write_pack(
        tmp_path / "approx", qs, {"aggregation": 2, "time": 1, "top_n": 1, "ambiguous": 1}
    )
    world.ch.set(bound(approx, "clickhouse"), rs(["median_price"], [290_000.0]))
    from agentic_analytics.packs import load_pack

    out = run_gold_check(
        load_pack(root), qs, world.executors(), as_of=AS_OF, snapshot_sha256=SNAPSHOT, write=False
    )
    assert out.exit_code == 1
    assert any(
        f.startswith("q-002: lint approximate_function [clickhouse]: median()")
        for f in out.failures
    ), out.failures
