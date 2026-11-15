"""Record builders for aggregate tests: one call per (question, backend, fixes, trial) outcome."""

from __future__ import annotations

from typing import Any, Literal

from agentic_analytics.schemas import Question, Record
from tests.schemas import factories as f

Outcome = Literal["right", "wrong", "clarify", "error", "exec_error"]


def rec(
    q: str = "q-001",
    outcome: Outcome = "right",
    *,
    backend: str = "claude_mcp",
    fixes: tuple[str, ...] = (),
    trial: int = 1,
    expected: Literal["answer", "clarify"] = "answer",
    rows: tuple[tuple[Any, ...], ...] = ((250000, 812),),
    cost_usd: float = 0.01,
    cortex_messages: int | None = None,
    alloc: float | None = None,
    step_latencies: tuple[int, ...] = (120, 120),
    elapsed_ms: int = 40,
    answer_from_rows: bool | None = True,
    over_budget: bool = False,
    write_attempted: bool = False,
    injection_followed: bool | None = None,
    snapshot: str = f.H,
) -> Record:
    """A schema-valid record whose scores follow §6.1 for the given outcome."""
    cortex = backend == "cortex_analyst"
    engine = "snowflake" if cortex else "clickhouse"
    sql = f.SQL[engine]
    steps = () if cortex else tuple(f.step(i, latency_ms=ms) for i, ms in enumerate(step_latencies))
    answer_kw: dict[str, Any] = dict(
        backend=backend,
        model_snapshot="cortex" if cortex else "claude-sonnet-5-5",
        steps=steps,
        sql=sql,
    )
    execution = None
    answer_expected = expected == "answer"
    if outcome in ("right", "wrong"):
        execution = f.execution(
            engine=engine,
            sql=sql,
            result=f.result(rows),
            rows_total=len(rows),
            elapsed_ms=elapsed_ms,
        )
        match = outcome == "right"
        scores = f.scores(
            result_match=match if answer_expected else None,
            correct=match and answer_expected,
            answer_from_rows=answer_from_rows,
        )
    elif outcome == "exec_error":
        execution = f.execution(
            engine=engine,
            sql=sql,
            result=None,
            rows_total=None,
            error="sql_error",
            elapsed_ms=elapsed_ms,
        )
        scores = f.scores(
            result_match=False if answer_expected else None,
            correct=False,
            answer_from_rows=None,
        )
    elif outcome == "clarify":
        answer_kw.update(sql=None, answer="", clarification="Which average do you mean?")
        scores = f.scores(
            result_match=False if answer_expected else None,
            clarified=True,
            false_clarification=answer_expected,
            correct=not answer_expected,
            answer_from_rows=None,
        )
    else:  # answer error
        answer_kw.update(sql=None, answer="", error="step_budget")
        scores = f.scores(
            result_match=False if answer_expected else None,
            correct=False,
            answer_from_rows=None,
        )
    scores = scores.model_copy(
        update=dict(
            over_budget=over_budget,
            write_attempted=write_attempted,
            # S2.1 scores every probe; default to a pass so tests opt in to failures
            injection_followed=(False if q.startswith("p-") else None)
            if injection_followed is None
            else injection_followed,
        )
    )
    return f.record(
        question_id=q,
        backend=backend,
        fixes=list(fixes),
        trial=trial,
        answer=f.answer(**answer_kw),
        execution=execution,
        scores=scores,
        cost_usd=cost_usd,
        cortex_messages=(1 if cortex else 0) if cortex_messages is None else cortex_messages,
        warehouse_credits_alloc=alloc,
        snapshot_sha256=snapshot,
    )


def qid(i: int) -> str:
    return f"q-{i:03d}"


def questions(n_answer: int, n_ambiguous: int = 0, category: str = "aggregation") -> dict:
    """Answer-expected questions q-001.. then ambiguous ones after them."""
    out: dict[str, Question] = {}
    for i in range(1, n_answer + 1):
        out[qid(i)] = f.question(id=qid(i), category=category)
    for i in range(n_answer + 1, n_answer + n_ambiguous + 1):
        out[qid(i)] = f.ambiguous(id=qid(i))
    return out


def trials(q: str, outcomes: str, **kw: Any) -> list[Record]:
    """`outcomes` is one letter per trial: R right, W wrong, C clarify, E error, X exec error."""
    letters = {"R": "right", "W": "wrong", "C": "clarify", "E": "error", "X": "exec_error"}
    return [rec(q, letters[o], trial=i + 1, **kw) for i, o in enumerate(outcomes)]  # type: ignore[arg-type]
