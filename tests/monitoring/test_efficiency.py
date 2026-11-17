"""MON-04: step efficiency, tool calls used against each question's minimum, per backend."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from agentic_analytics.aggregate import Rates, build_summary
from agentic_analytics.monitoring.efficiency import (
    MIN_TOOL_CALLS,
    min_tool_calls,
    step_efficiency,
    step_efficiency_by_config,
)
from agentic_analytics.schemas import BackendSummary, Record, RunSummary, StepEfficiency
from tests.aggregate.helpers import qid, questions, rec
from tests.schemas import factories as f


def calls(r: Record, n: int, *, final: bool = True, refused: int = 0) -> Record:
    """`r` with n MCP calls (the last `refused` of them refused by the budget) and, when
    `final`, a final_answer step after them."""
    steps = [f.step(i, error="budget" if i >= n - refused else None) for i in range(n)]
    if final:
        steps.append(f.step(n, tool="final_answer", arguments={"answer": "x"}))
    return r.model_copy(update={"answer": r.answer.model_copy(update={"steps": tuple(steps)})})


def test_minimums_follow_evalkit_min_steps_with_the_final_answer_free():
    from evalkit.agents.score import MIN_STEPS  # EvalKit's "fewest steps a perfect agent needs"

    assert MIN_TOOL_CALLS["answer"] == MIN_STEPS["analytics"] - 1  # query, then a free answer
    assert MIN_TOOL_CALLS["injection"] == MIN_STEPS["attack"] - 1
    assert MIN_TOOL_CALLS["clarify"] == 0  # no simulated user: ask back in the final answer
    assert min_tool_calls(f.question()) == 1
    assert min_tool_calls(f.ambiguous()) == 0
    assert min_tool_calls(f.probe()) == 1
    assert (
        min_tool_calls(f.probe(id="p-w01", kind="write", expected="refuse", injection_marker=None))
        == 0
    )


def test_step_efficiency_means_excess_and_share_at_minimum():
    qs = questions(3, n_ambiguous=1)
    recs = [
        calls(rec(qid(1)), 3),  # 2 beyond the minimum of 1
        calls(rec(qid(2)), 1),  # at the minimum
        calls(rec(qid(3), "error"), 7, refused=1),  # 6 beyond; refused calls count (MCP-04)
        calls(rec(qid(4), "clarify", expected="clarify"), 0),  # ambiguous, asked back: at 0
    ]
    eff = step_efficiency(recs, qs)
    assert eff == StepEfficiency(
        answers=4,
        tool_calls_mean=11 / 4,
        min_tool_calls_mean=3 / 4,
        excess_mean=8 / 4,
        at_minimum=2 / 4,
    )


def test_final_answer_is_not_a_call():
    """Planted defect: counting the free final_answer step would put every answer over."""
    qs = questions(1)
    eff = step_efficiency([calls(rec(qid(1)), 1, final=True)], qs)
    assert eff is not None and eff.excess_mean == 0.0 and eff.at_minimum == 1.0


def test_an_answer_under_its_minimum_counts_no_excess():
    qs = questions(2)
    eff = step_efficiency([calls(rec(qid(1)), 0), calls(rec(qid(2)), 3)], qs)
    assert eff is not None
    assert eff.excess_mean == 1.0  # (0 + 2) / 2: the unqueried answer does not offset
    assert eff.tool_calls_mean - eff.min_tool_calls_mean == 0.5
    assert eff.at_minimum == 0.5


def test_cortex_and_empty_input_have_no_step_efficiency():
    qs = questions(1)
    assert step_efficiency([rec(qid(1), backend="cortex_analyst")], qs) is None
    assert step_efficiency([], qs) is None


def test_step_efficiency_is_per_backend_and_needs_every_question():
    qs = questions(1)
    with pytest.raises(ValueError, match="per backend"):
        step_efficiency([rec(qid(1)), rec(qid(1), backend="gpt_mcp")], qs)
    with pytest.raises(ValueError, match="no question or probe"):
        step_efficiency([rec(qid(9))], qs)


def test_by_config_view_labels_each_mcp_configuration():
    qs = questions(1)
    recs = [
        calls(rec(qid(1)), 2),
        calls(rec(qid(1), fixes=("examples",)), 1),
        calls(rec(qid(1), backend="gpt_mcp"), 1),
        rec(qid(1), backend="cortex_analyst"),
    ]
    view = step_efficiency_by_config(recs, qs)
    assert list(view) == ["claude_mcp", "claude_mcp:examples", "gpt_mcp"]
    assert view["claude_mcp"].excess_mean == 1.0 and view["claude_mcp:examples"].excess_mean == 0.0


# The schema


def test_step_efficiency_schema_refuses_inconsistent_numbers():
    base: dict[str, Any] = dict(
        answers=2, tool_calls_mean=2.0, min_tool_calls_mean=1.0, excess_mean=1.0, at_minimum=0.0
    )
    StepEfficiency.model_validate(base)
    with pytest.raises(ValidationError, match="excess_mean"):
        StepEfficiency.model_validate({**base, "excess_mean": 0.5})
    with pytest.raises(ValidationError, match="no excess"):
        StepEfficiency.model_validate({**base, "at_minimum": 1.0})
    with pytest.raises(ValidationError):
        StepEfficiency.model_validate({**base, "at_minimum": 1.5})
    with pytest.raises(ValidationError):
        StepEfficiency.model_validate({**base, "answers": 0})


def test_cortex_summaries_carry_no_step_efficiency():
    eff = StepEfficiency(
        answers=1, tool_calls_mean=1.0, min_tool_calls_mean=1.0, excess_mean=0.0, at_minimum=1.0
    )
    f.backend_summary(step_efficiency=eff)
    with pytest.raises(ValidationError, match="no agent steps"):
        f.backend_summary(backend="cortex_analyst", step_efficiency=eff)


def test_an_unset_step_efficiency_is_left_out_so_old_summaries_keep_their_bytes():
    s = f.run_summary()
    text = s.model_dump_json(indent=2)
    assert "step_efficiency" not in text
    assert RunSummary.model_validate_json(text).model_dump_json(indent=2) == text
    eff = StepEfficiency(
        answers=1, tool_calls_mean=2.0, min_tool_calls_mean=1.0, excess_mean=1.0, at_minimum=0.0
    )
    b = f.backend_summary(step_efficiency=eff)
    back = BackendSummary.model_validate_json(b.model_dump_json())
    assert back == b and json.loads(b.model_dump_json())["step_efficiency"]["excess_mean"] == 1.0


# In summary.json


def _build(records: list[Record], qs: dict) -> RunSummary:
    return build_summary(
        records,
        qs,
        run_id="r-0001",
        config_hash=f.H,
        snapshot_sha256=f.H,
        as_of=date(2026, 9, 30),
        gold_check_id=f.H,
        pack="uk_price_paid@1.0",
        gold_mode="cross_engine",
        rates=Rates(credits_per_message=0.067, usd_per_credit=3.0),
        seed=7,
        started_at=f.T0,
        finished_at=f.T0 + timedelta(minutes=30),
        n_resamples=200,
    )


def test_summary_carries_step_efficiency_per_mcp_backend_without_probes():
    qs = questions(2)
    recs = [
        calls(rec(qid(1)), 1),
        calls(rec(qid(2)), 3),
        calls(rec(qid(1), backend="gpt_mcp"), 1),
        calls(rec(qid(2), backend="gpt_mcp"), 1),
        rec(qid(1), backend="cortex_analyst"),
        rec(qid(2), backend="cortex_analyst"),
        calls(rec("p-iq01", injection_followed=False), 9),  # probes are not benchmark answers
    ]
    s = _build(recs, qs)
    by = {b.backend: b.step_efficiency for b in s.backends}
    assert by["claude_mcp"] is not None and by["claude_mcp"].excess_mean == 1.0
    assert by["claude_mcp"].answers == 2  # the probe record is not counted
    assert by["gpt_mcp"] is not None and by["gpt_mcp"].excess_mean == 0.0
    assert by["cortex_analyst"] is None
    data = json.loads(s.model_dump_json())
    keys = {b["backend"]: "step_efficiency" in b for b in data["backends"]}
    assert keys == {"claude_mcp": True, "gpt_mcp": True, "cortex_analyst": False}


def test_adding_step_efficiency_moves_no_other_summary_number(monkeypatch):
    """The field is computed from the records alone and draws nothing from the bootstrap, so
    every other number is what it was before the field existed."""
    qs = questions(2)
    recs = [
        calls(rec(qid(1)), 1),
        calls(rec(qid(2), "wrong"), 4),
        rec(qid(1), backend="cortex_analyst"),
    ]
    with_field = json.loads(_build(recs, qs).model_dump_json())
    monkeypatch.setattr("agentic_analytics.aggregate.step_efficiency", lambda *_a: None)
    before = json.loads(_build(recs, qs).model_dump_json())
    assert any("step_efficiency" in b for b in with_field["backends"])
    for b in with_field["backends"]:
        b.pop("step_efficiency", None)
    assert with_field == before
