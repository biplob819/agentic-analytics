"""The demo page (D4.2: DEM-01..DEM-05; PRD Flow B, US7; Tech Spec §10).

Everything a visitor can trigger is a pure function here, tested without Streamlit; `main(st)`
is the layout and takes the `streamlit` module (imported only when the page runs, so the package
never depends on it). `demo/app.py` is the thin entry for `streamlit run`.

Flow B: the page opens on a sample question already answered on both backends (replay, no
call); the visitor picks another sample, or enters the passcode to ask their own (live, behind
EvalKit's guard, with today's budget shown). Each panel shows the SQL, the answer, a Vega-Lite
chart (or the rows), its cost and its trace link. The banner carries the licence credit and the
honest Cortex line (NFR6, C1).

**Deployment (DEM-05, D-57).** The page runs as its own Streamlit Community Cloud app from this
repo: main file `demo/app.py` (it puts `src/` on the path and calls `main`), packages from the
pinned `demo/requirements.txt` (the `demo` extra, `pip install agentic-analytics[demo]`, adds
streamlit and psycopg for a local run). Secrets live in the host's secret store (Streamlit secrets),
never in the repo or the image: `DEMO_PASSCODE`, `EVALKIT_DEMO_DATABASE_URL` (the Neon database
EvalKit's demo already uses; this project's counter sits under its own key), the model keys for
the agents shown, the ClickHouse MCP key and read-only executor, and the Snowflake read-only key
pair; the host has no key file, so the key is the PEM text in `SNOWFLAKE_RO_PRIVATE_KEY`
(`scripts/streamlit_secrets.py | pbcopy` builds the whole secrets block from `.env`). The demo
host never gets a `*_ADMIN_*` variable. `AA_DEMO_CACHE` points at the replay file
(default `demo/replay.json`, else the bundled synthetic example). Live mode takes its models,
effort and as-of date from the deployment's `run.yaml` (`AA_RUN_CONFIG` overrides the path), never
from the replay file, and stays off while the page shows the synthetic example or when `run.yaml`
disagrees with the replay answers. The browser only ever receives answers, and every text a panel
shows passes the trace `Redactor` first; logs hold one aggregate line per ask. The NFR2 p95 is
measured with `python -m agentic_analytics.demo.p95` (see that module) under the owner
measurement budget (OWNER D-53): typing the owner passcode (`DEMO_OWNER_PASSCODE`, a separate
secret; optional `DEMO_OWNER_CAP_USD`, default $15, at most $25) runs the ask against the owner's
own counter instead of the visitors' $3/day, and appends it to the spend ledger (`AA_SPEND_LEDGER`,
default `docs/measurements/spend-ledger.jsonl`). Nothing else on the page selects owner mode.

**Visitor own keys (OWNER D-56).** Live mode's form also takes an optional Anthropic key (the
Claude panel) and OpenAI key (the GPT panel), one field per provider that serves a live panel.
The submit button's callback moves what was typed out of the widgets into a pending slot and
empties the fields, so the key never sits in a widget; the key is accepted (offline format check,
`demo.keys`) only after the visitor passcode passes, never on the owner passcode, and is then
kept in this browser session's `st.session_state` as a `VisitorKeys` until "Forget my key" or the
session ends. Every text the page shows for that session passes a `Redactor` that also knows the
key. The notice says the key goes to the Streamlit server for this session only, is not stored,
and that the visitor pays their provider directly; the warehouse spend of own-key asks has its own
daily limit (`ask.BYOK_DAILY_CAP_USD`, counter `agentic-analytics-byok`).

**Saved answers by default (OWNER D-58, D-59, D-60).** No backend is live unless the owner names
it in `DEMO_LIVE_BACKENDS` (`claude_mcp`, `gpt_mcp`, `cortex_analyst`, or `all`; empty or missing
means none; an unknown name is shown as an error and keeps every backend off). Near the top and
on each panel the page says the answers are saved answers from the benchmark run and that live
querying on Snowflake and ClickHouse is switched off to save compute, with a link to ask the owner
(`DEMO_CONTACT_URL`, an https URL, default the repo's GitHub Issues page). A backend that is not
live gets no services built, no executor, no guard and no budget. The passcode box and the
own-key fields are always shown: live needs a valid passcode AND the backend being live; a valid
passcode (visitor or owner) while compute is off says so, links the request and connects to
nothing, and own keys never replace the passcode.
"""

from __future__ import annotations

import contextlib
import hmac
import importlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from evalkit.demo.guard import GuardRefused

from agentic_analytics.demo.ask import (
    BYOK_SPEND_KEY,
    CORTEX_LINE,
    DEFAULT_CONTACT_URL,
    DEMO_BACKENDS,
    LIVE_UNTRACED,
    MAX_QUESTION_CHARS,
    OWNER_PASSCODE_VAR,
    STORE_DOWN,
    DemoContext,
    DemoSettingError,
    Panel,
    ask_panels,
    compute_off_text,
    contact_url,
    ledger_path,
    live_backends,
    live_problem,
    log_request,
    make_byok_guard,
    make_guard,
    make_owner_guard,
    pick_guard,
    run_live,
    warm_up,
)
from agentic_analytics.demo.cache import (
    SYNTHETIC_LABEL,
    DemoCache,
    DemoStamp,
    load_cache,
    load_example_cache,
    stamp_differences,
)
from agentic_analytics.demo.keys import (
    FAILURE_TEXTS,
    PANEL_NAMES,
    PROVIDER_NAMES,
    REDACT_NAMES,
    KeyFormatError,
    VisitorKeys,
    accept_keys,
    provider_for_model,
)
from agentic_analytics.schemas import MCP_BACKENDS, BackendName, RunConfig
from agentic_analytics.tracing import Redactor

LOG = logging.getLogger("agentic_analytics.demo")
PAGE_TITLE = "Agentic Analytics demo"
HEADLINE = "Ask UK house-price data: an MCP agent and Cortex Analyst side by side"
DEFAULT_CACHE = Path("demo") / "replay.json"
DEFAULT_RUN_CONFIG = Path("run.yaml")
LIVE_OFF = "Live mode is not configured on this deployment; replay mode works."
LIVE_SYNTHETIC = (
    "Live mode is off: this deployment shows only the synthetic example answers. Record real "
    "ones with `agentic-analytics demo-cache` first; replay mode works."
)
BAD_CACHE = "The replay file could not be read; showing the synthetic example instead."
ERROR_TEXTS: dict[str, str] = {  # what a panel says for each AnalyticsAnswer error
    "transport": "No answer: the backend could not be reached.",
    "schema": "No answer: the reply was not in the expected form.",
    "refusal": "The backend declined to answer.",
    "step_budget": "Stopped at the step or cost budget before answering.",
    "timeout": "Timed out.",
}
BACKEND_LABELS: dict[str, str] = {
    "claude_mcp": "Claude + ClickHouse MCP",
    "gpt_mcp": "GPT + ClickHouse MCP",
    "cortex_analyst": "Snowflake Cortex Analyst",
}
KEYS_STATE = "aa_visitor_keys"  # st.session_state: this session's VisitorKeys (OWNER D-56)
PENDING_STATE = "aa_pending_keys"  # what the last submit typed, until the passcode is checked
FORGOT_STATE = "aa_keys_forgotten"  # set by the forget callback, shown once on the next run
KEY_WIDGETS: dict[str, str] = {"anthropic": "aa_key_anthropic", "openai": "aa_key_openai"}
KEY_LABELS: dict[str, str] = {
    "anthropic": "Your Anthropic API key (optional, Claude panel)",
    "openai": "Your OpenAI API key (optional, GPT panel)",
}
FORGET_LABEL = "Forget my key"
BYOK_NOTICE = (
    "Optional: use your own API key, after the passcode. Your key is sent to this demo's "
    "Streamlit server for this session only and is not stored: it stays in this session's memory "
    'until you press "Forget my key" or close the page, and is never logged, traced or cached. '
    "You pay your provider (Anthropic or OpenAI) directly for the model calls made with it. The "
    "models and effort stay the benchmark's; the demo still pays for the warehouse, under its own "
    "daily limit for own-key questions."
)
OWNER_NO_KEYS = "The owner passcode never uses a visitor key; the typed key was not kept."
KEYS_FORGOTTEN = "Your key was forgotten. Live questions now use the demo's budget."
KEYS_NOT_USED = "Nothing ran live, so your key was not used or kept."
ASK_LABEL = "Ask both backends"
WRONG_PASSCODE = "Wrong passcode."  # EvalKit's guard says the same
ENGINES: dict[str, str] = {  # where each backend's live compute runs
    "claude_mcp": "ClickHouse",
    "gpt_mcp": "ClickHouse",
    "cortex_analyst": "Snowflake",
}
SAVED_ANSWER = "Saved answer from the benchmark run."
REQUEST_LINK = "[Ask the owner to turn live compute on]({url})"
LIVE_SWITCHED_OFF = (
    "Live querying on Snowflake and ClickHouse is switched off to save compute. The passcode is "
    "still checked, but no question is asked live until the owner turns it on. {link}."
)
P95_TARGET_MS = 30_000  # NFR2
P95_ASKS = 20  # DEM-05
SECRETS = (
    "DEMO_PASSCODE",
    "DEMO_OWNER_PASSCODE",  # OWNER D-53: the owner measurement budget, never given to visitors
    "EVALKIT_DEMO_DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "CLICKHOUSE_MCP_KEY_ID",
    "CLICKHOUSE_MCP_KEY_SECRET",
    "CLICKHOUSE_SERVICE_ID",
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_RO_USER",
    "CLICKHOUSE_RO_PASSWORD",
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_RO_USER",
    "SNOWFLAKE_RO_PRIVATE_KEY",  # D-57: the PEM text (the host has no key file)
    "SNOWFLAKE_WAREHOUSE",
    "DEMO_LIVE_BACKENDS",  # OWNER D-59: not secret; empty or missing = saved answers only
    "DEMO_CONTACT_URL",  # not secret: the owner-request link
)


# -- saved answers and live compute (OWNER D-58, D-59, D-60) ------------------------------------


@dataclass(frozen=True)
class LiveSwitch:
    """What the deployment turned live, the owner-request link, and any setting problem to show
    (the setting's name and the fault, never a secret)."""

    on: frozenset[BackendName] = frozenset()
    url: str = DEFAULT_CONTACT_URL
    problems: tuple[str, ...] = ()


def live_switch(env: Mapping[str, str]) -> LiveSwitch:
    """`DEMO_LIVE_BACKENDS` and `DEMO_CONTACT_URL`. A bad live list turns every backend off; a
    bad link falls back to the default; each problem is reported for the owner to see."""
    problems: list[str] = []
    try:
        on = live_backends(env)
    except DemoSettingError as exc:
        problems.append(exc.reason)
        on = frozenset()
    try:
        url = contact_url(env)
    except DemoSettingError as exc:
        problems.append(exc.reason)
        url = DEFAULT_CONTACT_URL
    return LiveSwitch(on, url, tuple(problems))


def _engines(backends: Sequence[BackendName]) -> str:
    names = sorted({ENGINES.get(b, b) for b in backends}, reverse=True)  # Snowflake, ClickHouse
    return " and ".join(names)


def saved_notice(backends: Sequence[BackendName], switch: LiveSwitch) -> str:
    """The notice near the top: the answers shown are saved, and where live is switched off."""
    off: list[BackendName] = [b for b in backends if b not in switch.on]
    on: list[BackendName] = [b for b in backends if b in switch.on]
    link = REQUEST_LINK.format(url=switch.url)
    if not on:
        return (
            "Every answer on this page is a saved answer from the benchmark run. Live querying on "
            "Snowflake and ClickHouse is switched off to save compute, so nothing here connects "
            f"to a warehouse or a model provider. Want to try your own questions live? {link}."
        )
    if off:
        return (
            "The sample answers are saved answers from the benchmark run. Live querying for "
            f"{', '.join(BACKEND_LABELS.get(b, b) for b in off)} ({_engines(off)}) is switched off "
            f"to save compute: {link}. With the passcode, live questions run on "
            f"{', '.join(BACKEND_LABELS.get(b, b) for b in on)}."
        )
    return (
        "The sample answers are saved answers from the benchmark run. With the passcode, live "
        f"questions run on {_engines(on)}."
    )


def saved_panel_note(backend: BackendName, switch: LiveSwitch) -> str:
    """Each replay panel says it is a saved answer, and whether its live compute is off."""
    if backend in switch.on:
        return SAVED_ANSWER
    return (
        f"{SAVED_ANSWER} Live querying on {ENGINES.get(backend, backend)} is switched off to "
        f"save compute. {REQUEST_LINK.format(url=switch.url)}."
    )


def compute_off_panel_text(backend: BackendName, url: str) -> str:
    """A panel whose backend is not live, after a valid passcode: nothing was asked."""
    return f"{compute_off_text([backend])} {REQUEST_LINK.format(url=url)}."


def passcode_kind(env: Mapping[str, str], passcode: str) -> str | None:
    """'visitor', 'owner' or None for a typed passcode, compared in constant time against the
    deployment secrets, with no guard or budget built (used while live compute is off)."""
    typed = (passcode or "").encode()
    for kind, var in (("owner", OWNER_PASSCODE_VAR), ("visitor", "DEMO_PASSCODE")):
        secret = (env.get(var) or "").strip().encode()
        if secret and hmac.compare_digest(typed, secret):
            return kind
    return None


def page_backends(cache: DemoCache) -> tuple[BackendName, ...]:
    """The panels the page shows: the cache's backends that the demo serves."""
    return tuple(b for b in cache.backends if b in DEMO_BACKENDS)


# -- loading -----------------------------------------------------------------------------------


def cache_source(environ: Mapping[str, str], cwd: Path | None = None) -> Path | None:
    """The replay file to show: `AA_DEMO_CACHE`, else `demo/replay.json`, else None (the
    bundled synthetic example)."""
    explicit = (environ.get("AA_DEMO_CACHE") or "").strip()
    if explicit:
        return Path(explicit)
    default = (cwd or Path.cwd()) / DEFAULT_CACHE
    return default if default.is_file() else None


def load_demo_cache(environ: Mapping[str, str], cwd: Path | None = None) -> DemoCache:
    path = cache_source(environ, cwd)
    return load_example_cache() if path is None else load_cache(path)


# -- what the page shows -----------------------------------------------------------------------


def banner_lines(cache: DemoCache) -> list[str]:
    """DEM-04: the licence credit and the honest Cortex line, plus the synthetic label (the
    default one when the cache's own label is blank or does not say SYNTHETIC)."""
    lines = [cache.attribution, CORTEX_LINE]
    if cache.synthetic:
        lines.insert(0, cache.label if "SYNTHETIC" in cache.label else SYNTHETIC_LABEL)
    return lines


def opening(cache: DemoCache, pick: str | None = None) -> tuple[str | None, list[Panel]]:
    """DEM-01: a sample's panels from replay (no call): `pick`, else the first sample, which
    the page opens on."""
    samples = cache.samples()
    if not samples:
        return None, []
    q = samples[0] if pick is None else pick
    return q, ask_panels(q, cache.backends, "replay", cache=cache)


def _plain(value: Any) -> Any:
    """Frozen schema JSON (FrozenDict, tuples) as plain dicts and lists for Streamlit."""
    return json.loads(json.dumps(value))


def escape_md(text: str) -> str:
    """Model text shown as text, never as markup or links."""
    out = []
    for ch in text:
        out.append("\\" + ch if ch in "\\`*_{}[]()#+-.!|<>~$" else ch)
    return "".join(out)


@dataclass(frozen=True)
class PanelView:
    title: str
    answer: str = ""
    sql: str | None = None
    chart: dict[str, Any] | None = None
    table: list[dict[str, Any]] = field(default_factory=list)
    table_note: str | None = None
    suggestions: tuple[str, ...] = ()
    cost_line: str = ""
    trace: str = ""
    notes: tuple[str, ...] = ()
    error: str | None = None
    as_of: str = ""
    saved: str | None = None  # replay: saved answer, and whether live compute is off


_PATTERNS = Redactor()  # secret patterns only; live mode adds the deployment's secret values


def as_of_line(as_of: date | None) -> str:
    """D-50: every answer is about the data up to this date."""
    return f"Data as of {as_of.isoformat()}" if as_of is not None else ""


def cost_line(panel: Panel, *, live: bool = False, synthetic: bool = False) -> str:
    if synthetic:
        text = (
            f"Synthetic cost ${panel.cost_usd:.4f} · synthetic latency "
            f"{panel.latency_ms / 1000:.1f} s"
        )
    else:
        text = f"Cost ${panel.cost_usd:.4f} · {panel.latency_ms / 1000:.1f} s"
    if live and panel.bound_usd:
        text += f" (held at most ${panel.bound_usd:.2f})"
    return text


def trace_text(panel: Panel, *, synthetic: bool, live: bool) -> str:
    a = panel.answer
    if a.trace_url:
        return f"[Trace]({a.trace_url})"
    if a.trace_export:
        return f"Trace: `{a.trace_export}`"
    if live:
        return LIVE_UNTRACED
    if synthetic:
        return "No trace: synthetic example."
    return "No trace recorded for this answer."


def panel_view(
    panel: Panel,
    *,
    synthetic: bool = False,
    live: bool = False,
    as_of: date | None = None,
    redactor: Redactor | None = None,
    own_key: str | None = None,
    saved: str | None = None,
) -> PanelView:
    """DEM-02: one panel's SQL, answer, chart (or rows), cost, as-of date and trace link. Every
    string shown passes `redactor` (defence in depth: provider text never reaches a panel).
    `own_key` names the provider whose visitor key paid this panel's model calls (OWNER D-56)."""
    r = redactor or _PATTERNS
    a = panel.answer
    model = a.model_snapshot if a.backend != "cortex_analyst" else None
    title = BACKEND_LABELS.get(a.backend, a.backend) + (f" ({model})" if model else "")
    error = None
    if panel.timed_out:
        error = panel.note or "Timed out."
    elif a.error is not None:
        error = panel.note if panel.note in FAILURE_TEXTS else ERROR_TEXTS[a.error]
    elif panel.execution_error is not None:
        error = f"The SQL did not run ({panel.execution_error})."
    text = a.clarification or a.answer or ""
    notes = [n for n in (panel.note,) if n and n != error]
    if a.backend == "cortex_analyst" and CORTEX_LINE not in notes:
        notes.append(CORTEX_LINE)
    if own_key is not None:
        notes.append(own_key_note(own_key))
    table: list[dict[str, Any]] = []
    table_note = None
    if panel.rows is not None and panel.answer.chart is None:
        cols = list(panel.rows.columns)
        table = [dict(zip(cols, _plain(list(r)), strict=True)) for r in panel.rows.rows]
        if panel.rows_total is not None and panel.rows_total > len(table):
            table_note = f"First {len(table)} of {panel.rows_total:,} rows."
    return PanelView(
        title=r.text(title),
        answer=r.text(text),
        sql=r.text(a.sql) if a.sql is not None else None,
        chart=r(_plain(a.chart)) if a.chart is not None else None,
        table=r(table),
        table_note=table_note,
        suggestions=tuple(r.text(s) for s in a.suggestions),
        cost_line=cost_line(panel, live=live, synthetic=synthetic),
        trace=r.text(trace_text(panel, synthetic=synthetic, live=live)),
        notes=tuple(r.text(n) for n in notes),
        error=r.text(error) if error is not None else None,
        as_of=as_of_line(as_of),
        saved=saved,
    )


def budget_line(guard: Any, *, local_store: bool = False) -> str:
    """DEM-03: today's live budget, the per-request hold and the rate limit. The rate limit is
    per browser session (a reload starts a new one), so the daily budget, shared by everyone, is
    the real bound and the line says so."""
    left = guard.remaining_today()
    budget = "unavailable" if left is None else f"${left:.2f} of ${guard.daily_cap:.2f}"
    where = " (local counter)" if local_store else ""
    return (
        f"Today's live budget left: {budget}{where}, shared by all visitors and the real limit · "
        f"each request holds at most ${guard.per_request_cap:.2f} · {guard.rate_limit} live "
        "questions per hour per session"
    )


def own_key_note(provider: str) -> str:
    return (
        f"Model calls paid by your {PROVIDER_NAMES.get(provider, provider)} key; the demo paid "
        "only for the warehouse."
    )


def byok_budget_line(guard: Any) -> str:
    """OWNER D-56: the warehouse limit for questions on a visitor's own key."""
    left = guard.remaining_today()
    budget = "unavailable" if left is None else f"${left:.2f} of ${guard.daily_cap:.2f}"
    return (
        f"Own-key questions: warehouse limit left today {budget}, shared by all own-key visitors "
        f"· {guard.rate_limit} own-key questions per hour per session"
    )


def keys_line(keys: VisitorKeys) -> str:
    names = [f"{PROVIDER_NAMES[p]} ({PANEL_NAMES[p]})" for p in keys.providers()]
    return f"Using your own key for this session: {', '.join(names)}."


def key_paid_line(result: Any) -> str:
    return (
        f"Your own key paid about ${result.key_usd:.4f} for model calls; the demo's own-key "
        f"warehouse limit paid ${result.byok_usd:.4f}."
    )


def take_typed_keys(state: Any) -> None:
    """Submit callback: move the typed keys out of the widgets (emptied) into a pending slot
    that lives only until this run checks the passcode."""
    state[PENDING_STATE] = {p: str(state.get(w) or "") for p, w in KEY_WIDGETS.items()}
    for w in KEY_WIDGETS.values():
        state[w] = ""


def forget_keys(state: Any) -> None:
    """The "Forget my key" control: drop this session's keys and anything typed."""
    keys = state.pop(KEYS_STATE, None)
    if isinstance(keys, VisitorKeys):
        keys.forget()
    state.pop(PENDING_STATE, None)
    for w in KEY_WIDGETS.values():
        state[w] = ""
    state[FORGOT_STATE] = True


def key_providers(backends: Sequence[BackendName], config: RunConfig) -> list[str]:
    """The providers whose key would pay for one of the live agent panels."""
    out: list[str] = []
    for b in backends:
        if b in MCP_BACKENDS and b in config.models:
            p = provider_for_model(config.models[b])
            if p is not None and p not in out:
                out.append(p)
    return out


OWNER_MARKER = "Owner measurement budget"  # p95 page mode reads this (OWNER D-53)


def owner_line(guard: Any) -> str:
    """Shown after an owner ask: which budget paid, and that the visitors' was not touched."""
    left = guard.remaining_today()
    budget = "unavailable" if left is None else f"${left:.2f} of ${guard.daily_cap:.2f}"
    return f"{OWNER_MARKER} (OWNER D-53): {budget} left; the visitors' budget was not used."


def request_cost_line(panels: Sequence[Panel], bound_usd: float) -> str:
    spent = sum(p.cost_usd for p in panels)
    return f"This question cost ${spent:.4f} (held at most ${bound_usd:.2f})."


# -- DEM-05: p95 over live asks ----------------------------------------------------------------


@dataclass(frozen=True)
class P95Report:
    """NFR2 over live asks. Panels are cut at 25 s, so a p95 under 30 s alone proves nothing:
    it passes only when every panel answered (no timed-out and no errored panel), at least 20
    asks ran and none was stopped by the spend cap or refused."""

    n: int
    p50_ms: int
    p95_ms: int
    max_ms: int
    timed_out_panels: int
    latencies_ms: tuple[int, ...]
    errored_panels: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None  # why fewer asks than requested ran

    @property
    def passed(self) -> bool:
        return (
            self.n >= P95_ASKS
            and self.p95_ms < P95_TARGET_MS
            and self.timed_out_panels == 0
            and self.errored_panels == 0
            and self.stopped is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "timed_out_panels": self.timed_out_panels,
            "errored_panels": self.errored_panels,
            "spent_usd": round(self.spent_usd, 6),
            "stopped": self.stopped,
            "latencies_ms": list(self.latencies_ms),
            "target_ms": P95_TARGET_MS,
            "passed": self.passed,
        }


def measure_p95(
    ask_one: Callable[[str], Sequence[Any]],
    questions: Sequence[str],
    *,
    n: int = P95_ASKS,
    clock: Callable[[], float] = time.perf_counter,
    max_cost_usd: float | None = None,
    hold_usd: float = 0.0,
) -> P95Report:
    """Wall time of `n` live asks (cycling through `questions`), the visitor's view: from
    submit to the last panel. `ask_one` returns the panels (anything with `timed_out`, `ok`
    and `cost_usd`); an empty result is a refused request and stops the run. With
    `max_cost_usd`, an ask starts only while what was spent plus its hold fits the cap."""
    from evalkit.stats import percentile_ms

    if not questions:
        raise ValueError("give at least one question")
    lat: list[int] = []
    timed_out = errored = 0
    spent = 0.0
    stopped = None
    for i in range(n):
        if max_cost_usd is not None and spent + hold_usd > max_cost_usd + 1e-9:
            stopped = f"spend cap: ${spent:.2f} spent, next ask holds ${hold_usd:.2f}"
            break
        t0 = clock()
        panels = ask_one(questions[i % len(questions)])
        own = getattr(panels, "elapsed_s", None)  # an asker that times only submit-to-answer
        elapsed = int(((clock() - t0) if own is None else own) * 1000)
        if not panels:
            stopped = "a request was refused"
            spent += hold_usd
            break
        lat.append(elapsed)
        timed_out += sum(bool(p.timed_out) for p in panels)
        errored += sum(not p.ok and not p.timed_out for p in panels)
        spent += sum(float(p.cost_usd) for p in panels)
    return P95Report(
        n=len(lat),
        p50_ms=percentile_ms(lat, 0.5) if lat else 0,
        p95_ms=percentile_ms(lat, 0.95) if lat else 0,
        max_ms=max(lat) if lat else 0,
        timed_out_panels=timed_out,
        latencies_ms=tuple(lat),
        errored_panels=errored,
        spent_usd=spent,
        stopped=stopped,
    )


# -- live wiring -------------------------------------------------------------------------------


def secret_env(st: Any) -> dict[str, str]:
    """Deployment secrets: the host's secret store (st.secrets) over the environment and a
    local, git-ignored .env. Only string values are read (`scripts/streamlit_secrets.py` writes
    every value as a TOML string); a number, a boolean or a table is ignored. Values never
    leave the server."""
    from agentic_analytics.env import load_dotenv

    load_dotenv()
    env = dict(os.environ)
    with contextlib.suppress(Exception):  # no secrets file: a local run
        env.update({k: str(v) for k, v in st.secrets.items() if isinstance(v, str)})
    return env


@dataclass
class Live:
    guard: Any
    ctx: DemoContext
    local_store: bool
    owner_guard: Any = None  # OWNER D-53: reached only through the owner passcode
    ledger: Path | None = None  # where owner asks are recorded
    byok_guard: Any = None  # OWNER D-56: warehouse limit for asks on a visitor's own key


@dataclass(frozen=True)
class LiveSettings:
    """What live mode runs on: the deployment's run configuration (never the replay file's)
    and the pack the replay answers came from."""

    config: RunConfig
    pack: Any
    backends: tuple[BackendName, ...]


def live_settings(
    env: Mapping[str, str],
    cache: DemoCache,
    cwd: Path | None = None,
    *,
    live: Collection[BackendName] | None = None,
) -> LiveSettings | str:
    """Live mode's configuration from `run.yaml` (`AA_RUN_CONFIG`), or why live mode is off:
    the page shows the synthetic example (its as-of date and models are fake), there is no
    readable `run.yaml`, or `run.yaml` disagrees with the replay answers (models, effort, as-of
    date, pack version or fix_hash), so the two modes would answer from different setups.
    `live` (the page passes `DEMO_LIVE_BACKENDS`) keeps only those backends, so services are
    never built for a backend that is not live."""
    from agentic_analytics.demo.cache import pack_fix_hash
    from agentic_analytics.packs import load_pack
    from agentic_analytics.run.config import with_overrides
    from agentic_analytics.schemas import load_run_config

    if cache.synthetic:
        return LIVE_SYNTHETIC
    backends: tuple[BackendName, ...] = tuple(
        b for b in page_backends(cache) if live is None or b in live
    )
    if not backends:
        return compute_off_text(page_backends(cache))
    explicit = (env.get("AA_RUN_CONFIG") or "").strip()
    path = Path(explicit) if explicit else (cwd or Path.cwd()) / DEFAULT_RUN_CONFIG
    try:
        cfg = with_overrides(load_run_config(path), backends=backends, fixes=(), trials=1)
        pack = load_pack(cache.pack.split("@", 1)[0])
        stamp = DemoStamp.of(cfg, backends, pack.ref, pack_fix_hash(pack))
    except Exception as exc:  # a missing or broken run.yaml or pack: never echo the message
        LOG.warning("demo live configuration unavailable (%s)", type(exc).__name__)
        return LIVE_OFF
    replay = DemoStamp.of(cache.config, backends, cache.pack, cache.fix_hash)
    diff = stamp_differences(stamp, replay, ignore=("backends",))
    if diff:
        return (
            f"Live mode is off: this deployment's run.yaml differs from its replay answers in "
            f"{', '.join(diff)}. Rebuild the replay file or fix run.yaml; replay mode works."
        )
    return LiveSettings(cfg, pack, backends)


_LIVE: dict[str, Live] = {}
_LIVE_LOCK = threading.Lock()


def shared_live(
    env: Mapping[str, str],
    settings: LiveSettings,
    *,
    build: Callable[[Mapping[str, str], LiveSettings], Live] | None = None,
) -> Live | None:
    """One guard and one set of live services per server process (the rate-limit memory is
    shared by every session). A failure is logged by type only and retried next time."""
    slot = ",".join(settings.backends)  # one per set of live backends
    with _LIVE_LOCK:
        if slot in _LIVE:
            return _LIVE[slot]
        try:
            live = (build or build_live)(env, settings)
        except Exception as exc:  # never show the message: it can quote a URL or a key
            LOG.warning("demo live mode unavailable (%s)", type(exc).__name__)
            return None
        _LIVE[slot] = live
        return live


def build_live(env: Mapping[str, str], settings: LiveSettings) -> Live:
    """Live services on the deployment's run configuration (no connection until an ask)."""
    from agentic_analytics.demo.ask import context_from_env

    ctx = context_from_env(settings.config, settings.pack, env)
    local = not (env.get("EVALKIT_DEMO_DATABASE_URL") or "").strip()
    try:
        owner = make_owner_guard(env)
    except ValueError as exc:  # a refused owner setup never takes visitor live mode down
        LOG.warning("demo owner budget unavailable (%s)", type(exc).__name__)
        owner = None
    return Live(
        make_guard(env), ctx, local, owner, ledger_path(env) if owner else None,
        make_byok_guard(env),
    )  # fmt: skip


def start_warm_up(executor: Any) -> threading.Thread:
    """H13 in the background: the `SELECT 1` wakes ClickHouse while the agent's first model
    turn runs, instead of before it."""
    t = threading.Thread(target=warm_up, args=(executor,), name="demo-warm-up", daemon=True)
    t.start()
    return t


# -- layout ------------------------------------------------------------------------------------


def render_panel(c: Any, view: PanelView) -> None:
    c.subheader(view.title)
    if view.saved:
        c.info(view.saved)
    if view.as_of:
        c.caption(view.as_of)
    if view.error:
        c.error(view.error)
    if view.answer:
        c.markdown(escape_md(view.answer))
    if view.suggestions:
        c.markdown("Suggestions: " + "; ".join(escape_md(s) for s in view.suggestions))
    if view.sql:
        c.code(view.sql, language="sql")
    if view.chart is not None:
        c.vega_lite_chart(view.chart, width="stretch")  # `use_container_width` is deprecated
    elif view.table:
        # executed rows and column names come from model-written SQL: `st.table` renders GFM
        # in cells and headers, `st.dataframe` shows plain text only
        c.dataframe(view.table, hide_index=True)
        if view.table_note:
            c.caption(view.table_note)
    c.caption(view.cost_line)
    c.markdown(view.trace)
    for note in view.notes:
        c.caption(note)


def show_panels(
    st: Any,
    panels: Sequence[Panel],
    *,
    synthetic: bool,
    live: bool,
    as_of: date | None,
    redactor: Redactor | None = None,
    own_keys: Mapping[str, str] | None = None,
    switch: LiveSwitch | None = None,
    off: Sequence[BackendName] = (),
) -> None:
    """The panels side by side, then one column per backend whose live compute is off."""
    cols = st.columns(len(panels) + len(off))
    for col, p in zip(cols, panels, strict=False):
        view = panel_view(
            p, synthetic=synthetic, live=live, as_of=as_of, redactor=redactor,
            own_key=(own_keys or {}).get(p.backend),
            saved=saved_panel_note(p.answer.backend, switch) if switch is not None else None,
        )  # fmt: skip
        render_panel(col, view)
    for col, b in zip(cols[len(panels) :], off, strict=True):
        col.subheader(BACKEND_LABELS.get(b, b))
        col.info(compute_off_panel_text(b, switch.url if switch else DEFAULT_CONTACT_URL))


def replay_view(st: Any, cache: DemoCache, switch: LiveSwitch | None = None) -> None:
    samples = cache.samples()
    if not samples:
        st.warning("No prepared answers yet. Run `agentic-analytics demo-cache`.")
        return
    pick = st.selectbox("Sample question", samples, index=0)  # the first opens answered
    if not pick:
        return
    _, panels = opening(cache, pick)
    show_panels(st, panels, synthetic=cache.synthetic, live=False, as_of=cache.config.as_of,
                switch=switch or LiveSwitch())  # fmt: skip


def _live_or_why(
    env: Mapping[str, str], cache: DemoCache, live_now: Sequence[BackendName]
) -> tuple[Live | None, tuple[BackendName, ...], str | None]:
    """Live services for the live backends only, or why live mode cannot run."""
    settings = live_settings(env, cache, live=live_now)
    if isinstance(settings, str):
        return None, (), settings
    if problem := live_problem(env, settings.backends):
        return None, (), problem
    live = shared_live(env, settings)
    if live is None:
        return None, (), STORE_DOWN
    return live, settings.backends, None


def live_view(
    st: Any, cache: DemoCache, env: Mapping[str, str], switch: LiveSwitch | None = None
) -> None:
    switch = switch or live_switch(env)
    shown = page_backends(cache)
    live_now: tuple[BackendName, ...] = tuple(b for b in shown if b in switch.on)
    live: Live | None = None
    backends: tuple[BackendName, ...] = ()
    why: str | None = None
    if not live_now:  # OWNER D-59/D-60: nothing is built, connected or reserved
        st.info(LIVE_SWITCHED_OFF.format(link=REQUEST_LINK.format(url=switch.url)))
    else:
        live, backends, why = _live_or_why(env, cache, live_now)
        if why:
            st.warning(why)
    off: tuple[BackendName, ...] = (
        tuple(b for b in shown if b not in backends) if live is not None else shown
    )
    redactor = Redactor.from_environ(env)
    state = st.session_state
    session = state.setdefault("session", secrets.token_urlsafe(16))
    if live is not None:
        st.caption(budget_line(live.guard, local_store=live.local_store))
    config = live.ctx.config if live is not None else cache.config
    providers = key_providers(backends or shown, config)  # OWNER D-60: always shown
    byok_guard = live.byok_guard if live is not None else None
    stored = state.get(KEYS_STATE) if providers else None
    if providers:  # OWNER D-56: own keys, after the passcode
        st.caption(BYOK_NOTICE)
        if byok_guard is not None:
            st.caption(byok_budget_line(byok_guard))
        if state.pop(FORGOT_STATE, False):
            st.caption(KEYS_FORGOTTEN)
        if isinstance(stored, VisitorKeys) and stored:
            st.caption(keys_line(stored))
            st.button(FORGET_LABEL, on_click=forget_keys, args=(state,))
    with st.form("live"):
        passcode = st.text_input("Passcode", type="password")
        question = st.text_area("Your question", max_chars=MAX_QUESTION_CHARS)
        for p in providers:  # read back only through the pending slot (`take_typed_keys`)
            st.text_input(KEY_LABELS[p], type="password", key=KEY_WIDGETS[p])
        go = st.form_submit_button(
            ASK_LABEL, on_click=take_typed_keys if providers else None, args=(state,)
        )
    pending = state.pop(PENDING_STATE, None) or {}
    typed = {p: v for p, v in pending.items() if p in providers and str(v).strip()}
    if not go:
        return
    if live is None:  # nothing may run: check the passcode only, then say why (keys dropped)
        if why is None and passcode_kind(env, passcode) is None:
            log_request(mode="live", backends=shown, latency_ms=0, cost_usd=0.0,
                        outcome="refused")  # fmt: skip
            st.error(WRONG_PASSCODE)
            return
        if why is not None:
            st.warning(why)
        else:
            log_request(mode="live", backends=shown, latency_ms=0, cost_usd=0.0,
                        outcome="refused")  # fmt: skip
            st.warning(f"{compute_off_text(shown)} {REQUEST_LINK.format(url=switch.url)}.")
            show_panels(st, [], synthetic=False, live=True, as_of=None, switch=switch, off=shown)
        if typed:
            st.caption(KEYS_NOT_USED)
        return
    if isinstance(stored, VisitorKeys) and stored:
        redactor = redactor.with_secrets(stored.secrets())
    redactor = redactor.with_secrets({f"{REDACT_NAMES[p]}_TYPED": v for p, v in typed.items()})
    guard, owner = pick_guard(passcode, live.guard, live.owner_guard)  # the passcode alone
    try:
        guard.check_passcode(passcode)  # before anything that reaches a model or warehouse
    except GuardRefused as exc:  # a typed key is dropped with the refused request
        log_request(mode="live", backends=backends, latency_ms=0, cost_usd=0.0, outcome="refused")
        st.error(redactor.text(exc.reason))
        return
    keys: VisitorKeys | None = None
    if owner:  # OWNER D-53 runs on the owner's keys only
        if typed:
            st.caption(OWNER_NO_KEYS)
    elif providers and byok_guard is not None:
        try:
            new = accept_keys(typed, owner_keys=env)
        except KeyFormatError as exc:  # nothing asked: the visitor meant to use their key
            st.error(exc.reason)
            return
        if new:
            stored = stored.merged(new) if isinstance(stored, VisitorKeys) else new
            state[KEYS_STATE] = stored
        keys = stored if isinstance(stored, VisitorKeys) and stored else None
    elif typed:  # no own-key limit on this deployment: the key is not taken
        st.caption(KEYS_NOT_USED)
    if not state.get("warmed") and any(b in MCP_BACKENDS for b in backends):
        state["warmed"] = True  # H13: wake ClickHouse once per session, only once let in
        start_warm_up(live.ctx.services.clickhouse)
    with st.spinner("Asking both backends (each has 25 seconds)..."):
        try:
            result = run_live(
                question,
                ctx=live.ctx,
                guard=guard,
                passcode=passcode,
                session=session,
                backends=backends,
                owner=owner,
                ledger=live.ledger if owner else None,
                visitor_keys=keys,
                byok_guard=byok_guard if keys else None,
                live=switch.on,
            )
        except Exception as exc:  # never a traceback: it could carry the question
            LOG.warning("demo live request failed (%s)", type(exc).__name__)
            st.error("Something went wrong with this question. Replay mode always works.")
            return
    if result.refused:
        st.error(redactor.text(result.refused))
        return
    as_of = live.ctx.config.as_of
    paid = {b: p for b in result.keyed if (p := provider_for_model(live.ctx.config.models[b]))}
    show_panels(st, result.panels, synthetic=False, live=True, as_of=as_of, redactor=redactor,
                own_keys=paid, off=off, switch=switch)  # fmt: skip
    st.caption(request_cost_line(result.panels, result.bound_usd))
    if result.keyed:
        st.caption(key_paid_line(result))
    if result.owner:
        st.caption(owner_line(guard))


def main(st: Any = None) -> None:
    """The page. `st` is the streamlit module (imported here when not given)."""
    if st is None:
        st = importlib.import_module("streamlit")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "openai", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # only the aggregate line at INFO
    st.set_page_config(page_title=PAGE_TITLE, layout="wide")
    env = secret_env(st)
    st.title(HEADLINE)
    switch = live_switch(env)
    redactor = Redactor.from_environ(env)
    for problem in switch.problems:  # for the owner: a setting to fix (names no secret)
        st.error(redactor.text(problem))
    try:
        cache = load_demo_cache(env)
    except (OSError, ValueError) as exc:  # e.g. entries that disagree with the cache's config
        LOG.warning("demo replay file refused (%s)", type(exc).__name__)
        st.error(BAD_CACHE)
        cache = load_example_cache()
    st.info(saved_notice(page_backends(cache), switch))  # near the top (OWNER D-59)
    for line in banner_lines(cache):
        st.info(line)
    mode = st.radio(
        "Mode", ["Sample questions (replay)", "Ask your own (passcode)"], horizontal=True, index=0
    )
    if str(mode).startswith("Sample"):
        replay_view(st, cache, switch)
    else:
        live_view(st, cache, env, switch)


__all__ = [
    "BACKEND_LABELS",
    "BYOK_NOTICE",
    "BYOK_SPEND_KEY",
    "ERROR_TEXTS",
    "FORGET_LABEL",
    "KEYS_STATE",
    "KEY_LABELS",
    "LIVE_OFF",
    "LIVE_SYNTHETIC",
    "OWNER_MARKER",
    "P95_ASKS",
    "P95_TARGET_MS",
    "SECRETS",
    "Live",
    "LiveSettings",
    "P95Report",
    "PanelView",
    "as_of_line",
    "banner_lines",
    "budget_line",
    "build_live",
    "byok_budget_line",
    "cache_source",
    "cost_line",
    "escape_md",
    "forget_keys",
    "key_paid_line",
    "key_providers",
    "keys_line",
    "live_settings",
    "live_view",
    "load_demo_cache",
    "main",
    "measure_p95",
    "opening",
    "own_key_note",
    "owner_line",
    "panel_view",
    "render_panel",
    "replay_view",
    "request_cost_line",
    "secret_env",
    "shared_live",
    "show_panels",
    "start_warm_up",
    "take_typed_keys",
    "trace_text",
]
