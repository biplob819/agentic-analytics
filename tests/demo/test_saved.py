"""OWNER D-58, D-59, D-60 (D4.1, D4.2; DEM-10, DEM-11, DEM-12): saved answers by default.

No backend is live unless `DEMO_LIVE_BACKENDS` names it; a backend that is not live shows the
saved answer, builds no service, executor, guard or budget and connects to nothing; the page says
so near the top and on each panel with the owner-request link; the passcode box and the own-key
fields are always shown; live needs a valid passcode AND the backend live; own keys never replace
the passcode; the owner passcode and the p95 script respect the setting. No network, no keys."""

from __future__ import annotations

import pytest

from agentic_analytics.demo import ask as A
from agentic_analytics.schemas import BackendName
from tests.demo.byok_fakes import VISITOR
from tests.demo.helpers import SpyStore, fake_context
from tests.run import world as w

Q1 = "How many property sales were recorded in England and Wales in 2023?"
ISSUES = "https://github.com/biplob819/agentic-analytics/issues"


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
