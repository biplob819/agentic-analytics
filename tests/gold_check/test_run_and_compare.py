"""GLD-01 (gold run on every engine through the executors, 200-row cap) and GLD-02 (cross-engine
match at each question's tolerance; a mismatch blocks with exit 1)."""

from __future__ import annotations

from datetime import date

import pytest

from agentic_analytics.gold import GOLD_ROW_CAP, GoldConfigError, check_question, run_gold_check
from tests.gold_check.fakes import AS_OF, SNAPSHOT, bound, rs
from tests.gold_check.world import make_world


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def run(world, *engines, **kw):
    kw.setdefault("write", False)
    return run_gold_check(
        world.pack,
        world.questions,
        world.executors(*engines),
        as_of=kw.pop("as_of", AS_OF),
        snapshot_sha256=SNAPSHOT,
        **kw,
    )


# -- GLD-01 -------------------------------------------------------------------------------------


def test_clean_world_passes_and_runs_every_answer_question_on_both_engines(world):
    out = run(world)
    assert out.failures == () and out.exit_code == 0
    assert out.check is not None and out.check.status == "pass"
    for q in world.questions:
        if q.expected == "answer":
            assert bound(q, "clickhouse") in world.ch.sqls()
            assert bound(q, "snowflake") in world.sf.sqls()
    assert all(cap == GOLD_ROW_CAP == 200 for _, cap in world.ch.calls + world.sf.calls)


def test_as_of_is_bound_before_execution(world):
    run(world)
    q = world.q("q-026")
    assert "'{as_of}'" in q.gold_sql["clickhouse"]  # type: ignore[index]
    sent = [s for s in world.ch.sqls() if "toDate(" in s]
    assert sent and all("toDate('2026-09-30')" in s for s in sent)
    assert not any("{as_of}" in s for s in world.ch.sqls() + world.sf.sqls())


def test_ambiguous_questions_are_not_executed(world):
    run(world)
    q = world.q("q-101")
    assert q.expected == "clarify"
    gold_sqls = {bound(x, "clickhouse") for x in world.questions if x.expected == "answer"}
    probe_sqls = {bound(p, "clickhouse") for p in world.probes}
    assert set(world.ch.sqls()) == gold_sqls | probe_sqls | {world.top_sql()}
    assert not any("q-101" in s for s in world.ch.sqls())


def test_error_truncation_and_empty_results_fail(world):
    world.sf.set(bound(world.q("q-001"), "snowflake"), "sql_error")
    world.ch.set(bound(world.q("q-026"), "clickhouse"), rs(["sales"], *[[i] for i in range(201)]))
    world.sf.set(bound(world.q("q-002"), "snowflake"), rs(["MEDIAN_PRICE"]))
    out = run(world)
    assert out.exit_code == 1 and out.check is not None and out.check.status == "fail"
    assert "q-001 [snowflake]: execution failed (sql_error)" in out.failures
    assert "q-026 [clickhouse]: more than 200 gold rows (201)" in out.failures
    assert "q-002 [snowflake]: no rows (and expects_empty is false)" in out.failures


def test_expects_empty_inverts_the_empty_rule(world):
    q = world.q("q-001").model_copy(update={"expects_empty": True})
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert "q-001 [clickhouse]: 1 rows but expects_empty is true" in out.failures
    world.ch.set(bound(q, "clickhouse"), rs(["sales"]))
    world.sf.set(bound(q, "snowflake"), rs(["SALES"]))
    assert check_question(q, world.executors(), as_of=AS_OF).passed


def test_no_engine_is_a_config_error(world):
    with pytest.raises(GoldConfigError, match="no engine"):
        run_gold_check(world.pack, world.questions, {}, as_of=AS_OF, snapshot_sha256=SNAPSHOT)


def test_a_different_as_of_sends_different_sql(world):
    q = world.q("q-026")
    other = date(2025, 12, 31)
    world.ch.set(bound(q, "clickhouse", other), rs(["sales"], [1]))
    world.sf.set(bound(q, "snowflake", other), rs(["SALES"], [1]))
    assert check_question(q, world.executors(), as_of=other).passed


# -- GLD-02 -------------------------------------------------------------------------------------


def test_planted_cross_engine_mismatch_blocks(world):
    world.sf.set(bound(world.q("q-001"), "snowflake"), rs(["SALES"], [1_000_001]))
    out = run(world)
    assert out.exit_code == 1
    # p-iq01's gold is the same SQL as q-001's, so the planted mismatch fails both
    assert [f.split(":")[0] for f in out.failures] == ["q-001", "p-iq01"]
    assert out.failures[0].startswith("q-001: cross-engine mismatch: ")
    assert out.check is not None and out.check.status == "fail"
    assert "q-001" not in out.check.questions  # never selected while failing


def test_tolerance_covers_only_the_named_columns(world):
    q = world.q("q-002")  # median_price at rel 0.005
    world.sf.set(bound(q, "snowflake"), rs(["MEDIAN_PRICE"], [290_000.0 * 1.004]))
    assert check_question(q, world.executors(), as_of=AS_OF).passed
    world.sf.set(bound(q, "snowflake"), rs(["MEDIAN_PRICE"], [290_000.0 * 1.006]))
    assert not check_question(q, world.executors(), as_of=AS_OF).passed
    count = world.q("q-001")  # exact
    world.sf.set(bound(count, "snowflake"), rs(["SALES"], [1_000_000.5]))
    assert not check_question(count, world.executors(), as_of=AS_OF).passed


def test_column_mapping_by_name_ignores_case_and_order_but_not_extra_columns(world):
    q = world.q("q-071")
    rows = [["terraced", 300_000], ["semi-detached", 250_000], ["flat", 200_000]]
    world.sf.set(bound(q, "snowflake"), rs(["SALES", "TYPE"], *[[s, t] for t, s in rows]))
    assert check_question(q, world.executors(), as_of=AS_OF).passed
    world.sf.set(bound(q, "snowflake"), rs(["TYPE", "SALES", "X"], *[[*r, 1] for r in rows]))
    out = check_question(q, world.executors(), as_of=AS_OF)
    assert out.failures == (
        "q-071: cross-engine mismatch: column_count (clickhouse=2, snowflake=3)",
    )


def test_order_matters_only_for_ordered_questions(world):
    q = world.q("q-071")  # ordered top-N
    rows = [["flat", 200_000], ["semi-detached", 250_000], ["terraced", 300_000]]
    world.sf.set(bound(q, "snowflake"), rs(["TYPE", "SALES"], *rows))
    assert not check_question(q, world.executors(), as_of=AS_OF).passed
    unordered = q.model_copy(update={"ordered": False})
    assert check_question(unordered, world.executors(), as_of=AS_OF).passed


def test_single_engine_mode_skips_the_comparison_and_is_stamped(world):
    world.sf.set(bound(world.q("q-001"), "snowflake"), rs(["SALES"], [5]))  # never asked
    out = run(world, "clickhouse")
    assert out.exit_code == 0 and out.check is not None
    assert out.check.mode == "single_engine" and out.check.engines == ("clickhouse",)
    assert world.sf.calls == []
