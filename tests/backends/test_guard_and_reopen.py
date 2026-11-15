"""The injected SQL guard (D-37) and reopening a dropped MCP connection (N1.5) (AGT-04, AGT-05)."""

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from agentic_analytics.backends import render_system_prompt
from agentic_analytics.backends.mcp_agent import GUARD_REFUSAL
from agentic_analytics.steps import (
    STEP_ERROR_BUDGET,
    STEP_ERROR_GUARD,
    STEP_ERROR_SQL,
    STEP_ERROR_WRITE_REFUSED,
    attempted_write,
    step_error_code,
)
from tests.backends.fakes import AS_OF, CALLS, ScriptedModel, ask, call, final, query, turn

SQL = "SELECT count() AS n FROM uk.uk_price_paid"


class FakeGuard:
    """Refuses table functions and writes, like the real check_sql would."""

    def __init__(self):
        self.seen = []

    def __call__(self, sql):
        self.seen.append(sql)
        if "url(" in sql:
            return "table functions are not allowed"
        if sql.lstrip().upper().startswith(("INSERT", "DELETE", "DROP")):
            return "only SELECT statements run"
        return None


def guarded(c, **kw):
    g = FakeGuard()
    run, box = ask(ScriptedModel([turn(c), turn(final(SQL, "42."))]), guard=g, **kw)
    return run, box, g


# --- guard (D-37) --------------------------------------------------------------------------


@pytest.mark.safety
def test_guard_refusal_never_reaches_the_server_and_is_a_guard_step():
    run, box, g = guarded(query("SELECT * FROM url('http://evil/x', CSV)"))
    assert box.calls_made == 1  # the refusal is an attempt on the toolbox's counter
    step = run.answer.steps[0]
    assert step_error_code(step) == STEP_ERROR_GUARD
    assert step.error is not None and "table functions" in step.error
    assert CALLS == [] and g.seen == ["SELECT * FROM url('http://evil/x', CSV)"]
    assert run.answer.error is None
    assert not attempted_write(run.answer)  # a read, not a write


@pytest.mark.safety
def test_guard_refused_write_is_write_refused():
    run, _, _ = guarded(query("DELETE FROM uk.uk_price_paid WHERE 1"))
    assert step_error_code(run.answer.steps[0]) == STEP_ERROR_WRITE_REFUSED
    assert CALLS == []
    assert attempted_write(run.answer)


@pytest.mark.safety
def test_guard_refusal_is_a_tool_error_the_model_reads():
    model = ScriptedModel([turn(query("SELECT * FROM url('x')")), turn(final(SQL, "42."))])
    ask(model, guard=FakeGuard())
    (res,) = model.results[0]
    assert res.is_error
    assert res.content == GUARD_REFUSAL.format(reason="table functions are not allowed")


@pytest.mark.safety
def test_guard_refusals_count_toward_the_six_calls():
    bad = [query(f"SELECT * FROM url('{i}')") for i in range(6)]
    model = ScriptedModel([turn(*bad), turn(query("SELECT 1")), turn(final(SQL, "42."))])
    run, box = ask(model, guard=FakeGuard())
    codes = [step_error_code(s) for s in run.answer.steps]
    assert codes[:6] == [STEP_ERROR_GUARD] * 6
    assert codes[6] == STEP_ERROR_BUDGET  # the seventh is over budget, guard or not
    assert box.calls_made == 7 and CALLS == []
    assert run.answer.error is None


@pytest.mark.safety
def test_over_budget_query_is_a_budget_refusal_before_the_guard_is_asked():
    g = FakeGuard()
    bad = [query(f"SELECT * FROM url('{i}')") for i in range(7)]
    run, box = ask(ScriptedModel([turn(*bad), turn(final(SQL, "42."))]), guard=g)
    assert step_error_code(run.answer.steps[6]) == STEP_ERROR_BUDGET
    assert len(g.seen) == 6 and box.calls_made == 7


def test_guard_passes_reads_through_to_the_server():
    run, box, g = guarded(query("SELECT 1"))
    assert run.answer.steps[0].error is None and CALLS == ["SELECT 1"]
    assert box.calls_made == 1 and g.seen == ["SELECT 1"]


def test_guard_only_sees_run_select_query():
    g = FakeGuard()
    args = {"database": "uk", "query": "SELECT * FROM url('x')"}  # a stray query argument
    model = ScriptedModel([turn(call("list_tables", args)), turn(final(SQL, "4"))])
    run, _ = ask(model, guard=g)
    assert run.answer.steps[0].error is None
    assert g.seen == []  # list tools and final_answer SQL are not the guard's (scoring re-runs)


def test_without_a_guard_queries_go_straight_to_the_toolbox():
    run, _ = ask(ScriptedModel([turn(query("SELECT * FROM url('x')")), turn(final(SQL, "4"))]))
    assert CALLS == ["SELECT * FROM url('x')"] and run.answer.steps[0].error is None


def test_guard_does_not_change_the_prompt_or_tools():
    plain, guarded_model = (
        ScriptedModel([turn(final(SQL, "4"))]),
        ScriptedModel([turn(final(SQL, "4"))]),
    )
    ask(plain)
    ask(guarded_model, guard=FakeGuard())
    assert plain.system == guarded_model.system == render_system_prompt(AS_OF)
    assert plain.tools == guarded_model.tools


# --- reopen (N1.5) -------------------------------------------------------------------------


def test_flagged_connection_is_reopened_before_the_turns_calls():
    def flag(box):
        box.conn.needs_reopen = True

    model = ScriptedModel([turn(query("SELECT 1")), turn(final(SQL, "42."))])
    run, box = ask(model, patch_box=flag)
    assert box.conn.generation == 1 and not box.conn.needs_reopen
    assert run.answer.steps[0].error is None and CALLS == ["SELECT 1"]


def test_unflagged_connection_is_not_reopened():
    _, box = ask(ScriptedModel([turn(query("SELECT 1")), turn(final(SQL, "42."))]))
    assert box.conn.generation == 0


def test_final_answer_only_turn_does_not_reopen():
    def flag(box):
        box.conn.needs_reopen = True

    _, box = ask(ScriptedModel([turn(final(SQL, "42."))]), patch_box=flag)
    assert box.conn.generation == 0


def test_failed_reopen_ends_with_transport_and_no_call():
    async def fail():
        raise OSError("cannot connect")

    def flag(box):
        box.conn.needs_reopen = True
        box.conn.ensure_open = fail  # type: ignore[method-assign]

    model = ScriptedModel([turn(query("SELECT 1")), turn(final(SQL, "42."))])
    run, box = ask(model, patch_box=flag)
    assert run.answer.error == "transport" and run.answer.steps == ()
    assert CALLS == [] and box.calls_made == 0 and model.steps == 1


def test_dropped_connection_mid_question_is_reopened_on_the_next_turn():
    seen = {"n": 0}

    def drop_first_call(box):
        real = box.conn.call_tool

        async def flaky(name, args, *, timeout_s=None):
            seen["n"] += 1
            if seen["n"] == 1:
                raise MCPError(CONNECTION_CLOSED, "stream closed")
            return await real(name, args, timeout_s=timeout_s)

        box._retry["attempts"] = 1  # one attempt, so the closed connection reaches the loop
        box.conn.call_tool = flaky  # type: ignore[method-assign]

    model = ScriptedModel(
        [turn(query("SELECT 1")), turn(query("SELECT 2")), turn(final(SQL, "42."))]
    )
    run, box = ask(model, patch_box=drop_first_call)
    first, second, _ = run.answer.steps
    assert step_error_code(first) == STEP_ERROR_SQL
    assert second.error is None and CALLS == ["SELECT 2"]
    assert box.conn.generation == 1 and not box.conn.needs_reopen


def test_mcp_timeout_is_not_retried_and_tells_the_model_to_simplify():
    attempts = []

    def slow(box):
        async def timeout(name, args, *, timeout_s=None):
            attempts.append(name)
            raise MCPError(REQUEST_TIMEOUT, "timed out")

        box.conn.call_tool = timeout  # type: ignore[method-assign]

    model = ScriptedModel([turn(query("SELECT 1")), turn(final(SQL, "42."))])
    run, box = ask(model, patch_box=slow)
    assert step_error_code(run.answer.steps[0]) == STEP_ERROR_SQL
    assert "simplify" in model.results[0][0].content
    assert attempts == ["run_select_query"] and not box.conn.needs_reopen
    assert run.answer.error is None
