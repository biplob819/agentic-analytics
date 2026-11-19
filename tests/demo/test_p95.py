"""DEM-05 / NFR2: the p95 measurement script (`agentic_analytics.demo.p95`) without a network:
the spend cap, what the measurement file records, and how page mode reads the page."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agentic_analytics.cost import credit_rates, model_prices
from agentic_analytics.demo import ask as A
from agentic_analytics.demo import p95 as M
from agentic_analytics.demo.cache import load_example_cache, save_cache
from tests.run import world as w

CFG = w.config(backends=A.DEMO_BACKENDS)


def _hold() -> float:
    return A.request_bound_usd(
        A.DEMO_BACKENDS, config=CFG, prices=model_prices(), rates=credit_rates(),
        limits=A.DemoLimits(),
    )  # fmt: skip


def test_the_default_cap_is_well_under_owner_approval_and_fits_20_asks():
    assert M.DEFAULT_MAX_COST_USD < M.OWNER_APPROVAL_USD
    assert M.worst_case_usd(20, _hold()) <= M.DEFAULT_MAX_COST_USD
    assert M.check_cap(20, _hold(), M.DEFAULT_MAX_COST_USD) is None


def test_a_worst_case_over_the_cap_refuses_before_any_ask():
    asked: list[str] = []

    def ask_one(q: str) -> list[M.PagePanel]:
        asked.append(q)
        return []

    with pytest.raises(ValueError, match="over --max-cost"):
        M.run(mode="in-process", config=CFG, questions=["q"], ask_one=ask_one,
              target="host", commit="abc", n=20, max_cost_usd=5.0)  # fmt: skip
    assert asked == []
    assert "owner approval" in (M.check_cap(1, 0.1, 30.0) or "")
    assert M.check_cap(1, 0.1, 0.0)


def _owner_paid(panels: list[M.PagePanel], owner: bool = True) -> M.TimedPanels:
    out = M.TimedPanels(panels)
    out.owner_budget = owner  # the page said the owner measurement budget paid (OWNER D-53)
    return out


def test_the_record_names_mode_target_backends_commit_and_n():
    t = iter([datetime(2026, 11, 15, 12, 0, tzinfo=UTC), datetime(2026, 11, 15, 12, 5, tzinfo=UTC)])
    rec = M.run(
        mode="page", config=CFG, questions=["a", "b"], target="https://demo.example/aa",
        ask_one=lambda q: _owner_paid([M.PagePanel(cost_usd=0.1), M.PagePanel(cost_usd=0.1)]),
        commit="0123abc", n=20, now=lambda: next(t),
    )  # fmt: skip
    assert rec["mode"] == "page" and rec["measures"].endswith("(DEM-05)")
    assert rec["target"] == "https://demo.example/aa" and rec["commit"] == "0123abc"
    assert rec["backends"] == list(A.DEMO_BACKENDS) and rec["n_requested"] == rec["n"] == 20
    assert rec["models"] == {"claude_mcp": CFG.models["claude_mcp"]}
    assert rec["as_of"] == CFG.as_of.isoformat() and rec["passed"] is True
    assert rec["spent_usd"] == pytest.approx(4.0) and rec["max_cost_usd"] == 15.0
    assert rec["hold_usd"] == pytest.approx(_hold(), rel=1e-6)
    json.dumps(rec)
    inproc = M.run(mode="in-process", config=CFG, questions=["a"], target="box",
                   ask_one=lambda q: [M.PagePanel()], commit="x", n=20)  # fmt: skip
    assert "not DEM-05" in inproc["measures"]


def test_the_run_stops_at_the_cap_while_running():
    rec = M.run(mode="in-process", config=CFG, questions=["a"], target="box", commit="x",
                ask_one=lambda q: [M.PagePanel(cost_usd=2.0)], n=5, max_cost_usd=5.0,
                hold_usd=1.0)  # fmt: skip
    assert rec["n"] == 3 and "spend cap" in rec["stopped"] and rec["passed"] is False


def test_page_text_is_read_into_panels():
    from agentic_analytics.demo.page import ERROR_TEXTS

    ok = "Claude ... Snowflake ... This question cost $0.2512 (held at most $0.74)."
    panels = M.panels_from_page_text(ok, 2)
    assert panels is not None and len(panels) == 2 and all(p.ok for p in panels)
    assert sum(p.cost_usd for p in panels) == pytest.approx(0.2512)
    late = "Timed out after 25 s; no answer from this backend. " + ok
    assert [p.timed_out for p in M.panels_from_page_text(late, 2) or []] == [True, False]
    bad = ERROR_TEXTS["transport"] + " " + ok
    assert [p.ok for p in M.panels_from_page_text(bad, 2) or []] == [False, True]
    assert M.panels_from_page_text("Wrong passcode.", 2) is None


def test_a_page_ask_is_timed_from_submit_not_from_page_load():
    from agentic_analytics.demo.page import measure_p95

    def ask_one(q):
        out = M.TimedPanels([M.PagePanel()])
        out.elapsed_s = 2.5
        return out

    ticks = iter(range(0, 10_000, 40))  # 40 s per call including page load
    r = measure_p95(ask_one, ["a"], clock=lambda: float(next(ticks)))
    assert r.p95_ms == 2_500 and r.passed


class _FakePlaywright:
    """A stand-in for `playwright.sync_api` (no browser, no network): every page step moves a
    fake clock by a set amount and is logged, so the measured time can be checked exactly."""

    LOAD_S, FILL_S, ANSWER_S = 40.0, 1.0, 2.5

    def __init__(self, text: str) -> None:
        self.now = 0.0
        self.log: list[str] = []
        self.text = text
        self.closed: list[str] = []
        self.fail_on: str | None = None

    # playwright.sync_api
    def sync_playwright(self):
        return self

    def start(self):
        return self

    @property
    def chromium(self):
        return self

    def launch(self):
        return self

    def new_context(self):
        return self

    def new_page(self):
        return self

    def close(self):
        self.closed.append("context-or-browser")

    def stop(self):
        self.closed.append("playwright")

    # page
    def goto(self, url, **kw):
        self.log.append(f"goto {url}")
        self.now += self.LOAD_S

    def get_by_text(self, text):
        return _Locator(self, f"text {text}")

    def get_by_label(self, label, exact=False):
        return _Locator(self, f"label {label}" + (" exact" if exact else ""))

    def get_by_role(self, role, name):
        return _Locator(self, f"{role} {name}")

    def wait_for_function(self, js, **kw):
        self.log.append("wait")
        self.now += self.ANSWER_S

    def inner_text(self, selector):
        return self.text


class _Locator:
    def __init__(self, pw: _FakePlaywright, what: str) -> None:
        self.pw, self.what = pw, what

    def click(self):
        self.pw.log.append(f"click {self.what}")

    def fill(self, value):
        if self.pw.fail_on and self.pw.fail_on in self.what:  # Playwright logs what it typed
            raise TimeoutError(f'Locator.fill: Timeout exceeded.\n  - fill("{value}")')
        self.pw.log.append(f"fill {self.what}")
        self.pw.now += self.pw.FILL_S


def _fake_playwright(monkeypatch, text: str) -> _FakePlaywright:
    import sys
    import types

    from agentic_analytics.demo.page import OWNER_MARKER

    pw = _FakePlaywright(text.replace("{owner}", OWNER_MARKER))
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = pw.sync_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(M, "time", types.SimpleNamespace(perf_counter=lambda: pw.now))
    return pw


ANSWERED = "Claude ... Snowflake ... This question cost $0.2512 (held at most $0.74). {owner}"


def test_page_mode_times_each_ask_from_submit_to_the_answer(monkeypatch):
    """DEM-05: page load (40 s here) and typing are outside the time; submit to answer is in."""
    pw = _fake_playwright(monkeypatch, ANSWERED)
    ask_one = M.page_asker("https://demo.example", "owner-pass", 2)
    out = ask_one("How many sales?")
    assert out.elapsed_s == pytest.approx(_FakePlaywright.ANSWER_S)
    assert out.owner_budget and len(out) == 2 and all(p.ok for p in out)
    assert pw.log == ["goto https://demo.example", "click text Ask your own (passcode)",
                      "fill label Passcode exact", "fill label Your question exact",
                      "click button Ask both backends", "wait"]  # fmt: skip
    r = M.run(mode="page", config=CFG, questions=["q"], ask_one=ask_one, target="https://d",
              commit="abc", hold_usd=0.5)  # fmt: skip
    assert r["p95_ms"] == 2_500 and r["owner_budget_asks"] == 20 and r["passed"]
    ask_one.close()  # type: ignore[attr-defined]
    assert "playwright" in pw.closed


def test_page_mode_stops_on_a_compute_off_page(monkeypatch):
    _fake_playwright(monkeypatch, "Live compute is off for claude_mcp, cortex_analyst.")
    ask_one = M.page_asker("https://demo.example", "owner-pass", 2)
    with pytest.raises(ValueError, match="live compute is off"):
        ask_one("q")


def test_main_refuses_the_synthetic_example_and_a_page_run_without_url(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    save_cache(tmp_path / "demo" / "replay.json", load_example_cache())
    assert M.main(["--mode", "in-process"], environ={}) == 2
    assert M.main(["--mode", "page"], environ={}) == 2
    capsys.readouterr()
    assert M.main(["--mode", "page", "--url", "http://demo.example"], environ={}) == 2
    assert "--url https://" in capsys.readouterr().err  # DEM-15 measures the page over HTTPS


@pytest.mark.parametrize(
    ("url", "opened"),
    [
        (
            "https://agenticanalytics12.streamlit.app/",
            "https://agenticanalytics12.streamlit.app/~/+/",
        ),
        ("https://Demo.Streamlit.App", "https://Demo.Streamlit.App/~/+/"),
        ("https://demo.streamlit.app/~/+/", "https://demo.streamlit.app/~/+/"),
        ("https://demo.streamlit.app/other", "https://demo.streamlit.app/other"),
        ("https://demo.example/", "https://demo.example/"),
        ("https://streamlit.app.evil.example/", "https://streamlit.app.evil.example/"),
    ],
)
def test_page_mode_opens_the_app_itself_on_streamlit_cloud(url, opened):
    """DEM-15: Community Cloud serves the app at /~/+/ inside a wrapper page; the driver opens
    the app itself there, and any other URL as given."""
    assert M.app_url(url) == opened


def test_page_mode_goes_to_the_app_frame_and_keeps_the_given_target(monkeypatch):
    pw = _fake_playwright(monkeypatch, ANSWERED)
    ask_one = M.page_asker("https://demo.streamlit.app/", "owner-pass", 2)
    ask_one("How many sales?")
    assert pw.log[0] == "goto https://demo.streamlit.app/~/+/"
    r = M.run(mode="page", config=CFG, questions=["q"], ask_one=ask_one,
              target="https://demo.streamlit.app/", commit="abc", hold_usd=0.5)  # fmt: skip
    assert r["target"] == "https://demo.streamlit.app/"
    ask_one.close()  # type: ignore[attr-defined]


@pytest.mark.safety
@pytest.mark.parametrize("field", ["Passcode", "Your question"])
def test_a_failed_page_step_never_carries_what_was_typed(monkeypatch, field):
    """DEM-15 safety: Playwright's error message logs the values it filled, the owner passcode
    among them; the driver's error names the step and the error type only, unchained."""
    pw = _fake_playwright(monkeypatch, ANSWERED)
    pw.fail_on = f"label {field}"
    ask_one = M.page_asker("https://demo.example", "owner-pass-SECRET", 2)
    with pytest.raises(M.PageDriverError) as info:
        ask_one("the question text")
    err = info.value
    assert "owner-pass-SECRET" not in str(err) and "the question text" not in str(err)
    assert "TimeoutError" in str(err) and "type the" in str(err)
    assert err.__cause__ is None and err.__suppress_context__
    assert isinstance(err, ValueError)  # main prints it and exits 3, with no traceback
    assert "context-or-browser" in pw.closed  # the browser session is still closed
    ask_one.close()  # type: ignore[attr-defined]
