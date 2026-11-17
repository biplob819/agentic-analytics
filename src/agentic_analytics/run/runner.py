"""The benchmark runner (R3.1: RUN-01..RUN-05; Tech Spec §2.1, §2.3, §2.6, §9, §11).

`run_benchmark(cfg, pack, services=..., options=...)` fans out backend × question (and probe) ×
fix set × trial:

1. **Preconditions (RUN-04).** The current snapshot's parity record and the pack's clean gold
   check (`run/gold.py`); any failure stops before anything else with exit 1.
2. **Plan.** One job per (backend, item, fix set, trial) with its D-1 cache key. Cached jobs
   cost nothing; the estimate covers the rest, is reported before the first call and stops the
   run with exit 3 if it is over `max_cost_usd`. `--dry-run` stops here with exit 0.
3. **Ask and Execute** (uncached jobs only), per backend with its own pool of
   `concurrency[backend]` workers. Each MCP worker owns its MCP connection (opened in its own
   task, as N1.5 requires) and builds a fresh `Toolbox` per question; `run_select_query` passes
   the N1.2 SQL guard first (D-37). Every ask reserves its estimated cost on a `SpendCap` and
   settles the actual cost; a reservation past the cap stops new work (in-flight asks finish and
   are cached) and the run exits 3 with a resume hint. Config errors stop the run (exit 2, EK
   L-21). Results are cached atomically as soon as they exist.
4. **Score** every job from the cache entry and the stored gold (S2.1), stamp the `Record`
   (git sha, prompt/fix/semantic hashes, gold_check_id, snapshot, seed) and export traces for
   failures and a deterministic sample (A2.3). One writer task appends `records.jsonl` and the
   JSON-lines `run.log` (RUN-03).
5. **Finish.** Write-probe row-count fingerprints before and after, `summary.json` (M2.1), the
   dashboard (B3.1), `results/latest`, and a spend-ledger line when anything was spent. Exit 1
   when a probe failed or the fingerprint changed.

`rescore=True` recomputes records and the summary from the cache alone (no services needed);
it exits 2 if any job is not cached.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import math
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from agentic_analytics.aggregate import (
    Rates,
    build_summary,
    latency_ms,
    probe_passed,
    probes_failed,
    write_summary,
)
from agentic_analytics.answer import assemble, with_trace
from agentic_analytics.backends.prompts import as_of_line, prompt_hash
from agentic_analytics.cost import (
    CostCapReached,
    SpendCap,
    append_ledger,
    credit_rates,
    estimate_run,
    model_prices,
)
from agentic_analytics.packs import Pack, PackError, read_pack_text
from agentic_analytics.questions import load_probes, load_questions
from agentic_analytics.run.cache import (
    AnswerCache,
    AskResult,
    KeyParts,
    cache_key,
    sha256_json,
    sha256_text,
)
from agentic_analytics.run.config import (
    ASSUMPTIONS_FILE,
    RunConfigError,
    concurrency_for,
    fixsets,
    load_assumptions,
    measure_assumptions,
    save_assumptions,
)
from agentic_analytics.run.gold import (
    AnyGold,
    GoldCheck,
    PreconditionError,
    current_snapshot,
    require_gold,
    require_parity,
)
from agentic_analytics.schemas import (
    ENGINE_OF,
    ENGINES,
    MCP_BACKENDS,
    SCHEMA_VERSION,
    BackendName,
    Engine,
    FixName,
    Probe,
    Question,
    Record,
    RunConfig,
    RunSummary,
)
from agentic_analytics.schemas.config import SNOWFLAKE_REGION_PATTERN
from agentic_analytics.scoring import reexecute, score_record
from agentic_analytics.tracing import (
    NoopTracer,
    Redactor,
    Trace,
    TraceKey,
    Tracer,
    build_trace,
    export_trace,
    select_for_export,
    trace_from_record,
)

EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_COST_CAP = 0, 1, 2, 3
CORTEX_PROMPT_VERSION = "cortex-v1"
SMOKE_PER_CATEGORY = 2
NO_GIT_SHA = "0000000"
RunKind = Literal["run", "smoke", "rescore"]
ToolboxSource = Callable[[], AbstractAsyncContextManager[Callable[[], Any]]]
# A stopwatch factory: each call starts a watch; calling the watch gives the elapsed ms.
Stopwatch = Callable[[], Callable[[], int]]
CATEGORY_ORDER = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")


def utc_now() -> datetime:
    return datetime.now(UTC)


def perf_stopwatch() -> Callable[[], int]:
    """A wall-clock watch started now (`time.perf_counter`); calling it gives the elapsed ms."""
    t0 = time.perf_counter()
    return lambda: max(0, round((time.perf_counter() - t0) * 1000))


@dataclass(frozen=True)
class CodeVersion:
    """Which code made a run. `sha` is the source checkout's commit (`0000000` when the package
    is not in a git checkout, e.g. an installed wheel); `dirty` says the checkout had
    uncommitted changes to tracked files (None outside git); `version` is the package version."""

    sha: str
    dirty: bool | None
    version: str

    @property
    def label(self) -> str:
        if self.dirty is None:
            return f"v{self.version}"
        return self.sha + ("+dirty" if self.dirty else "")

    def as_dict(self) -> dict[str, Any]:
        return {"git_sha": self.sha, "dirty": self.dirty, "version": self.version}


def _git(folder: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def code_version(package_dir: Path | None = None) -> CodeVersion:
    """The code version of the package's own source, never of the current folder: the package
    directory must be tracked by the git checkout around it (a wheel installed in some other
    repository's virtualenv is not), else the package version is recorded instead."""
    from agentic_analytics import __version__

    folder = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parents[1]
    tracked = _git(folder, "ls-files", "--error-unmatch", "__init__.py")
    sha = _git(folder, "rev-parse", "HEAD") if tracked is not None else None
    if not sha or len(sha) not in range(7, 41) or any(c not in "0123456789abcdef" for c in sha):
        return CodeVersion(NO_GIT_SHA, None, __version__)
    status = _git(folder, "status", "--porcelain", "--untracked-files=no", "--", ".")
    return CodeVersion(sha, bool(status) if status is not None else None, __version__)


def git_sha(package_dir: Path | None = None) -> str:
    """The commit for `Record.git_sha` (hex only; see `code_version` for dirty and version)."""
    return code_version(package_dir).sha


@dataclass
class Services:
    """What uncached asks need. Rescores and fully cached reruns never build one."""

    models: dict[str, Any] = field(default_factory=dict)  # MCP backend -> AgentModel
    toolbox_source: ToolboxSource | None = None  # per worker: opens a connection, yields a factory
    clickhouse: Any = None  # Executor (agents' final SQL; write-probe fingerprints)
    snowflake: Any = None  # Executor (Cortex SQL; write-probe fingerprints)
    cortex: Any = None  # CortexClient (anything with `async send(question) -> bytes`)
    self_check: Any = None  # F3.1's SelfCheck hook, for the self_check fix
    tracer: Tracer = field(default_factory=NoopTracer)

    def executor(self, engine: Engine) -> Any:
        return self.clickhouse if engine == "clickhouse" else self.snowflake


@dataclass
class RunOptions:
    results_dir: Path = Path("results")
    cache_dir: Path = Path(".cache")
    measurements_dir: Path = Path("docs/measurements")
    ledger_path: Path | None = Path("docs/measurements/spend-ledger.jsonl")
    engines: tuple[Engine, ...] | None = None  # configured engines; default: the backends' engines
    semantic_text: str | None = None  # default: the pack's semantic.yaml, if any
    kind: RunKind = "run"
    fix_hash_reason: str | None = None  # logged reason to run fixes against another baseline hash
    dry_run: bool = False
    rescore: bool = False
    question_ids: Sequence[str] | None = None
    include_probes: bool = True
    run_id: str | None = None
    now: Callable[[], datetime] = utc_now
    stopwatch: Stopwatch = perf_stopwatch  # times each ask, question in to answer out
    git_sha: str | None = None
    n_resamples: int = 10_000
    sample_every: int = 10
    render: bool = True
    wall_s: float = 120.0
    prices: Any = None
    rates: Any = None  # credit_rates() mapping
    redactor: Redactor | None = None
    say: Callable[[str], None] | None = None


@dataclass
class RunOutcome:
    exit_code: int
    message: str
    run_id: str | None = None
    run_dir: Path | None = None
    summary: RunSummary | None = None
    jobs: int = 0
    cached: int = 0
    called: int = 0
    estimate_usd: float = 0.0
    remaining_usd: float = 0.0
    warehouse_usd: float = 0.0
    spent_usd: float = 0.0
    cumulative_usd: float = 0.0  # model spend behind every answer of the run, cached ones too
    gold_mode: str | None = None
    probes_failed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.exit_code == EXIT_OK,
            "exit_code": self.exit_code,
            "message": self.message,
            "run_id": self.run_id,
            "run_dir": None if self.run_dir is None else str(self.run_dir),
            "jobs": self.jobs,
            "cached": self.cached,
            "called": self.called,
            "estimate_usd": round(self.estimate_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "warehouse_usd": round(self.warehouse_usd, 6),
            "spent_usd": round(self.spent_usd, 6),
            "cumulative_usd": round(self.cumulative_usd, 6),
            "gold_mode": self.gold_mode,
            "probes_failed": list(self.probes_failed),
        }


@dataclass(frozen=True)
class Job:
    backend: BackendName
    item: Question | Probe
    fixes: tuple[FixName, ...]
    trial: int
    parts: KeyParts
    key: str

    @property
    def engine(self) -> Engine:
        return ENGINE_OF[self.backend]

    @property
    def label(self) -> str:
        fx = "+".join(self.fixes) or "baseline"
        return f"{self.backend}/{fx}/{self.item.id}/t{self.trial}"


# -- hashing ---------------------------------------------------------------------------------


def item_hash(item: Question | Probe) -> str:
    """The scoring stamp (`Record.question_hash`): the whole question, gold included."""
    return sha256_json(item.model_dump(mode="json"))


def ask_hash(item: Question | Probe) -> str:
    """The answer-cache hash: only what a backend sees (id and text). Tolerance, gold SQL,
    interpretations, author and review status change scoring, never the answer."""
    return sha256_json({"id": item.id, "text": item.text})


def config_hash(cfg: RunConfig) -> str:
    return sha256_json(cfg.model_dump(mode="json"))


def agent_prompt_hash(as_of: date) -> str:
    """The MCP agents' prompt hash without the server's tool descriptions, which are only known
    after connecting: the key must be computable offline for resume and rescore."""
    return prompt_hash(as_of, ())


def cortex_prompt_hash(as_of: date) -> str:
    return sha256_json({"version": CORTEX_PROMPT_VERSION, "as_of_line": as_of_line(as_of)})


def fix_hash(fixes: Sequence[str], blocks: Mapping[str, str]) -> str:
    return sha256_json({f: blocks.get(f) for f in fixes})  # None only in a refused dry run


def cortex_question(text: str, as_of: date) -> str:
    """D-10: Cortex gets the same as-of line as the agents, ahead of the question."""
    return f"{as_of_line(as_of)}\n\n{text}"


def smoke_questions(questions: Sequence[Question], per: int = SMOKE_PER_CATEGORY) -> list[Question]:
    """The first `per` questions of each category, by id (12 for the six categories)."""
    out: list[Question] = []
    for cat in CATEGORY_ORDER:
        out += sorted((q for q in questions if q.category == cat), key=lambda q: q.id)[:per]
    return out


# -- planning --------------------------------------------------------------------------------


def _limits(cfg: RunConfig, backend: str, wall_s: float) -> dict[str, float | int]:
    if backend in MCP_BACKENDS:
        return {
            "max_tool_calls": cfg.max_tool_calls,
            "max_turns": cfg.max_turns,
            "row_cap_model": cfg.row_cap_model,
            "row_cap_exec": cfg.row_cap_exec,
            "wall_s": wall_s,
        }
    return {"row_cap_exec": cfg.row_cap_exec}


def plan_jobs(
    cfg: RunConfig,
    questions: Sequence[Question],
    probes: Sequence[Probe],
    *,
    snapshot_sha256: str,
    fix_blocks: Mapping[str, str],
    semantic_hash: str,
    semantic_view: str = "",
    wall_s: float = 120.0,
) -> list[Job]:
    jobs: list[Job] = []
    hashes = {it.id: ask_hash(it) for it in (*questions, *probes)}
    ph = {"agent": agent_prompt_hash(cfg.as_of), "cortex": cortex_prompt_hash(cfg.as_of)}
    for backend in cfg.backends:
        mcp = backend in MCP_BACKENDS
        sets = fixsets(cfg) if mcp else ((),)
        items: list[Question | Probe] = [*questions, *(p for p in probes if backend in p.backends)]
        for fixes in sets:
            fh = fix_hash(fixes, fix_blocks)
            for item in items:
                for trial in range(1, cfg.trials + 1):
                    parts = KeyParts(
                        backend=backend,
                        model_snapshot=cfg.models[backend] if mcp else backend,
                        question_hash=hashes[item.id],
                        fixes=tuple(fixes),
                        fix_hash=fh,
                        prompt_hash=ph["agent" if mcp else "cortex"],
                        # an MCP baseline never sees the semantic model
                        semantic_hash=semantic_hash if (fixes or not mcp) else None,
                        snapshot_sha256=snapshot_sha256,
                        trial=trial,
                        as_of=cfg.as_of.isoformat(),
                        limits=_limits(cfg, backend, wall_s),
                        params={"effort": cfg.effort} if mcp else {"semantic_view": semantic_view},
                    )
                    jobs.append(Job(backend, item, tuple(fixes), trial, parts, cache_key(parts)))
    return jobs


def per_ask_estimate(
    cfg: RunConfig, n_items: int, *, prices: Any, rates: Any, assumptions: Any
) -> tuple[dict[str, float], dict[str, float]]:
    """Estimated dollars per ask for each backend (Tech Spec §9), split into model cash (what
    `max_cost_usd` caps: "model spend is cash") and warehouse spend paid from trial credits
    (Cortex messages and the XS warehouse at $/credit, ClickHouse compute shared over the
    agents' asks), which is reported but not capped."""
    est = estimate_run(cfg, max(n_items, 1), prices=prices, rates=rates, assumptions=assumptions)
    ch = next((ln for ln in est.lines if ln.backend == "clickhouse"), None)
    ch_share = ch.usd / ch.asks if ch is not None and ch.asks else 0.0
    cash: dict[str, float] = {}
    credits: dict[str, float] = {}
    for ln in est.lines:
        if ln.backend == "clickhouse" or not ln.asks:
            continue
        cash[ln.backend] = ln.usd / ln.asks
        credits[ln.backend] = ln.credits * est.usd_per_credit / ln.asks + (
            ch_share if ln.backend in MCP_BACKENDS else 0.0
        )
    return cash, credits


# -- the single writer (RUN-03) --------------------------------------------------------------


class Writer:
    """The only code that appends to records.jsonl and run.log; workers send it events."""

    def __init__(
        self, run_dir: Path, now: Callable[[], datetime], redactor: Redactor | None = None
    ) -> None:
        self.run_dir, self.now, self.redactor = run_dir, now, redactor
        self.queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self.records: list[Record] = []

    def log(self, event: str, **fields: Any) -> None:
        """Every log line passes the redactor (secrets in exception text never reach disk)."""
        payload: dict[str, Any] = {"event": event, **fields}
        if self.redactor is not None:
            payload = self.redactor(json.loads(json.dumps(payload, default=str)))
        self.queue.put_nowait(("log", payload))

    def record(self, record: Record) -> None:
        self.queue.put_nowait(("record", record))

    async def run(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        rec_path, log_path = self.run_dir / "records.jsonl", self.run_dir / "run.log"
        with (
            rec_path.open("a", encoding="utf-8", newline="\n") as rec_fh,
            log_path.open("a", encoding="utf-8", newline="\n") as log_fh,
        ):
            while True:
                item = await self.queue.get()
                if item is None:
                    break
                kind, payload = item
                if kind == "record":
                    self.records.append(payload)
                    rec_fh.write(payload.model_dump_json() + "\n")
                    rec_fh.flush()
                else:
                    line = {"at": self.now().isoformat(timespec="seconds"), **payload}
                    log_fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
                    log_fh.flush()

    async def close(self) -> None:
        await self.queue.put(None)


# -- asks ------------------------------------------------------------------------------------


def _guard(query: str) -> str | None:
    """D-37: the agents' MCP queries pass the N1.2 SQL guard before they reach the server."""
    from agentic_analytics.warehouse.guard import check_sql

    verdict = check_sql(query, "clickhouse")
    return None if verdict.ok else (verdict.reason or "refused")


async def ask_agent(
    job: Job,
    cfg: RunConfig,
    services: Services,
    new_toolbox: Callable[[], Any],
    fix_blocks: Mapping[str, str],
    wall_s: float,
    stopwatch: Stopwatch = perf_stopwatch,
) -> AskResult:
    from agentic_analytics.backends import ask_mcp

    blocks = [fix_blocks[f] for f in job.fixes if f in ("descriptions", "examples")]
    watch = stopwatch()
    run = await ask_mcp(
        job.item.text,
        model=services.models[job.backend],
        toolbox=new_toolbox(),
        as_of=cfg.as_of,
        fix_blocks=blocks,
        max_turns=cfg.max_turns,
        wall_s=wall_s,
        self_check=(
            # F3.1: the hook is told whether this question expects no rows, so a correct empty
            # answer to an expects_empty question gets no "zero rows" feedback
            functools.partial(
                services.self_check, expects_empty=bool(getattr(job.item, "expects_empty", False))
            )
            if "self_check" in job.fixes and services.self_check is not None
            else None
        ),
        guard=_guard,
    )
    execution = reexecute(run.answer, services.clickhouse, row_cap=cfg.row_cap_exec)
    wall_ms = watch()  # beside the step latencies M2.1 sums (L-9)
    answer = assemble(run.answer, execution, question_text=job.item.text)
    tokens = {k: int(run.tokens.get(k, 0)) for k in ("in", "out", "cached", "reasoning")}
    return AskResult(
        answer=answer,
        execution=execution,
        cost_usd=float(run.cost_usd),
        tokens=tokens,
        cortex_messages=0,
        warehouse_elapsed_ms=execution.elapsed_ms if execution is not None else 0,
        turns=run.turns,
        ask_wall_ms=wall_ms,
    )


async def ask_cortex_job(
    job: Job, cfg: RunConfig, services: Services, stopwatch: Stopwatch = perf_stopwatch
) -> AskResult:
    """One Cortex ask. Its wall time is measured here, around `ask_cortex`: the API round trip
    (retries included) plus the execution of Cortex's SQL, so a Cortex answer is timed question
    in to answer out like the agents' (`Record.ask_wall_ms`, M2.1's latency). Cortex's own
    `analyst_latency_ms` is kept beside it, so the report can separate it from the transit."""
    from agentic_analytics.backends.cortex import ask_cortex

    watch = stopwatch()
    run = await ask_cortex(
        cortex_question(job.item.text, cfg.as_of),
        services.cortex,
        services.snowflake,
        row_cap=cfg.row_cap_exec,
    )
    wall_ms = watch()
    answer = assemble(run.answer, run.execution, question_text=job.item.text)
    return AskResult(
        answer=answer,
        execution=run.execution,
        cost_usd=0.0,  # Cortex spend is credits, counted through cortex_messages (§9)
        tokens={},
        cortex_messages=run.cortex_messages,
        warehouse_elapsed_ms=run.warehouse_elapsed_ms,
        turns=0,
        ask_wall_ms=wall_ms,
        analyst_latency_ms=run.response.analyst_latency_ms if run.response is not None else None,
    )


# -- write-probe row-count fingerprint -------------------------------------------------------


def fingerprint(pack: Pack, services: Services, engines: Sequence[Engine]) -> dict[str, Any]:
    """Row counts of every pack table on every engine, read through the read-only executors."""
    out: dict[str, Any] = {}
    for engine in engines:
        ex = services.executor(engine)
        if ex is None:
            out[engine] = None
            continue
        db = pack.manifest.databases[engine]
        for table in pack.manifest.tables:
            if engine == "clickhouse":
                sql = f"SELECT count() AS n FROM {db}.{table}"
            else:
                sql = f"SELECT COUNT(*) AS N FROM {db}.{table.upper()}"
            execution = ex.execute(sql, row_cap=1)
            rs = execution.result
            out[f"{engine}:{table}"] = rs.rows[0][0] if rs is not None and rs.rows else None
    return out


# -- Snowflake region (OWNER D-62) -----------------------------------------------------------

REGION_SQL = "SELECT CURRENT_REGION() AS REGION"


def snowflake_region(cfg: RunConfig, services: Services | None) -> dict[str, Any] | None:
    """The run stamp's Snowflake account region, for the OWNER D-62 caveat beside every Cortex
    latency figure: `run.yaml`'s `snowflake_region` when given, else `SELECT CURRENT_REGION()`
    through the read-only Snowflake executor at run start. None when the run has no Cortex
    backend. A region that cannot be read (no executor because every answer is cached, a failed
    or odd query) is stamped `region: None` with the reason, and the run goes on: the caveat
    then says the region was not recorded."""
    if cfg.snowflake_region is not None:
        return {"region": cfg.snowflake_region.upper(), "source": "run.yaml"}
    if "cortex_analyst" not in cfg.backends:
        return None
    ex = services.snowflake if services is not None else None
    if ex is None:
        return {"region": None, "source": None, "error": "no Snowflake executor (all cached)"}
    try:
        execution = ex.execute(REGION_SQL, row_cap=1)
        rs = execution.result
        value = rs.rows[0][0] if rs is not None and rs.rows and rs.rows[0] else None
        error = execution.error
    except Exception as exc:  # a failed probe never stops the run; the asks will report it
        value, error = None, type(exc).__name__
    if isinstance(value, str) and re.match(SNOWFLAKE_REGION_PATTERN, value.strip()):
        return {"region": value.strip().upper(), "source": "current_region()"}
    return {"region": None, "source": "current_region()", "error": error or "unreadable"}


def stamped_region(manifest: Mapping[str, Any]) -> str | None:
    """The region a run's run.json stamped, or None."""
    stamp = manifest.get("snowflake_region")
    region = stamp.get("region") if isinstance(stamp, Mapping) else None
    return region if isinstance(region, str) and region else None


# -- the run ---------------------------------------------------------------------------------


def _say(options: RunOptions, text: str) -> None:
    if options.say is not None:
        options.say(text)


def _gold_engines(cfg: RunConfig, options: RunOptions) -> tuple[Engine, ...]:
    used = {ENGINE_OF[b] for b in cfg.backends}
    configured = options.engines if options.engines is not None else tuple(used)
    missing = [e for e in used if e not in configured]
    if missing:
        raise RunConfigError(f"the run needs {', '.join(sorted(missing))}, which is not configured")
    return tuple(e for e in ENGINES if e in configured)


def _semantic_hash(pack: Pack, options: RunOptions) -> str:
    """N1.4's `semantic_hash` of the pack's semantic model (sha256 of no text without one)."""
    if options.semantic_text is not None:
        return sha256_text(options.semantic_text)
    try:
        read_pack_text(pack, "semantic.yaml")
    except PackError:
        return sha256_text("")
    from agentic_analytics.semantic import SemanticError, load_semantic, semantic_hash

    try:
        return semantic_hash(load_semantic(pack))
    except SemanticError as exc:
        raise RunConfigError(str(exc)) from exc


def semantic_view_name(pack: Pack) -> str:
    """Cortex's semantic view: N1.4's default name for the pack, `DB.SCHEMA.<TABLE>_SEMANTIC`."""
    db = pack.manifest.databases.get("snowflake")
    if db is None:
        return ""
    try:
        read_pack_text(pack, "semantic.yaml")
        from agentic_analytics.semantic import default_names, load_semantic

        return default_names(pack, load_semantic(pack))[0]
    except Exception:  # no or invalid semantic model: the pack's first table, by convention
        return f"{db}.{pack.manifest.tables[0].upper()}_SEMANTIC"


def frozen_fixes(cfg: RunConfig, pack: Pack) -> tuple[dict[str, str], str, str | None]:
    """F3.1's frozen fix content (D-13, D-48, D-52): every run, baseline included, refuses
    (PreconditionError, exit 1) unless `fixes/FROZEN.json` exists and the pack's current fix
    content matches it. Returns the fix blocks (descriptions and examples prompt texts,
    self-check rules text), the frozen `fix_hash` every record carries, and the freeze's
    logged reason when F3.1 records one."""
    from agentic_analytics.fixes import FixError, fix_blocks, frozen_problems, load_frozen

    try:
        frozen = load_frozen(pack)
        problems = frozen_problems(pack)
        blocks = fix_blocks(pack) if not problems and cfg.fixes else {}
    except FixError as exc:
        raise PreconditionError(f"fix content: {exc}") from exc
    if frozen is None or problems:
        why = problems[0] if problems else f"{pack.ref} has no fixes/FROZEN.json"
        raise PreconditionError(
            f"the fixes must be frozen before any run, baselines included (D-13): {why}; "
            "freeze with `python -m agentic_analytics.fixes`"
        )
    reason = getattr(frozen, "reason", None)
    return blocks, frozen.fix_hash, reason if isinstance(reason, str) else None


def baseline_fix_hash_check(
    cfg: RunConfig,
    jobs: Sequence[Job],
    cached: Mapping[str, AskResult],
    fix_hash_now: str,
    options: RunOptions,
) -> dict[str, Any] | None:
    """D-52: a fix run's frozen fix_hash must equal the one on its baseline answers (cached
    from an earlier invocation). A difference refuses (exit 1) unless the operator gives a
    reason, which is logged in run.log and run.json."""
    if not cfg.fixes:
        return None
    seen = sorted(
        {
            cached[j.key].fix_hash  # type: ignore[misc]
            for j in jobs
            if not j.fixes and j.backend in MCP_BACKENDS and j.key in cached
        }
        - {None, fix_hash_now}
    )
    if not seen:
        return None
    if not (options.fix_hash_reason or "").strip():
        raise PreconditionError(
            f"the baseline answers were made under fix_hash {seen[0][:12]}, the fixes are now "
            f"frozen as {fix_hash_now[:12]} (D-52); rerun the baseline, or pass "
            "--force-fix-hash REASON to compare against it anyway"
        )
    return {"reason": options.fix_hash_reason, "baseline": seen, "fix_hash": fix_hash_now}


NO_SNAPSHOT = "0" * 64  # dry-run cache keys when no snapshot record exists yet


def dry_run_selection(pack: Pack, bank: Sequence[Question]) -> list[Question]:
    """What a run would ask without a clean gold check: the gold check's selection when one can
    be read, else the first `quotas[category]` questions of each category by id."""
    from agentic_analytics.run.gold import load_gold_check

    try:
        check = load_gold_check(pack)
    except PreconditionError:
        check = None
    if check is not None:
        wanted = set(check.questions)
        return [q for q in sorted(bank, key=lambda q: q.id) if q.id in wanted]
    out: list[Question] = []
    for cat in CATEGORY_ORDER:
        n = pack.manifest.quotas.get(cat, 0)
        out += sorted((q for q in bank if q.category == cat), key=lambda q: q.id)[:n]
    return out


def self_check_hook(pack: Pack, services: Services) -> Any:
    """F3.1's self-check hook over the read-only ClickHouse executor and the pack's rules."""
    from agentic_analytics.fixes import FixError, load_rules, make_self_check

    if services.clickhouse is None:
        return None
    try:
        return make_self_check(services.clickhouse, load_rules(pack))
    except FixError as exc:
        raise RunConfigError(f"self-check: {exc}") from exc


def _check_services(services: Services, jobs: Sequence[Job]) -> None:
    need = {j.backend for j in jobs}
    problems: list[str] = []
    for b in sorted(need):
        if b in MCP_BACKENDS:
            if services.models.get(b) is None:
                problems.append(f"{b}: no model")
            if services.toolbox_source is None:
                problems.append(f"{b}: no MCP connection")
            if services.clickhouse is None:
                problems.append(f"{b}: no ClickHouse executor")
        else:
            if services.cortex is None:
                problems.append("cortex_analyst: no Cortex client")
            if services.snowflake is None:
                problems.append("cortex_analyst: no Snowflake executor")
    if any("self_check" in j.fixes for j in jobs) and services.self_check is None:
        problems.append("self_check: no self-check hook (needs the ClickHouse executor)")
    if problems:
        raise RunConfigError("cannot run: " + "; ".join(problems))


def _record(
    job: Job,
    res: AskResult,
    gold: Mapping[tuple[str, str], AnyGold],
    cfg: RunConfig,
    *,
    run_id: str,
    sha: str,
    gold_check_id: str,
    fix_hash: str,
    semantic_hash: str,
    now: datetime,
) -> Record:
    g = gold.get((job.item.id, job.engine))  # question gold, or a probe's gold (Q2.2)
    gold_rs = g.result if g is not None else None
    scores = score_record(
        job.item, res.answer, res.execution, gold_rs, max_tool_calls=cfg.max_tool_calls
    )
    return Record(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        question_id=job.item.id,
        question_hash=item_hash(job.item),  # D-1: the whole question, gold included
        backend=job.backend,
        fixes=job.fixes,
        trial=job.trial,
        answer=res.answer,
        execution=res.execution,
        scores=scores,
        tokens={k: v for k, v in res.tokens.items() if k in ("in", "out", "cached", "reasoning")},
        cost_usd=res.cost_usd,
        cortex_messages=res.cortex_messages,
        warehouse_elapsed_ms=res.warehouse_elapsed_ms,
        warehouse_credits_alloc=None,
        ask_wall_ms=res.ask_wall_ms,
        analyst_latency_ms=res.analyst_latency_ms if job.backend == "cortex_analyst" else None,
        timestamp=now,
        seed=cfg.seed,
        git_sha=sha,
        prompt_hash=job.parts.prompt_hash,
        fix_hash=fix_hash,
        semantic_hash=semantic_hash,
        gold_check_id=gold_check_id,
        snapshot_sha256=job.parts.snapshot_sha256,
    )


def record_trace(record: Record, *, question: str, public: bool = False) -> Trace:
    """A2.3's trace of a stored record. A Cortex record timed by the runner gets its Cortex span
    from the measured wall time less the execution (the API round trip), so its spans add up to
    the wall time (L-9) instead of showing the execution alone."""
    if record.backend != "cortex_analyst" or record.ask_wall_ms is None:
        return trace_from_record(record, question=question, public=public)
    executed = record.execution.elapsed_ms if record.execution is not None else 0
    key = TraceKey(record.run_id, record.question_id, record.backend, record.trial, record.fixes)
    return build_trace(
        key,
        question,
        record.answer,
        execution=record.execution,
        cost_usd=record.cost_usd,
        tokens=dict(record.tokens),
        cortex_messages=record.cortex_messages,
        latency_ms=max(0, record.ask_wall_ms - executed),
        public=public,
        scores=record.scores.model_dump(mode="json"),
        started_at=record.timestamp,
    )


def _with_trace(
    record: Record,
    job: Job,
    options: RunOptions,
    tracer: Tracer | None,
    redactor: Redactor,
) -> Record:
    key = TraceKey(record.run_id, record.question_id, record.backend, record.trial, record.fixes)
    correct = probe_passed(record) if isinstance(job.item, Probe) else record.scores.correct
    reason = select_for_export(key, correct=correct, sample_every=options.sample_every)
    # ANS-04: the deterministic sample is public (in the export and on Langfuse); failures not
    trace = record_trace(record, question=job.item.text, public=(reason == "sample"))
    url = None
    if tracer is not None and not isinstance(tracer, NoopTracer):
        url = tracer.send(trace)
    path = None
    if reason is not None:
        path = export_trace(trace, options.results_dir, redactor=redactor)
    if url is None and path is None:
        return record
    answer = with_trace(record.answer, trace_url=url, trace_export=path)
    return Record.model_validate({**record.model_dump(), "answer": answer.model_dump()})


def write_latest(results_dir: Path, run_id: str) -> None:
    p = results_dir / "latest"
    tmp = results_dir / ".latest.tmp"
    tmp.write_text(run_id + "\n", encoding="utf-8")
    os.replace(tmp, p)


def resolve_run(results_dir: Path, spec: str) -> Path:
    """`latest` (from results/latest) or a run id, as a run folder that exists."""
    run_id = spec
    if spec == "latest":
        p = results_dir / "latest"
        if not p.is_file():
            raise RunConfigError(f"no runs yet in {results_dir} (no {p})")
        run_id = p.read_text(encoding="utf-8").strip()
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise RunConfigError(f"bad run id {spec!r}")
    d = results_dir / run_id
    if not d.is_dir():
        raise RunConfigError(f"no run {run_id!r} in {results_dir}")
    return d


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def aggregate_rates(rates: Mapping[str, Any]) -> Rates:
    return Rates(
        credits_per_message=rates["cortex_analyst_credits_per_message"].value,
        usd_per_credit=rates["snowflake_usd_per_credit"].value,
    )


def summarise(
    records: Sequence[Record],
    questions: Mapping[str, Question],
    manifest: Mapping[str, Any],
    rates: Rates,
) -> RunSummary:
    """summary.json from a run's records and its run.json manifest (used by run and reconcile)."""
    return build_summary(
        records,
        questions,
        run_id=manifest["run_id"],
        config_hash=manifest["config_hash"],
        snapshot_sha256=manifest["snapshot_sha256"],
        as_of=date.fromisoformat(manifest["as_of"]),
        gold_check_id=manifest["gold_check_id"],
        pack=manifest["pack"],
        gold_mode=manifest["gold_mode"],
        rates=rates,
        seed=manifest["seed"],
        started_at=datetime.fromisoformat(manifest["started_at"]),
        finished_at=datetime.fromisoformat(manifest["finished_at"]),
        n_resamples=manifest["n_resamples"],
    )


def run_benchmark(
    cfg: RunConfig,
    pack: Pack,
    *,
    services: Services | Callable[[], Services] | None = None,
    options: RunOptions | None = None,
) -> RunOutcome:
    """Run (or dry-run, or rescore) the benchmark; never raises for operator errors."""
    options = options or RunOptions()
    try:
        return asyncio.run(_run(cfg, pack, services, options))
    except PreconditionError as exc:
        return RunOutcome(EXIT_FAILED, f"refusing to start: {exc}")
    except (RunConfigError, PackError) as exc:
        return RunOutcome(EXIT_ERROR, str(exc))


async def _run(
    cfg: RunConfig,
    pack: Pack,
    services_in: Services | Callable[[], Services] | None,
    options: RunOptions,
) -> RunOutcome:
    from agentic_analytics.questions import BankError

    engines = _gold_engines(cfg, options)
    measurements = Path(options.measurements_dir)

    # 1. preconditions (RUN-04, D-13). A dry run reports a failed one and still estimates
    #    (OSS-03: no accounts, no records needed); a real run refuses with exit 1.
    refusals: list[str] = []

    def soft(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except PreconditionError as exc:
            if not options.dry_run:
                raise
            refusals.append(str(exc))
            return None

    fix_setup = soft(frozen_fixes, cfg, pack)
    blocks, record_fix_hash, freeze_reason = (
        fix_setup if fix_setup is not None else ({}, sha256_json({}), None)
    )
    sem_hash = _semantic_hash(pack, options)
    snapshot: str | None = soft(current_snapshot, measurements)
    if snapshot is not None:
        soft(require_parity, measurements, snapshot, engines)
    try:
        bank = load_questions(pack)
    except BankError as exc:
        raise RunConfigError(str(exc)) from exc
    got = (
        soft(functools.partial(require_gold, as_of=cfg.as_of), pack, bank, engines, snapshot)
        if snapshot is not None
        else None
    )
    check: GoldCheck | None
    if got is not None:
        check, selected, gold = got
    else:  # dry run only
        check, gold = None, {}
        selected = dry_run_selection(pack, bank)
    snapshot_key: str = snapshot if snapshot is not None else NO_SNAPSHOT
    gold_mode: Literal["cross_engine", "single_engine"] = (
        "single_engine"
        if len(engines) == 1 or (check is not None and check.mode == "single_engine")
        else "cross_engine"
    )
    questions = list(selected)
    if options.question_ids is not None:
        wanted = set(options.question_ids)
        unknown = wanted - {q.id for q in questions}
        if unknown:
            raise RunConfigError(f"questions not in the gold check: {sorted(unknown)}")
        questions = [q for q in questions if q.id in wanted]
    probes: list[Probe] = []
    if options.include_probes:
        try:
            probes = list(load_probes(pack))
        except PackError:
            probes = []
        except BankError as exc:
            raise RunConfigError(str(exc)) from exc

    # 2. plan
    jobs = plan_jobs(
        cfg,
        questions,
        probes,
        snapshot_sha256=snapshot_key,
        fix_blocks=blocks,
        semantic_hash=sem_hash,
        semantic_view=semantic_view_name(pack),
        wall_s=options.wall_s,
    )
    if not jobs:
        raise RunConfigError("nothing to run: no questions selected")
    cache = AnswerCache(options.cache_dir)
    cached: dict[str, AskResult] = {}
    for j in jobs:
        if j.key not in cached:
            hit = cache.get(j.key)
            if hit is not None:
                cached[j.key] = hit
    remaining = [j for j in jobs if j.key not in cached]
    override = soft(baseline_fix_hash_check, cfg, jobs, cached, record_fix_hash, options)
    rates = options.rates if options.rates is not None else credit_rates()
    prices = options.prices if options.prices is not None else model_prices()
    assumptions = load_assumptions(measurements / ASSUMPTIONS_FILE)
    per_ask, per_ask_wh = per_ask_estimate(
        cfg, len(questions) + len(probes), prices=prices, rates=rates, assumptions=assumptions
    )
    total_usd = math.fsum(per_ask.get(j.backend, 0.0) for j in jobs)
    remaining_usd = math.fsum(per_ask.get(j.backend, 0.0) for j in remaining)
    warehouse_usd = math.fsum(per_ask_wh.get(j.backend, 0.0) for j in remaining)
    base = dict(
        jobs=len(jobs),
        cached=len(jobs) - len(remaining),
        estimate_usd=total_usd,
        remaining_usd=remaining_usd,
        warehouse_usd=warehouse_usd,
        gold_mode=gold_mode,
    )
    _say(
        options,
        f"{pack.ref}: {len(questions)} questions, {len(probes)} probes, "
        f"{len(cfg.backends)} backends, {len(fixsets(cfg))} fix sets, {cfg.trials} trials = "
        f"{len(jobs)} asks ({len(jobs) - len(remaining)} cached). Estimate ${total_usd:.2f} "
        f"model spend, remaining ${remaining_usd:.2f}, plus about ${warehouse_usd:.2f} of "
        "Cortex and warehouse use from trial credits"
        + (f", cap ${cfg.max_cost_usd:.2f}" if cfg.max_cost_usd is not None else "")
        + (" [single-engine gold]" if gold_mode == "single_engine" else ""),
    )
    if options.rescore and remaining:
        return RunOutcome(
            EXIT_ERROR,
            f"--rescore needs every answer cached; {len(remaining)} of {len(jobs)} are not "
            "(run without --rescore to fill them)",
            **base,  # type: ignore[arg-type]
        )
    if options.dry_run:
        msg = "dry run: no calls made"
        if refusals:
            msg += f"; a real run would refuse to start: {refusals[0]}"
        return RunOutcome(EXIT_OK, msg, **base)  # type: ignore[arg-type]
    assert check is not None and not refusals  # a real run raised above
    if cfg.max_cost_usd is not None and remaining_usd > cfg.max_cost_usd:
        return RunOutcome(
            EXIT_COST_CAP,
            f"the model-spend estimate for the remaining asks (${remaining_usd:.2f}) is over the "
            f"${cfg.max_cost_usd:.2f} cap; nothing was called. Raise --max-cost or narrow the run",
            **base,  # type: ignore[arg-type]
        )

    services: Services | None = None
    if remaining:
        services = services_in() if callable(services_in) else services_in
        if services is None:
            raise RunConfigError("uncached asks need backends, but no services were configured")
        if services.self_check is None and any("self_check" in j.fixes for j in remaining):
            services.self_check = self_check_hook(pack, services)
        _check_services(services, remaining)
    region = snowflake_region(cfg, services)  # OWNER D-62, before the first ask

    # 3. run
    started = options.now()
    chash = config_hash(cfg)
    run_id = options.run_id or (f"{options.kind}-{started.strftime('%Y%m%dT%H%M%SZ')}-{chash[:6]}")
    run_dir = Path(options.results_dir) / run_id
    if run_dir.exists():
        raise RunConfigError(f"run folder {run_dir} already exists")
    run_dir.mkdir(parents=True)
    version = code_version()
    sha = options.git_sha or version.sha
    redactor = options.redactor if options.redactor is not None else Redactor.from_environ()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": options.kind,
        "run_id": run_id,
        "config": cfg.model_dump(mode="json"),
        "config_hash": chash,
        "pack": pack.ref,
        "pack_source": pack.name if pack.bundled else str(pack.root),
        "snapshot_sha256": snapshot,
        "gold_check_id": check.gold_check_id,
        "fix_hash": record_fix_hash,
        "fix_freeze_reason": freeze_reason,
        "fix_hash_override": override,
        "semantic_hash": sem_hash,
        "gold_mode": gold_mode,
        "engines": list(engines),
        "as_of": cfg.as_of.isoformat(),
        "seed": cfg.seed,
        "n_resamples": options.n_resamples,
        "git_sha": sha,
        "code": {**version.as_dict(), "git_sha": sha, "label": version.label},
        "snowflake_region": region,
        "started_at": started.isoformat(),
        "finished_at": None,
    }
    _write_json(run_dir / "run.json", manifest)
    (run_dir / "questions.jsonl").write_text(
        "".join(q.model_dump_json() + "\n" for q in questions), encoding="utf-8", newline="\n"
    )
    writer = Writer(run_dir, options.now, redactor)
    writer_task = asyncio.create_task(writer.run())
    writer.log(
        "start",
        run_id=run_id,
        kind=options.kind,
        jobs=len(jobs),
        cached=len(jobs) - len(remaining),
        estimate_usd=round(total_usd, 6),
        remaining_usd=round(remaining_usd, 6),
        gold_mode=gold_mode,
        code=version.label,
        snowflake_region=region,
    )
    if override is not None:
        writer.log("fix_hash_override", **override)

    cap = SpendCap(cfg.max_cost_usd)
    fresh: dict[str, AskResult] = {}
    done = {"closed": False}

    def redact(text: str) -> str:
        return redactor.text(text)

    async def finish(outcome: RunOutcome) -> RunOutcome:
        """Always runs once: run.json gets finished_at (and the error), the writer closes, and
        the spend-ledger line is appended whenever anything was called."""
        if done["closed"]:
            return outcome
        done["closed"] = True
        called, spent = len(fresh), cap.spent
        manifest["finished_at"] = options.now().isoformat()
        manifest["exit_code"] = outcome.exit_code
        manifest["called"] = called
        manifest["spent_usd"] = round(spent, 6)
        manifest["cumulative_usd"] = round(outcome.cumulative_usd, 6)
        try:
            writer.log("finish", exit_code=outcome.exit_code, message=outcome.message)
            await writer.close()
            await writer_task
        finally:
            _write_json(run_dir / "run.json", manifest)
            if called and options.ledger_path is not None:
                messages = sum(r.cortex_messages for r in fresh.values())
                per_message = rates["cortex_analyst_credits_per_message"].value
                credits = {"snowflake": messages * per_message} if messages else {}
                append_ledger(
                    Path(options.ledger_path),
                    kind=options.kind,
                    run_id=run_id,
                    spent_usd=spent,
                    credits=credits,  # trial credits are recorded too (Cortex messages)
                    note=f"{called} asks called, exit {outcome.exit_code}; {messages} Cortex "
                    "messages; warehouse credits not metered here",
                    at=options.now(),
                )
        return outcome

    def common() -> dict[str, Any]:
        results = {**cached, **fresh}
        return dict(
            run_id=run_id,
            run_dir=run_dir,
            called=len(fresh),
            spent_usd=cap.spent,
            cumulative_usd=math.fsum(results[j.key].cost_usd for j in jobs if j.key in results),
            **base,
        )

    try:
        return await _execute(
            cfg,
            pack,
            options,
            services=services,
            jobs=jobs,
            remaining=remaining,
            cached=cached,
            fresh=fresh,
            cache=cache,
            cap=cap,
            per_ask=per_ask,
            blocks=blocks,
            gold=gold,
            questions=questions,
            engines=engines,
            check=check,
            manifest=manifest,
            writer=writer,
            run_id=run_id,
            run_dir=run_dir,
            sha=sha,
            record_fix_hash=record_fix_hash,
            sem_hash=sem_hash,
            redactor=redactor,
            rates=rates,
            measurements=measurements,
            finish=finish,
            common=common,
        )
    except Exception as exc:  # anything after the run folder exists: record it, keep the cache
        text = redact(f"{type(exc).__name__}: {exc}")[:300]  # redact the whole text, then cut
        manifest["error"] = text
        writer.log("error", error=text)
        return await finish(
            RunOutcome(
                EXIT_ERROR,
                f"runtime error after {len(fresh)} new answers: {text}. Cached answers are "
                "kept; fix the problem and rerun the same command to resume",
                **common(),
            )
        )
    finally:
        if not done["closed"]:  # cancelled (Ctrl-C): still close the writer and run.json
            await finish(RunOutcome(EXIT_ERROR, "interrupted", **common()))


async def _execute(
    cfg: RunConfig,
    pack: Pack,
    options: RunOptions,
    *,
    services: Services | None,
    jobs: list[Job],
    remaining: list[Job],
    cached: dict[str, AskResult],
    fresh: dict[str, AskResult],
    cache: AnswerCache,
    cap: SpendCap,
    per_ask: Mapping[str, float],
    blocks: Mapping[str, str],
    gold: Mapping[tuple[str, str], AnyGold],
    questions: Sequence[Question],
    engines: Sequence[Engine],
    check: GoldCheck,
    manifest: dict[str, Any],
    writer: Writer,
    run_id: str,
    run_dir: Path,
    sha: str,
    record_fix_hash: str,
    sem_hash: str,
    redactor: Redactor,
    rates: Any,
    measurements: Path,
    finish: Callable[[RunOutcome], Any],
    common: Callable[[], dict[str, Any]],
) -> RunOutcome:
    """Ask the uncached jobs, then score, summarise and report (inside `_run`'s try)."""
    write_probe_jobs = [
        j for j in remaining if isinstance(j.item, Probe) and j.item.kind == "write"
    ]
    before: dict[str, Any] | None = None
    if services is not None and write_probe_jobs:
        before = fingerprint(pack, services, engines)
        writer.log("fingerprint", stage="before", counts=before)

    stop = asyncio.Event()
    stop_reason: list[str] = []
    fatal: list[BaseException] = []
    turns_by_backend: dict[str, list[tuple[int, Mapping[str, int]]]] = {}
    queues: dict[str, deque[Job]] = {}
    seen: set[str] = set()
    for j in remaining:
        if j.key in seen:
            continue
        seen.add(j.key)
        queues.setdefault(j.backend, deque()).append(j)

    async def do_ask(job: Job, new_toolbox: Callable[[], Any] | None) -> None:
        assert services is not None
        try:
            reservation = await cap.reserve(per_ask.get(job.backend, 0.0))
        except CostCapReached as exc:
            stop_reason.append(str(exc))
            stop.set()
            return
        if job.backend in MCP_BACKENDS:
            assert new_toolbox is not None
            res = await ask_agent(
                job, cfg, services, new_toolbox, blocks, options.wall_s, options.stopwatch
            )
        else:
            res = await ask_cortex_job(job, cfg, services, options.stopwatch)
        res = dataclasses.replace(res, fix_hash=record_fix_hash)
        actual = res.cost_usd  # model cash; Cortex is paid in credits (cost_usd 0)
        if not res.transient:  # transport failures and timeouts are retried on resume
            cache.put(job.key, res, job.parts)
        fresh[job.key] = res
        if job.backend in MCP_BACKENDS:
            turns_by_backend.setdefault(job.backend, []).append((res.turns, dict(res.tokens)))
        await cap.settle(reservation, actual)
        writer.log(
            "ask",
            node="ask",
            cache_key=job.key,  # not `key`: the redactor scrubs fields named key
            cache_hit=False,
            cached=not res.transient,
            job=job.label,
            cost_usd=round(actual, 6),
            error=res.answer.error,
        )

    async def worker(backend: str) -> None:
        assert services is not None
        q = queues[backend]
        if not q:
            return
        if backend in MCP_BACKENDS:
            assert services.toolbox_source is not None
            async with services.toolbox_source() as new_toolbox:
                while q and not stop.is_set():
                    await do_ask(q.popleft(), new_toolbox)
        else:
            while q and not stop.is_set():
                await do_ask(q.popleft(), None)

    async def guarded(backend: str) -> None:
        try:
            await worker(backend)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # config errors and bugs stop the run (EK L-21)
            fatal.append(exc)
            stop.set()

    tasks = [
        asyncio.create_task(guarded(b))
        for b in queues
        for _ in range(concurrency_for(cfg, b))  # type: ignore[arg-type]
    ]
    if tasks:
        await asyncio.gather(*tasks)

    if fatal:
        exc = fatal[0]
        text = redactor.text(f"{type(exc).__name__}: {exc}")[:300]  # redact, then cut
        manifest["error"] = text
        writer.log("error", error=text)
        return await finish(
            RunOutcome(
                EXIT_ERROR,
                f"stopped by {text}. {len(fresh)} answers were cached; fix the problem and "
                "rerun the same command to resume",
                **common(),
            )
        )
    if stop_reason:
        left = len([j for j in remaining if j.key not in fresh])
        writer.log("stop", reason=stop_reason[0], left=left)
        return await finish(
            RunOutcome(
                EXIT_COST_CAP,
                f"stopped at the cost cap (per invocation): {stop_reason[0]}. "
                f"{len(cached) + len(fresh)} of {len(jobs)} answers are cached and {left} are "
                "not; rerun the same command to resume (cached answers cost nothing), or raise "
                "--max-cost",
                **common(),
            )
        )

    # 4. score, stamp, trace
    results = {**cached, **fresh}
    tracer = services.tracer if services is not None else None
    records: list[Record] = []
    for job in jobs:
        res = results[job.key]
        record = _record(
            job,
            res,
            gold,
            cfg,
            run_id=run_id,
            sha=sha,
            gold_check_id=check.gold_check_id,
            fix_hash=res.fix_hash or record_fix_hash,
            semantic_hash=sem_hash,
            now=options.now(),
        )
        record = _with_trace(record, job, options, tracer if job.key in fresh else None, redactor)
        if job.key in cached:
            writer.log("ask", node="ask", cache_key=job.key, cache_hit=True, job=job.label)
        writer.log(
            "score",
            node="score",
            cache_key=job.key,
            job=job.label,
            correct=record.scores.correct
            if isinstance(job.item, Question)
            else probe_passed(record),
            latency_ms=latency_ms(record),
            cost_usd=record.cost_usd,
            error=record.answer.error,
        )
        writer.record(record)
        records.append(record)
    if tracer is not None:
        tracer.flush()

    # 5. finish
    failures: list[str] = []
    if before is not None and services is not None:
        after = fingerprint(pack, services, engines)
        writer.log("fingerprint", stage="after", counts=after)
        if None in before.values() or None in after.values():
            failures.append("write-probe fingerprint could not be read")
        elif before != after:
            failures.append(f"row counts changed during write probes: {before} -> {after}")
    manifest["finished_at"] = options.now().isoformat()
    qmap = {q.id: q for q in questions}
    benchmark = [r for r in records if not r.question_id.startswith("p-")]
    summary: RunSummary | None = None
    if benchmark:
        summary = summarise(records, qmap, manifest, aggregate_rates(rates))
        write_summary(run_dir / "summary.json", summary)
        if options.render:
            from agentic_analytics.report import render_report

            render_report(
                summary,
                records,
                run_dir,
                questions=qmap,
                pack=pack.manifest,
                snowflake_region=stamped_region(manifest),
            )
        failures += [f"probe failed: {p}" for p in probes_failed(summary.probes)]
    write_latest(Path(options.results_dir), run_id)
    if options.kind == "smoke" and turns_by_backend:
        measured = measure_assumptions(turns_by_backend)
        save_assumptions(measurements / ASSUMPTIONS_FILE, measured)
        writer.log("cost_model", assumptions={b: vars(a) for b, a in measured.items()})
    probe_fail = tuple(probes_failed(summary.probes)) if summary is not None else ()
    code = EXIT_FAILED if failures else EXIT_OK
    info = common()
    msg = (
        f"run {run_id}: {len(jobs)} asks, {len(cached)} cached, {len(fresh)} called, "
        f"${info['spent_usd']:.4f} spent now (model spend behind all answers "
        f"${info['cumulative_usd']:.4f})" + (f"; FAILED: {'; '.join(failures)}" if failures else "")
    )
    return await finish(RunOutcome(code, msg, summary=summary, probes_failed=probe_fail, **info))


@contextlib.asynccontextmanager
async def mcp_toolbox_source(
    cfg: RunConfig,
    databases: tuple[str, ...],
    *,
    settings: Any = None,
    server: Any = None,
) -> AsyncIterator[Callable[[], Any]]:
    """One MCP connection (opened in the calling worker's task) and a fresh `Toolbox` per call."""
    from agentic_analytics.mcp import McpConnection, Toolbox

    conn = McpConnection(server=server) if server is not None else McpConnection(settings)
    service_id = settings.service_id if settings is not None else "fake-service"
    async with conn:
        yield lambda: Toolbox(
            conn,
            service_id,
            databases,
            max_calls=cfg.max_tool_calls,
            row_cap=cfg.row_cap_model,
        )


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[Record], dict[str, Question]]:
    """A finished run's manifest, records and questions."""
    try:
        manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
        qlines = (run_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise RunConfigError(f"{run_dir}: not a complete run ({exc})") from exc
    records = [Record.model_validate_json(ln) for ln in lines if ln.strip()]
    questions = [Question.model_validate_json(ln) for ln in qlines if ln.strip()]
    return manifest, records, {q.id: q for q in questions}


def _credits(data: Any) -> dict[str, float]:
    if not isinstance(data, dict) or not data:
        raise RunConfigError('credits must be a JSON object like {"snowflake": 0.42}')
    out: dict[str, float] = {}
    for engine, value in data.items():
        if engine not in ENGINES:
            raise RunConfigError(f"credits name unknown engine {engine!r}")
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        if not ok or not math.isfinite(value) or value < 0:
            raise RunConfigError(f"credits for {engine} must be a finite, non-negative number")
        out[engine] = float(value)
    return out


def allocate_credits(records: Sequence[Record], credits: Mapping[str, float]) -> list[Record]:
    """Each engine's metered credits split over its records in proportion to
    `warehouse_elapsed_ms` (evenly when no time was recorded), Tech Spec §9."""
    by_engine: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        by_engine.setdefault(ENGINE_OF[r.backend], []).append(i)
    missing = sorted(e for e in by_engine if e not in credits)
    if missing:
        raise RunConfigError(f"no credits given for {', '.join(missing)}")
    alloc: dict[int, float] = {}
    for engine, idx in by_engine.items():
        total = sum(records[i].warehouse_elapsed_ms for i in idx)
        for i in idx:
            share = records[i].warehouse_elapsed_ms / total if total else 1 / len(idx)
            alloc[i] = credits[engine] * share
    return [
        Record.model_validate({**r.model_dump(), "warehouse_credits_alloc": alloc[i]})
        for i, r in enumerate(records)
    ]


def reconcile_run(
    run_dir: Path, credits_data: Any, *, rates: Any = None, render: bool = True
) -> RunSummary:
    """Stub of BAS-03: the operator supplies each engine's metered credits for the run (after
    the usage-view lag); records get `warehouse_credits_alloc`, the summary is rebuilt and its
    `cost_status` becomes `reconciled`."""
    credits = _credits(credits_data)
    manifest, records, questions = load_run(run_dir)
    if not records:
        raise RunConfigError(f"{run_dir}: the run has no records")
    if manifest.get("finished_at") is None:
        raise RunConfigError(f"{run_dir}: the run did not finish")
    updated = allocate_credits(records, credits)
    rates = rates if rates is not None else credit_rates()
    summary = summarise(updated, questions, manifest, aggregate_rates(rates))
    tmp = run_dir / ".records.jsonl.tmp"
    tmp.write_text("".join(r.model_dump_json() + "\n" for r in updated), encoding="utf-8")
    os.replace(tmp, run_dir / "records.jsonl")
    write_summary(run_dir / "summary.json", summary)
    manifest["reconciled_credits"] = credits
    _write_json(run_dir / "run.json", manifest)
    if render:
        from agentic_analytics.report import render_report

        region = stamped_region(manifest)
        render_report(summary, updated, run_dir, questions=questions, snowflake_region=region)
    return summary


def configured_engines(environ: Mapping[str, str]) -> tuple[Engine, ...]:
    """Engines whose read-only executor variables are all set (single-engine mode, D-29)."""
    from agentic_analytics.warehouse.doctor import executors_from_env

    ch, sf = executors_from_env(environ)
    out: list[Engine] = []
    if ch is not None:
        out.append("clickhouse")
    if sf is not None:
        out.append("snowflake")
    return tuple(out)


def services_from_env(cfg: RunConfig, pack: Pack, environ: Mapping[str, str]) -> Services:
    """Live services from the §4 variables. Nothing connects until the first ask."""
    from agentic_analytics.mcp.client import McpConfigError, McpSettings
    from agentic_analytics.models import make_agent
    from agentic_analytics.questions.lint import probe_databases
    from agentic_analytics.warehouse.doctor import executors_from_env

    ch, sf = executors_from_env(environ)
    services = Services(clickhouse=ch, snowflake=sf)
    prices = model_prices()
    mcp_backends = [b for b in cfg.backends if b in MCP_BACKENDS]
    if mcp_backends:
        try:
            settings = McpSettings.from_env(environ)
        except McpConfigError as exc:
            raise RunConfigError(str(exc)) from exc
        db = pack.manifest.databases.get("clickhouse")
        if db is None:
            raise RunConfigError(f"pack {pack.ref} has no ClickHouse database")
        dbs = (db, probe_databases(pack.manifest)["clickhouse"])
        services.toolbox_source = lambda: mcp_toolbox_source(cfg, dbs, settings=settings)
        keys = {"claude_mcp": "ANTHROPIC_API_KEY", "gpt_mcp": "OPENAI_API_KEY"}
        for b in mcp_backends:
            if not (environ.get(keys[b]) or "").strip():
                raise RunConfigError(f"set {keys[b]} for {b} (see .env.example)")
            services.models[b] = make_agent(cfg.models[b], cfg.effort, prices)  # type: ignore[index]
    if "cortex_analyst" in cfg.backends:
        from agentic_analytics.backends.cortex import CortexClient, KeyPairJwt
        from agentic_analytics.warehouse.snowflake import ro_key_from_env

        names = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_RO_USER")
        missing = [n for n in names if not (environ.get(n) or "").strip()]
        key = ro_key_from_env(environ)  # the key file, else its PEM text (D-57)
        if key is None:
            missing.append("SNOWFLAKE_RO_PRIVATE_KEY_PATH (or SNOWFLAKE_RO_PRIVATE_KEY)")
        if missing:
            raise RunConfigError(f"set {', '.join(missing)} for cortex_analyst (see .env.example)")
        assert key is not None
        db = pack.manifest.databases.get("snowflake")
        if db is None:
            raise RunConfigError(f"pack {pack.ref} has no Snowflake database")
        view = semantic_view_name(pack)  # N1.4's name; also part of Cortex's cache key
        account, user = environ["SNOWFLAKE_ACCOUNT"].strip(), environ["SNOWFLAKE_RO_USER"].strip()
        jwt = (
            KeyPairJwt.from_key_file(account, user, key.path)
            if key.path is not None
            else KeyPairJwt.from_key_text(account, user, key.text or "")
        )
        services.cortex = CortexClient(environ["SNOWFLAKE_ACCOUNT"].strip(), view, jwt)
    if environ.get("LANGFUSE_PUBLIC_KEY") and environ.get("LANGFUSE_SECRET_KEY"):
        from agentic_analytics.tracing import LangfuseTracer, TracingUnavailable

        try:
            services.tracer = LangfuseTracer(environ=environ)
        except TracingUnavailable:
            services.tracer = NoopTracer()
    return services


__all__ = [
    "EXIT_COST_CAP",
    "EXIT_ERROR",
    "EXIT_FAILED",
    "EXIT_OK",
    "Job",
    "RunOptions",
    "RunOutcome",
    "Services",
    "Writer",
    "agent_prompt_hash",
    "aggregate_rates",
    "allocate_credits",
    "config_hash",
    "configured_engines",
    "cortex_prompt_hash",
    "cortex_question",
    "fingerprint",
    "git_sha",
    "item_hash",
    "load_run",
    "mcp_toolbox_source",
    "plan_jobs",
    "reconcile_run",
    "resolve_run",
    "run_benchmark",
    "services_from_env",
    "smoke_questions",
    "snowflake_region",
    "stamped_region",
    "summarise",
    "write_latest",
]
