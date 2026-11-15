"""Budgets and error mapping: calls, turns, wall clock, refusal, cut-off, transport, config
(AGT-04)."""

import asyncio

import pytest

from agentic_analytics.backends import ask_mcp
from agentic_analytics.models import AnthropicAgent
from agentic_analytics.models.errors import QuotaExhausted
from agentic_analytics.steps import STEP_ERROR_BUDGET, step_error_code
from tests.backends.fakes import (
    AS_OF,
    CALLS,
    ManualClock,
    ScriptedModel,
    ask,
    call,
    final,
    hang,
    query,
    toolbox,
    turn,
)
from tests.models.fakes import FakeAnthropic, Status, claude_message, no_sleep

SQL = "SELECT count() AS n FROM uk.uk_price_paid"


# --- six MCP calls -------------------------------------------------------------------------


@pytest.mark.safety
def test_seventh_call_is_refused_and_the_model_may_still_finish():
    script = [turn(query(f"SELECT {i}")) for i in range(7)] + [turn(final(SQL, "42."))]
    run, box = ask(ScriptedModel(script))
    a = run.answer
    assert a.error is None and a.sql == SQL
    assert len(CALLS) == 6 and box.calls_made == 7
    assert step_error_code(a.steps[6]) == STEP_ERROR_BUDGET
    assert a.steps[6].rows_returned is None


@pytest.mark.safety
def test_calling_mcp_again_after_the_budget_refusal_ends_with_step_budget():
    script = [turn(*[query(f"SELECT {i}") for i in range(7)]), turn(query("SELECT 8"))]
    script.append(turn(final(SQL, "never read")))
    model = ScriptedModel(script)
    run, _ = ask(model)
    a = run.answer
    assert a.error == "step_budget" and a.sql is None
    assert len(CALLS) == 6 and len(a.steps) == 8 and model.steps == 2


@pytest.mark.safety
def test_final_answer_in_the_same_turn_as_a_second_overrun_is_still_taken():
    script = [turn(*[query(f"SELECT {i}") for i in range(7)])]
    script.append(turn(query("SELECT 8"), final(SQL, "42.")))
    run, _ = ask(ScriptedModel(script))
    assert run.answer.error is None and len(CALLS) == 6


# --- ten turns -----------------------------------------------------------------------------


def test_turn_cap_ends_with_step_budget():
    model = ScriptedModel([turn(call("list_databases")) for _ in range(4)])
    run, _ = ask(model, max_turns=3)
    assert run.answer.error == "step_budget" and model.steps == 3 and run.turns == 3
    assert len(run.answer.steps) == 3  # the steps taken so far are kept


def test_final_answer_on_the_last_allowed_turn_is_accepted():
    model = ScriptedModel([turn(call("list_databases")), turn(final(SQL, "42."))])
    run, _ = ask(model, max_turns=2)
    assert run.answer.error is None


def test_default_turn_cap_is_ten():
    model = ScriptedModel([turn(call("list_databases")) for _ in range(11)])
    run, _ = ask(model, box_kw={"max_calls": 100})
    assert run.answer.error == "step_budget" and model.steps == 10 and run.turns == 10


# --- 120 seconds ---------------------------------------------------------------------------


def test_wall_clock_past_the_limit_ends_with_step_budget():
    clock = ManualClock()
    model = ScriptedModel(
        [
            clock.after(60, turn(query("SELECT 1"))),
            clock.after(61, turn(final(SQL, "42."))),  # arrives at 121 s
        ]
    )
    run, _ = ask(model, clock=clock)
    assert run.answer.error == "step_budget" and len(run.answer.steps) == 1


def test_wall_clock_exactly_at_the_limit_is_allowed():
    clock = ManualClock()
    model = ScriptedModel(
        [clock.after(60, turn(query("SELECT 1"))), clock.after(60, turn(final(SQL, "42.")))]
    )
    run, _ = ask(model, clock=clock)
    assert run.answer.error is None


def test_wall_clock_is_checked_before_asking_the_model_again():
    clock = ManualClock()
    model = ScriptedModel([clock.after(130, turn(query("SELECT 1"))), turn(final(SQL, "42."))])
    run, _ = ask(model, clock=clock, wall_s=120)
    assert run.answer.error == "step_budget" and model.steps == 1


def test_hung_model_turn_is_cut_by_the_wall_clock():
    model = ScriptedModel([turn(query("SELECT 1")), hang])
    run, _ = ask(model, wall_s=0.3)
    assert run.answer.error == "step_budget" and len(run.answer.steps) == 1


def test_hung_tool_call_is_cut_and_recorded():
    async def slow(name, arguments):
        await asyncio.sleep(30)

    def patch(box):
        box.call = slow  # type: ignore[method-assign]

    model = ScriptedModel([turn(query("SELECT 1")), turn(final(SQL, "42."))])
    run, _ = ask(model, wall_s=0.3, patch_box=patch)
    a = run.answer
    assert a.error == "step_budget" and len(a.steps) == 1
    assert step_error_code(a.steps[0]) == STEP_ERROR_BUDGET


def test_timeout_raised_inside_the_model_is_transport_not_step_budget():
    model = ScriptedModel([TimeoutError("read timed out")])
    run, _ = ask(model)
    assert run.answer.error == "transport"


# --- stops ---------------------------------------------------------------------------------


@pytest.mark.safety
def test_refusal_stop_is_refusal_and_its_tool_calls_never_run():
    model = ScriptedModel(
        [turn(query("DELETE FROM uk.uk_price_paid"), stop="refusal", text="I can't do that.")]
    )
    run, _ = ask(model, "Delete every sale in Leeds.")
    a = run.answer
    assert a.error == "refusal" and a.answer == "I can't do that." and a.sql is None
    assert CALLS == [] and a.steps == ()


def test_max_tokens_stop_is_step_budget_and_its_tool_calls_never_run():
    model = ScriptedModel([turn(query("SELECT 1"), stop="max_tokens")])
    run, _ = ask(model)
    assert run.answer.error == "step_budget" and CALLS == [] and run.answer.steps == ()
    assert run.answer.model_snapshot  # still recorded from the turn


# --- provider errors -----------------------------------------------------------------------


@pytest.mark.parametrize("exc", [Status(503), Status(429), ConnectionError("reset")])
def test_transient_provider_failure_after_retries_is_transport(exc):
    model = ScriptedModel([turn(call("list_databases")), exc])
    run, _ = ask(model)
    a = run.answer
    assert a.error == "transport" and a.sql is None
    assert len(a.steps) == 1  # what happened before the failure is kept
    assert a.model_snapshot


def test_transport_on_the_first_turn_still_records_a_snapshot():
    model = ScriptedModel([Status(500)])
    run, _ = ask(model)
    assert run.answer.error == "transport" and run.answer.model_snapshot == "claude-sonnet-5-5"
    assert run.turns == 0 and run.cost_usd == 0.0


@pytest.mark.parametrize(
    "exc", [Status(401), Status(400), Status(404), QuotaExhausted("no credit"), TypeError("bug")]
)
def test_config_errors_and_bugs_raise_and_stop_the_run(exc):
    model = ScriptedModel([exc])
    with pytest.raises(type(exc)):
        ask(model)


def test_real_adapter_retries_six_times_then_reports_transport():
    fake = FakeAnthropic([Status(529)] * 6)
    agent = AnthropicAgent("claude-sonnet-5-5", "medium", _prices(), client=fake, sleep=no_sleep)

    async def go():
        async with toolbox() as box:
            return await ask_mcp("Q?", model=agent, toolbox=box, as_of=AS_OF)

    run = asyncio.run(go())
    assert run.answer.error == "transport"
    assert len(fake.messages.calls) == 6


def test_real_adapter_config_error_raises_after_one_attempt():
    fake = FakeAnthropic([Status(401), claude_message([])])
    agent = AnthropicAgent("claude-sonnet-5-5", "medium", _prices(), client=fake, sleep=no_sleep)

    async def go():
        async with toolbox() as box:
            return await ask_mcp("Q?", model=agent, toolbox=box, as_of=AS_OF)

    with pytest.raises(Status):
        asyncio.run(go())
    assert len(fake.messages.calls) == 1


def _prices():
    from agentic_analytics.cost import model_prices

    return model_prices()
