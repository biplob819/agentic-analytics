"""OWNER D-56, the page side (D4.2; DEM-06, DEM-08, DEM-09): own keys are entered on the live
form, accepted only after the visitor passcode, kept in session memory only, redacted wherever
the page shows text, forgettable, and reach no log, trace, export, cache, ledger or file; the
notice is plain; live mode on the synthetic cache, p95 page mode and the owner budget are
unchanged."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_analytics import tracing as T
from agentic_analytics.demo import ask as A
from agentic_analytics.demo import keys as K
from agentic_analytics.demo import p95 as M
from agentic_analytics.demo import page as P
from tests.demo.byok_fakes import (
    ANTHROPIC_KEY,
    OPENAI_KEY,
    OWNER,
    OWNER_ANTHROPIC_KEY,
    VISITOR,
    Factory,
    anthropic_errors,
    guards,
    keys,
    stores,
)
from tests.demo.helpers import FakeSt, fake_context
from tests.demo.test_keys import assert_no_key
from tests.demo.test_page import clean, deployed  # noqa: F401  (fixtures)
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"
LIVE = "Ask your own (passcode)"
ASK = "Ask both backends"
A_LABEL = P.KEY_LABELS["anthropic"]
O_LABEL = P.KEY_LABELS["openai"]


class SpyTracer:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, trace):
        self.sent.append(trace)
        return "https://trace.invalid/x"

    def flush(self) -> None:
        return None


def page_live(where: Path, monkeypatch, counter=None, backends=A.DEMO_BACKENDS, byok=True):
    ctx = fake_context(backends, counter or w.Counter())  # type: ignore[arg-type]
    ctx.services.tracer = SpyTracer()  # a configured tracer must still see nothing
    s = stores(where)
    visitor, owner, byok_guard = guards(s)
    live = P.Live(visitor, ctx, local_store=True, owner_guard=owner,
                  ledger=where / "ledger.jsonl",
                  byok_guard=byok_guard if byok else None)  # fmt: skip
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "shared_live", lambda env, settings: live)
    monkeypatch.setattr(P, "start_warm_up", lambda ex: None)
    return live, s


def use_factory(monkeypatch, factory) -> None:
    monkeypatch.setattr(K, "visitor_agent", factory)


def ask(passcode=VISITOR, **extra) -> dict:
    return {"Mode": LIVE, "Passcode": passcode, "Your question": Q1, ASK: True, **extra}


def page_text(st: FakeSt) -> str:
    return json.dumps([[n, list(map(str, a)), {k: str(v) for k, v in kw.items()}]
                       for n, a, kw in st.calls])  # fmt: skip


# ---- entry: after the passcode only ------------------------------------------------------------


def test_a_typed_key_is_kept_only_after_the_visitor_passcode(deployed, monkeypatch):  # noqa: F811
    counter = w.Counter()
    _, s = page_live(deployed, monkeypatch, counter)
    f = Factory()
    use_factory(monkeypatch, f)
    state: dict = {}
    st = FakeSt(ask("wrong", **{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    assert st.texts("error") == ["Wrong passcode."]
    assert P.KEYS_STATE not in state and P.PENDING_STATE not in state
    assert state[P.KEY_WIDGETS["anthropic"]] == ""  # the field was emptied on submit
    assert f.built == [] and counter.total == 0 and s.byok.reserved == []
    st = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    stored = state[P.KEYS_STATE]
    assert isinstance(stored, K.VisitorKeys) and stored.providers() == ("anthropic",)
    assert P.PENDING_STATE not in state and state[P.KEY_WIDGETS["anthropic"]] == ""
    assert f.built and s.visitor.reserved == [] and len(s.byok.reserved) == 1
    captions = st.texts("caption")
    assert any(c.startswith("Your own key paid about $") for c in captions)
    assert any(P.own_key_note("anthropic") == c for c in captions)
    st = FakeSt(ask(), state=state)  # a later ask reuses the session's key
    P.main(st)
    assert len(f.built) == 2 and len(s.byok.reserved) == 2 and s.visitor.reserved == []
    assert P.keys_line(stored) in st.texts("caption")


def test_the_owner_passcode_never_takes_a_typed_or_stored_key(deployed, monkeypatch):  # noqa: F811
    counter = w.Counter()
    _, s = page_live(deployed, monkeypatch, counter)
    f = Factory()
    use_factory(monkeypatch, f)
    state = {P.KEYS_STATE: keys()}
    passed: list = []
    real_run_live = P.run_live
    monkeypatch.setattr(
        P, "run_live", lambda *a, **kw: passed.append(kw) or real_run_live(*a, **kw)
    )
    st = FakeSt(ask(OWNER, **{A_LABEL: "sk-ant-" + "q" * 40}), state=state)
    P.main(st)
    assert passed and passed[0]["visitor_keys"] is None and passed[0]["owner"] is True
    assert f.built == [] and counter.model_steps > 0
    assert len(s.owner.reserved) == 1 and s.byok.reserved == [] and s.visitor.reserved == []
    captions = st.texts("caption")
    assert P.OWNER_NO_KEYS in captions and any(c.startswith(P.OWNER_MARKER) for c in captions)
    kept = state[P.KEYS_STATE].get("anthropic")
    assert kept is not None and kept.reveal() == ANTHROPIC_KEY  # the typed one was not kept


def test_a_malformed_key_is_refused_before_any_call(deployed, monkeypatch):  # noqa: F811
    counter = w.Counter()
    _, s = page_live(deployed, monkeypatch, counter)
    bad = "sk-ant-" + "a" * 20 + " PLANTEDbadKEYtail0123456789"
    state: dict = {}
    st = FakeSt(ask(**{A_LABEL: bad}), state=state)
    P.main(st)
    (err,) = st.texts("error")
    assert "space, a control character" in err and P.KEYS_STATE not in state
    assert counter.total == 0 and s.byok.reserved == [] and s.visitor.reserved == []
    assert_no_key(page_text(st), bad)


def test_the_deployments_own_key_cannot_be_typed_as_a_visitor_key(deployed, monkeypatch):  # noqa: F811
    counter = w.Counter()
    page_live(deployed, monkeypatch, counter)
    st = FakeSt(ask(**{A_LABEL: OWNER_ANTHROPIC_KEY}),
                secrets={"ANTHROPIC_API_KEY": OWNER_ANTHROPIC_KEY})  # fmt: skip
    P.main(st)
    assert any("cannot be used" in e for e in st.texts("error")) and counter.total == 0


def test_one_field_per_provider_serving_a_live_panel(deployed, monkeypatch):  # noqa: F811
    page_live(deployed, monkeypatch)
    st = FakeSt({"Mode": LIVE})
    P.main(st)
    fields = {a[0]: kw for a, kw in st.named("text_input")}
    assert A_LABEL in fields and O_LABEL not in fields  # the demo panels: Claude + Cortex
    assert fields[A_LABEL]["type"] == "password" and fields["Passcode"]["type"] == "password"
    assert P.key_providers(("claude_mcp", "gpt_mcp", "cortex_analyst"),
                           w.config(backends=("claude_mcp", "gpt_mcp"))) == [
        "anthropic", "openai"]  # fmt: skip
    page_live(deployed, monkeypatch, byok=False)  # no own-key limit: fields shown (OWNER D-60)
    st2 = FakeSt({"Mode": LIVE})
    P.main(st2)
    assert [a[0] for a, _ in st2.named("text_input")] == ["Passcode", A_LABEL]
    assert not any(c.startswith("Own-key questions") for c in st2.texts("caption"))
    f = Factory()  # ...but a typed key is not taken, and the ask runs on the demo's budget
    use_factory(monkeypatch, f)
    state: dict = {}
    st3 = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st3)
    assert f.built == [] and P.KEYS_STATE not in state and P.KEYS_NOT_USED in st3.texts("caption")


def test_both_keys_pay_for_their_own_panels(deployed, monkeypatch):  # noqa: F811
    three = ("claude_mcp", "gpt_mcp", "cortex_analyst")
    counter = w.Counter()
    _, s = page_live(deployed, monkeypatch, counter, backends=three)
    monkeypatch.setattr(P, "live_settings", lambda env, cache, **kw: P.LiveSettings(
        w.config(backends=three), None, three))  # type: ignore[arg-type]  # fmt: skip
    f = Factory()
    use_factory(monkeypatch, f)
    st = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY, O_LABEL: OPENAI_KEY}))
    P.main(st)
    assert sorted(p for _, _, p in f.built) == ["anthropic", "openai"]
    assert counter.model_steps == 0 and s.visitor.reserved == [] and len(s.byok.reserved) == 1
    captions = st.texts("caption")
    assert P.own_key_note("anthropic") in captions and P.own_key_note("openai") in captions


# ---- forget ------------------------------------------------------------------------------------


def test_forget_my_key_clears_it_from_the_session(deployed, monkeypatch):  # noqa: F811
    _, s = page_live(deployed, monkeypatch)
    f = Factory()
    use_factory(monkeypatch, f)
    vk = keys(ANTHROPIC_KEY)
    held = vk.get("anthropic")
    state = {P.KEYS_STATE: vk, P.KEY_WIDGETS["anthropic"]: "leftover"}
    st = FakeSt({"Mode": LIVE, P.FORGET_LABEL: True}, state=state)
    P.main(st)
    assert P.KEYS_STATE not in state and state[P.KEY_WIDGETS["anthropic"]] == ""
    assert held is not None and held.forgotten and not vk
    with pytest.raises(ValueError):
        held.reveal()
    st = FakeSt(ask(), state=state)  # the next run says so, and asks on the demo's budget
    P.main(st)
    assert P.KEYS_FORGOTTEN in st.texts("caption") and P.FORGOT_STATE not in state
    assert f.built == [] and len(s.visitor.reserved) == 1 and s.byok.reserved == []
    assert P.FORGET_LABEL not in [a[0] for a, _ in st.named("button")]


# ---- sinks: a planted key reaches none of them -------------------------------------------------


def test_a_planted_key_reaches_no_log_trace_export_cache_ledger_or_file(
    deployed,  # noqa: F811
    monkeypatch,
    caplog,
):
    """Every sink at once, with provider errors that quote the whole key."""
    caplog.set_level(logging.DEBUG)
    exported: list = []
    monkeypatch.setattr(T, "export_trace", lambda *a, **kw: exported.append(a))
    counter = w.Counter()
    live, _ = page_live(deployed, monkeypatch, counter)
    tracer = live.ctx.services.tracer
    assert isinstance(tracer, SpyTracer)
    before = {p for p in deployed.rglob("*") if p.is_file()}

    clients: list = []

    def fake_sdk_client(key):  # the real visitor_agent, on a client whose errors quote the key
        from tests.demo.byok_fakes import FakeClient

        client = FakeClient(anthropic_errors(key.reveal())["rejected"])
        clients.append(client)
        return client

    monkeypatch.setattr(K, "sdk_client", fake_sdk_client)
    state: dict = {}
    st = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    assert clients and clients[0].calls == 1
    shown = page_text(st)
    assert K.failure_text("anthropic", "rejected") in st.texts("error")  # the panel's error
    # logs, at every level, from every logger
    logged = "\n".join(r.getMessage() + str(r.exc_info or "") for r in caplog.records)
    assert_no_key(logged)
    assert caplog.records  # the aggregate line was logged
    # traces (Langfuse or any tracer) and failure-trace exports
    assert tracer.sent == [] and exported == []
    # files written during the ask: spend counters, ledger, caches
    for path in deployed.rglob("*"):
        if path.is_file():
            assert ANTHROPIC_KEY.encode() not in path.read_bytes(), path
    new_files = {p for p in deployed.rglob("*") if p.is_file()} - before
    assert not (deployed / "ledger.jsonl").exists()
    assert {p.name for p in new_files} <= {"byok.sqlite", "visitor.sqlite", "owner.sqlite"}
    # the page and the session's repr
    assert_no_key(shown)
    assert_no_key(repr(state) + str(state))
    # the answer a replay cache would store
    r = A.run_live(Q1, ctx=live.ctx, guard=live.guard, passcode=VISITOR, session="s2",
                   visitor_keys=keys(), byok_guard=live.byok_guard,
                   live=A.BACKEND_IDS)  # fmt: skip
    assert_no_key(r.model_dump_json() + repr(r) + json.dumps([p.model_dump(mode="json")
                                                              for p in r.panels]))  # fmt: skip
    assert counter.model_steps == 0  # never the owner's key


def test_the_page_redactor_knows_the_session_key(deployed, monkeypatch):  # noqa: F811
    """A panel text quoting the key (it never should) is shown with the session label, which
    only the session's redactor gives (the generic pattern says 'anthropic key')."""
    page_live(deployed, monkeypatch)
    f = Factory()

    def echoing(*args):
        agent = f(*args)
        from tests.demo.helpers import clarify_turn

        turn = clarify_turn()
        call = turn.tool_calls[0]
        leaked = {**(call.arguments or {}), "clarification": f"Is your key {ANTHROPIC_KEY}?"}
        agent.turns.append(replace(turn, tool_calls=(replace(call, arguments=leaked),)))
        return agent

    use_factory(monkeypatch, echoing)
    state: dict = {}
    st = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    shown = page_text(st)
    i = shown.find(ANTHROPIC_KEY)
    assert i < 0, shown[max(0, i - 300) : i + 50]
    assert "[REDACTED:VISITOR_ANTHROPIC_KEY" in shown.replace("\\", "")  # escape_md undone
    st = FakeSt(ask(), state=state)  # the next ask: the stored key, under the session label
    P.main(st)
    assert "[REDACTED:VISITOR_ANTHROPIC_KEY]" in page_text(st).replace("\\", "")


def test_redactor_with_secrets_adds_values_and_leaves_the_original_alone():
    base = T.Redactor({"DEMO_PASSCODE": "open-sesame-123"})
    plain = "plainvisitorsecret-0123456789"  # matches no generic pattern
    more = base.with_secrets({"VISITOR_OPENAI_KEY": plain})
    assert more.text(f"x {plain} y") == "x [REDACTED:VISITOR_OPENAI_KEY] y"
    assert more.text("open-sesame-123") == "[REDACTED:DEMO_PASSCODE]"
    assert base.text(plain) == plain
    assert plain not in repr(more) and "open-sesame" not in repr(base)


# ---- notice and fixed models -------------------------------------------------------------------


def test_the_notice_says_where_the_key_goes_and_who_pays(deployed, monkeypatch):  # noqa: F811
    page_live(deployed, monkeypatch)
    st = FakeSt({"Mode": LIVE})
    P.main(st)
    captions = st.texts("caption")
    assert P.BYOK_NOTICE in captions
    n = P.BYOK_NOTICE
    for phrase in ("Streamlit server", "for this session only", "is not stored",
                   "You pay your provider", "directly", "after the passcode",
                   "models and effort stay the benchmark's"):  # fmt: skip
        assert phrase in n, phrase
    assert any(c.startswith("Own-key questions: warehouse limit left today $5.00 of $5.00")
               for c in captions)  # fmt: skip
    # the only choice on the page is the mode: no model or effort picker
    assert [a[1] for a, _ in st.named("radio")] == [["Sample questions (replay)", LIVE]]
    assert st.named("selectbox") == []  # no model picker in live mode


def test_live_mode_is_still_refused_on_the_synthetic_cache_with_a_key_in_session(
    clean,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")  # even with live compute turned on
    monkeypatch.setenv("DEMO_PASSCODE", VISITOR)
    built: list = []
    monkeypatch.setattr(P, "build_live", lambda env, settings: built.append(settings))
    f = Factory()
    use_factory(monkeypatch, f)
    state = {P.KEYS_STATE: keys()}
    st = FakeSt(ask(**{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    # the passcode box and key field stay visible (OWNER D-60), but nothing runs
    assert st.texts("warning") == [P.LIVE_SYNTHETIC, P.LIVE_SYNTHETIC]  # opened, then submitted
    assert [a[0] for a, _ in st.named("text_input")] == ["Passcode", A_LABEL]
    assert built == [] and f.built == [] and st.named("subheader") == []
    assert P.KEYS_NOT_USED in st.texts("caption")


# ---- regressions: p95 page mode and the owner budget -------------------------------------------


def test_p95_page_mode_reads_an_owner_ask_unchanged_with_own_keys_on(deployed, monkeypatch):  # noqa: F811
    counter = w.Counter()
    _, s = page_live(deployed, monkeypatch, counter)
    use_factory(monkeypatch, Factory())
    st = FakeSt(ask(OWNER), state={P.KEYS_STATE: keys()})
    P.main(st)
    texts = [str(a[0]) for _, a, _ in st.calls if a and isinstance(a[0], str)]
    body = "\n".join(texts)
    assert P.OWNER_MARKER in body and "This question cost $" in body
    panels = M.panels_from_page_text(body, 2)
    assert panels is not None and len(panels) == 2 and all(p.ok for p in panels)
    assert len(s.owner.reserved) == 1 and s.byok.reserved == [] and s.visitor.reserved == []
    # the labels p95 page mode types into stay unique (Playwright matches substrings)
    labels = [a[0] for a, _ in st.named("text_input")] + [a[0] for a, _ in st.named("text_area")]
    assert sum("passcode" in x.lower() for x in labels) == 1
    assert sum("your question" in x.lower() for x in labels) == 1
    buttons = [a[0] for a, _ in st.named("form_submit_button")] + [
        a[0] for a, _ in st.named("button")]  # fmt: skip
    assert sum(ASK.lower() in b.lower() for b in buttons) == 1
    # no new page text is read as an ask's end, an error or a cost line
    new = [P.BYOK_NOTICE, P.OWNER_NO_KEYS, P.KEYS_FORGOTTEN, P.byok_budget_line(_G()),
           P.keys_line(keys(ANTHROPIC_KEY, OPENAI_KEY)), P.own_key_note("openai"),
           P.key_paid_line(A.LiveResult(key_usd=0.1, byok_usd=0.2)), *K.FAILURE_TEXTS]  # fmt: skip
    for text in new:
        assert not any(m in text for m in M.DONE_MARKERS), text
        assert not any(t in text for t in P.ERROR_TEXTS.values()), text
        assert not M.COST_RE.search(text) and M.TIMEOUT_TEXT not in text


class _G:
    daily_cap = 5.0
    rate_limit = 5

    def remaining_today(self):
        return 5.0


def test_the_owner_budget_and_p95_constants_are_unchanged():
    assert A.OWNER_CAP_USD == 15.0 and A.OWNER_CAP_MAX_USD == 25.0 and A.DAILY_CAP_USD == 3.0
    assert M.DEFAULT_MAX_COST_USD == 15.0 and M.OWNER_APPROVAL_USD == 25.0
    assert A.OWNER_SPEND_KEY == "agentic-analytics-owner" and A.SPEND_KEY == "agentic-analytics"
    assert A.make_owner_guard({"DEMO_PASSCODE": VISITOR, "DEMO_OWNER_PASSCODE": OWNER},
                              store=object()).daily_cap == 15.0  # type: ignore[union-attr]  # fmt: skip
    import inspect

    src = inspect.getsource(M)
    assert "visitor_keys" not in src and "byok" not in src.lower()  # p95 never takes a key
