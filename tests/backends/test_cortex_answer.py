"""CTX-03..CTX-06: execution, suggestions, the answer template and the run record."""

from __future__ import annotations

import pytest

from agentic_analytics.backends.cortex import ROW_CAP, format_cell, template_answer
from agentic_analytics.schemas import AnalyticsAnswer
from tests.backends.cortex_fakes import (
    SQL,
    FakeExecutor,
    body,
    execution,
    run,
    sql_block,
    sql_body,
    suggestions_block,
    text_block,
    ticks,
)

# --- CTX-03: execution through the injected executor -------------------------------------------


def test_sql_executed_once_through_the_injected_executor():
    ex = FakeExecutor(execution(elapsed_ms=777))
    out, _, _ = run(sql_body(), executor=ex)
    assert ex.calls == [(SQL, ROW_CAP)]
    assert ROW_CAP == 10_000
    assert out.execution is ex.result
    assert out.warehouse_elapsed_ms == 777
    assert out.answer.sql == SQL


def test_row_cap_passed_through():
    ex = FakeExecutor()
    run(sql_body(), executor=ex, row_cap=50)
    assert ex.calls == [(SQL, 50)]


def test_executor_returning_another_engine_is_a_wiring_bug():
    ex = FakeExecutor(execution(engine="clickhouse"))
    with pytest.raises(TypeError, match="Snowflake"):
        run(sql_body(), executor=ex)


def test_executor_config_errors_stop_the_run():
    class KeyRejected(RuntimeError):
        pass

    with pytest.raises(KeyRejected):
        run(sql_body(), executor=FakeExecutor(KeyRejected("bad key")))


@pytest.mark.parametrize(
    "reply",
    [
        body(text_block(), suggestions_block("A?")),
        body(text_block("I cannot answer that.")),
    ],
)
def test_nothing_executed_without_sql(reply):
    out, _, ex = run(reply)
    assert ex.calls == []
    assert out.execution is None
    assert out.warehouse_elapsed_ms == 0


# --- CTX-04: suggestions are a clarification ---------------------------------------------------


def test_suggestions_become_a_clarification_with_the_suggestions_kept():
    reply = body(
        text_block("Your question is ambiguous."),
        suggestions_block("Median price in Leeds in 2024?", "Mean price in Leeds in 2024?"),
    )
    out, _, _ = run(reply)
    a = out.answer
    assert a.suggestions == ("Median price in Leeds in 2024?", "Mean price in Leeds in 2024?")
    assert a.sql is None
    assert a.clarification is None
    assert a.asked_for_clarification is True
    assert a.error is None
    assert a.answer == ""
    assert a.interpretation == "Your question is ambiguous."
    assert out.cortex_messages == 1


def test_suggestion_spelling_also_makes_a_clarification():
    out, _, _ = run(body(suggestions_block("A?", kind="suggestion", key="suggestion")))
    assert out.answer.asked_for_clarification is True
    assert out.answer.suggestions == ("A?",)


def test_sql_answer_is_not_a_clarification_even_if_suggestions_came_too():
    out, _, ex = run(body(sql_block(), suggestions_block("A?")))
    assert out.answer.asked_for_clarification is False
    assert out.answer.suggestions == ()
    assert len(ex.calls) == 1


# --- CTX-05: the answer template ---------------------------------------------------------------


def test_template_single_value():
    assert template_answer(execution(((254_312.5,),))) == "The result is 254312.5."
    assert template_answer(execution(((1_234_567,),))) == "The result is 1234567."


def test_template_one_row():
    ex = execution((("LEEDS", 254_312.5, 1_204),), ("TOWN", "MEDIAN_PRICE", "SALES"))
    assert template_answer(ex) == "TOWN: LEEDS, MEDIAN_PRICE: 254312.5, SALES: 1204"


def test_template_many_rows():
    ex = execution((("LEEDS", 1), ("YORK", 2), ("HULL", 3)), ("TOWN", "N"))
    assert template_answer(ex) == "3 rows returned; first: TOWN: LEEDS, N: 1"


def test_template_truncated_counts_rows_held():
    ex = execution((("A", 1), ("B", 2)), ("T", "N"), rows_total=5)
    assert template_answer(ex) == "2 rows returned (cut at the row cap); first: T: A, N: 1"


def test_template_no_rows():
    assert template_answer(execution((), ("N",))) == "The query returned no rows."


@pytest.mark.parametrize("err", ["sql_error", "guard", "timeout", "transport"])
def test_template_failed_execution(err):
    assert template_answer(execution(error=err)) == f"The query failed ({err})."


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "NULL"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-42, "-42"),
        (12_345_678_901_234, "12345678901234"),
        (254_312.5, "254312.5"),
        (254_000.0, "254000.0"),
        (0.1, "0.1"),
        (1e-05, "0.00001"),
        (1.5e16, "15000000000000000"),
        (-3.25, "-3.25"),
        ("2024-01-01", "2024-01-01"),
        ("M1 1AA", "M1 1AA"),
        ([1, "a"], '[1, "a"]'),
    ],
)
def test_format_cell_keeps_values_exact(value, text):
    assert format_cell(value) == text


def test_template_numbers_reparse_to_the_cells():
    ex = execution(((0.1, 1e-05, 1.5e16, -3.25, 7),), ("A", "B", "C", "D", "E"))
    text = template_answer(ex)
    parts = [p.split(": ")[1] for p in text.split(", ")]
    assert [float(p) for p in parts] == [0.1, 1e-05, 1.5e16, -3.25, 7.0]


ROW_SHAPES = [
    ((254_312.5,),),
    ((1_234_567,),),
    ((0.125,),),
    ((-3.2,),),
    (("LEEDS", 254_312.5, 1_204, "2024-03-01"),),
    tuple((f"TOWN{i}", 100_000 + i * 12_345.5, i) for i in range(12)),
    tuple((i, 1e-05 * (i + 1)) for i in range(3)),
    ((None, 0, True),),
]


@pytest.mark.parametrize("rows", ROW_SHAPES)
def test_template_passes_answer_from_rows_by_construction(rows):
    numbers = pytest.importorskip("agentic_analytics.scoring.numbers")
    cols = tuple(f"COL_{chr(65 + i)}" for i in range(len(rows[0])))
    ex = execution(rows, cols)
    assert ex.result is not None
    text = template_answer(ex)
    assert numbers.unsupported_numbers(text, ex.result, "a question with no numbers") == ()


def test_answer_text_is_the_template_and_cortex_text_is_the_interpretation():
    reply = body(text_block("Roughly 300k across 9 towns, I think."), sql_block())
    out, _, _ = run(reply, executor=FakeExecutor(execution(((254_312.5,),))))
    assert out.answer.answer == "The result is 254312.5."
    assert out.answer.interpretation == "Roughly 300k across 9 towns, I think."
    assert "300k" not in out.answer.answer
    assert out.answer.chart is None


def test_failed_execution_keeps_sql_and_leaves_the_answer_unerrored():
    ex = FakeExecutor(execution(error="sql_error", elapsed_ms=33))
    out, _, _ = run(sql_body(), executor=ex)
    assert out.answer.sql == SQL
    assert out.answer.error is None
    assert out.answer.answer == "The query failed (sql_error)."
    assert out.execution is not None
    assert out.execution.error == "sql_error"
    assert out.warehouse_elapsed_ms == 33
    assert out.cortex_messages == 1


def test_guard_refusal_keeps_sql_and_execution_records_it():
    out, _, _ = run(sql_body(), executor=FakeExecutor(execution(error="guard", elapsed_ms=0)))
    assert out.answer.sql == SQL
    assert out.answer.answer == "The query failed (guard)."
    assert out.execution is not None and out.execution.error == "guard"


# --- CTX-06: single turn, errors, run record ---------------------------------------------------


def test_one_request_with_one_user_message():
    _, api, _ = run(sql_body())
    [sent] = api.bodies()
    assert len(api.requests) == 1
    assert len(sent["messages"]) == 1
    assert sent["messages"][0]["role"] == "user"


def test_text_only_reply_is_a_refusal_with_the_text_kept():
    out, _, ex = run(body(text_block("I can only answer read-only questions.")))
    a = out.answer
    assert a.error == "refusal"
    assert a.answer == "I can only answer read-only questions."
    assert a.interpretation == "I can only answer read-only questions."
    assert a.sql is None
    assert a.suggestions == ()
    assert out.cortex_messages == 1
    assert ex.calls == []


def test_empty_content_is_a_refusal_with_no_text():
    out, _, _ = run(body())
    assert out.answer.error == "refusal"
    assert out.answer.answer == ""
    assert out.answer.interpretation is None


def test_api_latency_from_the_injected_clock():
    out, _, _ = run(sql_body(), clock=ticks(10.0, 10.25, 99.0))
    assert out.api_latency_ms == 250


def test_api_latency_recorded_on_failures_too():
    out, _, _ = run(*[{"message": "x"}], clock=ticks(5.0, 5.5))
    assert out.answer.error == "schema"
    assert out.api_latency_ms == 500


def test_api_latency_excludes_execution():
    clock_values = iter([1.0, 1.1, 50.0, 60.0])

    def clock() -> float:
        return next(clock_values)

    out, _, _ = run(sql_body(), clock=clock)
    assert out.api_latency_ms == 100


def test_model_snapshot_and_request_id_on_every_answer():
    for reply in (
        sql_body(),
        body(suggestions_block("A?")),
        body(text_block("no")),
    ):
        out, _, _ = run(reply)
        assert out.answer.model_snapshot == "claude-sonnet-5-5,mistral-large2"
        assert out.answer.request_id == "req-0001"
        assert out.answer.backend == "cortex_analyst"
        assert out.answer.steps == ()


def test_no_model_names_gives_no_snapshot():
    out, _, _ = run(body(sql_block(), model_names=()))
    assert out.answer.model_snapshot is None


def test_every_answer_satisfies_the_schema_invariants():
    replies = [
        sql_body(),
        body(suggestions_block("A?")),
        body(text_block("x")),
        b"junk",
    ]
    for reply in replies:
        out, _, _ = run(reply)
        AnalyticsAnswer.model_validate(out.answer.model_dump())


def test_blank_question_refused_before_any_call():
    with pytest.raises(ValueError, match="blank"):
        run(sql_body(), question="   ")


def test_warnings_kept_on_the_suggestions_path():
    out, _, _ = run(body(suggestions_block("A?"), warnings=["view has 30 dimensions"]))
    assert out.answer.asked_for_clarification
    assert out.warnings == ("view has 30 dimensions",)


def test_warnings_kept_on_the_refusal_path():
    out, _, _ = run(body(text_block("no"), warnings=["w1", "w2"]))
    assert out.answer.error == "refusal"
    assert out.warnings == ("w1", "w2")
