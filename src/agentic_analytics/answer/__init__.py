"""Answer assembly: the one chart builder and `{sql, answer, chart}` for every backend (A2.3)."""

from agentic_analytics.answer.assemble import assemble, with_trace
from agentic_analytics.answer.chart import MAX_BARS, VEGA_LITE_SCHEMA, ChartKind, chart_for

__all__ = ["MAX_BARS", "VEGA_LITE_SCHEMA", "ChartKind", "assemble", "chart_for", "with_trace"]
