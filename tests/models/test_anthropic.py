import asyncio

import pytest
from evalkit.cost import call_cost

from agentic_analytics.cost import model_prices
from agentic_analytics.models import AnthropicAgent, ToolResult, ToolSpec
from tests.models.fakes import FakeAnthropic, Status, claude_message, no_sleep

PRICES = model_prices()
TOOLS = (
    ToolSpec("run_select_query", "Run one SELECT.", {"type": "object", "properties": {}}),
    ToolSpec("final_answer", "Finish.", {"type": "object", "properties": {}}, strict=True),
)


def agent(responses):
    fake = FakeAnthropic(responses)
    return AnthropicAgent("claude-sonnet-5-5", "medium", PRICES, client=fake, sleep=no_sleep), fake


def run(coro):
    return asyncio.run(coro)


def test_request_shape_has_effort_cache_and_no_sampling():
    a, fake = agent([claude_message([{"type": "text", "text": "hi"}], stop="end_turn")])
    conv = a.start("SYSTEM", "Q?", TOOLS)
    run(a.step(conv))
    params = fake.messages.calls[0]
    assert params["output_config"] == {"effort": "medium"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["system"] == "SYSTEM" and params["model"] == "claude-sonnet-5-5"
    assert not {"temperature", "top_p", "top_k"} & params.keys()
    assert params["tools"][0] == {
        "name": "run_select_query",
        "description": "Run one SELECT.",
        "input_schema": {"type": "object", "properties": {}},
    }
    assert params["tools"][1]["strict"] is True


def test_parse_tool_calls_usage_and_cost_exactly():
    msg = claude_message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Checking."},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "run_select_query",
                "input": {"query": "SELECT 1"},
            },
        ],
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 200,
        },
    )
    a, _ = agent([msg])
    turn = run(a.step(a.start("S", "Q", TOOLS)))
    assert turn.stop == "tool_use" and turn.text == "Checking."
    assert turn.tool_calls[0].name == "run_select_query"
    assert turn.tool_calls[0].arguments == {"query": "SELECT 1"}
    assert turn.usage.tokens_in == 1200 and turn.usage.tokens_cached == 900
    assert turn.usage.tokens_cache_write == 200
    want = call_cost(
        PRICES["claude-sonnet-5-5"],
        tokens_in=1200,
        tokens_out=50,
        tokens_cached=900,
        tokens_cache_write=200,
    )
    assert turn.cost_usd == want


def test_thinking_is_split_from_output_when_reported():
    msg = claude_message(
        [{"type": "text", "text": "x"}],
        stop="end_turn",
        usage={
            "input_tokens": 10,
            "output_tokens": 80,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens_details": {"thinking_tokens": 60},
        },
    )
    a, _ = agent([msg])
    turn = run(a.step(a.start("S", "Q", TOOLS)))
    assert (turn.usage.tokens_out, turn.usage.tokens_reasoning) == (20, 60)


def test_history_keeps_thinking_blocks_and_tool_results_round_trip():
    msg = claude_message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "tu_1", "name": "run_select_query", "input": {}},
        ]
    )
    a, fake = agent([msg, claude_message([{"type": "text", "text": "done"}], stop="end_turn")])
    conv = a.start("S", "Q", TOOLS)
    run(a.step(conv))
    a.add_tool_results(conv, [ToolResult("tu_1", "budget used", is_error=True)])
    a.add_user(conv, "Your query returned 0 rows; check it.")
    run(a.step(conv))
    sent = fake.messages.calls[1]["messages"]
    assert sent[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert sent[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": "budget used",
        "is_error": True,
    }
    assert sent[2]["content"][1] == {
        "type": "text",
        "text": "Your query returned 0 rows; check it.",
    }
    assert len(sent) == 3  # the follow-up joined the last user turn


@pytest.mark.parametrize(
    "stop, want", [("end_turn", "end"), ("max_tokens", "max_tokens"), ("refusal", "refusal")]
)
def test_stop_reasons(stop, want):
    a, _ = agent([claude_message([{"type": "text", "text": "x"}], stop=stop)])
    assert run(a.step(a.start("S", "Q", TOOLS))).stop == want


def test_refusal_category_is_kept():
    msg = claude_message(
        [{"type": "text", "text": ""}],
        stop="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
    )
    a, _ = agent([msg])
    turn = run(a.step(a.start("S", "Q", TOOLS)))
    assert turn.stop == "refusal" and turn.refusal_category == "cyber"


def test_transient_errors_retry_and_bad_requests_do_not():
    ok = claude_message([{"type": "text", "text": "x"}], stop="end_turn")
    a, fake = agent([Status(429), Status(503), ok])
    assert run(a.step(a.start("S", "Q", TOOLS))).text == "x"
    assert len(fake.messages.calls) == 3
    b, fake_b = agent([Status(400), ok])
    with pytest.raises(Status):
        run(b.step(b.start("S", "Q", TOOLS)))
    assert len(fake_b.messages.calls) == 1


def test_unknown_model_fails_at_construction():
    from evalkit.cost import UnknownPrice

    with pytest.raises(UnknownPrice):
        AnthropicAgent("claude-unknown-9", "medium", PRICES, client=FakeAnthropic([]))
