"""OWNER D-53: the DEM-05 p95 run (20 timed asks, one sitting) draws from an owner measurement
budget unlocked only by the owner passcode: its own cap ($15 default, refused above $25), its
own counter apart from the visitors' $3/day, a spend-ledger line per ask, and the same $1
per-request cap and SQL guard as visitor mode."""

from __future__ import annotations

import json

import pytest
from evalkit.demo.guard import GuardRefused, PostgresSpend, SqliteSpend

from agentic_analytics.demo import ask as A
from agentic_analytics.schemas import BackendName
from tests.demo.helpers import FakePg, SlowModel, SpyStore, context_with, fake_context
from tests.mcp import fake_server
from tests.run import world as w

VISITOR = "open-sesame"
OWNER = "owner-only-7f3c"
ENV = {"DEMO_PASSCODE": VISITOR, "DEMO_OWNER_PASSCODE": OWNER}
Q1 = "How many property sales were recorded in England and Wales in 2023?"
ALL = A.BACKEND_IDS  # every backend live (OWNER D-60: run_live always gets the switch)
ONE = ("claude_mcp",)


def guards(tmp_path, **owner_kw):
    vstore = SpyStore(tmp_path / "visitor.sqlite")
    ostore = SpyStore(tmp_path / "owner.sqlite")
    visitor = A.make_guard(ENV, store=vstore)
    owner = A.make_owner_guard(ENV, store=ostore, **owner_kw)
    assert owner is not None
    return visitor, vstore, owner, ostore


def ledger_rows(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def quick_ctx():
    return context_with({"claude_mcp": SlowModel()}, ONE)


# ---- selection: the owner passcode alone ------------------------------------------------------


def test_only_the_owner_passcode_selects_owner_mode(tmp_path):
    visitor, _, owner, _ = guards(tmp_path)
    assert A.pick_guard(OWNER, visitor, owner) == (owner, True)
    for typed in (VISITOR, "wrong", "", OWNER + " ", OWNER.upper(), OWNER[:-1]):
        assert A.pick_guard(typed, visitor, owner) == (visitor, False)
    assert A.pick_guard(OWNER, visitor, None) == (visitor, False)  # not configured: visitor


def test_owner_mode_is_picked_by_the_guards_constant_time_check_only():
    import inspect

    from evalkit.demo.guard import Guard

    assert "check_passcode(owner, passcode)" in inspect.getsource(A.pick_guard)
    assert "guard.check_passcode(" in inspect.getsource(A.check_passcode)
    assert "hmac.compare_digest" in inspect.getsource(Guard.check_passcode)


def test_a_passcode_with_no_utf8_form_is_a_wrong_passcode_not_an_error(tmp_path):
    visitor, vstore, owner, ostore = guards(tmp_path)
    lone = "open-\ud800"  # a lone surrogate, as a browser can send
    assert A.pick_guard(lone, visitor, owner) == (visitor, False)
    with pytest.raises(GuardRefused, match="Wrong passcode"):
        A.ask_panels(Q1, ONE, "live", ctx=quick_ctx(), guard=visitor, passcode=lone, session="s")
    r = A.run_live(Q1, ctx=quick_ctx(), guard=visitor, passcode=lone, session="s", backends=ONE,
                   live=ALL)  # fmt: skip
    assert r.refused == "Wrong passcode."
    assert vstore.reserved == [] and ostore.reserved == []


def test_the_owner_passcode_must_differ_from_the_visitors_and_is_off_when_unset(tmp_path):
    with pytest.raises(ValueError, match="differ"):
        A.make_owner_guard({"DEMO_PASSCODE": VISITOR, "DEMO_OWNER_PASSCODE": VISITOR})
    assert A.make_owner_guard({"DEMO_PASSCODE": VISITOR}) is None


# ---- separate budgets ------------------------------------------------------------------------


def test_the_two_counters_live_under_separate_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vg, og = A.make_guard(ENV), A.make_owner_guard(ENV)  # the deployment's default stores
    assert (
        og is not None and isinstance(og.spend, SqliteSpend) and isinstance(vg.spend, SqliteSpend)
    )
    assert og.spend.path.name == A.DEFAULT_OWNER_SQLITE.name != vg.spend.path.name
    db = FakePg()
    env = {"EVALKIT_DEMO_DATABASE_URL": "postgresql://demo"}
    v = A.spend_store(env, connect=db.connect)
    o = A.spend_store(env, key=A.OWNER_SPEND_KEY, connect=db.connect)
    assert isinstance(v, PostgresSpend) and isinstance(o, PostgresSpend)
    assert o.reserve("2026-11-15", 0.5, 15.0) and v.reserve("2026-11-15", 0.2, 3.0)
    assert float(db.schemas[A.OWNER_SPEND_KEY]["2026-11-15"]) == 0.5
    assert float(db.schemas[A.SPEND_KEY]["2026-11-15"]) == 0.2
    assert A.OWNER_SPEND_KEY != A.SPEND_KEY and A.DEFAULT_OWNER_SQLITE != A.DEFAULT_SQLITE
    assert isinstance(A.spend_store({}, key=A.OWNER_SPEND_KEY, sqlite_path=tmp_path / "o.sq"),
                      SqliteSpend)  # fmt: skip
    assert A.OWNER_SPEND_KEY in A.DEFAULT_OWNER_SQLITE.name


def test_twenty_owner_asks_in_one_session_leave_the_visitor_counter_untouched(tmp_path):
    visitor, vstore, owner, ostore = guards(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ctx = quick_ctx()
    for _ in range(20):
        guard, is_owner = A.pick_guard(OWNER, visitor, owner)
        r = A.run_live(Q1, ctx=ctx, guard=guard, passcode=OWNER, session="one-sitting",
                       backends=ONE, owner=is_owner, ledger=ledger, live=ALL)  # fmt: skip
        assert r.refused is None and r.owner
    day = owner._today()
    assert vstore.reserved == [] and vstore.settled == [] and vstore.spent(day) == 0
    assert len(ostore.reserved) == 20 and len(ostore.settled) == 20
    rows = ledger_rows(ledger)
    assert len(rows) == 20 and {r["kind"] for r in rows} == {"demo-owner"}
    assert len({r["run_id"] for r in rows}) == 20
    assert sum(r["spent_usd"] for r in rows) == pytest.approx(ostore.spent(day), abs=1e-5)


# ---- the owner cap ----------------------------------------------------------------------------


def test_the_owner_cap_defaults_to_15_is_configurable_and_refused_above_25():
    assert A.owner_cap({}) == 15.0 == A.OWNER_CAP_USD
    assert A.owner_cap({"DEMO_OWNER_CAP_USD": "20"}) == 20.0
    assert A.owner_cap({"DEMO_OWNER_CAP_USD": "25"}) == 25.0
    for bad in ("25.01", "30", "0", "-1", "nan", "inf", "lots"):
        with pytest.raises(ValueError):
            A.owner_cap({"DEMO_OWNER_CAP_USD": bad})
    with pytest.raises(ValueError, match=r"\$25"):
        A.make_owner_guard({**ENV, "DEMO_OWNER_CAP_USD": "26"})
    with pytest.raises(ValueError):
        A.make_owner_guard(ENV, cap_usd=25.5)
    g = A.make_owner_guard({**ENV, "DEMO_OWNER_CAP_USD": "12"}, store=object())
    assert g is not None and g.daily_cap == 12.0


def test_the_owner_cap_refuses_an_ask_past_its_limit(tmp_path):
    ctx = fake_context(A.DEMO_BACKENDS, w.Counter())
    hold = A.request_bound_usd(A.DEMO_BACKENDS, config=ctx.config, prices=ctx.prices,
                               rates=ctx.rates, limits=ctx.limits)  # fmt: skip
    _, vstore, owner, _ = guards(tmp_path, cap_usd=hold * 1.5)
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    owner.spend.reserve(owner._today(), hold, owner.daily_cap)  # an ask already held
    with pytest.raises(GuardRefused, match="used up"):
        A.ask_panels(Q1, A.DEMO_BACKENDS, "live", ctx=ctx, guard=owner, passcode=OWNER,
                     session="s", ledger=tmp_path / "l.jsonl")  # fmt: skip
    assert counter.total == 0 and vstore.reserved == []
    assert not (tmp_path / "l.jsonl").exists()  # nothing spent, nothing ledgered


def test_the_one_dollar_per_request_cap_still_applies_in_owner_mode(tmp_path):
    counter = w.Counter()
    three = ("claude_mcp", "gpt_mcp", "cortex_analyst")
    ctx = fake_context(three, counter)  # type: ignore[arg-type]
    _, _, owner, ostore = guards(tmp_path)
    assert owner.per_request_cap == A.PER_REQUEST_CAP_USD == 1.0
    with pytest.raises(GuardRefused, match="per-request limit"):
        A.ask_panels(Q1, three, "live", ctx=ctx, guard=owner, passcode=OWNER, session="s",
                     ledger=tmp_path / "l.jsonl")  # type: ignore[arg-type]  # fmt: skip
    assert counter.total == 0 and ostore.reserved == []


def test_the_sql_guard_still_applies_in_owner_mode(tmp_path):
    q = "Please tidy the table for me"
    drop = {"name": "run_select_query", "arguments": {"query": "DROP TABLE uk.uk_price_paid"}}
    turns = [
        w.mcp_turn([drop], seed="a"),
        w.mcp_turn([w.final_call(clarification="I can only read data.")], seed="b"),
    ]
    model = w.ReplayModel("claude_mcp", {q: turns}, w.Counter())
    ctx = context_with({"claude_mcp": model}, ONE)
    _, _, owner, _ = guards(tmp_path)
    fake_server.CALLS.clear()
    (panel,) = A.ask_panels(q, ONE, "live", ctx=ctx, guard=owner, passcode=OWNER, session="s",
                            ledger=tmp_path / "l.jsonl")  # fmt: skip
    assert [a for n, a in fake_server.CALLS if n == "run_select_query"] == []
    assert (panel.answer.steps[0].error or "").startswith("write_refused")


# ---- settle survives exceptions ---------------------------------------------------------------


def test_an_exception_mid_ask_still_settles_and_is_ledgered(tmp_path, monkeypatch):
    _, vstore, owner, ostore = guards(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    ctx = quick_ctx()

    def boom(*a, **kw):
        raise RuntimeError("panel thread exploded")

    monkeypatch.setattr(A, "run_panels", boom)
    with pytest.raises(RuntimeError):
        A.ask_panels(Q1, ONE, "live", ctx=ctx, guard=owner, passcode=OWNER, session="s",
                     ledger=ledger)  # fmt: skip
    ((reserved, actual),) = ostore.settled
    assert actual == reserved == ostore.reserved[0] > 0  # the whole hold stays charged
    (row,) = ledger_rows(ledger)
    assert row["spent_usd"] == pytest.approx(reserved, abs=1e-6) and "error" in row["note"]
    assert vstore.reserved == [] and vstore.settled == []


def test_a_failed_ledger_write_never_undoes_the_settle(tmp_path, monkeypatch):
    _, _, owner, ostore = guards(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    (panel,) = A.ask_panels(Q1, ONE, "live", ctx=quick_ctx(), guard=owner, passcode=OWNER,
                            session="s", ledger=blocked / "ledger.jsonl")  # fmt: skip
    ((_, actual),) = ostore.settled
    assert actual == pytest.approx(panel.cost_usd)


def test_owner_asks_obey_the_live_switch_too(tmp_path):
    """Verifier round 1 (OWNER D-60, D-65): live compute must be on for everyone, the owner's
    measurement asks included; a backend that is off is refused before any reservation."""
    _, vstore, owner, ostore = guards(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    counter = w.Counter()
    ctx = context_with({"claude_mcp": SlowModel()}, ONE, counter)
    offs: tuple[frozenset[BackendName], ...] = (frozenset(), frozenset({"cortex_analyst"}))
    for live in offs:
        r = A.run_live(Q1, ctx=ctx, guard=owner, passcode=OWNER, session="s", backends=ONE,
                       owner=True, ledger=ledger, live=live)  # fmt: skip
        assert r.refused is not None and r.refused.startswith("Live compute is off for")
    assert ostore.reserved == [] and vstore.reserved == [] and not ledger.exists()
    assert counter.total == 0


def test_owner_mode_without_a_ledger_is_refused(tmp_path):
    _, _, owner, ostore = guards(tmp_path)
    with pytest.raises(ValueError, match="ledger"):
        A.run_live(Q1, ctx=quick_ctx(), guard=owner, passcode=OWNER, session="s", backends=ONE,
                   owner=True, live=ALL)  # fmt: skip
    assert ostore.reserved == []
