"""OWNER D-56, the ask side (D4.1 code; DEM-06, DEM-07): a visitor's own key is checked offline,
used only behind the visitor passcode on per-request clients and the benchmark's fixed models,
pays for its own panel's model calls (not the $3/day), while that ask's warehouse spend is held
against its own daily limit; a failing key fails only its panel and never falls back."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import os
import pickle
import socket

import anthropic
import openai
import pytest
from evalkit.demo.guard import GuardRefused, PostgresSpend, SqliteSpend

from agentic_analytics.demo import ask as A
from agentic_analytics.demo import keys as K
from tests.demo.byok_fakes import (
    ANTHROPIC_KEY,
    ENV,
    OPENAI_KEY,
    OWNER,
    OWNER_ANTHROPIC_KEY,
    VISITOR,
    Factory,
    anthropic_errors,
    failing_factory,
    guards,
    keys,
    openai_errors,
    stores,
)
from tests.demo.example_data import demo_world
from tests.demo.helpers import FakePg, SlowModel, fake_context
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"
ALL = A.BACKEND_IDS  # every backend live (OWNER D-60: run_live always gets the switch)
TWO = A.DEMO_BACKENDS  # claude_mcp, cortex_analyst
THREE = ("claude_mcp", "gpt_mcp", "cortex_analyst")


def _fragments(secret: str, n: int = 8) -> set[str]:
    body = secret.split("-", 2)[-1]
    return {body[i : i + n] for i in range(len(body) - n + 1)}


def assert_no_key(text: str, *secrets: str) -> None:
    for s in secrets or (ANTHROPIC_KEY, OPENAI_KEY):
        assert s not in text
        leaked = [f for f in _fragments(s) if f in text]
        assert not leaked, leaked[:3]


def bounds(ctx, backends):
    return [
        A.panel_bound_usd(b, config=ctx.config, prices=ctx.prices, rates=ctx.rates,
                          limits=ctx.limits)
        for b in backends
    ]  # fmt: skip


def wh_bound(ctx) -> float:
    return ctx.limits.timeout_s / 3600 * A._rate(ctx.rates, "clickhouse_usd_per_hour")


# ---- format checks, offline --------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("no network or client during a format check")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", refuse)
    monkeypatch.setattr(openai, "AsyncOpenAI", refuse)


BAD_KEYS = [
    ("anthropic", "", "Paste your Anthropic"),
    ("anthropic", "sk-ant-" + "a" * 20 + " " + "b" * 20, "space, a control character"),
    ("anthropic", "sk-ant-" + "a" * 20 + "\t" + "b" * 20, "space, a control character"),
    ("anthropic", "sk-ant-" + "a" * 20 + "\x00" + "b" * 20, "space, a control character"),
    ("anthropic", "sk-ant-" + "a" * 20 + "​" + "b" * 20, "space, a control character"),
    ("anthropic", "sk-ant-" + "a" * 20 + "é" + "b" * 20, "space, a control character"),
    ("anthropic", "sk-" + "a" * 50, "starts with sk-ant-"),
    ("anthropic", "pk-ant-" + "a" * 50, "starts with sk-ant-"),
    ("anthropic", "sk-ant-" + "a" * 32, "40 to 256 characters"),  # 39 characters
    ("anthropic", "sk-ant-" + "a" * 250, "40 to 256 characters"),  # 257 characters
    ("openai", "sk-ant-" + "a" * 50, "Anthropic key"),
    ("openai", "ak-" + "a" * 50, "starts with sk-"),
    ("openai", "sk-" + "a" * 36, "40 to 256 characters"),  # 39 characters
    ("openai", "sk-" + "a" * 20 + "\n" + "a" * 20, "space, a control character"),
]


@pytest.mark.parametrize(("provider", "raw", "why"), BAD_KEYS)
def test_bad_keys_are_refused_offline_and_the_message_names_no_part_of_the_key(
    no_network, provider, raw, why
):
    with pytest.raises(K.KeyFormatError) as info:
        K.check_key(provider, raw)
    assert why in info.value.reason and isinstance(info.value, ValueError)
    if raw:
        assert_no_key(str(info.value) + repr(info.value), raw)


def test_good_keys_pass_offline_at_the_length_boundaries(no_network):
    pairs: tuple[tuple[K.Provider, str], ...] = (("anthropic", "sk-ant-"), ("openai", "sk-"))
    for provider, prefix in pairs:
        for n in (K.KEY_MIN_CHARS, K.KEY_MAX_CHARS):
            raw = prefix + "x" * (n - len(prefix))
            assert K.check_key(provider, raw).reveal() == raw
    assert K.check_key("anthropic", ANTHROPIC_KEY).provider == "anthropic"
    assert K.check_key("openai", OPENAI_KEY).reveal() == OPENAI_KEY
    pasted = K.check_key("anthropic", f"  {ANTHROPIC_KEY}\r\n")  # paste edges are trimmed
    assert pasted.reveal() == ANTHROPIC_KEY


def test_the_deployments_own_key_is_refused_as_a_visitor_key(no_network):
    env = {"ANTHROPIC_API_KEY": OWNER_ANTHROPIC_KEY}
    with pytest.raises(K.KeyFormatError, match="cannot be used") as info:
        K.check_key("anthropic", OWNER_ANTHROPIC_KEY, owner_keys=env)
    assert_no_key(str(info.value), OWNER_ANTHROPIC_KEY)
    assert K.check_key("anthropic", ANTHROPIC_KEY, owner_keys=env).reveal() == ANTHROPIC_KEY
    got = K.accept_keys({"anthropic": ANTHROPIC_KEY, "openai": " "}, owner_keys=env)
    assert got.providers() == ("anthropic",)


# ---- the key object --------------------------------------------------------------------------


def test_key_objects_never_show_the_key_and_cannot_be_pickled():
    k = K.check_key("anthropic", ANTHROPIC_KEY)
    both = keys(ANTHROPIC_KEY, OPENAI_KEY)
    model = K.KeyedModel(K.visitor_agent("claude-sonnet-5-5", "medium", fake_context().prices,
                                         2048, k, client=object()), "anthropic")  # fmt: skip
    for obj in (k, both, model):
        shown = repr(obj) + str(obj) + f"{obj}" + format(obj, "")
        assert_no_key(shown)
        with pytest.raises(TypeError, match="pickle"):
            pickle.dumps(obj)
    assert repr(both) == "VisitorKeys(anthropic=set, openai=set)"
    with pytest.raises((TypeError, AttributeError)):
        vars(k)
    assert copy.copy(k) is k and copy.deepcopy(k) is k
    assert both.secrets() == {
        "VISITOR_ANTHROPIC_KEY": ANTHROPIC_KEY,
        "VISITOR_OPENAI_KEY": OPENAI_KEY,
    }
    both.forget()
    assert not both and both.providers() == () and both.secrets() == {}
    k2 = K.check_key("openai", OPENAI_KEY)
    k2.forget()
    with pytest.raises(ValueError, match="forgotten"):
        k2.reveal()
    assert repr(k2) == "VisitorKey(openai, forgotten)"


def test_merged_keys_replace_per_provider():
    old = keys(ANTHROPIC_KEY)
    newer = keys(None, OPENAI_KEY)
    m = old.merged(newer)
    assert m.providers() == ("anthropic", "openai")
    replaced = m.merged(keys("sk-ant-" + "z" * 40))
    anthropic_key = replaced.get("anthropic")
    assert anthropic_key is not None and anthropic_key.reveal() == "sk-ant-" + "z" * 40


# ---- clients ---------------------------------------------------------------------------------


def test_sdk_clients_are_new_per_request_on_the_visitor_key_with_no_retries(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", OWNER_ANTHROPIC_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    before = dict(os.environ)
    a = K.check_key("anthropic", ANTHROPIC_KEY)
    o = K.check_key("openai", OPENAI_KEY)
    c1, c2 = K.sdk_client(a), K.sdk_client(a)
    assert isinstance(c1, anthropic.AsyncAnthropic) and c1 is not c2
    assert c1.api_key == ANTHROPIC_KEY and c1.max_retries == 0  # never the owner's env key
    co = K.sdk_client(o)
    assert isinstance(co, openai.AsyncOpenAI) and co.api_key == OPENAI_KEY and co.max_retries == 0
    assert dict(os.environ) == before
    assert not any(ANTHROPIC_KEY in v or OPENAI_KEY in v for v in os.environ.values())
    agent = K.visitor_agent("claude-sonnet-5-5", "medium", fake_context().prices, 2048, a)
    assert agent.client.api_key == ANTHROPIC_KEY and agent.client.max_retries == 0
    assert agent._retry == K.VISITOR_RETRY == {"attempts": 2, "cap": 1.0}


def test_a_key_pays_only_for_its_own_providers_model():
    prices = fake_context().prices
    a = K.check_key("anthropic", ANTHROPIC_KEY)
    o = K.check_key("openai", OPENAI_KEY)
    with pytest.raises(ValueError, match="cannot pay"):
        K.visitor_agent("gpt-6.1-sol", "medium", prices, 2048, a, client=object())
    with pytest.raises(ValueError, match="cannot pay"):
        K.visitor_agent("claude-sonnet-5-5", "medium", prices, 2048, o, client=object())
    ctx = fake_context(THREE, w.Counter())  # type: ignore[arg-type]
    assert A.keyed_backends(THREE, ctx.config, keys(ANTHROPIC_KEY)) == ("claude_mcp",)
    assert A.keyed_backends(THREE, ctx.config, keys(None, OPENAI_KEY)) == ("gpt_mcp",)
    assert A.keyed_backends(THREE, ctx.config, keys(ANTHROPIC_KEY, OPENAI_KEY)) == (
        "claude_mcp",
        "gpt_mcp",
    )
    assert A.keyed_backends(THREE, ctx.config, None) == ()


def test_the_visitor_cannot_pick_the_model_or_effort(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(THREE, w.Counter())  # type: ignore[arg-type]
    f = Factory()
    A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=THREE,
               visitor_keys=keys(ANTHROPIC_KEY, OPENAI_KEY), byok_guard=byok,
               agent_factory=f, live=ALL)  # type: ignore[arg-type]  # fmt: skip
    assert sorted(f.built) == sorted(
        [(ctx.config.models["claude_mcp"], ctx.config.effort, "anthropic"),
         (ctx.config.models["gpt_mcp"], ctx.config.effort, "openai")]
    )  # fmt: skip
    for fn in (A.run_live, A.ask_panels):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"model", "models", "effort", "config"}


def test_each_ask_builds_its_own_agents(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    f = Factory()
    vk = keys()
    for _ in range(2):
        A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=vk, byok_guard=byok, agent_factory=f, live=ALL)  # fmt: skip
    assert len(f.agents) == 2 and f.agents[0] is not f.agents[1]
    assert all(a.steps == 1 for a in f.agents)


# ---- spend: model on the visitor, warehouse on its own limit --------------------------------


def test_a_keyed_panel_is_not_held_against_the_visitor_budget(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    counter = w.Counter()
    ctx = fake_context(TWO, counter)
    f = Factory()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok, agent_factory=f, live=ALL)  # fmt: skip
    assert r.refused is None and r.keyed == ("claude_mcp",) and len(r.panels) == 2
    assert s.visitor.reserved == [] and s.visitor.settled == []
    agent_b, cortex_b = bounds(ctx, TWO)
    (held,) = s.byok.reserved
    assert held == pytest.approx(wh_bound(ctx) + cortex_b, rel=1e-12)
    assert held < agent_b  # the model part is held nowhere
    ((_, actual),) = s.byok.settled
    agent, cortex = r.panels
    assert actual == pytest.approx(cortex.cost_usd + (agent.cost_usd - 0.001), abs=1e-9)
    assert r.byok_usd == pytest.approx(actual) and r.key_usd == pytest.approx(0.001)
    assert counter.model_steps == 0  # the deployment's Claude agent was never called
    assert f.agents[0].steps == 1 and agent.ok and cortex.ok


def replaying_factory(counter: w.Counter):
    """An `agent_factory` whose keyed agent replays the fake world's recorded Claude turns, so
    the keyed panel really queries ClickHouse (the fake bills 100 ms per query)."""
    scripts = demo_world().recording()["agents"]["claude_mcp"]

    def build(model, effort, prices, max_tokens, key):
        return w.ReplayModel("claude_mcp", scripts, counter)

    return build


def test_a_keyed_panels_clickhouse_compute_settles_on_the_own_key_limit(tmp_path):
    """Verifier round 1: the own-key counter settles to the Cortex panel's charge plus the
    keyed panel's measured ClickHouse compute (not 0), and the key pays only the model part."""
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    keyed_counter = w.Counter()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok,
                   agent_factory=replaying_factory(keyed_counter), live=ALL)  # fmt: skip
    agent, cortex = r.panels
    assert agent.ok and agent.answer.sql and keyed_counter.model_steps > 0
    ((_, actual),) = s.byok.settled
    warehouse = actual - cortex.cost_usd
    assert 0 < warehouse < wh_bound(ctx)  # measured, not the timeout bound and not free
    assert r.key_usd == pytest.approx(agent.cost_usd - warehouse, abs=1e-12)
    assert s.visitor.reserved == []


def test_a_keyed_panel_that_times_out_holds_its_whole_clickhouse_part(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    ctx.limits = dataclasses.replace(ctx.limits, timeout_s=0.5)

    def hung(*a):
        return SlowModel(delay=5.0)

    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok, agent_factory=hung,
                   live=ALL)  # fmt: skip
    agent, cortex = r.panels
    assert agent.timed_out and cortex.ok
    ((_, actual),) = s.byok.settled
    assert actual == pytest.approx(cortex.cost_usd + wh_bound(ctx), rel=1e-12)


def test_a_failed_keyed_panel_holds_its_whole_clickhouse_part(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    error = anthropic_errors()["rejected"]
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok,
                   agent_factory=failing_factory([], error), live=ALL)  # fmt: skip
    failed, cortex = r.panels
    assert not failed.ok and cortex.ok
    ((_, actual),) = s.byok.settled
    assert actual == pytest.approx(cortex.cost_usd + wh_bound(ctx), rel=1e-12)


def test_an_agent_panel_without_a_key_keeps_the_visitor_budget(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    counter = w.Counter()
    ctx = fake_context(THREE, counter)  # type: ignore[arg-type]
    _, gpt_b, cortex_b = bounds(ctx, THREE)
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=THREE,
                   visitor_keys=keys(ANTHROPIC_KEY), byok_guard=byok,
                   agent_factory=Factory(), live=ALL)  # type: ignore[arg-type]  # fmt: skip
    assert r.refused is None and r.keyed == ("claude_mcp",)
    assert s.visitor.reserved == [pytest.approx(gpt_b, rel=1e-12)]
    assert s.byok.reserved == [pytest.approx(wh_bound(ctx) + cortex_b, rel=1e-12)]
    ((_, v_actual),) = s.visitor.settled
    assert v_actual == pytest.approx(r.panels[1].cost_usd)
    assert counter.model_steps > 0  # the GPT panel ran on the deployment's agent


def test_a_key_no_panel_uses_leaves_the_ask_unchanged(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    f = Factory()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(None, OPENAI_KEY), byok_guard=byok,
                   agent_factory=f, live=ALL)  # fmt: skip
    assert r.keyed == () and f.built == [] and s.byok.reserved == []
    assert s.visitor.reserved == [pytest.approx(sum(bounds(ctx, TWO)), rel=1e-12)]


def test_the_own_key_limit_has_its_own_counter_cap_and_rate_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g = A.make_byok_guard(ENV)
    assert isinstance(g.spend, SqliteSpend) and g.spend.path.name == A.DEFAULT_BYOK_SQLITE.name
    assert A.BYOK_SPEND_KEY == "agentic-analytics-byok"
    assert len({A.SPEND_KEY, A.OWNER_SPEND_KEY, A.BYOK_SPEND_KEY}) == 3
    assert g.daily_cap == A.BYOK_DAILY_CAP_USD == 5.0
    assert g.per_request_cap == A.PER_REQUEST_CAP_USD and g.rate_limit == A.RATE_LIMIT == 5
    g.check_passcode(VISITOR)
    with pytest.raises(GuardRefused):
        g.check_passcode(OWNER)
    db = FakePg()
    env = {"EVALKIT_DEMO_DATABASE_URL": "postgresql://demo"}
    b = A.spend_store(env, key=A.BYOK_SPEND_KEY, connect=db.connect)
    v = A.spend_store(env, connect=db.connect)
    assert isinstance(b, PostgresSpend)
    assert b.reserve("2026-11-16", 0.25, 5.0) and v.reserve("2026-11-16", 0.5, 3.0)
    assert float(db.schemas[A.BYOK_SPEND_KEY]["2026-11-16"]) == 0.25
    assert float(db.schemas[A.SPEND_KEY]["2026-11-16"]) == 0.5


def test_a_used_up_own_key_limit_refuses_before_any_call(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    counter = w.Counter()
    ctx = fake_context(TWO, counter)
    byok.spend.reserve(byok._today(), A.BYOK_DAILY_CAP_USD - 0.01, A.BYOK_DAILY_CAP_USD)
    f = Factory()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok, agent_factory=f, live=ALL)  # fmt: skip
    assert r.refused is not None and "used up" in r.refused
    assert f.built == [] and counter.total == 0 and s.visitor.reserved == []


def test_own_key_asks_keep_the_per_session_rate_limit(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    vk = keys()
    for i in range(A.RATE_LIMIT):
        r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="one", live=ALL,
                       visitor_keys=vk, byok_guard=byok, agent_factory=Factory())  # fmt: skip
        assert r.refused is None, i
    f = Factory()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="one",
                   visitor_keys=vk, byok_guard=byok, agent_factory=f, live=ALL)  # fmt: skip
    assert r.refused is not None and r.refused.startswith("Rate limit") and f.built == []


def test_a_refused_visitor_hold_releases_the_warehouse_hold(tmp_path):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(THREE, w.Counter())  # type: ignore[arg-type]
    visitor.spend.reserve(visitor._today(), A.DAILY_CAP_USD, A.DAILY_CAP_USD)  # day used up
    f = Factory()
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=THREE,
                   visitor_keys=keys(ANTHROPIC_KEY), byok_guard=byok,
                   agent_factory=f, live=ALL)  # type: ignore[arg-type]  # fmt: skip
    assert r.refused is not None and "used up" in r.refused and f.built == []
    ((reserved, actual),) = s.byok.settled
    assert reserved > 0 and actual == 0.0 and byok.spend.spent(byok._today()) == 0.0


def test_an_exception_mid_ask_keeps_both_holds_charged(tmp_path, monkeypatch):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(THREE, w.Counter())  # type: ignore[arg-type]

    def boom(*a, **kw):
        raise RuntimeError("panel thread exploded")

    monkeypatch.setattr(A, "run_panels", boom)
    with pytest.raises(RuntimeError):
        A.ask_panels(Q1, THREE, "live", ctx=ctx, guard=visitor, passcode=VISITOR, session="s",
                     visitor_keys=keys(), byok_guard=byok,
                     agent_factory=Factory())  # type: ignore[arg-type]  # fmt: skip
    ((b_res, b_act),) = s.byok.settled
    ((v_res, v_act),) = s.visitor.settled
    assert b_act == b_res > 0 and v_act == v_res > 0


def test_a_keyed_panel_is_still_capped_at_its_model_bound(tmp_path):
    """BudgetedModel wraps the visitor's agent too: its spend per ask is bounded."""
    from agentic_analytics.models import ModelTurn, Usage

    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    huge = ModelTurn(text="let me think", tool_calls=(), stop="end",
                     usage=Usage(tokens_in=5_000_000, tokens_out=10), cost_usd=0.01,
                     latency_ms=1, model_snapshot="claude-sonnet-5-5")  # fmt: skip
    f = Factory()

    def factory(*args):
        agent = f(*args)
        agent.turns.append(huge)
        return agent

    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s", backends=TWO,
                   visitor_keys=keys(), byok_guard=byok, agent_factory=factory,
                   live=ALL)  # fmt: skip
    agent = r.panels[0]
    assert agent.capped and agent.answer.error == "step_budget"
    assert f.agents[0].steps == 1  # the second turn was never sent on the visitor's key
    model_bound = A.model_bound_usd("claude_mcp", config=ctx.config, prices=ctx.prices,
                                    limits=ctx.limits)  # fmt: skip
    assert bounds(ctx, TWO)[0] - wh_bound(ctx) == pytest.approx(model_bound)


# ---- the passcode first; owner mode never takes a key ----------------------------------------


def test_a_wrong_passcode_takes_no_key_and_builds_no_client(tmp_path, monkeypatch):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    counter = w.Counter()
    ctx = fake_context(TWO, counter)

    def no_client(*a, **kw):
        raise AssertionError("no client before the passcode")

    monkeypatch.setattr(K, "sdk_client", no_client)
    monkeypatch.setattr(K, "visitor_agent", no_client)
    for typed in ("wrong", "", OWNER):
        with pytest.raises(GuardRefused, match="Wrong passcode"):
            A.ask_panels(Q1, TWO, "live", ctx=ctx, guard=visitor, passcode=typed, session="s",
                         visitor_keys=keys(), byok_guard=byok)  # fmt: skip
    assert counter.total == 0 and s.byok.reserved == [] and s.visitor.reserved == []


def test_a_visitor_key_needs_the_guard_and_never_meets_the_ledger_or_a_trace(tmp_path):
    s = stores(tmp_path)
    visitor, owner, byok = guards(s)
    ctx = fake_context(TWO, w.Counter())
    vk = keys()
    with pytest.raises(ValueError, match="behind the passcode"):
        A.ask_panels(Q1, TWO, "live", ctx=ctx, visitor_keys=vk, byok_guard=byok)
    with pytest.raises(ValueError, match="behind the passcode"):
        A.ask_panels(Q1, TWO, "live", ctx=ctx, guard=visitor, passcode=VISITOR, session="s",
                     visitor_keys=vk)  # fmt: skip
    with pytest.raises(ValueError, match="owner mode"):
        A.ask_panels(Q1, TWO, "live", ctx=ctx, guard=owner, passcode=OWNER, session="s",
                     visitor_keys=vk, byok_guard=byok, ledger=tmp_path / "l.jsonl")  # fmt: skip
    with pytest.raises(ValueError, match="never traced"):
        A.ask_panels(Q1, TWO, "live", ctx=ctx, guard=visitor, passcode=VISITOR, session="s",
                     visitor_keys=vk, byok_guard=byok, trace_question_id="q1")  # fmt: skip
    assert s.byok.reserved == [] and s.visitor.reserved == [] and s.owner.reserved == []


def test_owner_mode_never_uses_a_visitor_key(tmp_path):
    s = stores(tmp_path)
    visitor, owner, byok = guards(s)
    counter = w.Counter()
    ctx = fake_context(TWO, counter)
    f = Factory()
    ledger = tmp_path / "ledger.jsonl"
    guard, is_owner = A.pick_guard(OWNER, visitor, owner)
    r = A.run_live(Q1, ctx=ctx, guard=guard, passcode=OWNER, session="s", backends=TWO,
                   owner=is_owner, ledger=ledger, visitor_keys=keys(), byok_guard=byok,
                   agent_factory=f, live=ALL)  # fmt: skip
    assert r.owner and r.keyed == () and f.built == []
    assert counter.model_steps > 0  # the deployment's agent
    assert len(s.owner.reserved) == 1 and s.byok.reserved == [] and s.visitor.reserved == []
    assert ledger.exists()


# ---- a failing key fails its panel, never falls back ---------------------------------------


CASES = [("anthropic", kind, exc) for kind, exc in anthropic_errors().items()] + [
    ("openai", kind, exc) for kind, exc in openai_errors().items()
]


@pytest.mark.parametrize(
    ("provider", "kind", "error"), CASES, ids=[f"{p}-{k}" for p, k, _ in CASES]
)
def test_a_failing_key_fails_its_panel_with_a_clear_secret_free_message(
    tmp_path, provider, kind, error
):
    s = stores(tmp_path)
    visitor, _, byok = guards(s)
    counter = w.Counter()
    backend = "claude_mcp" if provider == "anthropic" else "gpt_mcp"
    backends = (backend, "cortex_analyst")
    ctx = fake_context(backends, counter)  # type: ignore[arg-type]
    vk = keys(ANTHROPIC_KEY, OPENAI_KEY)
    clients: list = []
    r = A.run_live(Q1, ctx=ctx, guard=visitor, passcode=VISITOR, session="s",
                   backends=backends, visitor_keys=vk, byok_guard=byok,
                   agent_factory=failing_factory(clients, error), live=ALL)  # type: ignore[arg-type]  # fmt: skip
    failed, cortex = r.panels
    assert failed.answer.error == "transport" and not failed.ok
    assert failed.note == K.failure_text(provider, kind)
    assert failed.note is not None and K.NOT_USED in failed.note
    assert failed.answer.answer == f"No answer: {failed.note}"
    assert cortex.ok  # only that panel fails
    assert counter.model_steps == 0  # never the deployment's agent (no fallback)
    expected_calls = 2 if K.classify_failure(error) in ("rate_limited",) else 1
    if kind == "failed" and isinstance(error, RuntimeError | ValueError):
        expected_calls = 1
    assert clients[0].calls == expected_calls  # one quick retry, only for transient errors
    assert s.visitor.reserved == []
    assert_no_key(r.model_dump_json())


def test_classify_failure_reads_types_and_status_only():
    for provider, errors in (("anthropic", anthropic_errors()), ("openai", openai_errors())):
        for kind, exc in errors.items():
            assert K.classify_failure(exc) == kind, (provider, kind)
    from agentic_analytics.models.errors import QuotaExhausted

    assert K.classify_failure(QuotaExhausted("x")) == "quota"
    assert K.classify_failure(PermissionError()) == "failed"
    texts = {K.failure_text(p, k) for p in K.PROVIDERS
             for k in ("rejected", "quota", "rate_limited", "model", "failed")}  # fmt: skip
    assert texts == K.FAILURE_TEXTS and len(texts) == 10


def test_keyed_model_errors_carry_no_cause_context_or_key():
    import asyncio
    import traceback

    for error in (*anthropic_errors().values(), *openai_errors().values()):
        inner = _Raising(error)
        m = K.KeyedModel(inner, "anthropic")
        with pytest.raises(K.VisitorKeyFailed) as info:
            asyncio.run(m.step(None))
        exc = info.value
        assert exc.__cause__ is None and exc.__context__ is None
        text = "".join(traceback.format_exception(exc)) + repr(exc)
        assert_no_key(text)
        from agentic_analytics.models.errors import classify_error

        assert classify_error(exc) == "config"  # the agent loop stops the panel, no retry


class _Raising:
    provider = "anthropic"
    model = "claude-sonnet-5-5"
    effort = "medium"
    max_tokens = 2048

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def step(self, conv):
        raise self.error
