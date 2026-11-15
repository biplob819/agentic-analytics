"""SCR-01: re-execute the final SQL on the backend's engine."""

from __future__ import annotations

import pytest

from agentic_analytics.scoring import ROW_CAP, Executor, ScoringError, reexecute
from tests.schemas.factories import SQL, answer
from tests.scoring.fakes import FakeExecutor


def test_fake_executor_satisfies_the_protocol():
    assert isinstance(FakeExecutor(), Executor)


def test_final_sql_runs_once_with_the_executor_row_cap():
    ex = FakeExecutor()
    out = reexecute(answer(), ex)
    assert ex.calls == [(SQL["clickhouse"], ROW_CAP)] and ROW_CAP == 10_000
    assert out is not None and out.error is None and out.sql == SQL["clickhouse"]


def test_row_cap_is_passed_through():
    ex = FakeExecutor()
    reexecute(answer(), ex, row_cap=50)
    assert ex.calls[0][1] == 50


def test_no_sql_means_no_execution():
    ex = FakeExecutor()
    clar = answer(sql=None, clarification="Which year do you mean?")
    assert reexecute(clar, ex) is None and ex.calls == []


@pytest.mark.parametrize("err", ["sql_error", "guard", "timeout", "transport"])
def test_failed_execution_is_returned_not_raised(err):
    out = reexecute(answer(), FakeExecutor(error=err))
    assert out is not None and out.error == err and out.result is None


def test_cortex_answers_run_on_snowflake():
    cortex = answer(backend="cortex_analyst", model_snapshot=None, steps=[])
    out = reexecute(cortex, FakeExecutor(engine="snowflake"))
    assert out is not None and out.engine == "snowflake"
    with pytest.raises(ScoringError, match="snowflake"):
        reexecute(cortex, FakeExecutor(engine="clickhouse"))


def test_agent_answers_refuse_the_wrong_engine():
    with pytest.raises(ScoringError, match="clickhouse"):
        reexecute(answer(), FakeExecutor(engine="snowflake"))


def test_executor_reporting_another_engine_is_rejected():
    with pytest.raises(ScoringError, match="reported engine snowflake"):
        reexecute(answer(), FakeExecutor(report_engine="snowflake"))


def test_executor_that_rewrites_the_sql_is_rejected():
    with pytest.raises(ScoringError, match="unchanged"):
        reexecute(answer(), FakeExecutor(echo_sql=False))


def test_bad_row_cap_is_rejected():
    with pytest.raises(ScoringError):
        reexecute(answer(), FakeExecutor(), row_cap=0)
