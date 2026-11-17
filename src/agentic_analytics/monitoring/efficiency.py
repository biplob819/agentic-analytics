"""Step efficiency (M4.1 MON-04, OWNER D-74, OWNER D-75, US5): MCP tool calls used against the
question's minimum, per backend configuration, from the recorded steps.

The minimum follows EvalKit's agent-trajectory `min_steps` idea (`evalkit.agents.score`:
"the fewest steps a perfect agent needs") in this benchmark's units: tool calls, where the
final answer is free (D-5, B3), so EvalKit's analytics minimum of 2 steps (query, then answer)
is 1 call here. This protocol has no simulated user, so an ambiguous question's minimum is 0:
the agent asks back in its final answer without querying (EvalKit's 3 assumes an `ask_user`
turn and a query after it). Probes: a write probe needs no call (refuse); an injection probe
needs the one query that answers its question.

Cost per success and agent trial agreement are not computed: `cost_per_correct_usd` and
`trial_agreement` already answer those questions (§6.3, OWNER D-75).

Cortex Analyst answers have no agent steps (one API call per question), so they get no step
efficiency (None).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from agentic_analytics.schemas import MCP_BACKENDS, Probe, Question, Record
from agentic_analytics.schemas.summary import StepEfficiency
from agentic_analytics.steps import count_tool_calls

MIN_TOOL_CALLS = {"answer": 1, "clarify": 0, "write": 0, "injection": 1}


def min_tool_calls(item: Question | Probe) -> int:
    """The fewest MCP calls a perfect agent needs for this question or probe."""
    if isinstance(item, Probe):
        return MIN_TOOL_CALLS["write" if item.kind == "write" else "injection"]
    return MIN_TOOL_CALLS[item.expected]


def step_efficiency(
    records: Iterable[Record], items: Mapping[str, Question | Probe]
) -> StepEfficiency | None:
    """Mean tool calls, mean minimum, mean calls beyond the minimum (an answer under its minimum
    counts 0 beyond it) and the share of answers at or under the minimum, over every record
    given: errored answers too, since their calls were spent. None when there is nothing to
    measure: no records, or records of a backend without agent steps (Cortex)."""
    records = list(records)
    backends = {r.backend for r in records}
    if len(backends) > 1:
        raise ValueError(f"step efficiency is per backend; got {sorted(backends)}")
    if not records or records[0].backend not in MCP_BACKENDS:
        return None
    missing = sorted({r.question_id for r in records} - set(items))
    if missing:
        raise ValueError(f"no question or probe for {missing}")
    used = [count_tool_calls(r.answer) for r in records]
    least = [min_tool_calls(items[r.question_id]) for r in records]
    n = len(used)
    return StepEfficiency(
        answers=n,
        tool_calls_mean=sum(used) / n,
        min_tool_calls_mean=sum(least) / n,
        excess_mean=sum(max(0, u - m) for u, m in zip(used, least, strict=True)) / n,
        at_minimum=sum(u <= m for u, m in zip(used, least, strict=True)) / n,
    )


def step_efficiency_by_config(
    records: Iterable[Record], items: Mapping[str, Question | Probe]
) -> dict[str, StepEfficiency]:
    """{"backend" or "backend:fix+fix": StepEfficiency} for every MCP configuration, the
    monitoring view of the same numbers the summary carries per `BackendSummary`."""
    groups: dict[tuple[str, tuple[str, ...]], list[Record]] = {}
    for r in records:
        if r.backend in MCP_BACKENDS:
            groups.setdefault((r.backend, tuple(r.fixes)), []).append(r)
    out: dict[str, StepEfficiency] = {}
    for (backend, fixes), recs in sorted(groups.items()):
        eff = step_efficiency(recs, items)
        if eff is not None:
            out[backend + (f":{'+'.join(fixes)}" if fixes else "")] = eff
    return out


__all__ = ["MIN_TOOL_CALLS", "min_tool_calls", "step_efficiency", "step_efficiency_by_config"]
