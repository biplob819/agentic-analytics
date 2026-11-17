"""The demo (D4.1; Tech Spec §10): `ask()` over replay or guarded live mode, and the replay
cache.

`ask()` itself is `agentic_analytics.demo.ask.ask`; it is not re-exported here, so the name
`agentic_analytics.demo.ask` always means the module.
"""

from agentic_analytics.demo.ask import (
    CORTEX_LINE,
    DEMO_BACKENDS,
    SPEND_KEY,
    BudgetedModel,
    DemoContext,
    DemoLimits,
    LiveResult,
    Panel,
    WarmUp,
    ask_panels,
    make_guard,
    request_bound_usd,
    run_live,
    warm_up,
)
from agentic_analytics.demo.cache import (
    DemoCache,
    DemoEntry,
    build_demo_cache,
    load_cache,
    load_example_cache,
    save_cache,
    select_questions,
)

__all__ = [
    "CORTEX_LINE",
    "DEMO_BACKENDS",
    "SPEND_KEY",
    "BudgetedModel",
    "DemoCache",
    "DemoContext",
    "DemoEntry",
    "DemoLimits",
    "LiveResult",
    "Panel",
    "WarmUp",
    "ask_panels",
    "build_demo_cache",
    "load_cache",
    "load_example_cache",
    "make_guard",
    "request_bound_usd",
    "run_live",
    "save_cache",
    "select_questions",
    "warm_up",
]
