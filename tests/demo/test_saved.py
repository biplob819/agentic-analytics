"""OWNER D-58, D-59, D-60 (D4.1, D4.2; DEM-10, DEM-11, DEM-12): saved answers by default.

No backend is live unless `DEMO_LIVE_BACKENDS` names it; a backend that is not live shows the
saved answer, builds no service, executor, guard or budget and connects to nothing; the page says
so near the top and on each panel with the owner-request link; the passcode box and the own-key
fields are always shown; live needs a valid passcode AND the backend live; own keys never replace
the passcode; the owner passcode and the p95 script respect the setting. No network, no keys."""

from __future__ import annotations

import json

import pytest

from agentic_analytics.demo import ask as A
from agentic_analytics.demo import p95 as M
from agentic_analytics.demo import page as P
from agentic_analytics.demo.cache import save_cache
from agentic_analytics.run import runner as R
from agentic_analytics.schemas import BackendName
from agentic_analytics.warehouse import doctor as D
from tests.demo.byok_fakes import ANTHROPIC_KEY, OWNER, VISITOR, Factory, guards, stores
from tests.demo.helpers import FakeSt, SpyStore, fake_context
from tests.demo.test_page import clean, deployed, real_cache, write_run_yaml  # noqa: F401
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"
LIVE = "Ask your own (passcode)"
A_LABEL = P.KEY_LABELS["anthropic"]
ISSUES = "https://github.com/biplob819/agentic-analytics/issues"
LINK = f"[Ask the owner to turn live compute on]({ISSUES})"


def ask(passcode=VISITOR, **extra) -> dict:
    return {"Mode": LIVE, "Passcode": passcode, "Your question": Q1, P.ASK_LABEL: True, **extra}


def page_text(st: FakeSt) -> str:
    return json.dumps([[n, list(map(str, a)), {k: str(v) for k, v in kw.items()}]
                       for n, a, kw in st.calls])  # fmt: skip


@pytest.fixture
def saved_only(clean, monkeypatch):  # noqa: F811
    """A real replay file and run.yaml, both passcodes set, and DEMO_LIVE_BACKENDS missing."""
    cache = real_cache()
    save_cache(clean / "demo" / "replay.json", cache)
    write_run_yaml(clean, cache)
    monkeypatch.setenv("DEMO_PASSCODE", VISITOR)
    monkeypatch.setenv("DEMO_OWNER_PASSCODE", OWNER)
    return clean


def tripwires(monkeypatch, keep: tuple[str, ...] = ()) -> list[str]:
    """Every way the page could build, connect or reserve: each records its name and fails."""
    hit: list[str] = []

    def trip(name):
        def boom(*a, **kw):
            hit.append(name)
            raise AssertionError(f"{name} called while live compute is off")

        return boom

    for mod, names in (
        (P, ("build_live", "shared_live", "live_settings", "live_problem", "make_guard",
             "make_owner_guard", "make_byok_guard", "start_warm_up", "run_live")),
        (A, ("context_from_env", "make_guard", "make_owner_guard", "make_byok_guard",
             "spend_store", "warm_up", "ask_panels")),
        (R, ("services_from_env",)),
        (D, ("executors_from_env",)),
    ):  # fmt: skip
        for n in names:
            if n in keep:
                continue
            monkeypatch.setattr(mod, n, trip(f"{mod.__name__}.{n}"))
    return hit


# ---- DEM-10: the setting -----------------------------------------------------------------------


@pytest.mark.safety
@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, set()),
        ("", set()),
        ("  ,  ", set()),
        ("cortex_analyst", {"cortex_analyst"}),
        (" Claude_MCP , cortex_analyst, ", {"claude_mcp", "cortex_analyst"}),
        ("all", {"claude_mcp", "gpt_mcp", "cortex_analyst"}),
        ("ALL", {"claude_mcp", "gpt_mcp", "cortex_analyst"}),
    ],
)
def test_live_backends_is_empty_unless_the_owner_names_them(raw, want):
    env = {} if raw is None else {"DEMO_LIVE_BACKENDS": raw}
    assert A.live_backends(env) == frozenset(want)


@pytest.mark.safety
@pytest.mark.parametrize("raw", ["cortex", "claude_mcp,bogus", "all,clickhouse", "yes", "true"])
def test_an_unknown_backend_name_is_an_error_never_live(raw):
    with pytest.raises(A.DemoSettingError) as exc:
        A.live_backends({"DEMO_LIVE_BACKENDS": raw})
    assert "DEMO_LIVE_BACKENDS names an unknown backend" in exc.value.reason
    assert "Live compute stays off" in exc.value.reason
    with pytest.raises(A.DemoSettingError) as planted:  # the reason is shown on the page
        A.live_backends({"DEMO_LIVE_BACKENDS": f"claude_mcp,{raw}-Zq7"})
    assert "zq7" not in planted.value.reason.lower()


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        (None, ISSUES),
        ("", ISSUES),
        ("https://github.com/owner/repo/issues/new?labels=live", None),
        ("http://github.com/owner/repo/issues", False),
        ("mailto:owner@example.com", False),
        ("https://example.com/contact?to=owner@example.com", False),
        ("https://user@example.com/x", False),
        ("javascript:alert(1)", False),
        ("https://localhost/x", False),
        ("https://example.com/a b", False),
        ("https://example.com/x)[evil](https://evil.example", False),
        ("ftp://example.com/x", False),
        # verifier round: control, invisible, bidi and lookalike characters; broken hosts, ports
        ("https://exa​mple.com/x", False),  # zero-width space
        ("https://example.com/‮gpj.exe", False),  # bidi override
        ("https://gіthub.com/x", False),  # Cyrillic i lookalike
        ("https://github.com∕evil.example/x", False),  # division slash lookalike
        ("https://example.com/\x00x", False),
        ("https://example.com/\x7fx", False),
        ("https://github.com%40evil.example/x", False),
        ("https://./x", False),
        ("https://a./x", False),
        ("https://example.com:abc/x", False),
        ("https://example.com:99999/x", False),
        ("https://xn--gthub-cta.example/x", None),  # an IDN host in its ASCII form is fine
        ("https://example.com:8443/contact#live", None),
    ],
)
def test_the_contact_url_is_https_and_never_an_email(raw, ok):
    env = {} if raw is None else {"DEMO_CONTACT_URL": raw}
    if ok is False:
        with pytest.raises(A.DemoSettingError, match="DEMO_CONTACT_URL must be an https URL"):
            A.contact_url(env)
    else:
        assert A.contact_url(env) == (ok or raw)
    assert A.DEFAULT_CONTACT_URL == ISSUES


def test_run_live_cannot_be_called_without_the_live_switch(tmp_path):
    """Verifier round 1: `live` is required, so no caller can fail open by leaving it out."""
    import inspect

    param = inspect.signature(A.run_live).parameters["live"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    guard = A.make_guard({"DEMO_PASSCODE": VISITOR}, store=SpyStore(tmp_path / "s.sqlite"))
    with pytest.raises(TypeError, match="live"):
        A.run_live(Q1, ctx=fake_context(), guard=guard, passcode=VISITOR,  # type: ignore[call-arg]
                   session="s")  # fmt: skip


def test_run_live_refuses_a_backend_that_is_not_live_before_anything(tmp_path):
    counter = w.Counter()
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    store = SpyStore(tmp_path / "s.sqlite")
    guard = A.make_guard({"DEMO_PASSCODE": VISITOR}, store=store)
    offs: tuple[frozenset[BackendName], ...] = (frozenset(), frozenset({"claude_mcp"}))
    for live in offs:
        r = A.run_live(Q1, ctx=ctx, guard=guard, passcode=VISITOR, session="s",
                       backends=A.DEMO_BACKENDS, live=live)  # fmt: skip
        assert r.refused is not None and r.refused.startswith("Live compute is off for")
        assert "cortex_analyst" in r.refused and r.panels == ()
    assert counter.total == 0 and store.reserved == []
    every = A.live_backends({"DEMO_LIVE_BACKENDS": "all"})
    ok = A.run_live(Q1, ctx=ctx, guard=guard, passcode=VISITOR, session="s",
                    backends=A.DEMO_BACKENDS, live=every)  # fmt: skip
    assert ok.refused is None and len(ok.panels) == 2 and len(store.reserved) == 1


# ---- DEM-11: saved answers, nothing built or connected -----------------------------------------


@pytest.mark.safety
def test_a_missing_setting_shows_saved_answers_and_builds_or_connects_nothing(
    saved_only, monkeypatch
):
    hit = tripwires(monkeypatch)
    st = FakeSt()
    P.main(st)
    info = st.texts("info")
    top = info[0]
    for phrase in ("saved answer from the benchmark run", "switched off to save compute",
                   "Snowflake and ClickHouse", LINK):  # fmt: skip
        assert phrase in top, phrase
    titles = st.texts("subheader")
    assert len(titles) == 2 and len(st.named("code")) == 2  # the saved SQL is shown
    assert P.saved_panel_note("claude_mcp", P.LiveSwitch()) in info
    assert P.saved_panel_note("cortex_analyst", P.LiveSwitch()) in info
    for b, engine in (("claude_mcp", "ClickHouse"), ("cortex_analyst", "Snowflake")):
        note = P.saved_panel_note(b, P.LiveSwitch())  # type: ignore[arg-type]
        assert note.startswith(P.SAVED_ANSWER) and f"Live querying on {engine} is switched off" \
            in note and LINK in note  # fmt: skip
    assert hit == []


@pytest.mark.safety
@pytest.mark.parametrize("passcode", [VISITOR, OWNER])
def test_a_valid_passcode_while_compute_is_off_says_so_and_connects_nothing(
    saved_only, monkeypatch, passcode
):
    hit = tripwires(monkeypatch)
    state: dict = {}
    st = FakeSt({"Mode": LIVE}, state=state)
    P.main(st)  # OWNER D-60: the passcode box and the key field are always shown
    assert [a[0] for a, _ in st.named("text_input")] == ["Passcode", A_LABEL]
    assert P.BYOK_NOTICE in st.texts("caption") and st.texts("error") == []
    st = FakeSt(ask(passcode, **{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    (warning,) = st.texts("warning")
    assert (
        warning.startswith("Live compute is off for claude_mcp, cortex_analyst") and LINK in warning
    )
    offs = [i for i in st.texts("info") if i.startswith("Live compute is off for")]
    assert offs == [P.compute_off_panel_text("claude_mcp", ISSUES),
                    P.compute_off_panel_text("cortex_analyst", ISSUES)]  # fmt: skip
    assert P.KEYS_NOT_USED in st.texts("caption") and P.KEYS_STATE not in state
    assert ANTHROPIC_KEY not in page_text(st) and st.texts("error") == []
    assert hit == []


@pytest.mark.safety
@pytest.mark.parametrize("passcode", ["", "wrong"])
def test_own_keys_never_replace_the_passcode_whether_compute_is_on_or_off(
    saved_only, monkeypatch, passcode
):
    hit = tripwires(monkeypatch)
    st = FakeSt(ask(passcode, **{A_LABEL: ANTHROPIC_KEY}))
    P.main(st)
    assert st.texts("error") == ["Wrong passcode."] and st.texts("warning") == [] and hit == []


@pytest.mark.safety
def test_a_blank_passcode_is_wrong_even_when_no_owner_passcode_is_set(saved_only, monkeypatch):
    """Verifier round: an unset secret must never match a blank typed passcode."""
    monkeypatch.delenv("DEMO_OWNER_PASSCODE")
    assert P.passcode_kind({"DEMO_PASSCODE": VISITOR}, "") is None
    assert P.passcode_kind({"DEMO_PASSCODE": VISITOR, "DEMO_OWNER_PASSCODE": "  "}, "") is None
    assert P.passcode_kind({}, "") is None
    hit = tripwires(monkeypatch)
    st = FakeSt(ask(""))
    P.main(st)
    assert st.texts("error") == ["Wrong passcode."] and st.texts("warning") == [] and hit == []


@pytest.mark.safety
@pytest.mark.parametrize("passcode", ["", "wrong"])
def test_own_keys_never_replace_the_passcode_with_compute_on(saved_only, monkeypatch, passcode):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")
    counter = w.Counter()
    s = stores(saved_only)
    visitor, owner, byok = guards(s)
    live = P.Live(visitor, fake_context(A.DEMO_BACKENDS, counter), local_store=True,
                  owner_guard=owner, ledger=saved_only / "l.jsonl", byok_guard=byok)  # fmt: skip
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "shared_live", lambda env, settings: live)
    f = Factory()
    monkeypatch.setattr(P, "start_warm_up", lambda ex: pytest.fail("warmed"))
    from agentic_analytics.demo import keys as K

    monkeypatch.setattr(K, "visitor_agent", f)
    state: dict = {}
    st = FakeSt(ask(passcode, **{A_LABEL: ANTHROPIC_KEY}), state=state)
    P.main(st)
    assert st.texts("error") == ["Wrong passcode."]
    assert f.built == [] and counter.total == 0 and P.KEYS_STATE not in state
    assert s.visitor.reserved == s.byok.reserved == s.owner.reserved == []


def test_an_unknown_setting_is_shown_to_the_owner_and_keeps_every_backend_off(
    saved_only, monkeypatch
):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "claude_mcp,cortex")
    hit = tripwires(monkeypatch)
    st = FakeSt(ask())
    P.main(st)
    (err,) = st.texts("error")
    assert err.startswith("DEMO_LIVE_BACKENDS names an unknown backend")
    assert "'cortex'" not in err  # the raw value is not echoed on the public page
    assert st.texts("warning")[0].startswith("Live compute is off for claude_mcp, cortex_analyst")
    assert hit == []


def test_a_bad_contact_url_is_reported_and_the_default_link_is_used(saved_only, monkeypatch):
    monkeypatch.setenv("DEMO_CONTACT_URL", "mailto:owner@example.com")
    st = FakeSt()
    P.main(st)
    assert any(e.startswith("DEMO_CONTACT_URL must be an https URL") for e in st.texts("error"))
    assert "owner@example.com" not in page_text(st) and LINK in st.texts("info")[0]
    monkeypatch.setenv("DEMO_CONTACT_URL", "https://example.org/ask-for-live")
    st = FakeSt()
    P.main(st)
    assert st.texts("error") == []
    assert (
        "[Ask the owner to turn live compute on](https://example.org/ask-for-live)"
        in (st.texts("info")[0])
    )


# ---- DEM-12: live only where turned on ---------------------------------------------------------


def _page_with(where, monkeypatch, counter):
    """Fake live services on BOTH backends, so asking a backend that is off would show up in
    the counter; `settings` records what the page asked to build."""
    s = stores(where)
    visitor, owner, byok = guards(s)
    live = P.Live(visitor, fake_context(A.DEMO_BACKENDS, counter), local_store=True,
                  owner_guard=owner, ledger=where / "ledger.jsonl", byok_guard=byok)  # fmt: skip
    built: list = []
    warmed: list = []
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "shared_live", lambda env, settings: built.append(settings) or live)
    monkeypatch.setattr(P, "start_warm_up", lambda ex: warmed.append(ex))
    return s, built, warmed


@pytest.mark.safety
@pytest.mark.parametrize("passcode", [VISITOR, OWNER])
def test_only_the_live_backends_are_built_and_asked(saved_only, monkeypatch, passcode):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "claude_mcp")
    counter = w.Counter()
    s, built, warmed = _page_with(saved_only, monkeypatch, counter)
    st = FakeSt(ask(passcode))
    P.main(st)
    assert [b.backends for b in built] == [("claude_mcp",)]
    assert built[0].config.backends == ("claude_mcp",)  # services only for the live backend
    assert counter.cortex_requests == 0 and counter.executions["snowflake"] == 0
    assert counter.model_steps > 0 and len(warmed) == 1
    reserved = s.owner.reserved if passcode == OWNER else s.visitor.reserved
    assert len(reserved) == 1
    titles = st.texts("subheader")
    assert (
        titles[0].startswith("Claude + ClickHouse MCP") and titles[1] == "Snowflake Cortex Analyst"
    )
    assert P.compute_off_panel_text("cortex_analyst", ISSUES) in st.texts("info")
    assert any(c.startswith("This question cost $") for c in st.texts("caption"))
    notice = st.texts("info")[0]
    assert "Snowflake Cortex Analyst (Snowflake) is switched off" in notice and LINK in notice


@pytest.mark.safety
def test_the_page_always_hands_the_live_switch_to_run_live(saved_only, monkeypatch):
    """Verifier round: defence in depth, `run_live` gets `live=` from DEMO_LIVE_BACKENDS on
    every page ask, so a backend that is not live is refused there too."""
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "claude_mcp")
    _page_with(saved_only, monkeypatch, w.Counter())
    seen: list = []
    real = P.run_live

    def spy(*a, **kw):
        seen.append(kw.get("live"))
        return real(*a, **kw)

    monkeypatch.setattr(P, "run_live", spy)
    P.main(FakeSt(ask()))
    assert seen == [frozenset({"claude_mcp"})]


def test_clickhouse_is_never_woken_when_only_cortex_is_live(saved_only, monkeypatch):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "cortex_analyst")
    counter = w.Counter()
    _, built, warmed = _page_with(saved_only, monkeypatch, counter)
    P.main(FakeSt(ask()))
    assert [b.backends for b in built] == [("cortex_analyst",)]
    assert counter.cortex_requests == 1 and counter.model_steps == 0
    assert counter.executions["clickhouse"] == 0 and warmed == []


def test_build_live_needs_no_model_key_for_a_backend_that_is_off(saved_only, monkeypatch):
    """The real services: with only Cortex live, no Anthropic key and no ClickHouse variable is
    needed; with every backend live the missing Anthropic key stops the build."""
    from tests.demo.test_deploy import pem_key

    env = {"DEMO_PASSCODE": VISITOR, "SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_RO_USER": "ro",
           "SNOWFLAKE_WAREHOUSE": "wh", "SNOWFLAKE_RO_PRIVATE_KEY": pem_key()}  # fmt: skip
    settings = P.live_settings(env, real_cache(), saved_only, live={"cortex_analyst"})
    assert isinstance(settings, P.LiveSettings) and settings.backends == ("cortex_analyst",)
    assert P.live_problem(env, settings.backends) is None
    live = P.build_live(env, settings)
    assert live.ctx.services.models == {} and live.ctx.services.cortex is not None
    every = P.live_settings(env, real_cache(), saved_only, live=A.BACKEND_IDS)
    assert isinstance(every, P.LiveSettings)
    with pytest.raises(R.RunConfigError, match=r"set (CLICKHOUSE_MCP|ANTHROPIC_API_KEY)"):
        P.build_live(env, every)
    assert P.live_settings(env, real_cache(), saved_only, live=()).startswith(  # type: ignore[union-attr]
        "Live compute is off"
    )


def test_replay_panels_of_a_live_backend_still_say_they_are_saved(saved_only, monkeypatch):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")
    st = FakeSt()
    P.main(st)
    info = st.texts("info")
    assert info.count(P.SAVED_ANSWER) == 2
    assert info[0].startswith("The sample answers are saved answers from the benchmark run")


def test_the_synthetic_example_stays_off_even_with_every_backend_live(clean, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")
    monkeypatch.setenv("DEMO_PASSCODE", VISITOR)
    hit = tripwires(monkeypatch, keep=("live_settings",))
    st = FakeSt(ask())
    P.main(st)
    assert st.texts("warning") == [P.LIVE_SYNTHETIC, P.LIVE_SYNTHETIC]
    assert st.named("subheader") == [] and hit == []


# ---- p95 needs its backends live ---------------------------------------------------------------


@pytest.mark.safety
def test_p95_refuses_clearly_unless_every_measured_backend_is_live(saved_only, monkeypatch, capsys):
    assert "claude_mcp, cortex_analyst not in DEMO_LIVE_BACKENDS" in (
        M.live_problem_for({}, A.DEMO_BACKENDS) or ""
    )
    assert "cortex_analyst not in" in (
        M.live_problem_for({"DEMO_LIVE_BACKENDS": "claude_mcp"}, A.DEMO_BACKENDS) or ""
    )
    assert M.live_problem_for({"DEMO_LIVE_BACKENDS": "all"}, A.DEMO_BACKENDS) is None
    assert "unknown backend" in (M.live_problem_for({"DEMO_LIVE_BACKENDS": "x"}, ()) or "")
    asked: list = []
    monkeypatch.setattr(A, "context_from_env", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(M, "page_asker", lambda *a, **k: asked.append(a))
    for mode in ("in-process", "page"):
        argv = ["--mode", mode, "--url", "https://demo.example/aa",
                "--cache", str(saved_only / "demo" / "replay.json"),
                "--config", str(saved_only / "run.yaml")]  # fmt: skip
        assert M.main(argv, environ={"DEMO_OWNER_PASSCODE": OWNER}) == 3
        assert "p95 needs every measured backend live" in capsys.readouterr().err
    assert asked == []
    # page mode: the refusal a switched-off page shows ends the wait; nothing shown before the
    # click does (the notices say "switched off", not the marker)
    assert A.compute_off_text(["cortex_analyst"]).startswith(M.COMPUTE_OFF_MARKER)
    assert M.COMPUTE_OFF_MARKER in M.DONE_MARKERS
    switch = P.LiveSwitch()
    before = [P.saved_notice(A.DEMO_BACKENDS, switch), P.LIVE_SWITCHED_OFF,
              P.saved_panel_note("claude_mcp", switch), P.KEYS_NOT_USED]  # fmt: skip
    for text in before:
        assert not any(m in text for m in M.DONE_MARKERS), text
