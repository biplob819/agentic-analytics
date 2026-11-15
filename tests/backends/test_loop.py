"""One loop for both models: what it sends, what it returns (AGT-01, AGT-04 parallel calls)."""

import pytest

from agentic_analytics.backends import FINAL_ANSWER_SPEC, render_system_prompt
from agentic_analytics.schemas import AnalyticsAnswer
from tests.backends.fakes import (
    AS_OF,
    CALLS,
    QUESTION,
    SNAPSHOT,
    ScriptedModel,
    ask,
    call,
    final,
    gpt,
    query,
    turn,
)

SQL = "SELECT count() AS n FROM uk.uk_price_paid WHERE town = 'LEEDS'"


def answer_script():
    return [
        turn(call("list_tables", {"database": "uk"}), cost=0.1, tokens=(1000, 40, 10)),
        turn(query(SQL), cost=0.2, tokens=(1200, 30, 0)),
        turn(final(SQL, "There were 42 sales."), cost=0.3, tokens=(1300, 25, 5)),
    ]


def test_answer_path_returns_valid_answer_steps_cost_and_tokens():
    model = ScriptedModel(answer_script())
    run, box = ask(model)
    a = run.answer
    assert isinstance(a, AnalyticsAnswer)
    assert (a.backend, a.sql, a.answer, a.error) == (
        "claude_mcp",
        SQL,
        "There were 42 sales.",
        None,
    )
    assert a.clarification is None and a.suggestions == () and a.chart is None
    assert a.model_snapshot == SNAPSHOT and a.request_id == "req-1"
    assert [s.tool for s in a.steps] == ["list_tables", "run_select_query", "final_answer"]
    assert [s.index for s in a.steps] == [0, 1, 2]
    assert a.steps[1].rows_returned == 1 and a.steps[1].error is None
    assert run.cost_usd == 0.6  # exact (fsum); a running total gives 0.6000000000000001 (L-27)
    assert run.tokens == {"in": 3500, "out": 95, "reasoning": 15, "cached": 30, "cache_write": 0}
    assert run.turns == 3 and run.self_check == "not_run"
    assert box.calls_made == 2  # final_answer is free (D-5)


def test_clarification_path_keeps_suggestions():
    model = ScriptedModel(
        [
            turn(
                final(
                    clarification="Which property type and which year?",
                    suggestions=["Median price of detached houses in Leeds in 2024"],
                )
            )
        ]
    )
    run, _ = ask(model, "Average price in Leeds?")
    a = run.answer
    assert a.sql is None and a.error is None and a.answer == ""
    assert a.clarification == "Which property type and which year?"
    assert a.suggestions == ("Median price of detached houses in Leeds in 2024",)
    assert a.asked_for_clarification


def test_same_loop_serves_gpt_as_gpt_mcp():
    model = gpt([turn(final(SQL, "42 sales."), snapshot="gpt-6.1-sol-2026-10-01")])
    run, _ = ask(model)
    assert run.answer.backend == "gpt_mcp"
    assert run.answer.model_snapshot == "gpt-6.1-sol-2026-10-01"


def test_unknown_provider_is_refused_before_any_call():
    model = ScriptedModel([turn(final(SQL, "42."))], provider="mistral")
    with pytest.raises(ValueError, match="provider"):
        ask(model)
    assert model.steps == 0


@pytest.mark.parametrize("make", [ScriptedModel, gpt])
def test_both_models_get_identical_prompt_tools_and_user_turn(make):
    model = make(answer_script())
    _, box = ask(model, fix_blocks=("COLUMN NOTES",))
    assert model.system == render_system_prompt(AS_OF, ("COLUMN NOTES",))
    assert model.user == QUESTION
    assert model.tools == (*box.specs, FINAL_ANSWER_SPEC)
    assert [t.name for t in model.tools] == [
        "list_databases",
        "list_tables",
        "run_select_query",
        "final_answer",
    ]


def test_parallel_calls_run_in_order_and_return_in_one_batch():
    calls = [
        call("list_databases"),
        call("list_tables", {"database": "uk"}),
        query("SELECT 1 AS n"),
    ]
    model = ScriptedModel([turn(*calls), turn(final(SQL, "42."))])
    run, box = ask(model)
    assert CALLS == ["list_databases", "list_tables", "SELECT 1 AS n"]
    assert len(model.results) == 1
    assert [r.call_id for r in model.results[0]] == [c.id for c in calls]
    assert box.calls_made == 3
    assert [s.tool for s in run.answer.steps][:3] == [c.name for c in calls]


def test_parallel_calls_are_budgeted_per_call():
    model = ScriptedModel(
        [turn(*[query(f"SELECT {i}") for i in range(7)]), turn(final(SQL, "42."))]
    )
    run, box = ask(model)
    assert box.calls_made == 7 and len(CALLS) == 6  # the seventh never reached the server
    assert run.answer.steps[6].error is not None and run.answer.steps[6].error.startswith("budget")
    assert [r.is_error for r in model.results[0]] == [False] * 6 + [True]


@pytest.mark.parametrize("kw", [{"max_turns": 0}, {"wall_s": 0}, {"wall_s": -1.0}])
def test_bad_limits_are_refused(kw):
    with pytest.raises(ValueError):
        ask(ScriptedModel([]), **kw)
