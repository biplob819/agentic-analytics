"""Run preconditions: the gold check record, gold results and parity (RUN-04, Tech Spec §5.7, US9).

A run refuses to start (exit 1) unless, for the current snapshot:

- a **parity record** (`parity-*.json`, written by N1.3's `run_parity`) passed for every engine
  the run uses (a single-engine record is accepted only when one engine is configured); and
- the pack's **gold check** `gold/gold_check.json` passed, names that snapshot, covers every
  engine the run uses, and its `gold_check_id` still equals the hash recomputed from the
  questions and gold result files on disk, so editing a question or a gold file after the check
  invalidates it.

**Gold check file format** (`<pack>/gold/gold_check.json`, schema version 1.0; Q2.2 writes it,
R3.1 reads it)::

    {
      "schema_version": "1.0",
      "kind": "gold_check",
      "pack": "uk_price_paid@1.0",
      "snapshot_sha256": "<64 hex>",
      "as_of": "2026-09-30",             # the as-of date the gold SQL was bound to (D-10)
      "engines": ["clickhouse", "snowflake"],
      "mode": "cross_engine",            # or "single_engine" when only one engine was checked
      "status": "pass",                  # or "fail"
      "questions": ["q-001", ...],       # the selected questions (quota selection, §5.6)
      "probes": ["p-iq01", ...],         # probes whose gold was run and stored
      "failures": [],                    # "q-007: tie at the LIMIT cut", ... (empty on pass)
      "checked_at": "2026-11-15T12:00:00+00:00",
      "gold_check_id": "<64 hex>"
    }

`gold_check_id` = sha256 over canonical JSON of {"snapshot_sha256", "as_of", "questions": [each
selected question's `model_dump(mode="json")`, by id], "probes": [each listed probe's dump, by
id], "gold": [{"question_id" (the question or probe id), "engine", "sql_hash", "columns",
"rows"} for every gold result of those questions and probes, by id then engine]}. Only the
selected questions count, so editing an unselected question does not invalidate the check.
Clarify-expected questions have no gold results; they are covered by their question JSON.
Question gold lives at `<pack>/gold/<engine>/<qid>.json` as `GoldResult`; probe gold at
`<pack>/gold/<engine>/<probe id>.json` as `ProbeGold`, on each checked engine a probe's backends
use (MCP agents: ClickHouse; Cortex Analyst: Snowflake). A run also refuses when its as-of date
is not the gold check's.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from agentic_analytics.packs import Pack, PackError, pack_file
from agentic_analytics.run.cache import sha256_json
from agentic_analytics.schemas import (
    ENGINE_OF,
    ENGINES,
    Engine,
    GoldResult,
    Probe,
    Question,
    ResultSet,
)
from agentic_analytics.schemas._base import (
    NonBlank,
    NonNegInt,
    Sha256,
    Timestamp,
    Versioned,
    json_cells,
)

GOLD_CHECK_FILE = "gold/gold_check.json"


ProbeId = Annotated[str, StringConstraints(pattern=r"^p-(w|iq|id)[0-9]{2}$")]


class ProbeGold(Versioned):
    """A probe's executed gold (`GoldResult` holds question ids only)."""

    probe_id: ProbeId
    engine: Engine
    sql_hash: Sha256
    result: ResultSet
    elapsed_ms: NonNegInt
    executed_at: Timestamp

    @field_validator("result")
    @classmethod
    def _json_only(cls, v: ResultSet) -> ResultSet:
        return json_cells(v)


AnyGold = GoldResult | ProbeGold


def item_id(g: AnyGold) -> str:
    return g.question_id if isinstance(g, GoldResult) else g.probe_id


def probe_engines(probe: Probe, engines: Iterable[Engine]) -> tuple[Engine, ...]:
    """The checked engines a probe's gold runs on: those its backends are scored on."""
    used = {ENGINE_OF[b] for b in probe.backends}
    return tuple(e for e in engines if e in used) if probe.gold_sql is not None else ()


class PreconditionError(Exception):
    """A run precondition failed: missing or failed gold check or parity (exit code 1)."""


class GoldCheck(Versioned):
    kind: Literal["gold_check"]
    pack: NonBlank
    snapshot_sha256: Sha256
    as_of: date
    engines: Annotated[tuple[Engine, ...], Field(min_length=1)]
    mode: Literal["cross_engine", "single_engine"]
    status: Literal["pass", "fail"]
    questions: tuple[Annotated[str, Field(pattern=r"^q-[0-9]{3}$")], ...]
    probes: tuple[ProbeId, ...] = ()
    failures: tuple[str, ...] = ()
    checked_at: Timestamp
    gold_check_id: Sha256

    @model_validator(mode="after")
    def _consistent(self) -> GoldCheck:
        if len(set(self.engines)) != len(self.engines):
            raise ValueError("engines must be unique")
        if (self.mode == "single_engine") != (len(self.engines) == 1):
            raise ValueError("mode is single_engine exactly when one engine was checked")
        if (self.status == "pass") == bool(self.failures):
            raise ValueError("a passing gold check lists no failures; a failing one lists some")
        if len(set(self.questions)) != len(self.questions):
            raise ValueError("questions must be unique")
        if len(set(self.probes)) != len(self.probes):
            raise ValueError("probes must be unique")
        return self


def gold_payload(
    questions: Sequence[Question],
    gold: Mapping[tuple[str, str], AnyGold],
    snapshot_sha256: str,
    *,
    as_of: date,
    probes: Sequence[Probe] = (),
) -> dict[str, object]:
    qs = sorted(questions, key=lambda q: q.id)
    ps = sorted(probes, key=lambda p: p.id)
    ids = {q.id for q in qs} | {p.id for p in ps}
    rows = [
        {
            "question_id": item_id(g),
            "engine": g.engine,
            "sql_hash": g.sql_hash,
            "columns": list(g.result.columns),
            "rows": [list(r) for r in g.result.rows],
        }
        for (_id, _engine), g in sorted(gold.items())
        if item_id(g) in ids
    ]
    return {
        "snapshot_sha256": snapshot_sha256,
        "as_of": as_of.isoformat(),
        "questions": [q.model_dump(mode="json") for q in qs],
        "probes": [p.model_dump(mode="json") for p in ps],
        "gold": rows,
    }


def compute_gold_check_id(
    questions: Sequence[Question],
    gold: Mapping[tuple[str, str], AnyGold],
    snapshot_sha256: str,
    *,
    as_of: date,
    probes: Sequence[Probe] = (),
) -> str:
    return sha256_json(gold_payload(questions, gold, snapshot_sha256, as_of=as_of, probes=probes))


def _read_json(path: Path | object, what: str) -> object:
    try:
        text = path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    except (OSError, UnicodeDecodeError) as exc:
        raise PreconditionError(f"{what}: cannot read ({type(exc).__name__})") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise PreconditionError(f"{what}: not JSON ({exc})") from exc


def load_gold_check(pack: Pack) -> GoldCheck | None:
    """The pack's gold check record, or None when the pack has none yet."""
    try:
        f = pack_file(pack, GOLD_CHECK_FILE)
    except PackError:
        return None
    data = _read_json(f, f"{pack.ref} {GOLD_CHECK_FILE}")
    try:
        return GoldCheck.model_validate(data)
    except ValidationError as exc:
        raise PreconditionError(f"{pack.ref} {GOLD_CHECK_FILE}: {exc}") from exc


def load_gold_results(
    pack: Pack, questions: Iterable[Question], engines: Iterable[Engine]
) -> dict[tuple[str, str], GoldResult]:
    """`gold/<engine>/<qid>.json` for every answer-expected question on every engine."""
    out: dict[tuple[str, str], GoldResult] = {}
    missing: list[str] = []
    engines = tuple(engines)
    for q in questions:
        if q.expected != "answer":
            continue
        for engine in engines:
            rel = f"gold/{engine}/{q.id}.json"
            try:
                f = pack_file(pack, rel)
            except PackError:
                missing.append(rel)
                continue
            data = _read_json(f, f"{pack.ref} {rel}")
            try:
                g = GoldResult.model_validate(data)
            except ValidationError as exc:
                raise PreconditionError(f"{pack.ref} {rel}: {exc}") from exc
            if g.question_id != q.id or g.engine != engine:
                raise PreconditionError(f"{pack.ref} {rel} holds {g.question_id} on {g.engine}")
            out[(q.id, engine)] = g
    if missing:
        more = f" (and {len(missing) - 3} more)" if len(missing) > 3 else ""
        raise PreconditionError(
            f"{pack.ref} has no gold result for {', '.join(missing[:3])}{more}: "
            "run `agentic-analytics gold`"
        )
    return out


def load_probe_gold(
    pack: Pack, probes: Iterable[Probe], engines: Iterable[Engine]
) -> dict[tuple[str, str], ProbeGold]:
    """`gold/<engine>/<probe id>.json` for each probe on the engines its backends use."""
    out: dict[tuple[str, str], ProbeGold] = {}
    missing: list[str] = []
    engines = tuple(engines)
    for p in probes:
        for engine in probe_engines(p, engines):
            rel = f"gold/{engine}/{p.id}.json"
            try:
                f = pack_file(pack, rel)
            except PackError:
                missing.append(rel)
                continue
            data = _read_json(f, f"{pack.ref} {rel}")
            try:
                g = ProbeGold.model_validate(data)
            except ValidationError as exc:
                raise PreconditionError(f"{pack.ref} {rel}: {exc}") from exc
            if g.probe_id != p.id or g.engine != engine:
                raise PreconditionError(f"{pack.ref} {rel} holds {g.probe_id} on {g.engine}")
            out[(p.id, engine)] = g
    if missing:
        raise PreconditionError(
            f"{pack.ref} has no probe gold for {', '.join(missing[:3])}: "
            "run `agentic-analytics gold`"
        )
    return out


def _pack_probes(pack: Pack) -> tuple[Probe, ...]:
    from agentic_analytics.questions.bank import BankError, load_probes

    try:
        return load_probes(pack)
    except PackError:
        return ()
    except BankError as exc:
        raise PreconditionError(str(exc)) from exc


def write_gold_check(pack_dir: Path, check: GoldCheck) -> Path:
    path = Path(pack_dir) / GOLD_CHECK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(check.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def require_gold(
    pack: Pack,
    questions: Sequence[Question],
    engines: Sequence[Engine],
    snapshot_sha256: str,
    *,
    as_of: date | None = None,
    probes: Sequence[Probe] | None = None,
) -> tuple[GoldCheck, tuple[Question, ...], dict[tuple[str, str], AnyGold]]:
    """The clean gold check for this snapshot (and as-of date, when given), the questions it
    selected, and the gold of those questions and of the probes it lists. `probes` defaults to
    the pack's `probes.jsonl`."""
    check = load_gold_check(pack)
    if check is None:
        raise PreconditionError(
            f"{pack.ref} has no gold check ({GOLD_CHECK_FILE}): run `agentic-analytics gold` first"
        )
    if check.status != "pass":
        raise PreconditionError(
            f"the gold check for {pack.ref} failed ({len(check.failures)} failures, first: "
            f"{check.failures[0]}); fix the gold and rerun `agentic-analytics gold`"
        )
    if check.pack != pack.ref:
        raise PreconditionError(f"the gold check is for {check.pack}, not {pack.ref}")
    if check.snapshot_sha256 != snapshot_sha256:
        raise PreconditionError(
            f"the gold check is for snapshot {check.snapshot_sha256[:12]}, not the current "
            f"snapshot {snapshot_sha256[:12]}; rerun `agentic-analytics gold`"
        )
    if as_of is not None and check.as_of != as_of:
        raise PreconditionError(
            f"the gold check was run for as-of {check.as_of.isoformat()}, not the run's "
            f"{as_of.isoformat()}; rerun `agentic-analytics gold` with the run's as-of date"
        )
    unchecked = [e for e in engines if e not in check.engines]
    if unchecked:
        raise PreconditionError(f"the gold check does not cover {', '.join(unchecked)}")
    by_id = {q.id: q for q in questions}
    unknown = [qid for qid in check.questions if qid not in by_id]
    if unknown:
        raise PreconditionError(f"the gold check selects unknown questions {unknown[:5]}")
    selected = tuple(by_id[qid] for qid in sorted(check.questions))
    probe_by_id = {p.id: p for p in (_pack_probes(pack) if probes is None else probes)}
    unknown = [pid for pid in check.probes if pid not in probe_by_id]
    if unknown:
        raise PreconditionError(f"the gold check lists unknown probes {unknown[:5]}")
    listed = tuple(probe_by_id[pid] for pid in sorted(check.probes))
    gold: dict[tuple[str, str], AnyGold] = dict(load_gold_results(pack, selected, check.engines))
    gold.update(load_probe_gold(pack, listed, check.engines))
    recomputed = compute_gold_check_id(
        selected, gold, check.snapshot_sha256, as_of=check.as_of, probes=listed
    )
    if recomputed != check.gold_check_id:
        raise PreconditionError(
            "questions or gold results changed after the gold check (gold_check_id "
            f"{check.gold_check_id[:12]} != {recomputed[:12]}); rerun `agentic-analytics gold`"
        )
    return check, selected, gold


# -- snapshot and parity records (written by N1.3 under docs/measurements) ------------------


def _records(measurements: Path, prefix: str) -> list[tuple[Path, dict[str, object]]]:
    out: list[tuple[Path, dict[str, object]]] = []
    if not measurements.is_dir():
        return out
    for p in sorted(measurements.glob(f"{prefix}-*.json")):
        data = _read_json(p, str(p))
        if isinstance(data, dict):
            out.append((p, data))
    return out


def current_snapshot(measurements: Path) -> str:
    """The sha256 of the newest snapshot record (`snapshot-<date>.json`, by `downloaded_at`)."""
    found: list[tuple[str, str, str]] = []
    for p, data in _records(Path(measurements), "snapshot"):
        sha, at = data.get("sha256"), data.get("downloaded_at")
        if isinstance(sha, str) and len(sha) == 64 and isinstance(at, str):
            found.append((at, p.name, sha))
    if not found:
        raise PreconditionError(
            f"no snapshot record in {measurements}: run `agentic-analytics load` first"
        )
    return max(found)[2]


def require_parity(measurements: Path, snapshot_sha256: str, engines: Sequence[Engine]) -> Path:
    """The newest passing parity record for this snapshot that covers `engines`."""
    candidates: list[tuple[str, Path]] = []
    seen_for_snapshot = False
    for p, data in _records(Path(measurements), "parity"):
        if data.get("kind") != "parity" or data.get("snapshot_sha256") != snapshot_sha256:
            continue
        seen_for_snapshot = True
        status = data.get("status")
        recorded = data.get("engines")
        if not isinstance(recorded, list) or not all(e in recorded for e in engines):
            continue
        if status == "pass" or (status == "single_engine" and len(engines) == 1):
            at = data.get("generated_at")
            candidates.append((at if isinstance(at, str) else "", p))
        elif status == "fail":
            raise PreconditionError(f"parity failed for snapshot {snapshot_sha256[:12]} ({p})")
    if not candidates:
        why = "none covers " + ", ".join(engines) if seen_for_snapshot else "none found"
        raise PreconditionError(
            f"no passing parity record for snapshot {snapshot_sha256[:12]} in {measurements} "
            f"({why}): run `agentic-analytics load`"
        )
    return max(candidates)[1]


def check_engines(engines: Iterable[str]) -> tuple[Engine, ...]:
    wanted = set(engines)
    out: tuple[Engine, ...] = tuple(e for e in ENGINES if e in wanted)
    if not out:
        raise ValueError("at least one engine must be configured")
    return out


__all__ = [
    "GOLD_CHECK_FILE",
    "AnyGold",
    "GoldCheck",
    "PreconditionError",
    "ProbeGold",
    "check_engines",
    "compute_gold_check_id",
    "current_snapshot",
    "gold_payload",
    "item_id",
    "load_gold_check",
    "load_gold_results",
    "load_probe_gold",
    "probe_engines",
    "require_gold",
    "require_parity",
    "write_gold_check",
]
