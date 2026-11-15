"""Live smoke: three questions on both models through the real ClickHouse MCP server (AGT-06).

Needs `.env` with the model keys and `CLICKHOUSE_MCP_*`; marked `live`, skipped unless
`AA_LIVE=1`, and excluded from CI (`pytest -m "not live"`). Costs a few cents per run.
"""

import asyncio
import os
from datetime import date

import pytest

from agentic_analytics.backends import ask_mcp
from agentic_analytics.cost import model_prices
from agentic_analytics.mcp import McpConnection, McpSettings, Toolbox
from agentic_analytics.models import make_agent

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")

MODELS = ("claude-sonnet-5-5", "gpt-6.1-sol")
QUESTIONS = (
    ("How many sales are recorded in total?", "answer"),
    ("What was the median price of a detached house in Manchester in 2024?", "answer"),
    ("Average price in Leeds?", "either"),  # ambiguous: asking back is the expected answer
)


@pytest.mark.live
@LIVE
@pytest.mark.parametrize("model_id", MODELS)
def test_live_three_questions(model_id):  # pragma: no cover - needs real accounts
    import importlib

    importlib.import_module("agentic_analytics.env").load_dotenv()  # N1.2, live only
    settings = McpSettings.from_env()
    as_of = date.fromisoformat(os.environ.get("AA_AS_OF", "2026-09-30"))
    agent = make_agent(model_id, "medium", model_prices())

    async def go():
        runs = []
        async with McpConnection(settings) as conn:
            for text, _ in QUESTIONS:
                box = Toolbox(conn, settings.service_id, ("uk",))
                runs.append(await ask_mcp(text, model=agent, toolbox=box, as_of=as_of))
        return runs

    runs = asyncio.run(go())
    for (text, expect), run in zip(QUESTIONS, runs, strict=True):
        a = run.answer
        print(f"{model_id} | {text} | {a.error} | {a.sql} | {a.answer or a.clarification}")
        assert a.error is None, f"{text}: {a.error}"
        assert sum(s.tool != "final_answer" for s in a.steps) <= 6
        if expect == "answer":
            assert a.sql is not None
        assert run.cost_usd > 0
