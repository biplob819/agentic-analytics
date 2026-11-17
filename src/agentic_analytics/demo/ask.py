"""`ask()` for the demo (D4.1: ASK-01..ASK-05; Tech Spec §10, D-17, NFR2, NFR3, H13).

`ask(question, backends, mode)` returns one `AnalyticsAnswer` per backend; `ask_panels` returns
the same answers as `Panel`s, each with its cost, latency and bound, which is what the page shows.

- **Replay** (the default) reads a demo cache written by `agentic-analytics demo-cache` (or the
  bundled SYNTHETIC example) and makes no call of any kind.
- **Live** mirrors EvalKit's `compare()` (EK D-54): the passcode is checked before anything
  else; then one upper-bound cost is computed per panel, and their sum (the request's hold) is
  the single number the guard checks against the per-request cap, reserves atomically against
  the $3 daily counter under the key `agentic-analytics` (D-17), and that the panels are capped
  at while they run (EK L-39). After every panel has finished or timed out the reservation is
  settled to what was actually charged.
- **The bound is enforced, not hoped for.** Each MCP agent runs through `BudgetedModel`, which
  before every model turn prices that turn's worst case (every prompt token at the highest input
  price, one token per UTF-8 byte for text not yet counted by the provider, and the full output
  limit at the highest output price) and refuses the turn if it would take the panel past its
  bound; a turn that was in flight when the panel timed out or failed counts at its worst case
  (EK L-39: cancelled calls are not free). Cortex panels are priced at one message plus the
  warehouse's 60-second minimum, the most one question can cost.
- **Parallel with a timeout (ASK-04).** Every panel runs in its own thread with its own event
  loop (the executors are synchronous), all started together; a panel not finished after
  `timeout_s` (25 s) is cancelled and says so. `warm_up` sends one `SELECT 1` to ClickHouse (the
  page calls it once a visitor's passcode has passed, H13), at most once per `min_interval_s` per
  process.
- **Same safety layers (ASK-05).** A free-text question reaches the same agent loop as the
  benchmark: every `run_select_query` passes the N1.2 SQL guard first (D-37), the MCP toolbox
  offers only the three allow-listed tools, and the executors run the guard again under the
  read-only roles.
- **Aggregate-only logs.** One JSON line per ask: time, mode, backends, latency, cost and an
  outcome word. Never the question, SQL, answer, passcode or session.
- **Owner measurement budget (OWNER D-53).** The DEM-05 p95 run (20 timed asks in one sitting)
  draws from its own budget, never the visitors' $3/day: `make_owner_guard` is the same EvalKit
  guard with the owner passcode (`DEMO_OWNER_PASSCODE`, a separate secret that must differ from
  the visitor passcode), the per-request cap of $1, its own cap (`DEMO_OWNER_CAP_USD`, default
  $15, refused above $25) and its own counter (key `agentic-analytics-owner`). `pick_guard` is
  the only way in: the typed passcode, compared in constant time; a URL or the question cannot
  select it, and any other passcode is visitor mode. Every owner ask appends a line to the
  spend ledger. The SQL guard and the bounds are the same code in both modes.
- **Visitor own keys (OWNER D-56).** After the visitor passcode, a panel whose model the visitor
  gave a key for (Claude: Anthropic, GPT: OpenAI; `demo.keys`) runs on a per-request SDK client on
  that key, on the deployment's fixed model and effort. Its model calls are the visitor's and are
  not held against the $3/day budget; its warehouse spend (ClickHouse compute, and on such an ask
  the Cortex panel's message and Snowflake compute) is held against a separate daily limit
  (`BYOK_DAILY_CAP_USD`, counter key `agentic-analytics-byok`, the same guard machinery with the
  visitor passcode, $1 per request and 5 asks per session per hour). Agent panels without a key
  keep the $3/day path. A failing visitor key fails its panel with a fixed, secret-free message
  and never falls back to the owner's key. Owner mode never takes a visitor key, and own-key asks
  are never traced or ledgered.
- **Saved answers by default (OWNER D-58, D-59, D-60).** A backend is live only when the owner
  names it in `DEMO_LIVE_BACKENDS` (a comma list of `claude_mcp`, `gpt_mcp`, `cortex_analyst`, or
  `all`); empty or missing means none, and an unknown name is a `DemoSettingError` (live stays
  off for every backend). `live_backends` reads it; `run_live(live=...)` refuses, before the
  passcode or anything else is touched, an ask that names a backend not live. The passcode,
  budgets and own-key rules above apply unchanged to the live backends. `contact_url` gives the
  owner-request link (`DEMO_CONTACT_URL`, an https URL, never an email; default the repo's
  GitHub Issues page).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hmac
import json
import logging
import math
import re
import secrets
import threading
import time
import unicodedata
import weakref
from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args
from urllib.parse import urlsplit

from evalkit.cost import Price, price_for
from evalkit.demo.guard import Guard, GuardRefused, PostgresSpend, SqliteSpend, Ticket
from evalkit.schemas import ResultSet

from agentic_analytics.answer import assemble, with_trace
from agentic_analytics.models.base import ModelTurn, ToolResult, ToolSpec, Usage
from agentic_analytics.schemas import (
    MCP_BACKENDS,
    AnalyticsAnswer,
    BackendName,
    Execution,
    RunConfig,
)
from agentic_analytics.schemas._base import Model, NonNegFloat, NonNegInt

if TYPE_CHECKING:
    from agentic_analytics.demo.cache import DemoCache
    from agentic_analytics.demo.keys import VisitorKeys
    from agentic_analytics.run.runner import Services

Mode = Literal["replay", "live"]

DEMO_BACKENDS: tuple[BackendName, ...] = ("claude_mcp", "cortex_analyst")
SPEND_KEY = "agentic-analytics"  # D-17: this project's row set in the shared spend database
DAILY_CAP_USD = 3.0  # D-17
PER_REQUEST_CAP_USD = 1.0  # a two-panel request's hold is about $0.74 (spec, D4.1)
RATE_LIMIT = 5  # live requests per session per hour (EvalKit's default)
PANEL_TIMEOUT_S = 25.0  # Tech Spec §10: inside NFR2's 30 s p95
MAX_QUESTION_CHARS = 500
LIVE_MAX_TURNS = 6
LIVE_MAX_OUTPUT_TOKENS = 2_048  # per model turn, reasoning included (the adapters' max_tokens)
LIVE_INPUT_TOKENS_PER_TURN = 24_000  # planning size of one turn's prompt, for the hold
PROVIDER_OVERHEAD_TOKENS = 1_000  # the provider's own tool-use preamble, per turn
MESSAGE_OVERHEAD_TOKENS = 32  # framing around each tool result or user message
DEMO_ROW_CAP = 1_000  # rows executed for a panel (the chart builder needs far fewer)
PREVIEW_ROWS = 20  # rows kept on a panel for its table
WAREHOUSE_MIN_S = 60.0  # Snowflake bills at least 60 s per resume
WARM_UP_INTERVAL_S = 300.0
OWNER_PASSCODE_VAR = "DEMO_OWNER_PASSCODE"  # OWNER D-53: never the visitor passcode
OWNER_CAP_VAR = "DEMO_OWNER_CAP_USD"
OWNER_SPEND_KEY = "agentic-analytics-owner"  # OWNER D-53: apart from the visitors' counter
OWNER_CAP_USD = 15.0  # 20 asks x a ~$0.74 hold ~ $14.80 worst case (also p95's --max-cost)
OWNER_CAP_MAX_USD = 25.0  # above this needs the owner's approval; refused here and in p95
OWNER_RATE_LIMIT = 40  # live requests per session per hour: a 20-ask sitting fits twice
OWNER_LEDGER_KIND = "demo-owner"
BYOK_SPEND_KEY = "agentic-analytics-byok"  # OWNER D-56: warehouse spend on own-key asks
BYOK_DAILY_CAP_USD = 5.0  # ~19 two-panel own-key asks a day (warehouse hold ~ $0.25 each)
BYOK_RATE_LIMIT = RATE_LIMIT  # own-key asks per session per hour
LEDGER_VAR = "AA_SPEND_LEDGER"
DEFAULT_LEDGER = Path("docs") / "measurements" / "spend-ledger.jsonl"
WARM_UP_SQL = "SELECT 1"
LOG = logging.getLogger("agentic_analytics.demo")

CORTEX_LINE = (
    "Cortex Analyst returns SQL; its answer text here is a template over the rows it returned."
)
STORE_DOWN = (
    "The live budget store is unavailable right now; try again later. Replay mode always works."
)
LIVE_UNTRACED = "Live answers are not traced: visitor questions are never stored."
LIVE_BACKENDS_VAR = "DEMO_LIVE_BACKENDS"  # OWNER D-59: empty or missing = no backend is live
CONTACT_URL_VAR = "DEMO_CONTACT_URL"
DEFAULT_CONTACT_URL = "https://github.com/biplob819/agentic-analytics/issues"
BACKEND_IDS: tuple[BackendName, ...] = get_args(BackendName)
COMPUTE_OFF = "Live compute is off for {names}, to save compute; nothing was asked."
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")  # urlsplit lowercases the host


# -- limits, bounds and prices -----------------------------------------------------------------


@dataclass(frozen=True)
class DemoLimits:
    """Live-mode limits; every bound below is computed from these."""

    timeout_s: float = PANEL_TIMEOUT_S
    max_turns: int = LIVE_MAX_TURNS
    max_output_tokens: int = LIVE_MAX_OUTPUT_TOKENS
    input_tokens_per_turn: int = LIVE_INPUT_TOKENS_PER_TURN
    row_cap_exec: int = DEMO_ROW_CAP

    def __post_init__(self) -> None:
        if not (math.isfinite(self.timeout_s) and self.timeout_s > 0):
            raise ValueError("timeout_s must be a positive number of seconds")
        for name in ("max_turns", "max_output_tokens", "input_tokens_per_turn", "row_cap_exec"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{name} must be a positive integer")


def worst_prices(price: Price) -> tuple[float, float]:
    """($ per M input token, $ per M output token) at their highest: cache writes and the
    long-context tier can cost more than plain input, so the bound uses the maximum."""
    p_in = max(price.input, price.cache_write, price.long_input or 0.0)
    p_out = max(price.output, price.long_output or 0.0)
    return p_in, p_out


def step_bound_usd(price: Price, tokens_in: int, max_output_tokens: int) -> float:
    """The most one model turn can cost: every prompt token at the highest input price and the
    whole output limit (reasoning included) at the highest output price."""
    p_in, p_out = worst_prices(price)
    return (max(tokens_in, 0) * p_in + max_output_tokens * p_out) / 1_000_000


def strict_tokens(*texts: str) -> int:
    """One token per UTF-8 byte: byte-level tokenizers never emit more (EK D-54)."""
    return sum(len(t.encode("utf-8")) for t in texts)


def _rate(rates: Mapping[str, Any], name: str) -> float:
    r = rates[name]
    return float(getattr(r, "value", r))


def panel_bound_usd(
    backend: BackendName,
    *,
    config: RunConfig,
    prices: Mapping[str, Price],
    rates: Mapping[str, Any],
    limits: DemoLimits,
) -> float:
    """The most one panel may be charged: the hold's share for `backend`."""
    if backend in MCP_BACKENDS:
        return model_bound_usd(backend, config=config, prices=prices, limits=limits) + (
            limits.timeout_s / 3600 * _rate(rates, "clickhouse_usd_per_hour")
        )
    usd_per_credit = _rate(rates, "snowflake_usd_per_credit")
    message = _rate(rates, "cortex_analyst_credits_per_message") * usd_per_credit
    seconds = max(WAREHOUSE_MIN_S, limits.timeout_s)
    warehouse = seconds / 3600 * _rate(rates, "snowflake_xs_credits_per_hour") * usd_per_credit
    return message + warehouse


def model_bound_usd(
    backend: BackendName,
    *,
    config: RunConfig,
    prices: Mapping[str, Price],
    limits: DemoLimits,
) -> float:
    """The model-spend part of an agent panel's bound; `BudgetedModel` enforces it."""
    price = price_for(dict(prices), config.models[backend])
    step = step_bound_usd(price, limits.input_tokens_per_turn, limits.max_output_tokens)
    return limits.max_turns * step


def request_bound_usd(
    backends: Sequence[BackendName],
    *,
    config: RunConfig,
    prices: Mapping[str, Price],
    rates: Mapping[str, Any],
    limits: DemoLimits,
) -> float:
    """The request's hold: what the guard checks, reserves and settles (EK L-39)."""
    return math.fsum(
        panel_bound_usd(b, config=config, prices=prices, rates=rates, limits=limits)
        for b in backends
    )


# -- the budgeted model ------------------------------------------------------------------------


class BudgetedModel:
    """An `AgentModel` that never lets its panel be charged past `cap_usd`.

    Before each turn it prices the turn's worst case (`step_bound_usd`) from an upper bound on
    the prompt: the previous turn's billed prompt plus its whole output plus every byte added
    since (tool results and user messages, one token per byte, with framing), plus the
    provider's tool-use preamble. If that worst case does not fit in what is left, the turn is
    not sent: a zero-cost `max_tokens` stop ends the loop with `step_budget` and `capped` is
    set. A turn in flight is held at its worst case until it returns; if it raises or is
    cancelled it stays charged at that worst case. `stop()` refuses every later turn; it and
    `charged()` share a lock with the turn's start, so a timeout can never miss a turn.
    """

    def __init__(
        self,
        inner: Any,
        price: Price,
        cap_usd: float,
        *,
        max_output_tokens: int,
    ) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.effort = inner.effort
        self.price = price
        self.cap_usd = float(cap_usd)
        own = getattr(inner, "max_tokens", None)
        self.max_output_tokens = max(int(own), max_output_tokens) if own else max_output_tokens
        self.spent = 0.0
        self.in_flight = 0.0
        self.capped = False
        self.turns = 0
        self._next_in = 0
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Any:
        specs = json.dumps(
            [[t.name, t.description, t.parameters] for t in tools], ensure_ascii=False
        )
        self._next_in = strict_tokens(system, user, specs) + PROVIDER_OVERHEAD_TOKENS
        return self.inner.start(system, user, tools)

    def charged(self) -> float:
        with self._lock:
            return self.spent + self.in_flight

    def stop(self) -> float:
        """Refuse every later turn; returns what the panel is charged now (in flight included)."""
        with self._lock:
            self._stopped = True
            return self.spent + self.in_flight

    def _refused_turn(self) -> ModelTurn:
        return ModelTurn(
            text="",
            tool_calls=(),
            stop="max_tokens",
            usage=Usage(tokens_in=0, tokens_out=0),
            cost_usd=0.0,
            latency_ms=0,
            model_snapshot=self.model,
        )

    async def step(self, conv: Any) -> ModelTurn:
        worst = step_bound_usd(self.price, self._next_in, self.max_output_tokens)
        with self._lock:
            if self._stopped or self.spent + worst > self.cap_usd:
                self.capped = self.capped or not self._stopped
                return self._refused_turn()
            self.in_flight = worst
        try:
            turn: ModelTurn = await self.inner.step(conv)
        except BaseException:
            with self._lock:  # a failed or cancelled call may still be billed: keep it held
                self.spent += self.in_flight
                self.in_flight = 0.0
            raise
        cost = float(turn.cost_usd)
        with self._lock:
            self.spent += cost
            self.in_flight = 0.0
            self.turns += 1
        if cost > worst + 1e-12:  # the bound's premise broke: say so (never the content)
            LOG.warning("demo turn cost %.6f exceeded its bound %.6f", cost, worst)
        u = turn.usage
        self._next_in = u.tokens_in + u.tokens_out + u.tokens_reasoning + PROVIDER_OVERHEAD_TOKENS
        return turn

    def add_tool_results(self, conv: Any, results: list[ToolResult]) -> None:
        self._next_in += sum(strict_tokens(r.content) + MESSAGE_OVERHEAD_TOKENS for r in results)
        self.inner.add_tool_results(conv, results)

    def add_user(self, conv: Any, text: str) -> None:
        self._next_in += strict_tokens(text) + MESSAGE_OVERHEAD_TOKENS
        self.inner.add_user(conv, text)


# -- panels ------------------------------------------------------------------------------------


class Panel(Model):
    """One backend's answer as the page shows it, with what it cost and how long it took."""

    backend: BackendName
    answer: AnalyticsAnswer
    cost_usd: NonNegFloat
    latency_ms: NonNegInt
    bound_usd: NonNegFloat = 0.0
    timed_out: bool = False
    capped: bool = False  # an agent stopped before a turn that could pass its bound
    rows: ResultSet | None = None  # the first PREVIEW_ROWS executed rows
    rows_total: NonNegInt | None = None
    execution_error: str | None = None  # "sql_error", "guard", "timeout", "transport"
    note: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.answer.error is None and self.execution_error is None


def _preview(execution: Execution | None) -> dict[str, Any]:
    if execution is None:
        return {}
    if execution.result is None:
        return {"execution_error": execution.error}
    rs = execution.result
    rows = ResultSet(columns=rs.columns, rows=tuple(rs.rows[:PREVIEW_ROWS]))
    return {"rows": rows, "rows_total": execution.rows_total}


def _failed_answer(backend: BackendName, config: RunConfig, error: Any, text: str) -> Any:
    snapshot = config.models.get(backend) if backend in MCP_BACKENDS else None
    return AnalyticsAnswer(
        backend=backend, model_snapshot=snapshot, sql=None, answer=text, error=error
    )


@dataclass
class DemoContext:
    """What live asks need: the configuration (models, as-of date), the services and rates."""

    config: RunConfig
    services: Services
    prices: Mapping[str, Price]
    rates: Mapping[str, Any]
    limits: DemoLimits = field(default_factory=DemoLimits)


_EXEC_LOCKS: weakref.WeakKeyDictionary[Any, threading.Lock] = weakref.WeakKeyDictionary()
_EXEC_LOCKS_GUARD = threading.Lock()
_SHARED_EXEC_LOCK = threading.Lock()  # for an executor that cannot be weakly referenced


def _executor_lock(executor: Any) -> threading.Lock:
    """One lock per executor object: panels run in threads, the drivers are not shared-safe.
    Held weakly, so the table never outgrows the executors alive in the process."""
    with _EXEC_LOCKS_GUARD:
        try:
            return _EXEC_LOCKS.setdefault(executor, threading.Lock())
        except TypeError:  # not weakly referenceable or not hashable: serialise on one lock
            return _SHARED_EXEC_LOCK


def _clickhouse_timeout_usd(ctx: DemoContext) -> float:
    """ClickHouse compute for a whole panel timeout: what an agent panel's warehouse part is
    charged when its own measurement is lost (a timeout or a failure)."""
    return ctx.limits.timeout_s / 3600 * _rate(ctx.rates, "clickhouse_usd_per_hour")


def _sql_guard(query: str) -> str | None:
    """D-37: the agents' MCP queries pass the N1.2 SQL guard before they reach the server."""
    from agentic_analytics.warehouse.guard import check_sql

    verdict = check_sql(query, "clickhouse")
    return None if verdict.ok else (verdict.reason or "refused")


def _why(exc: BaseException) -> str:
    """A panel's failure text. Provider messages are never echoed: some quote part of a key."""
    from agentic_analytics.demo.keys import VisitorKeyFailed

    if isinstance(exc, VisitorKeyFailed):
        return exc.reason  # fixed text per failure kind (OWNER D-56)
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "the provider rejected the API key"
    return f"failed ({name})"


@dataclass
class _Outcome:
    answer: AnalyticsAnswer
    execution: Execution | None
    warehouse_usd: float
    cost_usd: float  # model spend for agents, message spend for Cortex
    tokens: dict[str, int] = field(default_factory=dict)
    cortex_messages: int = 0
    latency_ms: int = 0  # the Cortex API call (trace span)


async def _agent_panel(
    backend: BackendName, question: str, ctx: DemoContext, model: BudgetedModel
) -> _Outcome:
    from agentic_analytics.backends import ask_mcp
    from agentic_analytics.scoring import reexecute

    services, cfg, limits = ctx.services, ctx.config, ctx.limits
    if services.toolbox_source is None or services.clickhouse is None:
        raise RuntimeError(f"{backend} needs the MCP toolbox and the ClickHouse executor")
    async with services.toolbox_source() as new_toolbox:
        run = await ask_mcp(
            question,
            model=model,
            toolbox=new_toolbox(),
            as_of=cfg.as_of,
            max_turns=limits.max_turns,
            wall_s=limits.timeout_s + 5.0,  # the panel timeout fires first
            guard=_sql_guard,
        )
    with _executor_lock(services.clickhouse):  # two agent panels share one executor
        execution = reexecute(run.answer, services.clickhouse, row_cap=limits.row_cap_exec)
    answer = assemble(run.answer, execution, question_text=question)
    seconds = sum(s.latency_ms for s in answer.steps if s.tool == "run_select_query") / 1000
    seconds += (execution.elapsed_ms / 1000) if execution is not None else 0.0
    wh = min(seconds, limits.timeout_s) / 3600 * _rate(ctx.rates, "clickhouse_usd_per_hour")
    return _Outcome(answer, execution, wh, model.charged(), dict(run.tokens))


async def _cortex_panel(question: str, ctx: DemoContext) -> _Outcome:
    from agentic_analytics.backends.cortex import ask_cortex
    from agentic_analytics.run.runner import cortex_question

    services = ctx.services
    if services.cortex is None or services.snowflake is None:
        raise RuntimeError("cortex_analyst needs the Cortex client and the Snowflake executor")
    run = await ask_cortex(
        cortex_question(question, ctx.config.as_of),
        services.cortex,
        services.snowflake,
        row_cap=ctx.limits.row_cap_exec,
    )
    answer = assemble(run.answer, run.execution, question_text=question)
    usd_per_credit = _rate(ctx.rates, "snowflake_usd_per_credit")
    msg = run.cortex_messages * _rate(ctx.rates, "cortex_analyst_credits_per_message")
    wh_s = min(run.warehouse_elapsed_ms / 1000, max(WAREHOUSE_MIN_S, ctx.limits.timeout_s))
    wh = wh_s / 3600 * _rate(ctx.rates, "snowflake_xs_credits_per_hour") * usd_per_credit
    return _Outcome(
        answer,
        run.execution,
        wh,
        msg * usd_per_credit,
        cortex_messages=run.cortex_messages,
        latency_ms=run.api_latency_ms,
    )


class _PanelThread(threading.Thread):
    """Runs one panel's coroutine on its own event loop; `cancel()` cancels it from outside."""

    def __init__(self, make: Callable[[], Coroutine[Any, Any, _Outcome]]) -> None:
        super().__init__(daemon=True)
        self._make = make
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[_Outcome] | None = None
        self.result: _Outcome | None = None
        self.error: BaseException | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self.ready = threading.Event()
        self.finished = threading.Event()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            self._loop = loop
            self._task = loop.create_task(self._make())
            self.ready.set()
            try:
                self.result = loop.run_until_complete(self._task)
            except BaseException as exc:  # reported on the panel, never raised in the thread
                self.error = exc
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            self.ready.set()
            loop.close()
            self.finished_at = time.perf_counter()
            self.finished.set()

    def cancel(self) -> None:
        self.ready.wait(1.0)
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            with contextlib.suppress(RuntimeError):  # the loop already closed: nothing to stop
                loop.call_soon_threadsafe(task.cancel)


def _trace(
    ctx: DemoContext, backend: BackendName, question: str, question_id: str, out: _Outcome
) -> AnalyticsAnswer:
    """Send the panel's trace when a tracer is configured (owner runs only); the answer then
    carries the trace link."""
    from agentic_analytics.tracing import NoopTracer, TraceKey, build_trace

    tracer = ctx.services.tracer
    if tracer is None or isinstance(tracer, NoopTracer):
        return out.answer
    key = TraceKey(run_id="demo-cache", question_id=question_id, backend=backend, trial=1)
    trace = build_trace(
        key,
        question,
        out.answer,
        execution=out.execution,
        cost_usd=out.cost_usd,
        tokens=out.tokens or None,
        cortex_messages=out.cortex_messages,
        latency_ms=out.latency_ms,
        public=True,  # sample questions only: the replay page links to the trace
    )
    try:
        url = tracer.send(trace)
    except Exception as exc:  # tracing never fails an answer
        LOG.warning("demo trace not sent (%s)", type(exc).__name__)
        return out.answer
    return with_trace(out.answer, trace_url=url) if url else out.answer


def run_panels(
    question: str,
    backends: Sequence[BackendName],
    ctx: DemoContext,
    *,
    bounds: Sequence[float] | None = None,
    trace_question_id: str | None = None,
    on_panel: Callable[[int, Panel], None] | None = None,
    agents: Mapping[str, Any] | None = None,
    warehouse: dict[int, float] | None = None,
) -> list[Panel]:
    """Every backend in parallel, each capped at its bound and cut off at `limits.timeout_s`.
    Unguarded: callers that serve visitors go through `ask_panels(mode="live", guard=...)`.
    `agents` replaces the deployment's agent for those backends (a visitor's own key: there is
    no fallback to the deployment's agent). `warehouse`, when given, receives each agent panel's
    ClickHouse compute charge by index (what an own-key ask holds against its own limit)."""
    cfg, limits = ctx.config, ctx.limits
    own = dict(agents or {})
    if bounds is None:
        bounds = [
            panel_bound_usd(b, config=cfg, prices=ctx.prices, rates=ctx.rates, limits=limits)
            for b in backends
        ]
    wh_timeout = _clickhouse_timeout_usd(ctx)
    models: list[BudgetedModel | None] = []
    threads: list[_PanelThread] = []
    for backend, bound in zip(backends, bounds, strict=True):
        if backend in MCP_BACKENDS:
            inner = own[backend] if backend in own else ctx.services.models.get(backend)
            if inner is None:
                raise RuntimeError(f"no model configured for {backend}")
            price = price_for(dict(ctx.prices), cfg.models[backend])
            model = BudgetedModel(
                inner, price, bound - wh_timeout, max_output_tokens=limits.max_output_tokens
            )
            models.append(model)
            make = functools.partial(_agent_panel, backend, question, ctx, model)
        else:
            models.append(None)
            make = functools.partial(_cortex_panel, question, ctx)
        threads.append(_PanelThread(make))
    t0 = time.perf_counter()
    for t in threads:
        t.started_at = time.perf_counter()
        t.start()
    deadline = t0 + limits.timeout_s
    panels: list[Panel | None] = [None] * len(threads)
    pending = set(range(len(threads)))
    while pending:  # fill panels in the order they finish, so a page can show them early
        left = deadline - time.perf_counter()
        if left <= 0:
            break
        done = next((i for i in sorted(pending) if threads[i].finished.is_set()), None)
        if done is None:
            threads[min(pending)].finished.wait(min(left, 0.05))
            continue
        pending.discard(done)
        if warehouse is not None and backends[done] in MCP_BACKENDS:
            got = threads[done].result
            # a failed agent panel's queries may have run: its whole ClickHouse part
            warehouse[done] = got.warehouse_usd if got is not None else wh_timeout
        panels[done] = _finished_panel(
            backends[done], threads[done], models[done], bounds[done], question, ctx,
            trace_question_id,
        )  # fmt: skip
        if on_panel is not None:
            on_panel(done, panels[done])  # type: ignore[arg-type]
    for i in sorted(pending):  # timed out: stop the agent, cancel the task, charge the bound
        charged = models[i].stop() if models[i] is not None else 0.0  # type: ignore[union-attr]
        threads[i].cancel()
        b = backends[i]
        seconds = limits.timeout_s
        if b in MCP_BACKENDS:
            wh = wh_timeout
            cost = charged + wh
            if warehouse is not None:
                warehouse[i] = wh
        else:
            cost = bounds[i]  # the message may have been billed: count the whole bound
        text = f"Timed out after {seconds:g} s; no answer from this backend."
        panels[i] = Panel(
            backend=b,
            answer=_failed_answer(b, cfg, "timeout", text),
            cost_usd=min(cost, bounds[i]),
            latency_ms=int(seconds * 1000),
            bound_usd=bounds[i],
            timed_out=True,
            note=text,
        )
        if on_panel is not None:
            on_panel(i, panels[i])  # type: ignore[arg-type]
    return [p for p in panels if p is not None]


def _finished_panel(
    backend: BackendName,
    thread: _PanelThread,
    model: BudgetedModel | None,
    bound: float,
    question: str,
    ctx: DemoContext,
    trace_question_id: str | None,
) -> Panel:
    latency = int(max(thread.finished_at - thread.started_at, 0.0) * 1000)
    out = thread.result
    if out is None:  # the panel raised: a readable failure, charged what it was charged
        exc = thread.error or RuntimeError("no result")
        LOG.warning("demo panel %s failed (%s)", backend, type(exc).__name__)
        why = _why(exc)
        # an agent's model charge (a failed turn at its worst case) plus its whole ClickHouse
        # part, since queries may have run before the failure; Cortex its whole bound
        charged = model.charged() + _clickhouse_timeout_usd(ctx) if model is not None else bound
        text = f"No answer: {why.rstrip('.')}."
        return Panel(
            backend=backend,
            answer=_failed_answer(backend, ctx.config, "transport", text),
            cost_usd=min(charged, bound),
            latency_ms=latency,
            bound_usd=bound,
            note=why,
        )
    answer = out.answer
    if trace_question_id is not None:
        answer = _trace(ctx, backend, question, trace_question_id, out)
    capped = bool(model is not None and model.capped)
    note = None
    if capped:
        note = "Stopped before a model turn that could have passed the demo's cost cap."
    elif backend == "cortex_analyst":
        note = CORTEX_LINE
    return Panel(
        backend=backend,
        answer=answer,
        cost_usd=min(out.cost_usd + out.warehouse_usd, bound),
        latency_ms=latency,
        bound_usd=bound,
        capped=capped,
        note=note,
        **_preview(out.execution),
    )


# -- the guard ---------------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def keyed_connect(
    key: str = SPEND_KEY, connect: Callable[[str], Any] | None = None
) -> Callable[[str], Any]:
    """A Postgres `connect` for EvalKit's `PostgresSpend` that keeps this project's counter
    under its own key: the schema named `key` in the shared database (D-17). Every transaction
    starts with `SET LOCAL search_path`, so EvalKit's own `demo_spend` (in `public`) is never
    touched and nothing leaks to other clients through a transaction pooler."""
    schema = _quote_ident(key)
    state = {"ready": False}
    lock = threading.Lock()

    def open_conn(url: str) -> Any:
        base = connect
        if base is None:
            import importlib

            base = importlib.import_module("psycopg").connect  # the host's `demo` extra
        con = base(url)
        try:
            with lock:
                if not state["ready"]:
                    try:
                        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                        con.commit()
                    except Exception as exc:  # two processes creating it at once: one wins
                        if type(exc).__name__ not in ("UniqueViolation", "DuplicateSchema"):
                            raise
                        con.rollback()
                    state["ready"] = True
            con.execute(f"SET LOCAL search_path TO {schema}")
        except BaseException:
            with contextlib.suppress(Exception):
                con.close()
            raise
        return con

    return open_conn


def _sqlite_for(key: str) -> Path:
    return Path(".cache") / f"demo-spend-{key}.sqlite"


DEFAULT_SQLITE = _sqlite_for(SPEND_KEY)
DEFAULT_OWNER_SQLITE = _sqlite_for(OWNER_SPEND_KEY)
DEFAULT_BYOK_SQLITE = _sqlite_for(BYOK_SPEND_KEY)


def spend_store(
    environ: Mapping[str, str],
    *,
    key: str = SPEND_KEY,
    sqlite_path: Path | None = None,
    connect: Callable[[str], Any] | None = None,
) -> PostgresSpend | SqliteSpend:
    """Postgres (D-17, EK D-53) when `EVALKIT_DEMO_DATABASE_URL` is set, else local SQLite;
    either way the counter lives under `key` (the visitors' or the owner's, OWNER D-53)."""
    url = (environ.get("EVALKIT_DEMO_DATABASE_URL") or "").strip()
    if url:
        return PostgresSpend(url, keyed_connect(key, connect))
    return SqliteSpend(sqlite_path if sqlite_path is not None else _sqlite_for(key))


def make_guard(
    environ: Mapping[str, str],
    *,
    store: Any = None,
    per_request_cap: float = PER_REQUEST_CAP_USD,
    daily_cap: float = DAILY_CAP_USD,
    **kw: Any,
) -> Guard:
    """EvalKit's guard (D-17) from the deployment secrets: passcode and the keyed counter."""
    return Guard(
        (environ.get("DEMO_PASSCODE") or "").strip(),
        store if store is not None else spend_store(environ),
        per_request_cap=per_request_cap,
        daily_cap=daily_cap,
        rate_limit=kw.pop("rate_limit", RATE_LIMIT),
        **kw,
    )


def check_owner_cap(cap_usd: float) -> float:
    """The owner measurement cap, or ValueError: above $0 and at most $25 (OWNER D-53)."""
    if not (math.isfinite(cap_usd) and 0 < cap_usd <= OWNER_CAP_MAX_USD):
        raise ValueError(
            f"the owner measurement cap must be above $0 and at most ${OWNER_CAP_MAX_USD:.0f}"
        )
    return float(cap_usd)


def owner_cap(environ: Mapping[str, str]) -> float:
    """`DEMO_OWNER_CAP_USD`, default $15; ValueError when unreadable or above $25."""
    raw = (environ.get(OWNER_CAP_VAR) or "").strip()
    try:
        cap = float(raw) if raw else OWNER_CAP_USD
    except ValueError:
        raise ValueError(f"{OWNER_CAP_VAR} is not a number") from None
    return check_owner_cap(cap)


def make_owner_guard(
    environ: Mapping[str, str],
    *,
    store: Any = None,
    cap_usd: float | None = None,
    **kw: Any,
) -> Guard | None:
    """The owner measurement budget (OWNER D-53): EvalKit's guard with the owner passcode, the
    same $1 per-request cap, its own cap ($15 default, refused above $25) and its own counter
    under `agentic-analytics-owner`. None when no owner passcode is configured; ValueError when
    it equals the visitor passcode (visitors would then reach it) or the cap is refused."""
    owner = (environ.get(OWNER_PASSCODE_VAR) or "").strip()
    if not owner:
        return None
    visitor = (environ.get("DEMO_PASSCODE") or "").strip()
    if visitor and hmac.compare_digest(owner.encode(), visitor.encode()):
        raise ValueError("the owner passcode must differ from the visitor passcode")
    cap = owner_cap(environ) if cap_usd is None else check_owner_cap(cap_usd)
    return Guard(
        owner,
        store if store is not None else spend_store(environ, key=OWNER_SPEND_KEY),
        per_request_cap=kw.pop("per_request_cap", PER_REQUEST_CAP_USD),
        daily_cap=cap,
        rate_limit=kw.pop("rate_limit", OWNER_RATE_LIMIT),
        **kw,
    )


def make_byok_guard(
    environ: Mapping[str, str],
    *,
    store: Any = None,
    daily_cap: float = BYOK_DAILY_CAP_USD,
    **kw: Any,
) -> Guard:
    """The warehouse limit for asks on a visitor's own key (OWNER D-56): EvalKit's guard with
    the visitor passcode, $1 per request, its own daily cap (`BYOK_DAILY_CAP_USD`) and rate limit,
    and its own counter under `agentic-analytics-byok` (apart from the $3/day and the owner's)."""
    return Guard(
        (environ.get("DEMO_PASSCODE") or "").strip(),
        store if store is not None else spend_store(environ, key=BYOK_SPEND_KEY),
        per_request_cap=kw.pop("per_request_cap", PER_REQUEST_CAP_USD),
        daily_cap=daily_cap,
        rate_limit=kw.pop("rate_limit", BYOK_RATE_LIMIT),
        **kw,
    )


def check_passcode(guard: Guard, passcode: str) -> None:
    """The guard's constant-time passcode check (`hmac.compare_digest` inside). Text that has
    no UTF-8 form (a lone surrogate from a browser) is a wrong passcode, never an error."""
    try:
        (passcode or "").encode("utf-8")
    except UnicodeEncodeError:
        raise GuardRefused("Wrong passcode.") from None
    guard.check_passcode(passcode or "")


def pick_guard(passcode: str, visitor: Guard, owner: Guard | None) -> tuple[Guard, bool]:
    """The guard a live ask runs under, and whether it is owner mode. Owner mode only when the
    typed passcode is the owner passcode (constant-time compare); every other passcode is
    visitor mode, whose guard then checks it as before. Nothing else selects it."""
    if owner is not None:
        try:
            check_passcode(owner, passcode)  # hmac.compare_digest inside
        except GuardRefused:
            pass
        else:
            return owner, True
    return visitor, False


def ledger_path(environ: Mapping[str, str], cwd: Path | None = None) -> Path:
    """Where owner asks are recorded: `AA_SPEND_LEDGER`, else docs/measurements/."""
    explicit = (environ.get(LEDGER_VAR) or "").strip()
    return Path(explicit) if explicit else (cwd or Path.cwd()) / DEFAULT_LEDGER


def _ledger_owner_ask(
    path: Path, ticket: Ticket, charged: float, n_panels: int, outcome: str
) -> None:
    """One spend-ledger line per owner ask (never the question or the passcode). A failed
    write is logged by type; the budget counter has already been settled."""
    from agentic_analytics.cost import append_ledger

    try:
        append_ledger(
            path,
            kind=OWNER_LEDGER_KIND,
            run_id=f"{OWNER_LEDGER_KIND}-{ticket.day}-{secrets.token_hex(4)}",
            spent_usd=charged,
            note=f"owner measurement ask (OWNER D-53): {n_panels} panels, {outcome}",
        )
    except Exception as exc:
        LOG.warning("demo owner ledger write failed (%s)", type(exc).__name__)


# -- logging -----------------------------------------------------------------------------------


def log_request(
    *,
    mode: Mode,
    backends: Sequence[str],
    latency_ms: int,
    cost_usd: float,
    outcome: str,
) -> str:
    """One aggregate line per ask: time, mode, backends, latency, cost and an outcome word.
    Never the question, SQL, answer, passcode, session or address."""
    line = json.dumps(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mode": mode,
            "backends": list(backends),
            "latency_ms": int(latency_ms),
            "cost_usd": round(float(cost_usd), 6),
            "outcome": outcome,
        },
        sort_keys=True,
    )
    LOG.info(line)
    return line


# -- warm-up (H13) -----------------------------------------------------------------------------


@dataclass(frozen=True)
class WarmUp:
    ok: bool
    elapsed_ms: int
    skipped: bool = False
    error: str | None = None


@dataclass
class WarmState:
    last: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_WARM = WarmState()


def warm_up(
    executor: Any,
    *,
    state: WarmState = _WARM,
    min_interval_s: float = WARM_UP_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> WarmUp:
    """One `SELECT 1` through the read-only ClickHouse executor, so the first live question
    does not pay for waking an idle service (H13). At most once per `min_interval_s` in a
    process, so opening live mode repeatedly cannot keep the service awake."""
    with state.lock:
        now = clock()
        if state.last is not None and now - state.last < min_interval_s:
            return WarmUp(ok=True, elapsed_ms=0, skipped=True)
        state.last = now
    if executor is None:
        return WarmUp(ok=False, elapsed_ms=0, error="no ClickHouse executor")
    t0 = time.perf_counter()
    try:
        with _executor_lock(executor):  # may run beside a panel's re-execution (page thread)
            ex = executor.execute(WARM_UP_SQL, row_cap=1)
    except Exception as exc:
        return WarmUp(ok=False, elapsed_ms=_ms(t0), error=type(exc).__name__)
    return WarmUp(ok=ex.error is None, elapsed_ms=_ms(t0), error=ex.error)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# -- ask ---------------------------------------------------------------------------------------


_COLLAPSED_WHITESPACE = frozenset("\t\n\r")  # a text area's line breaks: become one space


def check_question(question: str) -> str:
    """The visitor's question, whitespace collapsed. Refuses NUL and every other control
    character (category Cc) except tab and line breaks, which a text area sends and which are
    collapsed to a space before the question goes anywhere; Unicode format characters (Cf:
    bidi overrides and isolates, zero-width characters, the BOM) and lone surrogates (Cs),
    which can make the text read differently from what the model receives."""
    for ch in question or "":
        cat = unicodedata.category(ch)
        if (cat == "Cc" and ch not in _COLLAPSED_WHITESPACE) or cat in ("Cf", "Cs"):
            raise ValueError("the question contains a control or invisible formatting character")
    q = " ".join((question or "").split())
    if not q:
        raise ValueError("ask a question")
    if len(q) > MAX_QUESTION_CHARS:
        raise ValueError(f"the question is longer than {MAX_QUESTION_CHARS} characters")
    return q


def _settle(guard: Guard, ticket: Ticket, panels: Sequence[Panel], hold: float) -> float:
    """Settle the reservation to what was charged and return that charge. Any panel missing
    (an exception before the panels existed) leaves the whole hold charged: overcounted, never
    undercounted."""
    actual = math.fsum(p.cost_usd for p in panels) if panels else hold
    charged = min(actual, hold)
    guard.settle(ticket, charged)
    return charged


def ask_panels(
    question: str,
    backends: Sequence[BackendName] = DEMO_BACKENDS,
    mode: Mode = "replay",
    *,
    cache: DemoCache | None = None,
    ctx: DemoContext | None = None,
    guard: Guard | None = None,
    passcode: str = "",
    session: str = "",
    trace_question_id: str | None = None,
    on_panel: Callable[[int, Panel], None] | None = None,
    ledger: Path | None = None,
    visitor_keys: VisitorKeys | None = None,
    byok_guard: Guard | None = None,
    agent_factory: Callable[..., Any] | None = None,
    receipt: Receipt | None = None,
) -> list[Panel]:
    """One `Panel` per backend, in `backends` order (ASK-01).

    Replay needs `cache` and makes no call. Live needs `ctx`; with `guard` (always, for
    visitors) the passcode is checked first and the request's hold is reserved before any
    call. With `ledger` (owner mode, OWNER D-53) every reserved ask, finished or failed, appends
    its settled charge to that spend ledger. With `visitor_keys` (OWNER D-56; needs `guard` and
    `byok_guard`, never `ledger` or tracing) agent panels whose provider has a key run on it:
    their model part is held nowhere, their warehouse part and the Cortex panel's bound against
    `byok_guard`, other agent panels against `guard`. `agent_factory` (tests) builds the agent
    for a key, by default `keys.visitor_agent`; `receipt` receives who paid what. Raises
    `GuardRefused` (a message safe to show), `ValueError` for a bad question and `KeyError` when
    replay has no prepared answers."""
    t0 = time.perf_counter()
    backends = tuple(backends)
    if not backends or len(set(backends)) != len(backends):
        raise ValueError("name each backend once")
    if mode == "replay":
        q = " ".join((question or "").split())
        if cache is None:
            raise KeyError("no demo cache loaded")
        entry = cache.find(q)
        if entry is None:
            raise KeyError("no prepared answers for this question; use live mode")
        by_backend = {p.backend: p for p in entry.panels}
        missing = [b for b in backends if b not in by_backend]
        if missing:
            raise KeyError(f"no prepared answers from {', '.join(missing)}")
        panels = [by_backend[b] for b in backends]
        log_request(
            mode="replay", backends=backends, latency_ms=_ms(t0), cost_usd=0.0, outcome="ok"
        )
        return panels
    if mode != "live":
        raise ValueError(f"mode must be replay or live, not {mode!r}")
    if guard is not None:
        check_passcode(guard, passcode)  # first: a wrong passcode learns nothing else
    q = check_question(question)
    if ctx is None:
        raise ValueError("live mode needs the live services")
    for b in backends:
        if b in MCP_BACKENDS and b not in ctx.config.models:
            raise ValueError(f"no model configured for {b}")
    keyed = keyed_backends(backends, ctx.config, visitor_keys)
    if keyed:
        if guard is None or byok_guard is None:
            raise ValueError("a visitor key is used only behind the passcode and its own limit")
        if ledger is not None:
            raise ValueError("owner mode never takes a visitor key")
        if trace_question_id is not None:
            raise ValueError("asks on a visitor key are never traced")
    bounds = [
        panel_bound_usd(b, config=ctx.config, prices=ctx.prices, rates=ctx.rates, limits=ctx.limits)
        for b in backends
    ]
    hold = math.fsum(bounds)
    if keyed:
        assert guard is not None and byok_guard is not None and visitor_keys is not None
        return _ask_keyed(
            q, backends, ctx, bounds, keyed, visitor_keys, guard, byok_guard, passcode,
            session, on_panel, agent_factory, receipt, t0,
        )  # fmt: skip
    ticket = guard.reserve(passcode, session, hold) if guard is not None else None
    panels: list[Panel] = []
    outcome = "error"
    try:
        panels = run_panels(
            q,
            backends,
            ctx,
            bounds=bounds,
            trace_question_id=trace_question_id,
            on_panel=on_panel,
        )
        outcome = "timeout" if any(p.timed_out for p in panels) else "ok"
    finally:
        cost = math.fsum(p.cost_usd for p in panels) if panels else hold
        if ticket is not None and guard is not None:
            charged = _settle(guard, ticket, panels, hold)
            if receipt is not None:
                receipt.visitor_usd = charged
            if ledger is not None:
                _ledger_owner_ask(ledger, ticket, charged, len(panels), outcome)
        log_request(
            mode="live", backends=backends, latency_ms=_ms(t0), cost_usd=cost, outcome=outcome
        )
    return panels


# -- asks on a visitor's own key (OWNER D-56) --------------------------------------------------


@dataclass
class Receipt:
    """Who paid for one live ask: the $3/day visitor budget, the own-key warehouse limit, and
    (an estimate from the adapters' token counts) the visitor's own provider account."""

    keyed: tuple[BackendName, ...] = ()
    visitor_usd: float = 0.0  # settled against the $3/day budget
    byok_usd: float = 0.0  # settled against the own-key warehouse limit
    key_usd: float = 0.0  # model spend on the visitor's keys (theirs, not the demo's)


def keyed_backends(
    backends: Sequence[BackendName], config: RunConfig, keys: VisitorKeys | None
) -> tuple[BackendName, ...]:
    """The agent panels a visitor key pays for: the panel's model is the deployment's, and the
    visitor gave a key for that model's provider."""
    from agentic_analytics.demo.keys import provider_for_model

    if not keys:
        return ()
    return tuple(
        b
        for b in backends
        if b in MCP_BACKENDS
        and b in config.models
        and keys.get(provider_for_model(config.models[b])) is not None
    )


def visitor_models(
    keyed: Sequence[BackendName],
    ctx: DemoContext,
    keys: VisitorKeys,
    factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One new agent per keyed panel for this request, on the visitor's key, the deployment's
    model and effort, and the demo's output limit; wrapped so its errors are secret-free."""
    from agentic_analytics.demo.keys import KeyedModel, provider_for_model, visitor_agent

    build = factory or visitor_agent
    out: dict[str, Any] = {}
    for b in keyed:
        model = ctx.config.models[b]
        provider = provider_for_model(model)
        key = keys.get(provider)
        assert provider is not None and key is not None
        inner = build(model, ctx.config.effort, ctx.prices, ctx.limits.max_output_tokens, key)
        out[b] = KeyedModel(inner, provider)
    return out


def _ask_keyed(
    q: str,
    backends: tuple[BackendName, ...],
    ctx: DemoContext,
    bounds: list[float],
    keyed: tuple[BackendName, ...],
    keys: VisitorKeys,
    guard: Guard,
    byok_guard: Guard,
    passcode: str,
    session: str,
    on_panel: Callable[[int, Panel], None] | None,
    factory: Callable[..., Any] | None,
    receipt: Receipt | None,
    t0: float,
) -> list[Panel]:
    """An ask with at least one panel on a visitor key. Holds: a keyed panel's ClickHouse part
    and the Cortex panel's whole bound against `byok_guard`; any other agent panel's whole bound
    against `guard` (only when there is one). Each settles to what its panels charged."""
    wh_bound = ctx.limits.timeout_s / 3600 * _rate(ctx.rates, "clickhouse_usd_per_hour")
    on_visitor = [i for i, b in enumerate(backends) if b in MCP_BACKENDS and b not in keyed]
    hold_v = math.fsum(bounds[i] for i in on_visitor)
    hold_b = math.fsum(
        min(wh_bound, bounds[i]) if b in keyed else bounds[i]
        for i, b in enumerate(backends)
        if b not in MCP_BACKENDS or b in keyed
    )
    byok_ticket = byok_guard.reserve(passcode, session, hold_b)  # passcode checked again
    ticket = None
    if on_visitor:
        try:
            ticket = guard.reserve(passcode, session, hold_v)
        except BaseException:
            byok_guard.settle(byok_ticket, 0.0)  # nothing ran: release the warehouse hold
            raise
    panels: list[Panel] = []
    warehouse: dict[int, float] = {}
    outcome = "error"
    try:
        models = visitor_models(keyed, ctx, keys, factory)
        panels = run_panels(
            q, backends, ctx, bounds=bounds, on_panel=on_panel, agents=models,
            warehouse=warehouse,
        )  # fmt: skip
        outcome = "timeout" if any(p.timed_out for p in panels) else "ok"
    finally:
        if panels:
            spent_b = math.fsum(
                warehouse.get(i, 0.0) if b in keyed else panels[i].cost_usd
                for i, b in enumerate(backends)
                if b not in MCP_BACKENDS or b in keyed
            )
            spent_v = math.fsum(panels[i].cost_usd for i in on_visitor)
            key_usd = math.fsum(
                max(panels[i].cost_usd - warehouse.get(i, 0.0), 0.0)
                for i, b in enumerate(backends)
                if b in keyed
            )
        else:  # an exception before the panels existed: the holds stay charged
            spent_b, spent_v, key_usd = hold_b, hold_v, 0.0
        charged_b = min(spent_b, hold_b)
        byok_guard.settle(byok_ticket, charged_b)
        charged_v = 0.0
        if ticket is not None:
            charged_v = min(spent_v, hold_v)
            guard.settle(ticket, charged_v)
        if receipt is not None:
            receipt.keyed = keyed
            receipt.byok_usd = charged_b
            receipt.visitor_usd = charged_v
            receipt.key_usd = key_usd
        cost = math.fsum(p.cost_usd for p in panels) if panels else hold_b + hold_v
        log_request(
            mode="live", backends=backends, latency_ms=_ms(t0), cost_usd=cost, outcome=outcome
        )
    return panels


def ask(
    question: str,
    backends: Sequence[BackendName] = DEMO_BACKENDS,
    mode: Mode = "replay",
    **kw: Any,
) -> list[AnalyticsAnswer]:
    """Tech Spec §4's `ask()`: one `AnalyticsAnswer` per backend. `ask_panels` takes the same
    arguments and also returns each answer's cost and latency."""
    return [p.answer for p in ask_panels(question, backends, mode, **kw)]


# -- the page's live request -------------------------------------------------------------------


class LiveResult(Model):
    panels: tuple[Panel, ...] = ()
    refused: str | None = None  # a message safe to show the visitor
    bound_usd: NonNegFloat = 0.0
    owner: bool = False  # ran under the owner measurement budget (OWNER D-53)
    keyed: tuple[BackendName, ...] = ()  # panels whose model calls the visitor's key paid
    byok_usd: NonNegFloat = 0.0  # charged to the own-key warehouse limit (OWNER D-56)
    key_usd: NonNegFloat = 0.0  # model spend on the visitor's own keys (estimated)


def run_live(
    question: str,
    *,
    ctx: DemoContext,
    guard: Guard,
    passcode: str,
    session: str,
    backends: Sequence[BackendName] = DEMO_BACKENDS,
    on_panel: Callable[[int, Panel], None] | None = None,
    owner: bool = False,
    ledger: Path | None = None,
    visitor_keys: VisitorKeys | None = None,
    byok_guard: Guard | None = None,
    agent_factory: Callable[..., Any] | None = None,
    live: Collection[BackendName],
) -> LiveResult:
    """The page's live ask: refusals and input problems come back as `refused`. `owner` (with
    the owner guard, chosen by `pick_guard`) records each ask in the spend ledger `ledger` and
    never uses `visitor_keys` (OWNER D-53, D-56); a visitor ask with keys draws its warehouse
    spend from `byok_guard`. `live` is required (the page passes `live_backends(env)`): an ask
    naming a backend not in it is refused before anything is touched, owner asks included
    (OWNER D-59, D-60, D-65)."""
    if owner and ledger is None:
        raise ValueError("owner mode records every ask in the spend ledger")
    off: list[BackendName] = [b for b in backends if b not in live]
    if off:
        log_request(mode="live", backends=backends, latency_ms=0, cost_usd=0.0,
                    outcome="refused")  # fmt: skip
        return LiveResult(refused=compute_off_text(off))
    keys = None if owner else visitor_keys
    receipt = Receipt()
    try:
        panels = ask_panels(
            question,
            backends,
            "live",
            ctx=ctx,
            guard=guard,
            passcode=passcode,
            session=session,
            on_panel=on_panel,
            ledger=ledger if owner else None,
            visitor_keys=keys,
            byok_guard=byok_guard if keys else None,
            agent_factory=agent_factory,
            receipt=receipt,
        )
    except GuardRefused as exc:
        log_request(mode="live", backends=backends, latency_ms=0, cost_usd=0.0, outcome="refused")
        return LiveResult(refused=exc.reason)
    except ValueError as exc:
        log_request(mode="live", backends=backends, latency_ms=0, cost_usd=0.0, outcome="bad_input")
        return LiveResult(refused=str(exc))
    return LiveResult(
        panels=tuple(panels),
        bound_usd=math.fsum(p.bound_usd for p in panels),
        owner=owner,
        keyed=receipt.keyed,
        byok_usd=receipt.byok_usd,
        key_usd=receipt.key_usd,
    )


# -- which backends are live (OWNER D-58, D-59, D-60) -------------------------------------------


class DemoSettingError(ValueError):
    """A deployment setting the owner must fix. `reason` is safe to show: it names the setting
    and what was wrong with it, never a secret."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def live_backends(environ: Mapping[str, str]) -> frozenset[BackendName]:
    """The backends the owner turned live (`DEMO_LIVE_BACKENDS`): a comma list of backend ids or
    `all`, case and blanks ignored. Empty or missing: none. Any unknown name is a
    `DemoSettingError` (never silently live, and never a partial list)."""
    raw = environ.get(LIVE_BACKENDS_VAR) or ""
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n != "all" and n not in BACKEND_IDS]
    if unknown:  # the reason is shown on the public page: never the setting's raw value
        raise DemoSettingError(
            f"{LIVE_BACKENDS_VAR} names an unknown backend; use a comma list of "
            f"{', '.join(BACKEND_IDS)}, or all. Live compute stays off until it is fixed."
        )
    if "all" in names:
        return frozenset(BACKEND_IDS)
    return frozenset(n for n in BACKEND_IDS if n in names)


def contact_url(environ: Mapping[str, str]) -> str:
    """Where a visitor asks the owner to turn live compute on (`DEMO_CONTACT_URL`, default the
    repo's GitHub Issues page). `DemoSettingError` unless it is a plain https URL in printable
    ASCII with a dotted host of valid DNS labels and a valid port: never an email (no `mailto:`,
    no `@` anywhere), no spaces, control, invisible or lookalike characters, and no characters
    that would break a Markdown link."""
    raw = (environ.get(CONTACT_URL_VAR) or "").strip()
    if not raw:
        return DEFAULT_CONTACT_URL
    bad = f"{CONTACT_URL_VAR} must be an https URL (never an email); the default link is shown."
    # printable ASCII only: no control, zero-width, bidi or lookalike characters (an IDN host
    # is written in its xn-- form)
    if "@" in raw or any(not "!" <= c <= "~" or c in "()<>[]\"'`\\" for c in raw):
        raise DemoSettingError(bad)
    try:
        parts = urlsplit(raw)
        _ = parts.port  # ValueError for a port that is not a number in range
    except ValueError:
        raise DemoSettingError(bad) from None
    labels = (parts.hostname or "").split(".")
    if (
        parts.scheme != "https"
        or len(labels) < 2
        or not all(_HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise DemoSettingError(bad)
    return raw


def compute_off_text(backends: Sequence[BackendName]) -> str:
    return COMPUTE_OFF.format(names=", ".join(backends) or "every backend")


# -- live services from the environment --------------------------------------------------------


def live_problem(environ: Mapping[str, str], backends: Sequence[BackendName]) -> str | None:
    """Why live mode cannot run on this deployment, or None. Never names a value."""
    from agentic_analytics.warehouse.doctor import executors_from_env

    if not (environ.get("DEMO_PASSCODE") or "").strip():
        return "Live mode is not configured on this deployment; replay mode works."
    ch, sf = executors_from_env(environ)
    need_ch = any(b in MCP_BACKENDS for b in backends)
    if (need_ch and ch is None) or ("cortex_analyst" in backends and sf is None):
        return "Live mode is not configured on this deployment; replay mode works."
    return None


def demo_agent(model: str, effort: Any, prices: Mapping[str, Price], max_tokens: int) -> Any:
    """The benchmark's adapter for `model` with the demo's output limit per turn."""
    from agentic_analytics.models import AnthropicAgent, OpenAIAgent

    if model.startswith("claude-"):
        return AnthropicAgent(model, effort, dict(prices), max_tokens=max_tokens)
    if model.startswith("gpt-"):
        return OpenAIAgent(model, effort, dict(prices), max_tokens=max_tokens)
    raise ValueError(f"no adapter for model {model!r}")


def context_from_env(
    config: RunConfig,
    pack: Any,
    environ: Mapping[str, str],
    *,
    limits: DemoLimits | None = None,
    trace: bool = False,
) -> DemoContext:
    """Live services from the §4 variables (nothing connects until the first ask), with the
    agents' output limit set to the demo's. `trace=False` (visitor asks) drops the tracer."""
    from agentic_analytics.cost import credit_rates, model_prices
    from agentic_analytics.run.runner import services_from_env
    from agentic_analytics.tracing import NoopTracer

    limits = limits or DemoLimits()
    prices = model_prices()
    services = services_from_env(config, pack, environ)
    for b in list(services.models):
        services.models[b] = demo_agent(
            config.models[b], config.effort, prices, limits.max_output_tokens
        )
    if not trace:
        services.tracer = NoopTracer()
    return DemoContext(config, services, prices, credit_rates(), limits)


__all__ = [
    "BACKEND_IDS",
    "BYOK_DAILY_CAP_USD",
    "BYOK_SPEND_KEY",
    "COMPUTE_OFF",
    "CONTACT_URL_VAR",
    "CORTEX_LINE",
    "DAILY_CAP_USD",
    "DEFAULT_CONTACT_URL",
    "DEMO_BACKENDS",
    "LIVE_BACKENDS_VAR",
    "LIVE_UNTRACED",
    "MAX_QUESTION_CHARS",
    "OWNER_CAP_MAX_USD",
    "OWNER_CAP_USD",
    "OWNER_PASSCODE_VAR",
    "OWNER_SPEND_KEY",
    "PANEL_TIMEOUT_S",
    "PER_REQUEST_CAP_USD",
    "SPEND_KEY",
    "STORE_DOWN",
    "BudgetedModel",
    "DemoContext",
    "DemoLimits",
    "DemoSettingError",
    "LiveResult",
    "Mode",
    "Panel",
    "Receipt",
    "WarmState",
    "WarmUp",
    "ask",
    "ask_panels",
    "check_owner_cap",
    "check_passcode",
    "check_question",
    "compute_off_text",
    "contact_url",
    "context_from_env",
    "demo_agent",
    "keyed_backends",
    "keyed_connect",
    "ledger_path",
    "live_backends",
    "live_problem",
    "log_request",
    "make_byok_guard",
    "make_guard",
    "make_owner_guard",
    "model_bound_usd",
    "owner_cap",
    "panel_bound_usd",
    "pick_guard",
    "request_bound_usd",
    "run_live",
    "run_panels",
    "spend_store",
    "step_bound_usd",
    "strict_tokens",
    "visitor_models",
    "warm_up",
    "worst_prices",
]
