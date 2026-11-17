"""AGG-08 build_summary and the atomic summary.json writer."""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta

import pytest

from agentic_analytics.aggregate import (
    Rates,
    build_summary,
    latency_ms,
    probes_failed,
    write_summary,
)
from agentic_analytics.schemas import RunSummary
from tests.aggregate.helpers import qid, questions, rec, trials
from tests.schemas import factories as f

RATES = Rates(credits_per_message=0.067, usd_per_credit=3.0)
N = 300


def _run():
    """Two answer and two ambiguous questions on three backends, a fix run and probes."""
    qs = questions(2, n_ambiguous=2)
    records = []
    for backend in ("claude_mcp", "gpt_mcp", "cortex_analyst"):
        records += trials(qid(1), "RRW", backend=backend)
        records += trials(qid(2), "WWE", backend=backend)
        records += trials(qid(3), "CCC", backend=backend, expected="clarify")
        records += trials(qid(4), "CWX", backend=backend, expected="clarify")
    records += trials(qid(1), "RRR", fixes=("descriptions",))
    records += trials(qid(2), "RRW", fixes=("descriptions",))
    records += trials(qid(3), "CCC", fixes=("descriptions",), expected="clarify")
    records += trials(qid(4), "CCW", fixes=("descriptions",), expected="clarify")
    records += [rec("p-w01", "clarify"), rec("p-iq01", "right", injection_followed=False)]
    return records, qs


def _build(records, qs, **kw):
    args = dict(
        run_id="r-0001",
        config_hash=f.H,
        snapshot_sha256=f.H,
        as_of=date(2026, 9, 30),
        gold_check_id=f.H,
        pack="uk_price_paid@1.0",
        gold_mode="cross_engine",
        rates=RATES,
        seed=7,
        started_at=f.T0,
        finished_at=f.T0 + timedelta(minutes=30),
        n_resamples=N,
    )
    args.update(kw)
    return build_summary(records, qs, **args)  # type: ignore[arg-type]


def test_build_summary_is_schema_valid_and_complete():
    records, qs = _run()
    s = _build(records, qs)
    assert RunSummary.model_validate_json(s.model_dump_json()) == s
    keys = [(b.backend, b.fixes) for b in s.backends]
    assert keys == [
        ("claude_mcp", ()),
        ("claude_mcp", ("descriptions",)),
        ("gpt_mcp", ()),
        ("cortex_analyst", ()),
    ]
    base = s.backends[0]
    assert base.trials == 3
    assert base.accuracy.point == pytest.approx((2 / 3 + 0 + 1 + 1 / 3) / 4)
    assert [c.category for c in base.by_category] == ["aggregation", "ambiguous"]
    assert base.errors == {"answer.step_budget": 1, "execution.sql_error": 1}
    assert [(u.backend, u.fix) for u in s.uplifts] == [("claude_mcp", "descriptions")]
    assert s.probes == {"p-iq01": {"claude_mcp": True}, "p-w01": {"claude_mcp": True}}
    assert probes_failed(s.probes) == []
    assert s.cost_status == "estimated"
    assert set(s.total_credits) == {"clickhouse", "snowflake"}
    assert s.total_credits["snowflake"] == pytest.approx(12 * 0.067)
    assert s.schema_version == "1.0"


def test_summary_json_is_byte_identical_for_same_inputs_and_seed(tmp_path):
    records, qs = _run()
    shuffled = records[:]
    random.Random(0).shuffle(shuffled)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_summary(a, _build(records, qs))
    write_summary(b, _build(shuffled, qs))
    assert a.read_bytes() == b.read_bytes()
    c = tmp_path / "c.json"
    write_summary(c, _build(records, qs, seed=8))
    assert c.read_bytes() != a.read_bytes()


def test_write_summary_writes_indented_json_atomically(tmp_path):
    records, qs = _run()
    s = _build(records, qs)
    path = tmp_path / "summary.json"
    path.write_text("old\n", encoding="utf-8")
    write_summary(path, s)
    text = path.read_text(encoding="utf-8")
    assert text == s.model_dump_json(indent=2) + "\n"
    assert json.loads(text)["run_id"] == "r-0001"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["summary.json"]


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    records, qs = _run()
    s = _build(records, qs)
    path = tmp_path / "summary.json"
    path.write_text("old\n", encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_summary(path, s)
    assert path.read_text(encoding="utf-8") == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["summary.json"]


def test_reconciled_only_when_every_record_has_warehouse_credits():
    records, qs = _run()
    assert _build(records, qs).cost_status == "estimated"
    filled = [r.model_copy(update={"warehouse_credits_alloc": 0.001}) for r in records]
    s = _build(filled, qs)
    assert s.cost_status == "reconciled"
    assert s.total_credits["clickhouse"] == pytest.approx(0.001 * (12 * 3 + 2))


def test_total_cost_covers_every_record_including_probes():
    records, qs = _run()
    s = _build(records, qs)
    model = sum(r.cost_usd for r in records)
    cortex = 12 * 0.067 * 3.0
    assert s.total_cost_usd == pytest.approx(model + cortex)


def test_cost_per_correct_none_for_a_backend_with_zero_correct():
    qs = questions(2)
    records = trials(qid(1), "WW") + trials(qid(2), "EW")
    records += trials(qid(1), "R", backend="gpt_mcp") + trials(qid(2), "R", backend="gpt_mcp")
    s = _build(records, qs)
    claude, gpt = s.backends
    assert claude.cost_per_correct_usd is None
    assert claude.accuracy.point == 0.0
    assert gpt.cost_per_correct_usd == pytest.approx(0.01)


def test_single_trial_run_and_missing_trials():
    qs = questions(3)
    records = trials(qid(1), "R") + trials(qid(2), "W") + trials(qid(3), "RR")
    s = _build(records, qs)
    (b,) = s.backends
    assert b.trials == 2  # the most trials any question has
    assert b.accuracy.point == pytest.approx(2 / 3)


def test_latency_is_agent_steps_plus_execution():
    r = rec(qid(1), step_latencies=(100, 250), elapsed_ms=50)
    assert latency_ms(r) == 400
    assert latency_ms(rec(qid(1), "clarify", step_latencies=(70,))) == 70
    assert latency_ms(rec(qid(1), backend="cortex_analyst", elapsed_ms=900)) == 900


def test_cortex_latency_is_the_runners_wall_time_when_recorded():
    """R3.1: a Cortex record timed question in to answer out (API round trip plus execution)
    is read on the agents' basis; without the wall time it falls back to the execution."""
    cortex = rec(qid(1), backend="cortex_analyst", elapsed_ms=900)
    timed = cortex.model_copy(update={"ask_wall_ms": 3_100, "analyst_latency_ms": 1_200.0})
    assert latency_ms(timed) == 3_100
    assert latency_ms(cortex) == 900
    # the agents keep steps plus execution; their wall time is recorded beside it (L-9)
    agent = rec(qid(1), step_latencies=(100, 250), elapsed_ms=50)
    assert latency_ms(agent.model_copy(update={"ask_wall_ms": 9_999})) == 400
    qs = questions(2)
    records = [timed, timed.model_copy(update={"question_id": qid(2), "ask_wall_ms": 5_000})]
    (b,) = _build(records, qs).backends
    assert (b.latency_p50_ms, b.latency_p95_ms) == (3_100, 5_000)  # nearest rank


def test_latency_percentiles_per_backend():
    qs = questions(10)
    records = [rec(qid(i), step_latencies=(i * 100,), elapsed_ms=0) for i in range(1, 11)]
    (b,) = _build(records, qs).backends
    assert (b.latency_p50_ms, b.latency_p95_ms) == (500, 1000)


def test_violation_counts():
    qs = questions(3)
    records = [
        rec(qid(1), answer_from_rows=False),
        rec(qid(2), over_budget=True),
        rec(qid(3), write_attempted=True, answer_from_rows=False),
    ]
    (b,) = _build(records, qs).backends
    assert b.answer_from_rows_violations == 2
    assert b.policy_violations == 2


def test_probes_are_not_part_of_accuracy():
    qs = questions(1)
    with_probe = [*trials(qid(1), "R"), rec("p-iq01", "wrong", injection_followed=False)]
    (b,) = _build(with_probe, qs).backends
    assert b.accuracy.point == 1.0 and b.trials == 1


def test_records_from_another_snapshot_or_gold_check_are_refused():
    qs = questions(1)
    with pytest.raises(ValueError, match="snapshot"):
        _build([rec(qid(1), snapshot="b" * 64)], qs)
    with pytest.raises(ValueError, match="gold_check_id"):
        _build([rec(qid(1))], qs, gold_check_id="c" * 64)


def test_a_run_needs_benchmark_records():
    with pytest.raises(ValueError, match="no benchmark records"):
        _build([rec("p-w01", "clarify")], questions(1))


def test_default_resamples_is_ten_thousand():
    import inspect

    from agentic_analytics import aggregate

    assert aggregate.DEFAULT_RESAMPLES == 10_000
    params = inspect.signature(build_summary).parameters
    assert params["n_resamples"].default == 10_000
