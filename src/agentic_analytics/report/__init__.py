"""Results dashboard: one self-contained HTML page per run (B3.1, EvalKit D-42)."""

from agentic_analytics.report.example import EXAMPLE_RUN_ID, load_example, render_example
from agentic_analytics.report.render import (
    failure_reason,
    render_html,
    render_report,
    resolve_credit,
    vega_lite_specs,
)

__all__ = [
    "EXAMPLE_RUN_ID",
    "failure_reason",
    "load_example",
    "render_example",
    "render_html",
    "render_report",
    "resolve_credit",
    "vega_lite_specs",
]
