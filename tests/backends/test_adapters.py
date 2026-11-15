"""The loop over the real adapters with real SDK response objects (AGT-01, EK L-17)."""

import asyncio
import json

from agentic_analytics.backends import ask_mcp, render_system_prompt
from agentic_analytics.cost import model_prices
from agentic_analytics.models import AnthropicAgent, OpenAIAgent
from tests.backends.fakes import AS_OF, toolbox
from tests.models.fakes import FakeAnthropic, FakeOpenAI, claude_message, gpt_completion, no_sleep

SQL = "SELECT count() AS n FROM uk.uk_price_paid LIMIT 2"
FINAL = {"sql": SQL, "answer": "There were 2 rows.", "clarification": None, "suggestions": []}


def run_agent(agent, **kw):
    async def go():
        async with toolbox(readonly=False) as box:
            return await ask_mcp("How many?", model=agent, toolbox=box, as_of=AS_OF, **kw)

    return asyncio.run(go())


def claude_tool(id_, name, args):
    return claude_message([{"type": "tool_use", "id": id_, "name": name, "input": args}])


def gpt_tool(id_, name, args):
    fn = {"name": name, "arguments": json.dumps(args)}
    return gpt_completion(tool_calls=[{"id": id_, "type": "function", "function": fn}])


def test_claude_adapter_end_to_end():
    fake = FakeAnthropic(
        [
            claude_tool("tu_1", "run_select_query", {"query": SQL}),
            claude_tool("tu_2", "final_answer", FINAL),
        ]
    )
    agent = AnthropicAgent("claude-sonnet-5-5", "medium", model_prices(), client=fake)
    run = run_agent(agent)
    a = run.answer
    assert (a.backend, a.sql, a.error) == ("claude_mcp", SQL, None)
    assert a.model_snapshot == "claude-sonnet-5-5"
    first, second = fake.messages.calls
    assert first["system"] == render_system_prompt(AS_OF)
    tools = {t["name"]: t for t in first["tools"]}
    assert tools["final_answer"]["strict"] is True
    assert list(tools) == ["list_databases", "list_tables", "run_select_query", "final_answer"]
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
    assert json.loads(result["content"])["rows_total"] == 2
    assert run.cost_usd > 0 and a.steps[0].rows_returned == 2


def test_gpt_adapter_end_to_end():
    fake = FakeOpenAI(
        [
            gpt_tool("call_1", "run_select_query", {"query": SQL}),
            gpt_tool("call_2", "final_answer", FINAL),
        ]
    )
    agent = OpenAIAgent("gpt-6.1-sol", "medium", model_prices(), client=fake)
    run = run_agent(agent)
    a = run.answer
    assert (a.backend, a.sql, a.error) == ("gpt_mcp", SQL, None)
    first, second = fake.rec.calls
    assert first["messages"][0] == {"role": "system", "content": render_system_prompt(AS_OF)}
    fns = {t["function"]["name"]: t["function"] for t in first["tools"]}
    assert fns["final_answer"]["strict"] is True
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"


def test_claude_self_check_feedback_joins_the_tool_result_turn():
    async def check(sql):
        return "Your query returned 2 rows; check it."

    fake = FakeAnthropic(
        [
            claude_tool("tu_1", "final_answer", FINAL),
            claude_tool("tu_2", "final_answer", FINAL),
        ]
    )
    agent = AnthropicAgent("claude-sonnet-5-5", "medium", model_prices(), client=fake)
    run = run_agent(agent, self_check=check)
    assert run.self_check == "sent" and run.answer.error is None
    last = fake.messages.calls[1]["messages"][-1]
    assert last["role"] == "user"
    assert [b["type"] for b in last["content"]] == ["tool_result", "text"]


def test_gpt_refusal_maps_to_refusal():
    fake = FakeOpenAI([gpt_completion(refusal="I can't help with that.", finish="stop")])
    agent = OpenAIAgent("gpt-6.1-sol", "medium", model_prices(), client=fake, sleep=no_sleep)
    run = run_agent(agent)
    assert run.answer.error == "refusal" and run.answer.answer == "I can't help with that."
