"""N1.6 verifier round 1: one test per surviving mutant or finding."""

import asyncio
import math
from types import SimpleNamespace
from typing import ClassVar

import pytest
from evalkit.cost import call_cost

from agentic_analytics.cost import (
    AgentAssumptions,
    CostCapReached,
    SpendCap,
    agent_question_cost,
    credit_rates,
    estimate_run,
    model_prices,
)
from agentic_analytics.models import AnthropicAgent, OpenAIAgent, ToolSpec
from agentic_analytics.models.errors import QuotaExhausted, classify_error
from tests.models.fakes import (
    FakeAnthropic,
    FakeOpenAI,
    Status,
    claude_message,
    gpt_completion,
    no_sleep,
)
from tests.schemas.factories import run_config

PRICES = model_prices()
TOOLS = (ToolSpec("t", "d", {"type": "object"}),)


def run(c):
    return asyncio.run(c)


def test_gpt_cost_counts_cached_and_reasoning_tokens_exactly():
    a = OpenAIAgent(
        "gpt-6.1-sol",
        "medium",
        PRICES,
        client=FakeOpenAI([gpt_completion(content="x", finish="stop")]),
        sleep=no_sleep,
    )
    t = run(a.step(a.start("S", "Q", TOOLS)))
    want = call_cost(
        PRICES["gpt-6.1-sol"],
        tokens_in=1000,
        tokens_out=100,
        tokens_reasoning=200,
        tokens_cached=600,
    )
    assert t.cost_usd == want
    assert want != call_cost(PRICES["gpt-6.1-sol"], tokens_in=1000, tokens_out=100)


def test_claude_cost_counts_thinking_tokens():
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
    a = AnthropicAgent("claude-sonnet-5-5", "medium", PRICES, client=FakeAnthropic([msg]))
    t = run(a.step(a.start("S", "Q", TOOLS)))
    assert t.cost_usd == call_cost(
        PRICES["claude-sonnet-5-5"], tokens_in=10, tokens_out=20, tokens_reasoning=60
    )


def test_estimate_absolute_value_matches_the_spec_formula():
    a = AgentAssumptions()
    p = PRICES["claude-sonnet-5-5"]
    per_turn = (1200 * 2.00 + 1800 * 0.10 + (250 + 150) * 10.00) / 1e6
    assert agent_question_cost(p, a) == pytest.approx(per_turn * 6)
    assert agent_question_cost(p, a) == pytest.approx(0.03948)


def test_estimate_includes_clickhouse_compute():
    e = estimate_run(run_config(trials=1), 100)
    ch = {ln.backend: ln for ln in e.lines}["clickhouse"]
    rate = credit_rates()["clickhouse_usd_per_hour"].value
    assert ch.asks == 200 and ch.usd == pytest.approx(200 * 4 * 2.0 / 3600 * rate)
    cortex_only = estimate_run(run_config(backends=["cortex_analyst"], models={}), 100)
    assert "clickhouse" not in {ln.backend for ln in cortex_only.lines}
    with pytest.raises(ValueError):
        estimate_run(run_config(), 1.5)  # type: ignore[arg-type]


def test_six_attempts_then_give_up():
    a = AnthropicAgent(
        "claude-sonnet-5-5",
        "medium",
        PRICES,
        client=FakeAnthropic([Status(429)] * 7),
        sleep=no_sleep,
    )
    with pytest.raises(Status):
        run(a.step(a.start("S", "Q", TOOLS)))
    assert len(a.client.messages.calls) == 6


def test_sdk_clients_are_built_with_retries_off(monkeypatch):
    import anthropic
    import openai

    seen = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: seen.setdefault("a", kw))
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: seen.setdefault("o", kw))
    AnthropicAgent("claude-sonnet-5-5", "low", PRICES).client  # noqa: B018
    OpenAIAgent("gpt-6.1-sol", "low", PRICES).client  # noqa: B018
    assert seen["a"]["max_retries"] == 0 and seen["o"]["max_retries"] == 0


def test_cap_allows_exactly_the_cap_and_refuses_beyond():
    async def go():
        cap = SpendCap(1.0)
        await cap.reserve(1.0)
        with pytest.raises(CostCapReached):
            await cap.reserve(1e-9)

    run(go())


def test_double_settle_and_bad_reservations_are_refused():
    async def go():
        cap = SpendCap(1.0)
        r1, r2 = await cap.reserve(0.4), await cap.reserve(0.4)
        await cap.settle(r1, 0.1)
        with pytest.raises(ValueError):
            await cap.settle(r1, 0.1)
        assert cap.in_flight == pytest.approx(0.4)  # r2 still held
        await cap.settle(r2, 0.0)
        with pytest.raises(ValueError):
            await cap.settle(r2, math.nan)

    run(go())


FULL = (
    "cortex_analyst_credits_per_message: {value: 0.067, verified: true, source: s1}\n"
    "snowflake_xs_credits_per_hour: {value: 1, verified: true, source: x}\n"
    "snowflake_usd_per_credit: {value: 3, verified: false, source: x}\n"
    "clickhouse_usd_per_hour: {value: 0.5, verified: false, source: x}\n"
)


def test_full_rate_file_loads(tmp_path):
    f = tmp_path / "credits.yaml"
    f.write_text(FULL, encoding="utf-8")
    assert len(credit_rates(f)) == 4


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b\n",
        'cortex_analyst_credits_per_message: {value: 0.067, verified: "false", source: x}\n'
        "snowflake_xs_credits_per_hour: {value: 1, verified: true, source: x}\n"
        "snowflake_usd_per_credit: {value: 3, verified: false, source: x}\n"
        "clickhouse_usd_per_hour: {value: 0.5, verified: false, source: x}\n",
        "cortex_analyst_credits_per_mesage: {value: 0.067, verified: true, source: x}\n",
        FULL + "extra_typo_rate: {value: 1, verified: true, source: x}\n",
        FULL.replace("source: s1", "source: ''"),
    ],
)
def test_rate_files_are_strict(tmp_path, text):
    f = tmp_path / "credits.yaml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        credit_rates(f)


@pytest.mark.parametrize("stop", ["model_context_window_exceeded", "pause_turn"])
def test_cut_off_turns_never_run_their_tool_calls(stop):
    msg = claude_message([{"type": "tool_use", "id": "t1", "name": "t", "input": {}}], stop=stop)
    a = AnthropicAgent("claude-sonnet-5-5", "medium", PRICES, client=FakeAnthropic([msg]))
    assert run(a.step(a.start("S", "Q", TOOLS))).stop == "max_tokens"


def test_unknown_stop_reason_is_treated_as_cut_off():
    a = AnthropicAgent("claude-sonnet-5-5", "medium", PRICES, client=FakeAnthropic([]))
    msg = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="t1", name="t", input={})],
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens_details=None,
        ),
        stop_reason="something_new",
        model="claude-sonnet-5-5",
        stop_details=None,
    )
    assert a.parse(msg, 0).stop == "max_tokens"


def test_deeply_nested_arguments_are_a_parse_error():
    call = {"id": "c", "type": "function", "function": {"name": "t", "arguments": "[" * 200000}}
    a = OpenAIAgent(
        "gpt-6.1-sol", "low", PRICES, client=FakeOpenAI([gpt_completion(tool_calls=[call])])
    )
    tc = run(a.step(a.start("S", "Q", TOOLS))).tool_calls[0]
    assert tc.arguments is None and tc.parse_error


def test_history_after_a_refusal_has_text_content():
    a = OpenAIAgent(
        "gpt-6.1-sol",
        "low",
        PRICES,
        client=FakeOpenAI([gpt_completion(refusal="No.", finish="stop")]),
    )
    conv = a.start("S", "Q", TOOLS)
    run(a.step(conv))
    assert conv.history[-1] == {"role": "assistant", "content": "No."}


def test_citation_fields_survive_history_replay():
    msg = claude_message(
        [
            {
                "type": "text",
                "text": "x",
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "c",
                        "document_index": 0,
                        "document_title": None,
                        "start_char_index": 0,
                        "end_char_index": 1,
                    }
                ],
            }
        ],
        stop="end_turn",
    )
    a = AnthropicAgent("claude-sonnet-5-5", "low", PRICES, client=FakeAnthropic([msg]))
    conv = a.start("S", "Q", TOOLS)
    run(a.step(conv))
    assert "document_title" in conv.history[-1]["content"][0]["citations"][0]


def test_insufficient_quota_is_config_and_not_retried():
    class Quota(Exception):
        status_code = 429
        body: ClassVar[dict] = {"error": {"code": "insufficient_quota"}}

    a = OpenAIAgent(
        "gpt-6.1-sol", "low", PRICES, client=FakeOpenAI([Quota(), Quota()]), sleep=no_sleep
    )
    with pytest.raises(QuotaExhausted) as exc:
        run(a.step(a.start("S", "Q", TOOLS)))
    assert classify_error(exc.value) == "config"
    assert len(a.client.rec.calls) == 1
    assert classify_error(Status(413)) == "config"


def test_odd_429_bodies_stay_retryable():
    class Proxy429(Exception):
        status_code = 429
        body: ClassVar[dict] = {"error": "Too many requests"}

    ok = claude_message([{"type": "text", "text": "x"}], stop="end_turn")
    a = AnthropicAgent(
        "claude-sonnet-5-5", "low", PRICES, client=FakeAnthropic([Proxy429(), ok]), sleep=no_sleep
    )
    assert run(a.step(a.start("S", "Q", TOOLS))).text == "x"
    assert classify_error(Proxy429()) == "transport"


def test_reservations_are_owned_and_immutable():
    from dataclasses import FrozenInstanceError, replace

    from agentic_analytics.cost import Reservation

    async def go():
        cap, other = SpendCap(1.0), SpendCap(1.0)
        r = await cap.reserve(0.5)
        with pytest.raises(FrozenInstanceError):
            r.amount = math.nan  # type: ignore[misc]
        with pytest.raises(ValueError):
            await cap.settle(Reservation(999, math.nan), 0.1)  # forged
        with pytest.raises(ValueError):
            await cap.settle(replace(r, amount=0.0), 0.1)  # altered
        with pytest.raises(ValueError):
            await other.settle(r, 0.1)  # another cap's
        await cap.settle(r, 0.1)
        assert cap.in_flight == 0.0 and cap.spent == pytest.approx(0.1)

    run(go())


def test_model_usd_excludes_compute():
    e = estimate_run(run_config(trials=1), 10)
    assert e.compute_usd > 0
    assert e.model_usd == pytest.approx(sum(ln.usd for ln in e.lines if ln.backend != "clickhouse"))
    assert e.total_usd == pytest.approx(e.model_usd + e.compute_usd + e.credits * e.usd_per_credit)
