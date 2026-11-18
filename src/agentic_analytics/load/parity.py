"""Parity fingerprints across engines (LOD-03, US9, H3, D-11).

The pack's `parity.yaml` holds one query per engine for each fingerprint. Every rendered query
must pass the read-only SQL guard, even when run on an admin connection. `run_parity` runs each
fingerprint on every engine given, then compares the result sets **exactly**: the casefolded
column names must be identical, and the rows must match under EvalKit's `sql_match` with
`rel_tol = 0` and `abs_tol = 0` (unordered unless the fingerprint says `ordered: true`).

Outcome: `pass` (every fingerprint matched), `fail` (a mismatch or a failed query, which blocks
the gold step and every run), or `single_engine` (only one engine configured, D-29: fingerprints
are recorded, nothing is compared). The report goes to `docs/measurements/parity-<date>.json`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from evalkit.scorers import sql_match
from pydantic import Field, ValidationError, model_validator

from agentic_analytics.load._common import Clock, LoaderError, ident, qualified, render, utc_now
from agentic_analytics.packs import Pack, read_pack_text
from agentic_analytics.schemas import ENGINES, Engine, ResultSet
from agentic_analytics.schemas._base import Model, Name, NonBlank, PosInt
from agentic_analytics.schemas.config import _UniqueKeyLoader
from agentic_analytics.warehouse._common import make_result
from agentic_analytics.warehouse.guard import check_sql

PARITY_FILE = "parity.yaml"
DEFAULT_OUT = Path("docs/measurements")
QueryRunner = Callable[[str], ResultSet]
Status = Literal["pass", "fail", "single_engine"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ParityError(RuntimeError):
    """Parity failed or its spec is invalid; blocks the run (US9)."""


class Fingerprint(Model):
    name: Name
    ordered: bool = False
    clickhouse: NonBlank | None = None
    snowflake: NonBlank | None = None

    def query(self, engine: Engine) -> str:
        sql = getattr(self, engine)
        if sql is None:
            raise ParityError(f"fingerprint {self.name!r} has no {engine} query")
        return sql


class ParitySpec(Model):
    version: PosInt
    fingerprints: Annotated[tuple[Fingerprint, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> ParitySpec:
        names = [f.name for f in self.fingerprints]
        if len(set(names)) != len(names):
            raise ValueError("fingerprint names must be unique")
        return self


def engine_database(pack: Pack, engine: Engine) -> str:
    target = pack.manifest.databases[engine]
    if engine == "clickhouse":
        return ident(target, "ClickHouse database")
    return ".".join(qualified(target, 2, "Snowflake database"))


def load_parity_spec(pack: Pack) -> ParitySpec:
    """Parse and validate `parity.yaml`: every fingerprint has a query for every pack engine,
    and every rendered query passes the read-only guard."""
    text = read_pack_text(pack, PARITY_FILE)
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
        spec = ParitySpec.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ParityError(f"{pack.ref} {PARITY_FILE}: {exc}") from exc
    for fp in spec.fingerprints:
        for engine in pack.manifest.engines:
            sql = rendered_query(pack, fp, engine)
            verdict = check_sql(sql, engine)
            if not verdict.ok:
                raise ParityError(
                    f"fingerprint {fp.name!r} on {engine} fails the read-only guard: "
                    f"{verdict.reason}"
                )
    return spec


def rendered_query(pack: Pack, fp: Fingerprint, engine: Engine) -> str:
    return render(fp.query(engine), {"database": engine_database(pack, engine)})


# ---- runners ----------------------------------------------------------------------------------


def clickhouse_runner(client: Any) -> QueryRunner:
    """Run fingerprints on a clickhouse-connect client (admin or read-only)."""

    def run(sql: str) -> ResultSet:
        res = client.query(sql)
        return make_result(res.column_names, res.result_rows)

    return run


def snowflake_runner(conn: Any) -> QueryRunner:
    """Run fingerprints on a Snowflake DB-API connection (admin or read-only)."""

    def run(sql: str) -> ResultSet:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        finally:
            cur.close()
        return make_result(cols, rows)

    return run


def executor_runner(executor: Any) -> QueryRunner:
    """Run fingerprints through a read-only executor (`Executor` protocol, N1.2)."""

    def run(sql: str) -> ResultSet:
        ex = executor.execute(sql)
        if ex.error is not None or ex.result is None:
            raise LoaderError(f"{executor.engine} executor returned {ex.error}")
        if ex.truncated:
            raise LoaderError(f"{executor.engine} fingerprint result was truncated")
        return ex.result

    return run


# ---- report -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FingerprintResult:
    name: str
    results: Mapping[str, ResultSet]
    errors: Mapping[str, str]
    match: bool | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "match": self.match,
            "reason": self.reason,
            "errors": dict(self.errors),
            "results": {
                e: {"columns": list(rs.columns), "rows": [list(r) for r in rs.rows]}
                for e, rs in self.results.items()
            },
        }


@dataclass(frozen=True)
class ParityReport:
    pack: str
    engines: tuple[str, ...]
    status: Status
    generated_at: dt.datetime
    fingerprints: tuple[FingerprintResult, ...]
    snapshot_sha256: str | None = None
    path: Path | None = field(default=None, compare=False)

    @property
    def passed(self) -> bool:
        """False only when parity failed; single-engine mode does not block."""
        return self.status != "fail"

    def result(self, name: str, engine: str) -> ResultSet:
        for fp in self.fingerprints:
            if fp.name == name and engine in fp.results:
                return fp.results[engine]
        raise KeyError(f"no {engine} result for fingerprint {name!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "kind": "parity",
            "pack": self.pack,
            "engines": list(self.engines),
            "status": self.status,
            "passed": self.passed,
            "snapshot_sha256": self.snapshot_sha256,
            "generated_at": self.generated_at.isoformat(),
            "fingerprints": [f.to_dict() for f in self.fingerprints],
        }


def compare(results: Mapping[str, ResultSet], ordered: bool) -> tuple[bool, str | None]:
    """Exact comparison of one fingerprint across engines."""
    engines = sorted(results)
    first = results[engines[0]]
    for other in engines[1:]:
        rs = results[other]
        a = [c.casefold() for c in first.columns]
        b = [c.casefold() for c in rs.columns]
        if a != b:
            return False, f"column names differ: {engines[0]} {a} vs {other} {b}"
        gold = first.model_copy(update={"ordered": ordered})
        score = sql_match(rs, gold, rel_tol=0.0, abs_tol=0.0)
        if score.value != 1.0:
            return False, f"{engines[0]} vs {other}: {dict(score.details)}"
    return True, None


def run_parity(
    pack: Pack,
    runners: Mapping[Engine, QueryRunner],
    *,
    snapshot_sha256: str | None = None,
    out_dir: Path | None = DEFAULT_OUT,
    now: Clock = utc_now,
) -> ParityReport:
    """Run every fingerprint on every engine in `runners`, compare, and write the report."""
    if not runners:
        raise ValueError("at least one engine runner is needed")
    stray = [e for e in runners if e not in pack.manifest.engines or e not in ENGINES]
    if stray:
        raise ValueError(f"pack {pack.ref} has no engine {stray}")
    if snapshot_sha256 is not None and not _SHA256.match(snapshot_sha256):
        raise ValueError(f"snapshot_sha256 is not a sha256 hex digest: {snapshot_sha256!r}")
    spec = load_parity_spec(pack)
    engines: tuple[Engine, ...] = tuple(e for e in ENGINES if e in runners)
    out: list[FingerprintResult] = []
    for fp in spec.fingerprints:
        results: dict[str, ResultSet] = {}
        errors: dict[str, str] = {}
        for engine in engines:
            try:
                results[engine] = runners[engine](rendered_query(pack, fp, engine))
            except Exception as exc:  # recorded, and the run fails
                errors[engine] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if errors:
            out.append(FingerprintResult(fp.name, results, errors, False, "query failed"))
        elif len(engines) == 1:
            out.append(FingerprintResult(fp.name, results, errors, None, None))
        else:
            ok, reason = compare(results, fp.ordered)
            out.append(FingerprintResult(fp.name, results, errors, ok, reason))
    if any(f.match is False for f in out):
        status: Status = "fail"
    elif len(engines) == 1:
        status = "single_engine"
    else:
        status = "pass"
    report = ParityReport(
        pack=pack.ref,
        engines=engines,
        status=status,
        generated_at=now(),
        fingerprints=tuple(out),
        snapshot_sha256=snapshot_sha256,
    )
    if out_dir is not None:
        path = write_parity_report(report, out_dir)
        report = replace(report, path=path)
    return report


def write_parity_report(report: ParityReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"parity-{report.generated_at.date().isoformat()}.json"
    text = json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def require_parity(report: ParityReport) -> None:
    """Raise `ParityError` when parity failed: a mismatch blocks gold and every run."""
    if not report.passed:
        bad = [f"{f.name} ({f.reason})" for f in report.fingerprints if f.match is False]
        raise ParityError(f"parity failed for {report.pack}: {'; '.join(bad)}")


__all__ = [
    "Fingerprint",
    "FingerprintResult",
    "ParityError",
    "ParityReport",
    "ParitySpec",
    "QueryRunner",
    "clickhouse_runner",
    "compare",
    "executor_runner",
    "load_parity_spec",
    "rendered_query",
    "require_parity",
    "run_parity",
    "snowflake_runner",
    "write_parity_report",
]
