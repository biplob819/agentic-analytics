"""MOD-02: GPT through the Responses API (D-54), with real SDK `Response` objects (EK L-17)."""

import asyncio
import os

import openai
import pytest
from evalkit.cost import call_cost
from httpx2 import Request, Response

from agentic_analytics.cost import model_prices
from agentic_analytics.models import OpenAIAgent, ToolResult, ToolSpec, make_agent
from agentic_analytics.models.errors import QuotaExhausted, classify_error
from agentic_analytics.models.openai import ResponseFailed
from tests.models.fakes import (
    ENCRYPTED,
    FakeOpenAI,
    Status,
    gpt_call,
    gpt_response,
    no_sleep,
)

PRICES = model_prices()
TOOLS = (
    ToolSpec(
        "run_select_query",
        "Run one SELECT.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        strict=True,
    ),  # strict schemas need every property required and no extras
    ToolSpec("list_tables", "List tables.", {"type": "object"}),
)
LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")


def agent(responses, **kw):
    fake = FakeOpenAI(responses)
    kw.setdefault("sleep", no_sleep)
    return OpenAIAgent("gpt-6.1-sol", "medium", PRICES, client=fake, **kw), fake


def run(coro):
    return asyncio.run(coro)


def test_request_shape():
    a, fake = agent([gpt_response(text="hi")])
    run(a.step(a.start("SYS", "Q?", TOOLS)))
    p = fake.rec.calls[0]
    assert p["model"] == "gpt-6.1-sol"
    assert p["instructions"] == "SYS"
    assert p["input"] == [{"role": "user", "content": "Q?"}]
    assert p["reasoning"] == {"effort": "medium"} and p["max_output_tokens"] == 16000
    assert p["store"] is False and p["include"] == ["reasoning.encrypted_content"]
    assert p["tools"] == [
        {
            "type": "function",
            "name": "run_select_query",
            "description": "Run one SELECT.",
            "parameters": TOOLS[0].parameters,
            "strict": True,
        },
        {
            "type": "function",
            "name": "list_tables",
            "description": "List tables.",
            "parameters": {"type": "object"},
            "strict": False,  # explicit: the Responses API turns strict on when it is omitted
        },
    ]
    banned = {"temperature", "top_p", "top_k", "previous_response_id", "messages"}
    assert not banned & p.keys()
    assert not {"reasoning_effort", "max_completion_tokens"} & p.keys()


def test_effort_and_max_tokens_are_passed_through():
    a, fake = agent([gpt_response(text="x")], max_tokens=1234)
    a.effort = "low"
    run(a.step(a.start("S", "Q", ())))
    p = fake.rec.calls[0]
    assert p["reasoning"] == {"effort": "low"} and p["max_output_tokens"] == 1234
    assert "tools" not in p  # never `tools: []` (07f69ac)


def test_tool_calls_usage_and_stateless_history():
    first = gpt_response(tool_calls=[gpt_call("c1", "run_select_query", '{"query": "SELECT 1"}')])
    a, fake = agent([first, gpt_response(text="done")])
    conv = a.start("S", "Q", TOOLS)
    turn = run(a.step(conv))
    assert turn.stop == "tool_use"
    tc = turn.tool_calls[0]
    assert (tc.id, tc.name, tc.arguments, tc.parse_error) == (
        "c1",
        "run_select_query",
        {"query": "SELECT 1"},
        None,
    )
    a.add_tool_results(conv, [ToolResult("c1", "3 rows"), ToolResult("c2", "bad", is_error=True)])
    a.add_user(conv, "Check it.")
    second = run(a.step(conv))
    assert (second.stop, second.text) == ("end", "done")
    sent = fake.rec.calls[1]["input"]
    assert sent[0] == {"role": "user", "content": "Q"}
    # the prior output items go back verbatim, the encrypted reasoning item included
    assert sent[1] == {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": ENCRYPTED,
    }
    assert sent[2] == gpt_call("c1", "run_select_query", '{"query": "SELECT 1"}')
    assert sent[3] == {"type": "function_call_output", "call_id": "c1", "output": "3 rows"}
    assert sent[4] == {"type": "function_call_output", "call_id": "c2", "output": "ERROR: bad"}
    assert sent[5] == {"role": "user", "content": "Check it."}
    assert len(sent) == 6
    assert "previous_response_id" not in fake.rec.calls[1]
    assert conv.history[-2]["type"] == "reasoning" and conv.history[-1]["type"] == "message"


def test_usage_reports_visible_output_without_reasoning():
    a, _ = agent([gpt_response(text="x")])
    u = run(a.step(a.start("S", "Q", TOOLS))).usage
    assert (u.tokens_in, u.tokens_out, u.tokens_reasoning, u.tokens_cached) == (1000, 100, 200, 600)
    assert u.tokens_cache_write == 0


def test_cost_is_exact_and_bills_reasoning_as_output():
    a, _ = agent([gpt_response(text="x")])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    # 400 uncached in at $2, 600 cached at $0.10, (100 visible + 200 reasoning) out at $10
    assert t.cost_usd == pytest.approx((400 * 2.00 + 600 * 0.10 + 300 * 10.00) / 1e6, abs=1e-15)
    assert t.cost_usd == call_cost(
        PRICES["gpt-6.1-sol"],
        tokens_in=1000,
        tokens_out=100,
        tokens_reasoning=200,
        tokens_cached=600,
    )


def test_cache_writes_are_reported_inside_the_prompt():
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 300},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 1010,
    }
    a, _ = agent([gpt_response(text="x", usage=usage)])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert (t.usage.tokens_cached, t.usage.tokens_cache_write) == (600, 300)
    assert t.cost_usd == call_cost(
        PRICES["gpt-6.1-sol"],
        tokens_in=1000,
        tokens_out=10,
        tokens_cached=600,
        tokens_cache_write=300,
    )


def test_snapshot_and_request_id():
    resp = gpt_response(text="x")
    resp._request_id = "req_abc"  # type: ignore[attr-defined]  # set by the SDK on real calls
    a, _ = agent([resp])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert (t.model_snapshot, t.request_id) == ("gpt-6.1-sol-2026-10-01", "req_abc")
    b, _ = agent([gpt_response(text="x")])
    assert run(b.step(b.start("S", "Q", TOOLS))).request_id == "resp_1"


def test_text_is_the_concatenated_output_text_parts():
    resp = gpt_response(text="a")
    resp.output[1].content.append(resp.output[1].content[0].model_copy(update={"text": "b"}))  # type: ignore[union-attr]
    a, _ = agent([resp])
    assert run(a.step(a.start("S", "Q", TOOLS))).text == "ab"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "[" * 200000])
def test_malformed_arguments_become_parse_errors(raw):
    a, _ = agent([gpt_response(tool_calls=[gpt_call("c1", "run_select_query", raw)])])
    tc = run(a.step(a.start("S", "Q", TOOLS))).tool_calls[0]
    assert tc.arguments is None and tc.parse_error


def test_empty_arguments_are_an_empty_object():
    a, _ = agent([gpt_response(tool_calls=[gpt_call("c1", "list_tables", "")])])
    tc = run(a.step(a.start("S", "Q", TOOLS))).tool_calls[0]
    assert tc.arguments == {} and tc.parse_error is None


def test_refusal_part_maps_to_refusal():
    a, _ = agent([gpt_response(refusal="I can't help with that.")])
    conv = a.start("S", "Q", TOOLS)
    t = run(a.step(conv))
    assert (t.stop, t.text, t.refusal_category) == ("refusal", "I can't help with that.", "refusal")
    assert conv.history[-1]["content"] == [
        {"type": "refusal", "refusal": "I can't help with that."}
    ]


def test_content_filter_maps_to_refusal():
    a, _ = agent([gpt_response(status="incomplete", incomplete="content_filter")])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert (t.stop, t.refusal_category) == ("refusal", "content_filter")


@pytest.mark.parametrize(
    ("status", "reason"),
    [("incomplete", "max_output_tokens"), ("incomplete", "max_messages"), ("cancelled", None)],
)
def test_incomplete_turns_are_cut_off_and_never_run_tools(status, reason):
    calls = [gpt_call("c1", "list_tables", "{}")]
    a, _ = agent([gpt_response(tool_calls=calls, status=status, incomplete=reason)])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert t.stop == "max_tokens" and t.refusal_category is None


def test_failed_status_is_retried_then_transport_and_never_enters_history():
    a, fake = agent([gpt_response(text="x", status="failed"), gpt_response(text="ok")])
    conv = a.start("S", "Q", TOOLS)
    t = run(a.step(conv))
    assert t.text == "ok" and t.stop == "end" and len(fake.rec.calls) == 2
    assert sum(1 for i in conv.history if isinstance(i, dict) and i.get("type") == "message") == 1
    b, _ = agent([gpt_response(text="x", status="failed")] * 10)
    with pytest.raises(ResponseFailed) as e:
        run(b.step(b.start("S", "Q", TOOLS)))
    assert classify_error(e.value) == "transport"


def test_plain_answer_is_end():
    a, _ = agent([gpt_response(text="42", reasoning=False)])
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert (t.stop, t.text, t.tool_calls) == ("end", "42", ())


def test_retry_policy():
    a, fake = agent([Status(500), gpt_response(text="x")])
    assert run(a.step(a.start("S", "Q", TOOLS))).text == "x"
    assert len(fake.rec.calls) == 2
    b, fake_b = agent([Status(401)])
    with pytest.raises(Status):
        run(b.step(b.start("S", "Q", TOOLS)))
    assert len(fake_b.rec.calls) == 1


def _api_error(cls, status, body):
    req = Request("POST", "https://api.openai.com/v1/responses")
    return cls("err", response=Response(status, request=req), body=body)


def test_real_responses_api_errors_are_classified():
    quota = _api_error(openai.RateLimitError, 429, {"code": "insufficient_quota", "message": "m"})
    a, fake = agent([quota, gpt_response(text="x")])
    with pytest.raises(QuotaExhausted) as exc:
        run(a.step(a.start("S", "Q", TOOLS)))
    assert classify_error(exc.value) == "config" and len(fake.rec.calls) == 1
    busy = _api_error(openai.RateLimitError, 429, {"code": "rate_limit_exceeded", "message": "m"})
    b, fake_b = agent([busy, gpt_response(text="x")])
    assert run(b.step(b.start("S", "Q", TOOLS))).text == "x" and len(fake_b.rec.calls) == 2
    assert classify_error(busy) == "transport"
    bad = _api_error(openai.BadRequestError, 400, {"code": None, "message": "tools"})
    c, fake_c = agent([bad])
    with pytest.raises(openai.BadRequestError):
        run(c.step(c.start("S", "Q", TOOLS)))
    assert classify_error(bad) == "config" and len(fake_c.rec.calls) == 1
    assert classify_error(_api_error(openai.AuthenticationError, 401, None)) == "config"
    assert classify_error(_api_error(openai.InternalServerError, 500, None)) == "transport"


def test_make_agent_routes_by_model():
    assert make_agent("gpt-6.1-sol", "low", PRICES, client=FakeOpenAI([])).provider == "openai"
    from tests.models.fakes import FakeAnthropic

    claude = make_agent("claude-sonnet-5-5", "low", PRICES, client=FakeAnthropic([]))
    assert claude.provider == "anthropic"
    with pytest.raises(ValueError):
        make_agent("llama-4", "low", PRICES)


@pytest.mark.live
@LIVE
def test_live_gpt_sol_tool_call_round_trip():  # pragma: no cover - needs OPENAI_API_KEY
    """One function call at effort low, then its result goes back statelessly (D-54)."""
    from agentic_analytics.env import load_dotenv

    load_dotenv()
    tool = ToolSpec(
        "add",
        "Add two integers and return the sum.",
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        strict=True,
    )
    a = OpenAIAgent("gpt-6.1-sol", "low", PRICES, max_tokens=2000)
    conv = a.start("Use the add tool for arithmetic.", "What is 17 + 25? Use the tool.", (tool,))

    async def go():  # one event loop: the SDK client's connections belong to it
        first = await a.step(conv)
        assert first.stop == "tool_use" and first.tool_calls[0].name == "add"
        assert first.tool_calls[0].arguments == {"a": 17, "b": 25}
        assert first.model_snapshot.startswith("gpt-6.1-sol") and first.cost_usd > 0
        a.add_tool_results(conv, [ToolResult(first.tool_calls[0].id, "42")])
        return first, await a.step(conv)

    first, second = run(go())
    kinds = [i.get("type") or i.get("role") for i in conv.history]
    assert "function_call" in kinds and "function_call_output" in kinds, kinds
    assert second.stop == "end" and "42" in second.text
    total = first.cost_usd + second.cost_usd
    print(
        f"\nlive gpt-6.1-sol: in={first.usage.tokens_in}+{second.usage.tokens_in} "
        f"out={first.usage.tokens_out}+{second.usage.tokens_out} "
        f"reasoning={first.usage.tokens_reasoning}+{second.usage.tokens_reasoning} "
        f"cost=${total:.6f} history={kinds}"
    )
    assert total < 0.05
