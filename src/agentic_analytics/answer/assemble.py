"""`{sql, answer, chart}` assembled the same way for every backend (ANS-02, D-6).

`assemble` sets the chart from the answer's own executed rows through the one builder
(`chart_for`), passing the execution's `rows_total` so a truncated result gets no chart. An
answer without SQL, without an execution, or whose execution failed has no chart: a chart never
exists without executed rows, so a chart already on such an answer is cleared.
Copies are re-validated through `AnalyticsAnswer.model_validate`, so the chart is checked as JSON
and deep-frozen exactly as a parsed answer would be.
"""

from __future__ import annotations

from typing import Any

from agentic_analytics.answer.chart import chart_for
from agentic_analytics.schemas import ENGINE_OF, AnalyticsAnswer, Execution


def _copy(answer: AnalyticsAnswer, **changes: Any) -> AnalyticsAnswer:
    return AnalyticsAnswer.model_validate({**answer.model_dump(), **changes})


def assemble(
    answer: AnalyticsAnswer,
    execution: Execution | None,
    *,
    question_text: str | None = None,
) -> AnalyticsAnswer:
    """The answer with `chart` built from its executed rows (or cleared when there are none)."""
    if execution is not None:
        if answer.sql is None:
            raise ValueError("an execution was given for an answer with no SQL")
        if execution.sql != answer.sql:
            raise ValueError("the execution must be of the answer's SQL")
        engine = ENGINE_OF[answer.backend]
        if execution.engine != engine:
            raise ValueError(f"{answer.backend} answers execute on {engine}")
    if execution is None or execution.result is None:
        return answer if answer.chart is None else _copy(answer, chart=None)
    chart = chart_for(execution.result, question_text, rows_total=execution.rows_total)
    return _copy(answer, chart=chart)


def with_trace(
    answer: AnalyticsAnswer,
    *,
    trace_url: str | None = None,
    trace_export: str | None = None,
) -> AnalyticsAnswer:
    """The answer with where its trace went; a None argument keeps the current value."""
    changes: dict[str, Any] = {}
    if trace_url is not None:
        changes["trace_url"] = trace_url
    if trace_export is not None:
        changes["trace_export"] = trace_export
    return _copy(answer, **changes) if changes else answer
