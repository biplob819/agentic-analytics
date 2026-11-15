"""final_answer: one of SQL+answer or a clarification; one retry; one reminder (AGT-03)."""

import pytest

from agentic_analytics.backends.mcp_agent import (
    REMINDER,
    FinalAnswer,
    FinalAnswerError,
    parse_final_answer,
)
from agentic_analytics.steps import STEP_ERROR_BAD_ARGS, step_error_code
from tests.backends.fakes import ScriptedModel, ask, bad_call, final, query, turn

SQL = "SELECT count() AS n FROM uk.uk_price_paid"
OK = {"sql": SQL, "answer": "42 sales.", "clarification": None, "suggestions": []}
ASK = {"sql": None, "answer": None, "clarification": "Which year?", "suggestions": ["2024?"]}


def test_valid_answer_and_valid_clarification_parse():
    assert parse_final_answer(OK) == FinalAnswer(SQL, "42 sales.", None, ())
    assert parse_final_answer(ASK) == FinalAnswer(None, None, "Which year?", ("2024?",))


def test_missing_keys_are_read_as_null_or_empty():
    assert parse_final_answer({"sql": SQL, "answer": "42."}) == FinalAnswer(SQL, "42.", None, ())
    assert parse_final_answer({"clarification": "Which?"}).suggestions == ()


# each case breaks exactly one rule and must name the field that broke it (L-2)
@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({**OK, "clarification": "Which year?"}, "clarification"),
        ({**OK, "answer": None}, "answer"),
        ({**OK, "answer": "   "}, "answer"),
        ({**OK, "sql": None}, "sql"),
        ({**OK, "sql": ""}, "sql"),
        ({"sql": None, "answer": None, "clarification": None, "suggestions": []}, "sql"),
        ({**OK, "suggestions": ["Try 2023?"]}, "suggestions"),
        ({**ASK, "suggestions": "2024?"}, "suggestions"),
        ({**ASK, "suggestions": ["  "]}, "suggestions"),
        ({**ASK, "suggestions": [3]}, "suggestions"),
        ({**ASK, "answer": "It is 42."}, "answer"),
        ({**OK, "sql": 42}, "sql"),
        ({**OK, "answer": ["42"]}, "answer"),
        ({**ASK, "clarification": {"q": "x"}}, "clarification"),
        ({**OK, "chart": {"type": "bar"}}, "chart"),
        (None, "arguments"),
    ],
)
def test_each_violation_names_its_field(args, field):
    with pytest.raises(FinalAnswerError) as exc:
        parse_final_answer(args)
    assert exc.value.field == field
    assert f"'{field}'" in str(exc.value)


def test_one_violation_gets_a_tool_error_naming_the_field_then_a_valid_answer_wins():
    model = ScriptedModel(
        [
            turn(final(SQL, "42.", clarification="Which year?")),
            turn(final(SQL, "42 sales.")),
        ]
    )
    run, box = ask(model)
    a = run.answer
    assert a.error is None and a.sql == SQL
    (err,) = model.results[0]
    assert err.is_error and "'clarification'" in err.content
    assert step_error_code(a.steps[0]) == STEP_ERROR_BAD_ARGS
    assert a.steps[0].error is not None and "clarification" in a.steps[0].error
    assert a.steps[1].error is None
    assert box.calls_made == 0


def test_second_violation_ends_with_schema():
    model = ScriptedModel(
        [turn(final(SQL, None)), turn(final(None, "42.")), turn(final(SQL, "never read"))]
    )
    run, _ = ask(model)
    a = run.answer
    assert a.error == "schema" and a.sql is None and a.answer == ""
    assert len(a.steps) == 2 and model.steps == 2


def test_violations_are_counted_across_turns_not_per_turn():
    model = ScriptedModel(
        [
            turn(final(SQL, None)),
            turn(query("SELECT 1")),
            turn(final(None, "42.")),
        ]
    )
    run, _ = ask(model)
    assert run.answer.error == "schema"


def test_unparseable_final_answer_arguments_are_a_violation():
    model = ScriptedModel([turn(bad_call("final_answer")), turn(final(SQL, "42."))])
    run, _ = ask(model)
    assert run.answer.error is None
    assert "'arguments'" in model.results[0][0].content
    assert run.answer.steps[0].arguments == {}


def test_final_answer_never_counts_toward_the_six_calls():
    calls = [query(f"SELECT {i}") for i in range(6)]
    model = ScriptedModel([turn(*calls), turn(final(SQL, None)), turn(final(SQL, "42."))])
    run, box = ask(model)
    assert run.answer.error is None
    assert box.calls_made == 6
    assert all(s.error is None for s in run.answer.steps if s.tool != "final_answer")


def test_ending_without_final_answer_gets_one_reminder():
    model = ScriptedModel([turn(text="There were 42 sales."), turn(final(SQL, "42 sales."))])
    run, _ = ask(model)
    assert run.answer.error is None
    assert model.user_texts == [REMINDER]
    assert "final_answer" in REMINDER
    assert run.turns == 2


def test_ending_twice_without_final_answer_is_schema():
    model = ScriptedModel([turn(text="42."), turn(text="42!"), turn(final(SQL, "42."))])
    run, _ = ask(model)
    assert run.answer.error == "schema" and model.steps == 2
    assert run.answer.steps == ()
    assert run.tokens["in"] == 200  # turns without steps still count in the totals


def test_tool_use_stop_with_no_calls_is_treated_as_ending_the_turn():
    model = ScriptedModel([turn(stop="tool_use"), turn(final(SQL, "42."))])
    run, _ = ask(model)
    assert run.answer.error is None and model.user_texts == [REMINDER]


def test_only_the_first_final_answer_of_a_turn_is_judged():
    model = ScriptedModel([turn(final(SQL, "42 sales."), final(None, None))])
    run, _ = ask(model)
    a = run.answer
    assert a.error is None and a.answer == "42 sales."
    assert [s.tool for s in a.steps] == ["final_answer", "final_answer"]
    assert a.steps[1].error is not None and a.steps[1].error.startswith("bad_args")


def test_a_rejected_then_accepted_final_answer_in_one_turn():
    model = ScriptedModel([turn(final(None, None), final(SQL, "42."))])
    run, _ = ask(model)
    assert run.answer.error is None and run.answer.sql == SQL


def test_a_duplicate_final_answer_is_not_a_violation():
    async def check(sql):
        return "Your query returned 0 rows; check it."

    model = ScriptedModel(
        [
            turn(final(SQL, "42."), final(None, None)),  # accepted, then a duplicate
            turn(final(SQL, None)),  # the first real violation
            turn(final(SQL, "42 sales.")),
        ]
    )
    run, _ = ask(model, self_check=check)
    assert run.answer.error is None and run.answer.answer == "42 sales."
    assert run.self_check == "sent"
