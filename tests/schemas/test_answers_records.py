import pytest
from pydantic import ValidationError

from tests.schemas.factories import answer, execution, record, result, scores, step


def test_execution_result_xor_error():
    with pytest.raises(ValidationError):
        execution(error="sql_error")  # result and error
    with pytest.raises(ValidationError):
        execution(result=None)  # neither
    failed = execution(result=None, rows_total=None, error="sql_error")
    assert failed.error == "sql_error"


def test_execution_truncation_must_be_honest():
    with pytest.raises(ValidationError):
        execution(rows_total=5)  # 1 row returned of 5 but truncated=False
    assert execution(rows_total=5, truncated=True).truncated
    with pytest.raises(ValidationError):
        execution(rows_total=0)  # more rows than rows_total


def test_answer_is_sql_or_clarification_not_both():
    with pytest.raises(ValidationError):
        answer(clarification="Which period?")
    clar = answer(sql=None, answer="", clarification="Which period and property type?")
    assert clar.asked_for_clarification
    with pytest.raises(ValidationError):
        answer(sql=None, answer="")  # neither, and no error
    assert answer(sql=None, answer="", error="step_budget").error == "step_budget"


def test_cortex_suggestions_count_as_clarification():
    c = answer(
        backend="cortex_analyst",
        model_snapshot=None,
        steps=[],
        sql=None,
        answer="",
        suggestions=["Average detached price in Leeds in 2025?"],
    )
    assert c.asked_for_clarification
    with pytest.raises(ValidationError):
        answer(backend="cortex_analyst", model_snapshot=None, steps=[], suggestions=["x?"])


def test_backend_specific_rules():
    with pytest.raises(ValidationError):
        answer(model_snapshot=None)  # MCP agents record the snapshot
    with pytest.raises(ValidationError):
        answer(backend="cortex_analyst", model_snapshot=None)  # cortex with agent steps
    with pytest.raises(ValidationError):
        answer(steps=[step(1)])  # indexes must start at 0
    with pytest.raises(ValidationError):
        answer(sql=None, answer="", clarification="Which?", chart={"mark": "bar"})


@pytest.mark.parametrize(
    "kw",
    [
        {"correct": False},
        {"clarified": True},  # clarified on an answer question but not flagged false
        {"clarified": True, "false_clarification": True},  # then correct must be False
        {"result_match": None},  # clarify-expected but not clarified, yet correct
        {"answer_from_rows": True, "unsupported_numbers": ["999"]},
    ],
)
def test_scores_must_be_consistent(kw):
    with pytest.raises(ValidationError):
        scores(**kw)


def test_scores_valid_combinations():
    assert scores(clarified=True, false_clarification=True, correct=False).correct is False
    clar = scores(result_match=None, clarified=True, correct=True, answer_from_rows=None)
    assert clar.correct
    assert scores(result_match=False, correct=False).correct is False


def test_record_links_answer_execution_and_scores():
    r = record()
    assert r.execution is not None and r.execution.sql == r.answer.sql


@pytest.mark.parametrize(
    "kw",
    [
        {"fixes": ["self_check", "descriptions"]},  # wrong order
        {"fixes": ["examples", "examples"]},
        {"fixes": ["magic"]},
        {"trial": 0},
        {"cost_usd": -0.01},
        {"cost_usd": float("inf")},
        {"question_hash": "A" * 64},
        {"tokens": {"in": 1, "bogus": 2}},
        {"execution": None},
        {"backend": "gpt_mcp"},  # differs from the answer's backend
    ],
)
def test_record_invariants(kw):
    with pytest.raises(ValidationError):
        record(**kw)


def test_cortex_records_carry_no_fixes():
    cortex = answer(backend="cortex_analyst", model_snapshot=None, steps=[])
    ex = execution(engine="snowflake")
    ok = record(backend="cortex_analyst", answer=cortex, execution=ex, cortex_messages=1)
    assert ok.fixes == ()
    with pytest.raises(ValidationError):
        record(
            backend="cortex_analyst",
            answer=cortex,
            execution=ex,
            cortex_messages=1,
            fixes=["descriptions"],
        )


def test_false_clarification_alone_is_checked():
    # correct is consistent (False) but false_clarification is wrong
    with pytest.raises(ValidationError):
        scores(clarified=True, false_clarification=False, correct=False)
    with pytest.raises(ValidationError):
        scores(
            result_match=None,
            clarified=True,
            false_clarification=True,
            correct=True,
            answer_from_rows=None,
        )


def _clar_answer():
    return answer(sql=None, answer="", clarification="Which year?")


def test_record_scores_must_match_the_answer_and_execution():
    # asked back, but scored as a matched answer
    with pytest.raises(ValidationError):
        record(answer=_clar_answer(), execution=None)
    # execution failed, yet result_match and correct
    failed = execution(result=None, rows_total=None, error="sql_error")
    with pytest.raises(ValidationError):
        record(execution=failed)
    # timed-out answer with no SQL scored correct on a clarify question
    timed_out = answer(sql=None, answer="", error="timeout")
    s = scores(result_match=None, clarified=False, correct=False, answer_from_rows=None)
    ok = record(question_id="q-090", answer=timed_out, execution=None, scores=s)
    assert not ok.scores.correct
    # the verifier's case: a timed-out answer with no SQL scored correct
    claims_clarified = scores(
        result_match=None, clarified=True, correct=True, answer_from_rows=None
    )
    with pytest.raises(ValidationError):
        record(question_id="q-090", answer=timed_out, execution=None, scores=claims_clarified)
    # ...but an errored answer can never be correct
    errored = answer(sql=answer().sql, answer="", error="step_budget")
    with pytest.raises(ValidationError):
        record(answer=errored)


def test_execution_engine_must_match_backend():
    with pytest.raises(ValidationError):
        record(execution=execution(engine="snowflake"))


def test_failed_execution_reports_no_rows():
    with pytest.raises(ValidationError):
        execution(result=None, rows_total=3, error="sql_error")
    with pytest.raises(ValidationError):
        execution(result=None, rows_total=None, truncated=True, error="timeout")


def test_blank_model_snapshot_is_rejected():
    with pytest.raises(ValidationError):
        answer(model_snapshot=" ")


def test_executed_sql_must_be_the_answer_sql():
    with pytest.raises(ValidationError):
        record(execution=execution(sql="SELECT 2"))


def test_clarification_record_has_no_execution():
    clar = answer(sql=None, answer="", clarification="Which period?")
    s = scores(result_match=None, clarified=True, correct=True, answer_from_rows=None)
    r = record(question_id="q-090", answer=clar, execution=None, scores=s)
    assert r.execution is None
    assert result().rows
