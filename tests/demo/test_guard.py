"""ASK-03: the live path behind EvalKit's guard (D-17, EK D-53/D-54, EK L-39).

One number, the request's hold (the sum of its panels' upper bounds), is what the guard checks
against the per-request cap, what the daily counter reserves under the key `agentic-analytics`,
and what the panels are capped at while they run; the reservation is settled to the charge.
"""

from __future__ import annotations

import threading

import pytest
from evalkit.cost import price_for
from evalkit.demo.guard import Guard, GuardRefused, PostgresSpend, SqliteSpend

from agentic_analytics.cost import credit_rates, model_prices
from agentic_analytics.demo import ask as A
from tests.demo.helpers import FakePg, SlowModel, SpyStore, clarify_turn, context_with, fake_context
from tests.run import world as w

PASS = "open-sesame"
Q1 = "How many property sales were recorded in England and Wales in 2023?"


def guard(tmp_path, **kw):
    store = kw.pop("store", None) or SpyStore(tmp_path / "spend.sqlite")
    return A.make_guard({"DEMO_PASSCODE": PASS}, store=store, **kw), store


def bound(ctx, backends=A.DEMO_BACKENDS):
    return A.request_bound_usd(
        backends, config=ctx.config, prices=ctx.prices, rates=ctx.rates, limits=ctx.limits
    )


# ---- the guard is EvalKit's ------------------------------------------------------------------


def test_the_guard_is_evalkits_with_the_d17_limits(tmp_path):
    g, _ = guard(tmp_path)
    assert type(g) is Guard
    assert g.daily_cap == A.DAILY_CAP_USD == 3.0
    assert g.per_request_cap == A.PER_REQUEST_CAP_USD
    assert g.rate_limit == A.RATE_LIMIT


def test_live_mode_needs_a_passcode(tmp_path):
    with pytest.raises(ValueError, match="passcode"):
        A.make_guard({}, store=SqliteSpend(tmp_path / "s.sqlite"))


def test_a_wrong_passcode_is_refused_before_any_call_or_reservation(tmp_path):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    g, store = guard(tmp_path)
    with pytest.raises(GuardRefused, match="Wrong passcode"):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode="nope", session="s")
    # even a bad question learns nothing before the passcode
    with pytest.raises(GuardRefused):
        A.ask_panels("", A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode="x", session="s")
    assert counter.total == 0 and store.reserved == []


def test_hold_reserved_equals_the_cap_check_and_the_panel_bounds_and_settles_to_charge(tmp_path):
    ctx = fake_context()
    g, store = guard(tmp_path)
    panels = A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode=PASS, session="s")
    hold = bound(ctx)
    assert store.reserved == [pytest.approx(hold, abs=1e-12)]
    assert sum(p.bound_usd for p in panels) == pytest.approx(hold, abs=1e-12)
    charged = sum(p.cost_usd for p in panels)
    assert store.settled == [(pytest.approx(hold), pytest.approx(charged))]
    day = g._today()  # the guard's UTC day, not the local date (failed near midnight)
    assert g.spend.spent(day) == pytest.approx(charged, rel=1e-6)


def test_the_two_panel_hold_fits_the_per_request_cap_and_three_panels_do_not():
    ctx = fake_context(("claude_mcp", "gpt_mcp", "cortex_analyst"))
    two = bound(ctx)
    three = bound(ctx, ("claude_mcp", "gpt_mcp", "cortex_analyst"))
    assert 0.5 < two < A.PER_REQUEST_CAP_USD < three
    assert A.DAILY_CAP_USD / two > 3  # a $3 day admits several requests at once


def test_a_hold_over_the_per_request_cap_is_refused_before_any_call(tmp_path):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    g, store = guard(tmp_path, per_request_cap=0.10)
    with pytest.raises(GuardRefused, match="per-request limit"):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode=PASS, session="s")
    assert counter.total == 0 and store.reserved == []


def test_the_session_rate_limit(tmp_path):
    ctx = context_with({"claude_mcp": SlowModel()}, ("claude_mcp",))
    g, _ = guard(tmp_path)
    for _ in range(A.RATE_LIMIT):
        A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx, guard=g, passcode=PASS,
                     session="s1")  # fmt: skip
    with pytest.raises(GuardRefused, match="Rate limit"):
        A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx, guard=g, passcode=PASS,
                     session="s1")  # fmt: skip
    A.ask_panels("Which town?", ("claude_mcp",), "live", ctx=ctx, guard=g, passcode=PASS,
                 session="s2")  # fmt: skip


def test_the_daily_budget_refuses_when_used_up(tmp_path):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    g, _ = guard(tmp_path, daily_cap=0.5)
    with pytest.raises(GuardRefused, match="budget is used up"):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode=PASS, session="s")
    assert counter.total == 0


def test_a_store_outage_refuses_readably_and_calls_nothing(tmp_path):
    class Down(SqliteSpend):
        def reserve(self, day, amount, cap):
            raise OSError("password hunter2 rejected by db")

    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    g, _ = guard(tmp_path, store=Down(tmp_path / "d.sqlite"))
    with pytest.raises(GuardRefused) as exc:
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=g, passcode=PASS, session="s")
    assert "hunter2" not in exc.value.reason and counter.total == 0


def test_concurrent_requests_never_pass_the_daily_cap_together(tmp_path):
    store = SqliteSpend(tmp_path / "c.sqlite")
    ok: list[bool] = []
    hold = 0.74
    threads = [
        threading.Thread(target=lambda: ok.append(store.reserve("2031-01-01", hold, 3.0)))
        for _ in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(ok) == 4 and store.spent("2031-01-01") == pytest.approx(4 * hold)


# ---- the bound is enforced at run time --------------------------------------------------


def test_budgeted_model_stops_before_a_turn_that_could_pass_its_cap():
    price = price_for(model_prices(), "claude-sonnet-5-5")
    inner = SlowModel(turns=[clarify_turn()])
    m = A.BudgetedModel(inner, price, cap_usd=0.001, max_output_tokens=2048)
    m.start("system", "question", ())
    import asyncio

    turn = asyncio.run(m.step(None))
    assert turn.stop == "max_tokens" and turn.cost_usd == 0.0
    assert m.capped and inner.steps == 0 and m.charged() == 0.0


def test_a_capped_agent_panel_ends_with_step_budget_within_its_bound(tmp_path):
    expensive = SlowModel(turns=[clarify_turn(cost=0.0)])
    ctx = context_with({"claude_mcp": expensive}, ("claude_mcp",), max_turns=1)
    # a bound that leaves the model less than one worst-case turn
    (panel,) = A.run_panels("Which town?", ("claude_mcp",), ctx, bounds=[0.01])
    assert panel.capped and panel.answer.error == "step_budget" and expensive.steps == 0
    assert panel.cost_usd <= panel.bound_usd == 0.01
    assert "cost cap" in (panel.note or "")


def test_the_worst_case_turn_counts_text_by_bytes_and_prices_at_the_highest_rates():
    price = price_for(model_prices(), "gpt-6.1-sol")
    p_in, p_out = A.worst_prices(price)
    assert p_in == 4.0 and p_out == 15.0  # the long-context tier, not the base price
    assert A.strict_tokens("abc") == 3 and A.strict_tokens("é€😀") == 2 + 3 + 4
    m = A.BudgetedModel(SlowModel(), price, 1.0, max_output_tokens=2048)
    m.start("s", "Prix médian à Leeds en 2024 ?", ())
    ascii_m = A.BudgetedModel(SlowModel(), price, 1.0, max_output_tokens=2048)
    ascii_m.start("s", "Prix median a Leeds en 2024 ?", ())
    assert m._next_in == ascii_m._next_in + 2  # each accented letter is two bytes


def _two_step(cap_from_worst, *, out=30_000, reasoning=20_000, tool_bytes=40_000):
    """A first turn billed 50,000 prompt tokens plus `out` output and `reasoning` tokens, then
    `tool_bytes` of tool results: the second turn's worst case must count all of them."""
    import asyncio

    from agentic_analytics.models import ModelTurn, ToolResult, Usage

    price = price_for(model_prices(), "claude-sonnet-5-5")
    first = ModelTurn(text="", tool_calls=(), stop="end", cost_usd=0.01, latency_ms=1,
                      usage=Usage(tokens_in=50_000, tokens_out=out, tokens_reasoning=reasoning),
                      model_snapshot="claude-sonnet-5-5")  # fmt: skip
    inner = SlowModel(turns=[first, clarify_turn()])
    expected_in = (50_000 + out + reasoning + A.PROVIDER_OVERHEAD_TOKENS
                   + tool_bytes + A.MESSAGE_OVERHEAD_TOKENS)  # fmt: skip
    worst2 = A.step_bound_usd(price, expected_in, 2048)
    m = A.BudgetedModel(inner, price, cap_from_worst(0.01 + worst2), max_output_tokens=2048)
    m.start("s", "q", ())
    asyncio.run(m.step(None))
    m.add_tool_results(None, [ToolResult(call_id="c1", content="x" * tool_bytes)])
    second = asyncio.run(m.step(None))
    return m, inner, second, price


def test_the_next_turns_worst_case_counts_the_prompt_its_output_and_tool_results():
    """Verifier round 1: the second turn's prompt bound is the first turn's billed prompt plus
    its output and reasoning tokens plus every tool-result byte (with framing)."""
    m, inner, second, _ = _two_step(lambda need: need * (1 + 1e-9))  # just fits
    assert inner.steps == 2 and second.stop != "max_tokens" and not m.capped
    p_in, _ = A.worst_prices(price_for(model_prices(), "claude-sonnet-5-5"))
    for part in (30_000, 20_000, 40_000):  # output, reasoning, tool results: each counted
        gap = part * p_in / 1_000_000 / 2  # a cap short of the true worst case by half a part
        m, inner, second, _ = _two_step(lambda need, g=gap: need - g)
        assert inner.steps == 1 and second.stop == "max_tokens" and m.capped, part


def test_tool_results_alone_can_push_the_next_turn_over_the_cap():
    p_in, _ = A.worst_prices(price_for(model_prices(), "claude-sonnet-5-5"))
    tool_part = (10_000 + A.MESSAGE_OVERHEAD_TOKENS) * p_in / 1_000_000
    caps: list[float] = []

    def cap(need: float) -> float:  # room for the turn without its tool results, not with them
        caps.append(need - tool_part / 2)
        return caps[-1]

    m, inner, second, _ = _two_step(cap, tool_bytes=10_000)
    assert inner.steps == 1 and second.stop == "max_tokens" and m.capped
    _, inner0, _, _ = _two_step(lambda need: caps[0], tool_bytes=0)
    assert inner0.steps == 2  # the same cap fits that turn without the tool results


def test_a_failed_or_cancelled_turn_stays_charged_at_its_worst_case():
    import asyncio

    price = price_for(model_prices(), "claude-sonnet-5-5")
    m = A.BudgetedModel(
        SlowModel(turns=[ConnectionError("reset")]), price, 1.0, max_output_tokens=2048
    )
    m.start("s", "q", ())
    worst = A.step_bound_usd(price, m._next_in, 2048)
    with pytest.raises(ConnectionError):
        asyncio.run(m.step(None))
    assert m.charged() == pytest.approx(worst)


def test_a_turn_after_stop_is_refused_without_a_call():
    import asyncio

    price = price_for(model_prices(), "claude-sonnet-5-5")
    inner = SlowModel()
    m = A.BudgetedModel(inner, price, 1.0, max_output_tokens=2048)
    m.start("s", "q", ())
    assert m.stop() == 0.0
    assert asyncio.run(m.step(None)).stop == "max_tokens" and inner.steps == 0
    assert not m.capped  # stopped by a timeout, not by the cap


def test_the_adapters_own_output_limit_is_used_when_larger():
    from agentic_analytics.demo.ask import demo_agent

    prices = model_prices()
    agent = demo_agent("claude-sonnet-5-5", "medium", prices, 2048)
    assert agent.max_tokens == 2048
    big = SlowModel()
    big.max_tokens = 16000  # type: ignore[attr-defined]
    m = A.BudgetedModel(big, price_for(prices, "claude-sonnet-5-5"), 1.0, max_output_tokens=2048)
    assert m.max_output_tokens == 16000


def test_the_cortex_bound_is_one_message_plus_the_warehouse_minimum():
    ctx = fake_context(("cortex_analyst",))
    rates = credit_rates()
    b = A.panel_bound_usd(
        "cortex_analyst", config=ctx.config, prices=ctx.prices, rates=rates, limits=ctx.limits
    )
    assert b == pytest.approx(0.067 * 3 + 60 / 3600 * 1.0 * 3)


def test_a_timed_out_cortex_panel_is_charged_its_whole_bound():
    import time

    import httpx2

    from agentic_analytics.backends.cortex import CortexClient
    from tests.backends.cortex_fakes import ACCOUNT, VIEW, make_jwt

    def slow(request):
        time.sleep(1.0)
        return httpx2.Response(500)

    ctx = context_with({}, ("cortex_analyst",), timeout_s=0.3)
    ctx.services.cortex = CortexClient(
        ACCOUNT, VIEW, make_jwt(), transport=httpx2.MockTransport(slow), timeout_s=5
    )
    (panel,) = A.run_panels("Average price?", ("cortex_analyst",), ctx)
    assert panel.timed_out and panel.cost_usd == pytest.approx(panel.bound_usd)


# ---- the counter lives under its own key -------------------------------------------------


def test_sqlite_locally_and_postgres_with_the_database_url(tmp_path):
    local = A.spend_store({}, sqlite_path=tmp_path / "x.sqlite")
    assert isinstance(local, SqliteSpend) and A.SPEND_KEY in A.DEFAULT_SQLITE.name
    remote = A.spend_store(
        {"EVALKIT_DEMO_DATABASE_URL": "postgresql://h/db"}, connect=FakePg().connect
    )
    assert isinstance(remote, PostgresSpend)


def test_the_postgres_counter_is_keyed_apart_from_evalkits(tmp_path):
    pg = FakePg()
    ours = PostgresSpend("postgresql://h/db", A.keyed_connect(A.SPEND_KEY, pg.connect))
    evalkit = PostgresSpend("postgresql://h/db", pg.connect)  # EvalKit's own, in `public`
    other = PostgresSpend("postgresql://h/db", A.keyed_connect("another-app", pg.connect))
    day = "2031-01-01"
    assert ours.reserve(day, 2.5, 3.0)
    assert evalkit.reserve(day, 2.5, 3.0)  # not blocked by our $2.50
    assert other.reserve(day, 2.5, 3.0)
    assert not ours.reserve(day, 1.0, 3.0)  # our own cap still holds
    assert ours.spent(day) == pytest.approx(2.5) and evalkit.spent(day) == pytest.approx(2.5)
    assert set(pg.schemas) == {"public", "agentic-analytics", "another-app"}
    # every statement of ours ran after SET LOCAL search_path, inside its own transaction
    sets = [s for s in pg.statements if s.startswith("SET LOCAL")]
    assert '"agentic-analytics"' in sets[0]


def test_keyed_connect_closes_the_connection_when_setup_fails():
    closed = []

    class Bad:
        def execute(self, sql, params=()):
            raise RuntimeError("no permission")

        def close(self):
            closed.append(True)

    connect = A.keyed_connect("k", lambda url: Bad())
    with pytest.raises(RuntimeError):
        connect("postgresql://h/db")
    assert closed == [True]


def test_identifiers_are_quoted():
    pg = FakePg()
    store = PostgresSpend("postgresql://h/db", A.keyed_connect('we"ird', pg.connect))
    assert store.reserve("2031-01-01", 0.1, 3.0)
    assert 'we"ird' in pg.schemas
