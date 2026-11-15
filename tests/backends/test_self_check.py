"""The self-check hook F3.1 plugs in: once, only while calls remain (AGT-05, Tech Spec §7)."""

import asyncio
import time

import pytest

from agentic_analytics.backends.mcp_agent import FEEDBACK_MIN_S
from tests.backends.fakes import ManualClock, ScriptedModel, ask, final, query, turn

SQL1 = "SELECT avg(price) FROM uk.uk_price_paid WHERE town = 'leeds'"
SQL2 = "SELECT avg(price) FROM uk.uk_price_paid WHERE town = 'LEEDS'"
FEEDBACK = "Your query returned 0 rows; check it."


class Checker:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    async def __call__(self, sql):
        self.seen.append(sql)
        return self.replies.pop(0) if self.replies else FEEDBACK


def test_feedback_is_sent_once_and_the_next_final_answer_is_final():
    check = Checker(FEEDBACK, FEEDBACK)
    model = ScriptedModel(
        [
            turn(final(SQL1, "No sales.")),
            turn(query(SQL2)),
            turn(final(SQL2, "The average was 250000.")),
        ]
    )
    run, _ = ask(model, self_check=check)
    a = run.answer
    assert a.error is None and a.sql == SQL2 and a.answer == "The average was 250000."
    assert check.seen == [SQL1]  # never asked twice
    assert model.user_texts == [FEEDBACK]
    assert run.self_check == "sent"
    assert [s.tool for s in a.steps] == ["final_answer", "run_select_query", "final_answer"]
    assert a.steps[0].error is None


def test_feedback_follows_the_tool_result_for_the_final_answer():
    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(final(SQL2, "250000."))])
    ask(model, self_check=Checker(FEEDBACK))
    assert model.events == ["step", "results", "user", "step"]
    (ack,) = model.results[0]
    assert not ack.is_error


def test_passing_self_check_ends_at_once():
    check = Checker(None)
    model = ScriptedModel([turn(final(SQL1, "No sales."))])
    run, _ = ask(model, self_check=check)
    assert run.answer.sql == SQL1 and run.self_check == "passed" and model.steps == 1


def test_self_check_is_skipped_when_no_calls_remain():
    check = Checker(FEEDBACK)
    model = ScriptedModel(
        [turn(*[query(f"SELECT {i}") for i in range(6)]), turn(final(SQL1, "No sales."))]
    )
    run, box = ask(model, self_check=check)
    assert run.answer.sql == SQL1 and run.self_check == "skipped"
    assert box.calls_made == 6 and model.user_texts == []
    assert check.seen == [SQL1]


def test_self_check_still_runs_with_one_call_left():
    model = ScriptedModel(
        [
            turn(*[query(f"SELECT {i}") for i in range(5)]),
            turn(final(SQL1, "No sales.")),
            turn(final(SQL2, "250000.")),
        ]
    )
    run, _ = ask(model, self_check=Checker(FEEDBACK))
    assert run.self_check == "sent" and run.answer.sql == SQL2


def test_clarifications_are_not_self_checked():
    check = Checker(FEEDBACK)
    model = ScriptedModel([turn(final(clarification="Which year?"))])
    run, _ = ask(model, self_check=check)
    assert check.seen == [] and run.self_check == "not_run"


def test_failure_after_feedback_ends_the_question():
    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(text="ok"), turn(text="ok")])
    run, _ = ask(model, self_check=Checker(FEEDBACK))
    assert run.answer.error == "schema" and run.answer.sql is None


def test_self_check_is_skipped_when_no_turn_is_left_to_answer_it():
    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(final(SQL2, "x"))])
    run, _ = ask(model, self_check=Checker(FEEDBACK), max_turns=1)
    assert run.answer.error is None and run.answer.sql == SQL1
    assert run.self_check == "skipped" and model.steps == 1


# --- wall clock and blank feedback (verifier round 1) --------------------------------------


def test_feedback_is_skipped_when_too_little_wall_clock_remains():
    clock = ManualClock()
    check = Checker(FEEDBACK)
    model = ScriptedModel(
        [clock.after(105, turn(final(SQL1, "No sales."))), turn(final(SQL2, "x"))]
    )
    run, _ = ask(model, self_check=check, clock=clock)  # 15 s left < FEEDBACK_MIN_S
    assert run.answer.error is None and run.answer.sql == SQL1
    assert run.self_check == "skipped" and model.steps == 1 and check.seen == [SQL1]


def test_feedback_is_sent_with_exactly_the_minimum_left():
    clock = ManualClock()
    model = ScriptedModel(
        [clock.after(120 - FEEDBACK_MIN_S, turn(final(SQL1, "No sales."))), turn(final(SQL2, "x"))]
    )
    run, _ = ask(model, self_check=Checker(FEEDBACK), clock=clock)
    assert run.self_check == "sent" and run.answer.sql == SQL2


def test_feedback_minimum_is_configurable():
    clock = ManualClock()
    model = ScriptedModel(
        [clock.after(115, turn(final(SQL1, "No sales."))), turn(final(SQL2, "x"))]
    )
    run, _ = ask(model, self_check=Checker(FEEDBACK), clock=clock, self_check_min_s=5)
    assert run.self_check == "sent" and run.answer.sql == SQL2


def test_follow_up_past_the_wall_clock_returns_the_first_answer():
    clock = ManualClock()
    model = ScriptedModel(
        [
            clock.after(50, turn(final(SQL1, "No sales."))),
            clock.after(80, turn(final(SQL2, "250000."))),  # arrives at 130 s
        ]
    )
    run, _ = ask(model, self_check=Checker(FEEDBACK), clock=clock)
    a = run.answer
    assert a.error is None and a.sql == SQL1 and a.answer == "No sales."
    assert run.self_check == "sent" and [s.tool for s in a.steps] == ["final_answer"]


def test_follow_up_past_the_turn_cap_returns_the_first_answer():
    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(query(SQL2))])
    run, _ = ask(model, self_check=Checker(FEEDBACK), max_turns=2)
    a = run.answer
    assert a.error is None and a.sql == SQL1 and run.self_check == "sent"
    assert [s.tool for s in a.steps] == ["final_answer", "run_select_query"]  # steps kept


def test_overrun_without_feedback_is_still_step_budget():
    model = ScriptedModel([turn(query(SQL2)), turn(query(SQL2))])
    run, _ = ask(model, self_check=Checker(FEEDBACK), max_turns=2)
    assert run.answer.error == "step_budget" and run.answer.sql is None


def test_hung_hook_is_cut_by_the_wall_clock_and_the_answer_stands():
    async def hung(sql):
        await asyncio.sleep(30)
        return FEEDBACK

    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(final(SQL2, "x"))])
    t0 = time.monotonic()
    run, _ = ask(model, self_check=hung, wall_s=0.3)
    assert time.monotonic() - t0 < 5  # cut at the deadline, not after the hook's 30 s
    assert run.answer.error is None and run.answer.sql == SQL1
    assert run.self_check == "skipped" and model.steps == 1


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_feedback_counts_as_passed(blank):
    model = ScriptedModel([turn(final(SQL1, "No sales.")), turn(final(SQL2, "x"))])
    run, _ = ask(model, self_check=Checker(blank))
    assert run.self_check == "passed" and model.user_texts == [] and run.answer.sql == SQL1
