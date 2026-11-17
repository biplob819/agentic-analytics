"""The demo's replay cache (D4.1: ASK-02; Tech Spec §4 `demo-cache`, §10).

`agentic-analytics demo-cache --questions 10` answers sample questions from the bundled pack on
every demo backend (live, on the owner's keys, capped per panel exactly as live mode is) and
writes them to one JSON file. Replay mode reads that file and makes no call. A sample already
answered on every backend under the same configuration is never paid for again; `--redo`
re-asks samples with a failed panel.

The file is a `DemoCache`: results format v1, the run configuration the answers came from, the
pack's licence credit, and one entry per question with a `Panel` per backend. Every entry
carries the `DemoStamp` it was asked under (backends, models, effort, as-of date, pack id and
version, fix_hash); the cache refuses to load when any entry's stamp differs from the stamp of
its own top-level configuration, so the configuration always describes every answer. A rebuild
under another configuration reuses nothing: it is refused unless `--redo` asks every sample
again. `synthetic: true` marks the bundled example under
`demo/example/`, generated from the test fakes by `tests/demo/example_data.py` so the page works
with no keys; it is labelled on the page and is not a measurement.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import model_validator

from agentic_analytics.demo.ask import (
    DEMO_BACKENDS,
    DemoContext,
    Panel,
    request_bound_usd,
    run_panels,
)
from agentic_analytics.schemas import MCP_BACKENDS, BackendName, Question, RunConfig
from agentic_analytics.schemas._base import FrozenMap, Model, NonBlank, Timestamp, Versioned

CACHE_KIND = "demo_cache"
DEFAULT_QUESTIONS = 10
DEFAULT_OUT = Path("demo") / "replay.json"
DEMO_CACHE_MAX_COST_USD = 10.0  # 10 two-panel samples hold at most about $7.40
EXAMPLE_FILE = "replay.json"
SYNTHETIC_LABEL = (
    "SYNTHETIC EXAMPLE: these answers were generated from the test fakes, not by the real "
    "backends. Run `agentic-analytics demo-cache` to record real ones."
)
CATEGORY_ORDER = ("aggregation", "time", "geography", "top_n", "yoy", "ambiguous")


def question_key(text: str) -> str:
    """How replay matches a question: whitespace collapsed, case folded."""
    return " ".join((text or "").split()).casefold()


class DemoStamp(Model):
    """What a sample's answers depend on. Two answers are interchangeable only when every field
    is equal: a cached answer is reused, and the cache's configuration describes it, only then."""

    backends: tuple[BackendName, ...]
    models: FrozenMap[NonBlank]  # the agent backends' models only
    effort: str
    as_of: date
    pack: NonBlank  # id@version
    fix_hash: str | None = None

    @classmethod
    def of(
        cls,
        config: RunConfig,
        backends: Sequence[BackendName],
        pack: str,
        fix_hash: str | None,
    ) -> DemoStamp:
        return cls(
            backends=tuple(backends),
            models={b: config.models[b] for b in backends if b in MCP_BACKENDS},
            effort=config.effort,
            as_of=config.as_of,
            pack=pack,
            fix_hash=fix_hash,
        )


def stamp_differences(a: DemoStamp, b: DemoStamp, *, ignore: Sequence[str] = ()) -> list[str]:
    """The fields on which two stamps differ (names only: safe to show)."""
    names = [f for f in DemoStamp.model_fields if f not in ignore]
    return [f for f in names if getattr(a, f) != getattr(b, f)]


class DemoEntry(Model):
    question_id: str | None = None
    question: NonBlank
    category: str | None = None
    stamp: DemoStamp  # the configuration these panels were asked under
    panels: tuple[Panel, ...]

    def complete(self, backends: Sequence[BackendName]) -> bool:
        """Every backend answered without a timeout or error (a provider blip is re-asked)."""
        got = {p.backend: p for p in self.panels}
        return all(b in got and got[b].ok for b in backends)


class DemoCache(Versioned):
    kind: Literal["demo_cache"] = CACHE_KIND
    synthetic: bool
    label: str = ""
    pack: NonBlank
    attribution: NonBlank
    config: RunConfig
    backends: tuple[BackendName, ...]
    fix_hash: str | None = None
    created_at: Timestamp
    entries: tuple[DemoEntry, ...]

    @model_validator(mode="after")
    def _describes_every_entry(self) -> DemoCache:
        if self.synthetic and "SYNTHETIC" not in self.label:
            if self.label.strip():
                raise ValueError("a synthetic cache's label must say SYNTHETIC")
            object.__setattr__(self, "label", SYNTHETIC_LABEL)
        missing = [b for b in self.backends if b in MCP_BACKENDS and b not in self.config.models]
        if missing:
            raise ValueError(f"the cache's config names no model for {', '.join(missing)}")
        top = self.stamp()
        for e in self.entries:
            diff = stamp_differences(e.stamp, top)
            if diff:
                raise ValueError(
                    f"entry {e.question_id or e.question[:40]!r} was asked under a different "
                    f"{', '.join(diff)} than the cache's config; re-ask it (`demo-cache --redo`)"
                )
        return self

    def stamp(self) -> DemoStamp:
        return DemoStamp.of(self.config, self.backends, self.pack, self.fix_hash)

    def find(self, question: str) -> DemoEntry | None:
        key = question_key(question)
        return next((e for e in self.entries if question_key(e.question) == key), None)

    def samples(self) -> list[str]:
        """Sample questions answered on every backend, in file order (the first opens)."""
        return [e.question for e in self.entries if e.complete(self.backends)]


def load_cache(path: str | os.PathLike[str]) -> DemoCache:
    return DemoCache.model_validate_json(Path(path).read_text(encoding="utf-8"))


def dump_cache(cache: DemoCache) -> str:
    data = cache.model_dump(mode="json")
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def save_cache(path: str | os.PathLike[str], cache: DemoCache) -> Path:
    """Atomic write: a crash leaves the old file or the new one, never half of one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(dump_cache(cache))
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def load_example_cache() -> DemoCache:
    """The bundled SYNTHETIC example (built from the fakes; works with no keys)."""
    text = (
        resources.files("agentic_analytics.demo")
        .joinpath("example")
        .joinpath(EXAMPLE_FILE)
        .read_text(encoding="utf-8")
    )
    return DemoCache.model_validate_json(text)


def select_questions(questions: Sequence[Question], n: int) -> list[Question]:
    """`n` samples spread over the categories: the first question of each category by id,
    then the second, and so on (the first sample is the one the page opens on)."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("--questions must be a positive integer")
    by_cat = {
        c: sorted((q for q in questions if q.category == c), key=lambda q: q.id)
        for c in CATEGORY_ORDER
    }
    out: list[Question] = []
    depth = 0
    while len(out) < n and any(len(v) > depth for v in by_cat.values()):
        for c in CATEGORY_ORDER:
            if depth < len(by_cat[c]) and len(out) < n:
                out.append(by_cat[c][depth])
        depth += 1
    return out


def pack_fix_hash(pack: Any) -> str | None:
    """The pack's fix content hash (F3.1), or None for a pack without fix files."""
    from agentic_analytics.fixes import FixError, current_fix_hash

    try:
        return current_fix_hash(pack)
    except FixError:
        return None


class StaleCache(ValueError):
    """The existing cache was recorded under another configuration (names the fields)."""


def reusable(existing: DemoCache | None, stamp: DemoStamp, *, redo: bool) -> dict[str, DemoEntry]:
    """The existing entries a rebuild under `stamp` may keep, by question key. Nothing is kept
    from a cache recorded under another stamp: that is refused (`StaleCache`) unless `redo`,
    which then re-asks every question."""
    if existing is None:
        return {}
    diff = stamp_differences(existing.stamp(), stamp)
    if diff:
        if not redo:
            raise StaleCache(
                f"the existing cache was recorded under a different {', '.join(diff)}; pass "
                "--redo to re-ask every sample under the new configuration, or move it away"
            )
        return {}
    return {question_key(e.question): e for e in existing.entries if e.stamp == stamp}


def to_ask(
    questions: Sequence[Question],
    existing: DemoCache | None,
    stamp: DemoStamp,
    *,
    redo: bool,
) -> list[Question]:
    """The questions a rebuild asks: those with no reusable entry, and with `redo` those whose
    entry has a failed panel. `build_demo_cache` and the CLI's estimate both use this."""
    kept = reusable(existing, stamp, redo=redo)
    out = []
    for q in questions:
        old = kept.get(question_key(q.text))
        if old is None or (redo and not old.complete(stamp.backends)):
            out.append(q)
    return out


def build_demo_cache(
    questions: Sequence[Question],
    ctx: DemoContext,
    *,
    pack: str,
    attribution: str,
    backends: Sequence[BackendName] = DEMO_BACKENDS,
    existing: DemoCache | None = None,
    redo: bool = False,
    synthetic: bool = False,
    label: str = "",
    fix_hash: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    say: Callable[[str], None] | None = None,
) -> tuple[DemoCache, list[DemoEntry]]:
    """Answer every question not already answered on every backend under the same stamp (the
    owner's run: no visitor guard, but every panel is capped and timed out exactly as in live
    mode). Returns the cache and the entries asked in this call. Raises `StaleCache` when
    `existing` was recorded under another stamp and `redo` is not set."""
    backends = tuple(backends)
    stamp = DemoStamp.of(ctx.config, backends, pack, fix_hash)
    kept = reusable(existing, stamp, redo=redo)
    ask_keys = {question_key(q.text) for q in to_ask(questions, existing, stamp, redo=redo)}
    entries: list[DemoEntry] = []
    asked: list[DemoEntry] = []
    for q in questions:
        key = question_key(q.text)
        old = kept.get(key)
        if old is not None and key not in ask_keys:
            entries.append(old)
            continue
        panels = run_panels(q.text, backends, ctx, trace_question_id=q.id)
        entry = DemoEntry(
            question_id=q.id,
            question=q.text,
            category=q.category,
            stamp=stamp,
            panels=tuple(panels),
        )
        asked.append(entry)
        entries.append(entry)
        if say is not None:
            spent = math.fsum(p.cost_usd for p in panels)
            ok = sum(p.ok for p in panels)
            say(f"{q.id}: {ok}/{len(panels)} panels answered, ${spent:.4f}")
    cache = DemoCache(
        schema_version="1.0",
        synthetic=synthetic,
        label=label,
        pack=pack,
        attribution=attribution,
        config=ctx.config,
        backends=backends,
        fix_hash=fix_hash,
        created_at=now(),
        entries=tuple(entries),
    )
    return cache, asked


# -- the CLI subcommand ------------------------------------------------------------------------


LEDGER_KIND = "demo-cache"


def ledger_line(path: Path, asked: Sequence[DemoEntry], rates: Mapping[str, Any]) -> dict[str, Any]:
    """The spend-ledger line of one `demo-cache` run (Tech Spec §9, as the runner's, D-78):
    the agent panels' charge (model dollars plus their ClickHouse compute estimate) as
    `spent_usd`, and the Cortex trial credits of its messages (one per Cortex panel asked,
    a timed-out one included) under `credits`. Never a question or an answer."""
    from agentic_analytics.cost import append_ledger

    panels = [p for e in asked for p in e.panels]
    agent_usd = math.fsum(p.cost_usd for p in panels if p.backend != "cortex_analyst")
    messages = sum(p.backend == "cortex_analyst" for p in panels)
    rate = rates["cortex_analyst_credits_per_message"]
    per_message = float(getattr(rate, "value", rate))
    credits = {"snowflake": messages * per_message} if messages else {}
    at = datetime.now(UTC)
    return append_ledger(
        path,
        kind=LEDGER_KIND,
        run_id=f"{LEDGER_KIND}-{at:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}",
        spent_usd=agent_usd,
        credits=credits,
        note=f"{len(asked)} sample questions asked, {len(panels)} panels; {messages} Cortex "
        "messages; agent model dollars plus ClickHouse estimate; Snowflake warehouse not metered",
        at=at,
    )


def cli_demo_cache(ns: Any, environ: Mapping[str, str]) -> tuple[int, dict[str, Any], str]:
    """`agentic-analytics demo-cache`: (exit code, JSON payload, text). Exit 2 on configuration
    errors, 3 when the estimate is over `--max-cost` (nothing asked)."""
    from agentic_analytics.demo.ask import DemoLimits, context_from_env, ledger_path
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.questions import BankError, load_questions
    from agentic_analytics.run.config import RunConfigError, parse_backends, with_overrides
    from agentic_analytics.run.runner import configured_engines
    from agentic_analytics.schemas import ConfigError, load_run_config

    def fail(msg: str, code: int = 2) -> tuple[int, dict[str, Any], str]:
        return code, {"ok": False, "error": msg}, msg

    try:
        if not Path(ns.config).is_file():
            raise RunConfigError(f"no {ns.config}: run `agentic-analytics init` first")
        cfg = load_run_config(ns.config)
        pack = load_pack(ns.pack)
        engines = configured_engines(environ)
        if not engines and not ns.dry_run:
            raise RunConfigError("no engine is configured: set the variables in .env.example")
        backends = parse_backends(ns.backend, engines or None)
        cfg = with_overrides(cfg, backends=backends, fixes=(), trials=1)
        questions = select_questions(load_questions(pack), ns.questions)
        stamp = DemoStamp.of(cfg, backends, pack.ref, pack_fix_hash(pack))
    except (RunConfigError, ConfigError, PackError, BankError, ValueError) as exc:
        return fail(str(exc))
    out = Path(ns.out)
    existing = None
    if out.is_file():
        try:
            existing = load_cache(out)
        except (OSError, ValueError) as exc:
            return fail(f"{out} is not a demo cache ({type(exc).__name__}); move it away first")
    try:
        todo = to_ask(questions, existing, stamp, redo=ns.redo)
    except StaleCache as exc:
        return fail(f"{out}: {exc}")
    from agentic_analytics.cost import credit_rates, model_prices

    limits = DemoLimits()
    hold = request_bound_usd(
        backends, config=cfg, prices=model_prices(), rates=credit_rates(), limits=limits
    )
    estimate = hold * len(todo)
    info = {
        "questions": len(questions),
        "to_ask": len(todo),
        "backends": list(backends),
        "upper_bound_usd": round(estimate, 4),
        "out": str(out),
    }
    head = (
        f"demo-cache: {len(todo)} of {len(questions)} questions to ask on "
        f"{', '.join(backends)}; at most ${estimate:.2f}"
    )
    if ns.dry_run:
        return 0, {"ok": True, "dry_run": True, **info}, head + " (dry run: nothing asked)"
    if estimate > ns.max_cost:
        msg = f"{head}, over --max-cost ${ns.max_cost:.2f}; nothing asked"
        return 3, {"ok": False, "error": "cost_cap", **info}, msg
    try:
        ctx = context_from_env(cfg, pack, environ, limits=limits, trace=True)
    except (RunConfigError, ValueError) as exc:
        return fail(str(exc))
    cache, asked = build_demo_cache(
        questions,
        ctx,
        pack=pack.ref,
        attribution=pack.manifest.attribution,
        backends=backends,
        existing=existing,
        redo=ns.redo,
        fix_hash=stamp.fix_hash,
    )
    path = save_cache(out, cache)
    failed = sum(not e.complete(backends) for e in cache.entries)
    spent = math.fsum(p.cost_usd for e in asked for p in e.panels)
    msg = f"{head}\nwrote {path}: {len(asked)} asked for ${spent:.4f}, {failed} with a failed panel"
    if asked:  # Tech Spec §9: every real run appends to the spend ledger
        ledger_line(ledger_path(environ), asked, credit_rates())
    payload = {
        "ok": True,
        **info,
        "asked": len(asked),
        "failed": failed,
        "spent_usd": round(spent, 6),
    }
    return 0, payload, msg


__all__ = [
    "CACHE_KIND",
    "DEFAULT_OUT",
    "DEFAULT_QUESTIONS",
    "DEMO_CACHE_MAX_COST_USD",
    "LEDGER_KIND",
    "SYNTHETIC_LABEL",
    "DemoCache",
    "DemoEntry",
    "DemoStamp",
    "StaleCache",
    "build_demo_cache",
    "cli_demo_cache",
    "dump_cache",
    "ledger_line",
    "load_cache",
    "load_example_cache",
    "pack_fix_hash",
    "question_key",
    "save_cache",
    "select_questions",
    "stamp_differences",
    "to_ask",
]
