"""Live check of the assumed Langfuse v3 API (ANS-03) against the real SDK.

Needs the `langfuse` package (not a project dependency) and `LANGFUSE_*` in `.env`; marked `live`,
skipped unless `AA_LIVE=1`, never run in CI. Sends one small public trace and prints its URL.
"""

import os

import pytest

from agentic_analytics.tracing import LangfuseTracer, TraceKey, build_trace
from tests.schemas import factories as f

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")


@pytest.mark.live
@LIVE
def test_live_langfuse_trace_round_trip():  # pragma: no cover - needs a real account
    pytest.importorskip("langfuse")
    import importlib

    importlib.import_module("agentic_analytics.env").load_dotenv()
    tracer = LangfuseTracer()
    key = TraceKey("live-smoke", "q-001", "claude_mcp", 1)
    url = tracer.send(build_trace(key, "Live smoke: median price?", f.answer(), public=True))
    tracer.flush()
    assert url and key.trace_id in url
    print(url)
