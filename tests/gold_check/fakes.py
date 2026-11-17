"""Fake read-only executors and small packs for the F3.1 and Q2.2 tests (no accounts).

`FakeExecutor` answers by exact SQL text with a `ResultSet` (or a list of them, one per call,
the last repeating), or an error kind string. Every statement passes the real N1.2 SQL guard
first, as the real executors do, and the row cap is applied the same way (`truncated`).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from agentic_analytics.packs import load_pack
from agentic_analytics.questions import bind_as_of, load_probes, load_questions
from agentic_analytics.schemas import Engine, Execution, Probe, Question, ResultSet
from agentic_analytics.warehouse import check_sql

BUNDLED = load_pack("uk_price_paid")
BUNDLED_DIR = Path(str(BUNDLED.root))
AS_OF = date(2026, 9, 30)
SNAPSHOT = "ab" * 32
Answer = ResultSet | Sequence[ResultSet | str] | str


def rs(columns: Sequence[str], *rows: Sequence[Any]) -> ResultSet:
    return ResultSet(columns=tuple(columns), rows=tuple(tuple(r) for r in rows))


class FakeExecutor:
    def __init__(
        self,
        engine: Engine,
        answers: Mapping[str, Answer] | None = None,
        default: Answer | None = None,
    ) -> None:
        self.engine: Engine = engine
        self.answers: dict[str, Any] = {k: v for k, v in (answers or {}).items()}
        self.default = default
        self.calls: list[tuple[str, int]] = []
        self._served: dict[str, int] = {}

    def set(self, sql: str, answer: Answer) -> None:
        self.answers[sql] = answer

    def execute(self, sql: str, *, row_cap: int = 10_000) -> Execution:
        self.calls.append((sql, row_cap))
        verdict = check_sql(sql, self.engine)
        if not verdict.ok:
            return self._error(sql, "guard")
        answer = self.answers.get(sql, self.default)
        if answer is None:
            raise AssertionError(f"{self.engine}: unexpected SQL {sql!r}")
        if not isinstance(answer, ResultSet | str):
            n = self._served.get(sql, 0)
            self._served[sql] = n + 1
            answer = list(answer)[min(n, len(answer) - 1)]
        if isinstance(answer, str):
            return self._error(sql, answer)
        rows = answer.rows
        kept = rows[:row_cap]
        return Execution(
            engine=self.engine,
            sql=sql,
            result=ResultSet(columns=answer.columns, rows=kept),
            rows_total=len(rows),
            truncated=len(rows) > row_cap,
            query_id="fake",
            elapsed_ms=3,
            error=None,
        )

    def _error(self, sql: str, kind: str) -> Execution:
        return Execution(
            engine=self.engine,
            sql=sql,
            result=None,
            rows_total=None,
            truncated=False,
            query_id=None,
            elapsed_ms=1,
            error=kind,  # type: ignore[arg-type]
        )

    def sqls(self) -> list[str]:
        return [s for s, _ in self.calls]


def bundled_questions(*ids: str) -> list[Question]:
    by_id = {q.id: q for q in load_questions(BUNDLED)}
    return [by_id[i] for i in ids]


def bundled_probes(*ids: str) -> list[Probe]:
    by_id = {p.id: p for p in load_probes(BUNDLED)}
    return [by_id[i] for i in ids]


def bound(q: Question | Probe, engine: Engine, as_of: date = AS_OF) -> str:
    assert q.gold_sql is not None
    return bind_as_of(q.gold_sql[engine], as_of)


def write_pack(
    root: Path,
    questions: Sequence[Question],
    quotas: Mapping[str, int],
    *,
    with_fixes: bool = False,
    probes: Sequence[Probe] = (),
) -> Path:
    """A pack directory with the bundled manifest (quotas replaced), these questions and probes,
    and the bundled load and probes SQL (so gold reading either table lints clean)."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = (BUNDLED_DIR / "pack.yaml").read_text(encoding="utf-8")
    head = manifest.split("quotas:")[0]
    body = "quotas:\n" + "".join(f"  {k}: {v}\n" for k, v in quotas.items())
    (root / "pack.yaml").write_text(head + body, encoding="utf-8")
    (root / "questions.jsonl").write_text(
        "".join(json.dumps(q.model_dump(mode="json")) + "\n" for q in questions), encoding="utf-8"
    )
    (root / "probes.jsonl").write_text(
        "".join(json.dumps(p.model_dump(mode="json")) + "\n" for p in probes), encoding="utf-8"
    )
    for sub in ("probes", "load"):
        (root / sub).mkdir(exist_ok=True)
        for engine in ("clickhouse", "snowflake"):
            shutil.copyfile(BUNDLED_DIR / sub / f"{engine}.sql", root / sub / f"{engine}.sql")
    if with_fixes:
        copy_fix_sources(root)
    return root


def copy_fix_sources(root: Path) -> None:
    """The bundled semantic model, load SQL and fix files (no frozen record)."""
    for rel in ("semantic.yaml", "load/clickhouse.sql", "load/snowflake.sql"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BUNDLED_DIR / rel, root / rel)
    (root / "fixes").mkdir(exist_ok=True)
    for rel in ("fixes/examples.yaml", "fixes/self_check.yaml"):
        shutil.copyfile(BUNDLED_DIR / rel, root / rel)


def copy_bundled(root: Path) -> Path:
    """A full copy of the bundled pack directory (without generated gold files)."""
    shutil.copytree(BUNDLED_DIR, root, ignore=shutil.ignore_patterns("__pycache__", "gold"))
    return root
