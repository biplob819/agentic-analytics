import asyncio

import pytest

from agentic_analytics.cost import model_prices
from agentic_analytics.models import OpenAIAgent, ToolResult, ToolSpec, make_agent
from tests.models.fakes import FakeOpenAI, Status, gpt_completion, no_sleep

PRICES = model_prices()
TOOLS = (ToolSpec("run_select_query", "Run one SELECT.", {"type": "object"}, strict=True),)


def agent(responses):
    fake = FakeOpenAI(responses)
    return OpenAIAgent("gpt-6.1-sol", "medium", PRICES, client=fake, sleep=no_sleep), fake


def run(coro):
    return asyncio.run(coro)


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def test_request_shape():
    a, fake = agent([gpt_completion(content="hi", finish="stop")])
    run(a.step(a.start("SYS", "Q?", TOOLS)))
    p = fake.rec.calls[0]
    assert p["reasoning_effort"] == "medium" and p["max_completion_tokens"] == 16000
    assert p["messages"][:2] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Q?"},
    ]
    assert p["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "run_select_query",
                "description": "Run one SELECT.",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }
    ]
    assert not {"temperature", "top_p", "top_k"} & p.keys()


def test_tool_calls_usage_and_history():
    resp = gpt_completion(tool_calls=[call("c1", "run_select_query", '{"query": "SELECT 1"}')])
    a, fake = agent([resp, gpt_completion(content="done", finish="stop")])
    conv = a.start("S", "Q", TOOLS)
    turn = run(a.step(conv))
    assert turn.stop == "tool_use"
    assert turn.tool_calls[0].arguments == {"query": "SELECT 1"}
    u = turn.usage
    assert (u.tokens_in, u.tokens_out, u.tokens_reasoning, u.tokens_cached) == (1000, 100, 200, 600)
    a.add_tool_results(conv, [ToolResult("c1", "3 rows"), ToolResult("c2", "bad", is_error=True)])
    run(a.step(conv))
    sent = fake.rec.calls[1]["messages"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"query": "SELECT 1"}'
    assert sent[3] == {"role": "tool", "tool_call_id": "c1", "content": "3 rows"}
    assert sent[4]["content"] == "ERROR: bad"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_malformed_arguments_become_parse_errors(raw):
    a, _ = agent([gpt_completion(tool_calls=[call("c1", "run_select_query", raw)])])
    tc = run(a.step(a.start("S", "Q", TOOLS))).tool_calls[0]
    assert tc.arguments is None and tc.parse_error


def test_refusal_and_length():
    a, _ = agent([gpt_completion(refusal="I can't help with that.", finish="stop")])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert t.stop == "refusal" and "can't" in t.text
    b, _ = agent([gpt_completion(content="x", finish="length")])
    assert run(b.step(b.start("S", "Q", TOOLS))).stop == "max_tokens"


def test_retry_policy():
    ok = gpt_completion(content="x", finish="stop")
    a, fake = agent([Status(500), ok])
    assert run(a.step(a.start("S", "Q", TOOLS))).text == "x"
    assert len(fake.rec.calls) == 2
    b, fake_b = agent([Status(401)])
    with pytest.raises(Status):
        run(b.step(b.start("S", "Q", TOOLS)))
    assert len(fake_b.rec.calls) == 1


def test_make_agent_routes_by_model():
    assert make_agent("gpt-6.1-sol", "low", PRICES, client=FakeOpenAI([])).provider == "openai"
    from tests.models.fakes import FakeAnthropic

    claude = make_agent("claude-sonnet-5-5", "low", PRICES, client=FakeAnthropic([]))
    assert claude.provider == "anthropic"
    with pytest.raises(ValueError):
        make_agent("llama-4", "low", PRICES)
