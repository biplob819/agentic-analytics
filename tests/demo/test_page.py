"""D4.2 the demo page: DEM-01..DEM-05, without Streamlit (it is not installed; the page takes
the `streamlit` module as an argument, and `FakeSt` records what the layout does)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentic_analytics.demo import ask as A
from agentic_analytics.demo import page as P
from agentic_analytics.demo.cache import (
    SYNTHETIC_LABEL,
    DemoCache,
    load_example_cache,
    pack_fix_hash,
    save_cache,
)
from agentic_analytics.schemas import AnalyticsAnswer, ResultSet
from tests.demo.helpers import FakeSt, SpyStore, fake_context
from tests.run import world as w

PASS = "open-sesame"
DEMO_DIR = Path(P.__file__).resolve().parent


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """No .env, no secrets, no demo/replay.json: the page falls back to the example."""
    monkeypatch.chdir(tmp_path)
    for k in ("DEMO_PASSCODE", "EVALKIT_DEMO_DATABASE_URL", "AA_DEMO_CACHE", "DEMO_OWNER_PASSCODE",
              "DEMO_LIVE_BACKENDS", "DEMO_CONTACT_URL"):  # fmt: skip
        monkeypatch.delenv(k, raising=False)
    P._LIVE.clear()
    yield tmp_path
    P._LIVE.clear()


def real_cache(**config_update) -> DemoCache:
    """The example's answers as a real (non-synthetic) cache stamped with the bundled pack's
    fix_hash, consistent entry by entry (validated, not `model_copy`)."""
    data = load_example_cache().model_dump(mode="json")
    fh = pack_fix_hash(w.BUNDLED)
    data.update(synthetic=False, label="", fix_hash=fh)
    data["config"].update(config_update)
    for e in data["entries"]:
        e["stamp"].update(fix_hash=fh, **{k: v for k, v in config_update.items()
                                          if k in e["stamp"]})  # fmt: skip
    return DemoCache.model_validate(data)


@pytest.fixture
def deployed(clean, monkeypatch):
    """A real replay file and a matching run.yaml in the working folder, with the owner having
    turned live compute on for every backend (OWNER D-59; `saved_only` leaves it off)."""
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")
    cache = real_cache()
    save_cache(clean / "demo" / "replay.json", cache)
    write_run_yaml(clean, cache)
    return clean


def write_run_yaml(where: Path, cache: DemoCache, **update) -> Path:
    data = cache.config.model_dump(mode="json")
    data.update(update)
    path = where / "run.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---- DEM-01 -------------------------------------------------------------------------------------


def test_the_page_opens_on_a_sample_already_answered_on_both_backends(clean):
    st = FakeSt()
    P.main(st)
    (args, kw), = st.named("selectbox")  # fmt: skip
    cache = load_example_cache()
    assert args[1] == cache.samples() and kw["index"] == 0
    assert st.named("columns") == [((2,), {})]
    titles = st.texts("subheader")
    assert titles[0].startswith("Claude + ClickHouse MCP") and titles[1] == (
        "Snowflake Cortex Analyst"
    )
    assert len(st.named("vega_lite_chart")) == 2 and len(st.named("code")) == 2
    assert all(kw == {"width": "stretch"} for _, kw in st.named("vega_lite_chart"))


def test_opening_is_replay_and_makes_no_call():
    counter = w.Counter()
    fake_context(A.DEMO_BACKENDS, counter)
    q, panels = P.opening(load_example_cache())
    assert q == load_example_cache().samples()[0] and len(panels) == 2
    assert counter.total == 0


@pytest.mark.safety
def test_the_page_opens_through_opening_from_replay_and_calls_nothing(deployed, monkeypatch):
    """DEM-01 on the page: with a passcode set and every backend live, the opening sample (and
    any other picked) is shown through `opening`, from replay, building and asking nothing."""
    monkeypatch.setenv("DEMO_PASSCODE", PASS)
    hit: list[str] = []

    def trip(name):
        def boom(*a, **kw):
            hit.append(name)
            raise AssertionError(f"{name} called in replay mode")

        return boom

    for name in ("build_live", "shared_live", "live_settings", "live_problem", "start_warm_up",
                 "run_live", "make_guard"):  # fmt: skip
        monkeypatch.setattr(P, name, trip(name))
    monkeypatch.setattr(A, "context_from_env", trip("context_from_env"))
    modes: list[str] = []
    real_ask = P.ask_panels

    def ask(q, backends, mode, **kw):
        modes.append(mode)
        return real_ask(q, backends, mode, **kw)

    monkeypatch.setattr(P, "ask_panels", ask)
    opened: list[str | None] = []
    real_opening = P.opening
    monkeypatch.setattr(P, "opening", lambda c, pick=None: (opened.append(pick),
                                                            real_opening(c, pick))[1])  # fmt: skip
    cache = P.load_demo_cache({})
    P.main(FakeSt())
    samples = cache.samples()
    P.main(FakeSt(answers={"Sample question": samples[3]}))
    assert opened == [samples[0], samples[3]] and modes == ["replay", "replay"] and hit == []


def test_a_real_cache_file_wins_over_the_example(clean):
    cache = load_example_cache()
    real = cache.model_copy(update={"synthetic": False, "label": ""})
    from agentic_analytics.demo.cache import save_cache

    save_cache(clean / "demo" / "replay.json", real)
    assert P.cache_source({}, clean) == clean / "demo" / "replay.json"
    assert not P.load_demo_cache({}, clean).synthetic
    assert P.cache_source({"AA_DEMO_CACHE": "/x/y.json"}, clean) == Path("/x/y.json")
    assert P.load_demo_cache({}, clean / "elsewhere").synthetic


def test_the_committed_replay_is_real_and_matches_run_yaml(clean, monkeypatch):
    """The hosted app opens on `demo/replay.json` (the real `demo-cache` run): real answers on
    both backends for every sample, the licence credit, no synthetic label, and a config that
    the repo's `run.yaml` agrees with, so live mode is not refused once the owner turns it on."""
    root = DEMO_DIR.parents[2]
    replay = root / "demo" / "replay.json"
    cache = P.load_demo_cache({"AA_DEMO_CACHE": str(replay)})
    assert not cache.synthetic and cache.backends == A.DEMO_BACKENDS
    assert len(cache.samples()) == len(cache.entries) >= 10
    for e in cache.entries:
        assert all(p.answer.sql and not p.answer.error and not p.timed_out for p in e.panels)
    settings = P.live_settings({"AA_RUN_CONFIG": str(root / "run.yaml")}, cache)
    assert isinstance(settings, P.LiveSettings), settings
    assert settings.backends == A.DEMO_BACKENDS
    monkeypatch.setenv("AA_DEMO_CACHE", str(replay))
    st = FakeSt()
    P.main(st)
    (args, kw), = st.named("selectbox")  # fmt: skip
    assert args[1] == cache.samples() and kw["index"] == 0
    info = st.texts("info")
    assert not any("SYNTHETIC" in i for i in info) and not st.texts("error")
    assert any("Contains HM Land Registry data © Crown copyright" in i for i in info)
    captions = st.texts("caption")
    assert captions.count(f"Data as of {cache.config.as_of.isoformat()}") == 2
    assert not any(c.startswith("Synthetic") for c in captions)


# ---- DEM-02 -------------------------------------------------------------------------------------


def test_a_panel_shows_sql_answer_chart_cost_and_trace_link():
    cache = load_example_cache()
    agent, cortex = cache.entries[0].panels
    v = P.panel_view(agent, synthetic=True)
    assert v.sql and v.answer.startswith("The answer is")
    assert v.chart is not None and v.chart["$schema"].startswith("https://vega.github.io")
    json.dumps(v.chart)  # plain JSON for st.vega_lite_chart
    assert v.cost_line.startswith("Synthetic cost $") and "synthetic latency" in v.cost_line
    assert P.panel_view(agent).cost_line.startswith("Cost $")
    assert v.trace == "No trace: synthetic example."
    traced = agent.model_copy(
        update={"answer": agent.answer.model_copy(update={"trace_url": "https://lf/t/1"})}
    )
    assert P.panel_view(traced).trace == "[Trace](https://lf/t/1)"
    assert A.CORTEX_LINE in P.panel_view(cortex).notes


def test_rows_are_shown_as_a_table_when_there_is_no_chart():
    rows = ResultSet(columns=("town", "type", "n"), rows=(("LEEDS", "D", 1), ("YORK", "F", 2)))
    answer = AnalyticsAnswer(
        backend="cortex_analyst", model_snapshot=None, sql="SELECT 1", answer="Two rows."
    )
    panel = A.Panel(
        backend="cortex_analyst", answer=answer, cost_usd=0.2, latency_ms=900, rows=rows,
        rows_total=40,
    )  # fmt: skip
    v = P.panel_view(panel)
    assert v.chart is None and v.table[0] == {"town": "LEEDS", "type": "D", "n": 1}
    assert v.table_note == "First 2 of 40 rows."


def test_timed_out_and_failed_panels_say_so():
    answer = AnalyticsAnswer(
        backend="claude_mcp", model_snapshot="m", sql=None, answer="", error="timeout"
    )
    late = A.Panel(
        backend="claude_mcp", answer=answer, cost_usd=0.1, latency_ms=25000, timed_out=True,
        note="Timed out after 25 s; no answer from this backend.",
    )  # fmt: skip
    assert P.panel_view(late).error == late.note
    bad = A.Panel(
        backend="claude_mcp",
        answer=answer.model_copy(update={"error": "refusal"}),
        cost_usd=0,
        latency_ms=1,
    )
    assert P.panel_view(bad).error == "The backend declined to answer."


def test_model_text_is_shown_as_text_never_as_markup():
    out = P.escape_md("[click](http://evil) <b>x</b> **y**")
    assert "](" not in out.replace("\\](", "") and "<b>" not in out.replace("\\<b\\>", "")


MARK = "[pwn](https://evil.example) **bold** <b>x</b> | --- |"
HEADER = "**h** [x](https://evil.example)"
RAW = ("[pwn](https", "**bold**", "<b>", "**h**", "[x](https")
# Streamlit calls that render Markdown (GFM) in their text; `st.dataframe` and `st.code` do not
MARKDOWN_CALLS = ("markdown", "caption", "info", "warning", "error", "success", "subheader",
                  "title", "header", "write", "table")  # fmt: skip


def _hostile(panel: A.Panel, *, clarify: bool = False) -> A.Panel:
    """`panel` with model-controlled markup in its answer and in an executed cell and column
    name, or (`clarify`) in its clarification and a suggestion (B6: those come without SQL)."""
    if clarify:
        update = {"answer": "", "clarification": MARK, "sql": None, "suggestions": (MARK,)}
        answer = panel.answer.model_copy(update={**update, "chart": None})
        return panel.model_copy(update={"answer": answer, "rows": None, "rows_total": None})
    answer = panel.answer.model_copy(update={"answer": MARK, "chart": None})
    rows = ResultSet(columns=(HEADER, "n"), rows=((MARK, 1),))
    return panel.model_copy(update={"answer": answer, "rows": rows, "rows_total": 1})


def _assert_no_raw_markup(st: FakeSt, *, texts: int, suggestions: int, frames: int) -> None:
    for name in MARKDOWN_CALLS:
        for args, kw in st.named(name):
            for s in [*args, *kw.values()]:
                assert not any(r in str(s) for r in RAW), (name, s)
    md = st.texts("markdown")
    assert md.count(P.escape_md(MARK)) == texts
    assert md.count("Suggestions: " + P.escape_md(MARK)) == suggestions
    assert not st.named("table")  # GFM in cells and headers
    shown = st.named("dataframe")  # plain text: the raw values are shown as they are
    assert shown == [(([{HEADER: MARK, "n": 1}],), {"hide_index": True})] * frames


@pytest.mark.safety
def test_markup_in_answer_suggestions_cells_and_headers_never_renders():
    """DEM-02: render_panel shows model text (answer, clarification, suggestions) escaped and
    executed rows (cells and column names from model-written SQL) in `st.dataframe`, never as
    Markdown."""
    agent = load_example_cache().entries[0].panels[0]
    st = FakeSt()
    P.render_panel(st, P.panel_view(_hostile(agent)))
    _assert_no_raw_markup(st, texts=1, suggestions=0, frames=1)
    st = FakeSt()
    P.render_panel(st, P.panel_view(_hostile(agent, clarify=True)))
    _assert_no_raw_markup(st, texts=1, suggestions=1, frames=0)


@pytest.mark.safety
def test_the_page_never_renders_markup_from_a_replay_answer(clean):
    """The same through `main()`: a replay file whose panels carry markup everywhere."""
    cache = real_cache()
    data = cache.model_dump(mode="json")
    agent, cortex = cache.entries[0].panels
    data["entries"][0]["panels"] = [
        _hostile(agent).model_dump(mode="json"),
        _hostile(cortex, clarify=True).model_dump(mode="json"),
    ]
    save_cache(clean / "demo" / "replay.json", DemoCache.model_validate(data))
    st = FakeSt()
    P.main(st)
    assert len(st.texts("subheader")) == 2 and not st.texts("error")
    _assert_no_raw_markup(st, texts=2, suggestions=1, frames=1)


# ---- DEM-03 -------------------------------------------------------------------------------------


def _live(tmp_path, monkeypatch, counter=None):
    ctx = fake_context(A.DEMO_BACKENDS, counter)
    store = SpyStore(tmp_path / "spend.sqlite")
    live = P.Live(A.make_guard({"DEMO_PASSCODE": PASS}, store=store), ctx, local_store=True)
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "shared_live", lambda env, settings: live)
    warmed = []
    monkeypatch.setattr(P, "start_warm_up", lambda ex: warmed.append(ex))
    return live, store, warmed


def test_live_mode_asks_with_the_passcode_and_shows_the_budget_line(deployed, monkeypatch):
    counter = w.Counter()
    live, store, warmed = _live(deployed, monkeypatch, counter)
    answers = {
        "Mode": "Ask your own (passcode)",
        "Passcode": PASS,
        "Your question": "How many property sales were recorded in England and Wales in 2023?",
        "Ask both backends": True,
    }
    st = FakeSt(answers)
    P.main(st)
    captions = st.texts("caption")
    assert captions[0].startswith("Today's live budget left: $3.00 of $3.00 (local counter)")
    assert "per hour per session" in captions[0] and "per visitor" not in captions[0]
    assert "shared by all visitors and the real limit" in captions[0]
    assert captions.count("Data as of 2026-09-30") == 2  # D-50 on every answer
    assert "each request holds at most $1.00" in captions[0]
    assert len(st.texts("subheader")) == 2 and any("This question cost $" in c for c in captions)
    assert len(store.reserved) == 1 and counter.cortex_requests == 1
    assert warmed == [live.ctx.services.clickhouse]
    assert A.LIVE_UNTRACED in st.texts("markdown")


def test_a_wrong_passcode_shows_the_refusal_and_calls_nothing(deployed, monkeypatch):
    counter = w.Counter()
    _, store, warmed = _live(deployed, monkeypatch, counter)
    st = FakeSt(
        {"Mode": "Ask your own (passcode)", "Passcode": "no", "Your question": "x",
         "Ask both backends": True}
    )  # fmt: skip
    P.main(st)
    assert st.texts("error") == ["Wrong passcode."]
    assert counter.total == 0 and store.reserved == [] and warmed == []


def test_warm_up_fires_only_after_the_passcode_check_passes(deployed, monkeypatch):
    counter = w.Counter()
    live, _, warmed = _live(deployed, monkeypatch, counter)
    state: dict = {}
    for _ in range(3):  # opening live mode (reruns included) warms nothing
        P.main(FakeSt({"Mode": "Ask your own (passcode)"}, state=state))
    bad = {"Mode": "Ask your own (passcode)", "Passcode": "nope", "Your question": "x",
           "Ask both backends": True}  # fmt: skip
    P.main(FakeSt(bad, state=state))  # an unauthenticated visitor: no warehouse call
    assert warmed == [] and counter.total == 0
    good = {**bad, "Passcode": PASS, "Your question": "Which town?"}
    for _ in range(2):
        P.main(FakeSt(good, state=state))
    assert warmed == [live.ctx.services.clickhouse]  # once per session
    P.main(FakeSt())  # replay mode never warms up
    assert len(warmed) == 1


def test_start_warm_up_runs_the_select_in_the_background(monkeypatch):
    seen = []
    monkeypatch.setattr(P, "warm_up", lambda ex: seen.append(ex))
    t = P.start_warm_up("executor")
    t.join(5)
    assert seen == ["executor"] and t.daemon


def test_live_mode_off_without_secrets(deployed):
    st = FakeSt({"Mode": "Ask your own (passcode)"})
    P.main(st)
    assert st.texts("warning") == [
        "Live mode is not configured on this deployment; replay mode works."
    ]


def test_live_mode_is_refused_on_the_synthetic_example(clean, monkeypatch):
    monkeypatch.setenv("DEMO_LIVE_BACKENDS", "all")  # even with live compute turned on
    built = []
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "build_live", lambda env, settings: built.append(settings))
    write_run_yaml(clean, load_example_cache())  # even with a matching run.yaml
    st = FakeSt({"Mode": "Ask your own (passcode)"})
    P.main(st)
    assert st.texts("warning") == [P.LIVE_SYNTHETIC] and built == []
    assert P.live_settings({}, load_example_cache(), clean) == P.LIVE_SYNTHETIC


def test_live_mode_takes_its_config_from_run_yaml_never_from_the_cache(deployed, monkeypatch):
    write_run_yaml(deployed, real_cache(), seed=99, max_turns=4)  # outside the stamp
    settings = P.live_settings({}, real_cache(), deployed)
    assert isinstance(settings, P.LiveSettings)
    assert settings.config.seed == 99 and real_cache().config.seed != 99
    got = []
    monkeypatch.setattr(A, "context_from_env", lambda cfg, pack, env: got.append(cfg) or "ctx")
    live = P.build_live({"DEMO_PASSCODE": PASS}, settings)
    assert got == [settings.config] and live.ctx == "ctx"
    other = deployed / "elsewhere.yaml"
    other.write_text(yaml.safe_dump({**real_cache().config.model_dump(mode="json"),
                                     "seed": 5}), encoding="utf-8")  # fmt: skip
    via_env = P.live_settings({"AA_RUN_CONFIG": str(other)}, real_cache(), deployed)
    assert isinstance(via_env, P.LiveSettings) and via_env.config.seed == 5


@pytest.mark.parametrize(
    ("update", "field"),
    [
        ({"as_of": "2026-08-31"}, "as_of"),
        ({"models": {"claude_mcp": "claude-opus-5-5"}}, "models"),
        ({"effort": "high"}, "effort"),
    ],
)
def test_live_mode_is_refused_when_run_yaml_disagrees_with_the_cache(
    deployed, monkeypatch, update, field
):
    built = []
    monkeypatch.setattr(P, "live_problem", lambda env, backends: None)
    monkeypatch.setattr(P, "build_live", lambda env, settings: built.append(settings))
    write_run_yaml(deployed, real_cache(), **update)
    st = FakeSt({"Mode": "Ask your own (passcode)"})
    P.main(st)
    (warning,) = st.texts("warning")
    assert warning.startswith("Live mode is off") and field in warning and built == []


def test_live_mode_is_refused_when_the_pack_fix_hash_differs(clean):
    stale = real_cache()
    data = stale.model_dump(mode="json")
    data["fix_hash"] = "0" * 64
    for e in data["entries"]:
        e["stamp"]["fix_hash"] = "0" * 64
    stale = DemoCache.model_validate(data)
    write_run_yaml(clean, stale)
    msg = P.live_settings({}, stale, clean)
    assert isinstance(msg, str) and "fix_hash" in msg


def test_live_mode_off_without_a_run_yaml(clean):
    assert P.live_settings({}, real_cache(), clean) == P.LIVE_OFF


def test_budget_line_when_the_store_is_down():
    class G:
        daily_cap, per_request_cap, rate_limit = 3.0, 1.0, 5

        def remaining_today(self):
            return None

    assert P.budget_line(G()).startswith("Today's live budget left: unavailable")


def test_shared_live_logs_only_the_failure_type_and_retries(caplog):
    P._LIVE.clear()
    settings = P.LiveSettings(real_cache().config, w.BUNDLED, A.DEMO_BACKENDS)

    def boom(env, settings):
        raise RuntimeError("password hunter2 rejected by host")

    assert P.shared_live({}, settings, build=boom) is None
    assert not any("hunter2" in r.getMessage() for r in caplog.records)
    live = P.Live(guard=None, ctx=None, local_store=True)  # type: ignore[arg-type]
    assert P.shared_live({}, settings, build=lambda e, c: live) is live
    P._LIVE.clear()


# ---- DEM-04 -------------------------------------------------------------------------------------


def test_banner_has_the_licence_credit_and_the_honest_cortex_line(clean):
    st = FakeSt()
    P.main(st)
    info = st.texts("info")
    assert any(i.startswith("SYNTHETIC EXAMPLE") for i in info)
    assert any("Contains HM Land Registry data © Crown copyright" in i for i in info)
    assert "Cortex Analyst returns SQL; its answer text here is a template over the rows it " \
        "returned." in info  # fmt: skip


def test_a_real_cache_has_no_synthetic_banner():
    real = load_example_cache().model_copy(update={"synthetic": False})
    assert not any("SYNTHETIC" in line for line in P.banner_lines(real))


@pytest.mark.parametrize("label", ["", "   ", "Real answers, honest"])
def test_a_synthetic_cache_always_shows_the_synthetic_label(label):
    unvalidated = load_example_cache().model_copy(update={"label": label})
    assert P.banner_lines(unvalidated)[0] == SYNTHETIC_LABEL


def test_every_answer_shows_the_as_of_date_and_synthetic_costs_say_so(clean):
    st = FakeSt()
    P.main(st)
    captions = st.texts("caption")
    assert captions.count("Data as of 2026-09-30") == 2
    costs = [c for c in captions if "cost $" in c.lower()]
    assert len(costs) == 2 and all(c.startswith("Synthetic cost $") for c in costs)
    assert all("synthetic latency" in c for c in costs)


def test_a_replay_file_that_disagrees_with_its_config_is_refused_on_the_page(clean):
    data = json.loads((DEMO_DIR / "example" / "replay.json").read_text(encoding="utf-8"))
    data["config"]["as_of"] = "2026-06-30"  # entries still say 2026-09-30
    (clean / "demo").mkdir()
    (clean / "demo" / "replay.json").write_text(json.dumps(data), encoding="utf-8")
    st = FakeSt()
    P.main(st)
    assert st.texts("error") == [P.BAD_CACHE]
    info = st.texts("info")  # the saved-answers notice, then the example, labelled
    assert info[0] == P.saved_notice(A.DEMO_BACKENDS, P.LiveSwitch()) and info[1] == SYNTHETIC_LABEL


# ---- DEM-05 -------------------------------------------------------------------------------------


def test_measure_p95_over_20_asks():
    ticks = iter(range(10_000))
    lat = [1.0 + 0.5 * i for i in range(20)]  # 1.0 s .. 10.5 s

    t = [0.0]

    def clock():
        return t[0]

    def ask_one(q):
        t[0] += lat[next(ticks) % 20]
        return [_ok(), _ok()]

    r = P.measure_p95(ask_one, ["a", "b"], clock=clock)
    assert r.n == 20 and r.p95_ms == 10_000 and r.max_ms == 10_500 and r.passed
    slow = P.measure_p95(lambda q: (t.__setitem__(0, t[0] + 31), [_ok()])[1], ["a"], clock=clock)
    assert not slow.passed and slow.to_dict()["target_ms"] == 30_000
    assert not P.measure_p95(ask_one, ["a"], n=5, clock=clock).passed  # fewer than 20 asks


def _ok(cost=0.1):
    from agentic_analytics.demo.p95 import PagePanel

    return PagePanel(cost_usd=cost)


def test_p95_fails_when_any_panel_timed_out_or_errored():
    from agentic_analytics.demo.p95 import PagePanel

    late = PagePanel(timed_out=True, ok=False)
    bad = PagePanel(ok=False)
    fast = iter(range(1000))  # every ask takes 1 ms: p95 is far under 30 s
    clock = lambda: next(fast) / 1000  # noqa: E731
    cut = P.measure_p95(lambda q: [_ok(), late] if q == "b" else [_ok(), _ok()], ["a", "b"],
                        clock=clock)  # fmt: skip
    assert cut.p95_ms < 30_000 and cut.timed_out_panels == 10 and not cut.passed
    assert cut.to_dict()["timed_out_panels"] == 10 and cut.to_dict()["passed"] is False
    err = P.measure_p95(lambda q: [_ok(), bad], ["a"], clock=clock)
    assert err.errored_panels == 20 and err.timed_out_panels == 0 and not err.passed
    assert err.to_dict()["errored_panels"] == 20
    good = P.measure_p95(lambda q: [_ok(), _ok()], ["a"], clock=clock)
    assert good.passed


def test_p95_stops_at_the_spend_cap_and_on_a_refusal():
    capped = P.measure_p95(lambda q: [_ok(0.5), _ok(0.5)], ["a"], max_cost_usd=5.0, hold_usd=1.0)
    assert capped.n == 5 and capped.stopped and "spend cap" in capped.stopped
    assert capped.spent_usd == pytest.approx(5.0) and not capped.passed  # 5 + a $1 hold > $5
    refused = P.measure_p95(lambda q: [], ["a"])
    assert refused.n == 0 and refused.stopped == "a request was refused" and not refused.passed


def test_deployment_secrets_are_runtime_only_and_documented():
    assert not any("ADMIN" in s for s in P.SECRETS)
    assert {"DEMO_PASSCODE", "EVALKIT_DEMO_DATABASE_URL"} <= set(P.SECRETS)
    doc = P.__doc__ or ""
    assert "secret store" in doc and "pip install agentic-analytics[demo]" in doc


def test_secret_env_reads_the_host_store_over_the_environment(clean, monkeypatch):
    monkeypatch.setenv("DEMO_PASSCODE", "from-env")
    monkeypatch.setenv("DEMO_OWNER_CAP_USD", "12")
    st = FakeSt(secrets={"DEMO_PASSCODE": "from-host", "nested": {"x": 1}, "AA_FLAG": True,
                         "DEMO_OWNER_CAP_USD": 20})  # fmt: skip
    env = P.secret_env(st)
    assert env["DEMO_PASSCODE"] == "from-host" and "nested" not in env
    assert "AA_FLAG" not in env and env["DEMO_OWNER_CAP_USD"] == "12"  # strings only


def test_the_page_module_does_not_import_streamlit():
    code = "import sys, agentic_analytics.demo.page; assert 'streamlit' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
    tree = ast.parse((DEMO_DIR / "page.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    names = [a.name for n in top for a in n.names] + [
        n.module or "" for n in top if isinstance(n, ast.ImportFrom)
    ]
    assert not any(x.startswith("streamlit") for x in names)


def test_app_entry_is_thin():
    tree = ast.parse((DEMO_DIR / "app.py").read_text(encoding="utf-8"))
    body = [
        n for n in tree.body if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Constant)
    ]
    assert len(body) == 2
    imp, call = body
    assert isinstance(imp, ast.ImportFrom) and imp.module == "agentic_analytics.demo.page"
    assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
