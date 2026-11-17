"""Generate the bundled SYNTHETIC demo cache (D4.1, ASK-02) from the test fakes.

    uv run python -m tests.demo.example_data

Writes `src/agentic_analytics/demo/example/replay.json`: 10 sample questions answered on the
demo's two backends by the real `run_panels` over the fake world of `tests/run/world.py`
(recorded model turns, the in-process MCP server, the fake warehouses and Cortex transport).
Latencies are replaced by values derived from the recorded steps so the file is byte-stable.
`test_example.py` fails when the file drifts from this generator.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from agentic_analytics.cost import credit_rates, model_prices
from agentic_analytics.demo.ask import DEMO_BACKENDS, DemoContext, DemoLimits, Panel
from agentic_analytics.demo.cache import (
    SYNTHETIC_LABEL,
    DemoCache,
    DemoEntry,
    build_demo_cache,
    dump_cache,
    select_questions,
)
from tests.run import world as w

EXAMPLE = Path(__file__).resolve().parents[2] / "src" / "agentic_analytics" / "demo" / "example"
EXAMPLE_FILE = EXAMPLE / "replay.json"
N_QUESTIONS = 10


def demo_world() -> w.World:
    """The fake world's 12 smoke questions, without probes (a probe's planted rows would
    answer one sample's SQL with another value)."""
    return w.World(w.World.from_bundled().questions, [])


def fake_context(backends=DEMO_BACKENDS, counter: w.Counter | None = None) -> DemoContext:
    """The fake world as live services (no network, no account)."""
    world = demo_world()
    cfg = w.config(backends=tuple(backends))
    services = w.services_from_recording(world.recording(), cfg, counter)
    return DemoContext(cfg, services, model_prices(), credit_rates(), DemoLimits())


def _stable(panel: Panel) -> Panel:
    a = panel.answer
    if a.steps:
        ms = sum(s.latency_ms for s in a.steps)
    else:
        ms = 1500 + int(hashlib.sha256((a.sql or a.answer).encode()).hexdigest()[:4], 16) % 1500
    return panel.model_copy(update={"latency_ms": ms})


def build_example() -> DemoCache:
    world = demo_world()
    ctx = fake_context()
    cache, _ = build_demo_cache(
        select_questions(world.questions, N_QUESTIONS),
        ctx,
        pack=w.BUNDLED.ref,
        attribution=w.BUNDLED.manifest.attribution,
        backends=DEMO_BACKENDS,
        synthetic=True,
        label=SYNTHETIC_LABEL,
        now=w.fixed_now,
    )
    entries = tuple(
        DemoEntry.model_validate(
            {**e.model_dump(), "panels": [_stable(p).model_dump() for p in e.panels]}
        )
        for e in cache.entries
    )
    return DemoCache.model_validate({**cache.model_dump(), "entries": entries})


def example_text() -> str:
    return dump_cache(build_example())


def main() -> int:
    EXAMPLE.mkdir(parents=True, exist_ok=True)
    EXAMPLE_FILE.write_text(example_text(), encoding="utf-8", newline="\n")
    print(f"wrote {EXAMPLE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
