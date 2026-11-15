"""Re-execute a backend's final SQL on its own engine (SCR-01, Tech Spec §2.3, D-3).

The executor is injected: anything with an `engine` and `execute(sql, *, row_cap)` that returns
an `Execution` and never raises (the warehouse executors of N1.2 satisfy it structurally).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentic_analytics.schemas import ENGINE_OF, AnalyticsAnswer, Engine, Execution

ROW_CAP = 10_000  # executor row cap (Tech Spec §8, RunConfig.row_cap_exec)


class ScoringError(ValueError):
    """Scoring was wired wrongly (wrong engine, missing gold, mismatched SQL); not a score."""


@runtime_checkable
class Executor(Protocol):
    engine: Engine

    def execute(self, sql: str, *, row_cap: int) -> Execution: ...  # SQL guard runs first


def reexecute(
    answer: AnalyticsAnswer, executor: Executor, *, row_cap: int = ROW_CAP
) -> Execution | None:
    """Run the answer's final SQL through `executor`; None when the answer has no SQL.

    A failed run comes back as an `Execution` with `error` set, which scores incorrect.
    """
    if answer.sql is None:
        return None
    want = ENGINE_OF[answer.backend]
    if executor.engine != want:
        raise ScoringError(f"{answer.backend} answers execute on {want}, not {executor.engine}")
    if row_cap < 1:
        raise ScoringError("row_cap must be at least 1")
    execution = executor.execute(answer.sql, row_cap=row_cap)
    if execution.engine != want:
        raise ScoringError(f"the executor reported engine {execution.engine}, expected {want}")
    if execution.sql != answer.sql:
        raise ScoringError("the executor must report the SQL it was given, unchanged")
    return execution
