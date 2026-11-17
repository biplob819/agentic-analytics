"""Question bank and probes files: JSONL into schemas (QST-01, QST-06, QST-07).

A pack keeps one question per line in `questions.jsonl` and one probe per line in `probes.jsonl`
(Tech Spec §2.7). `load_questions(pack)` and `load_probes(pack)` validate every line into
`Question` / `Probe`; every problem names the file and line, duplicate ids are refused (with the
line of the first use), and so are duplicate JSON keys and NaN or infinite numbers. Blank lines
are skipped, so adding a question is appending one line (US8).

Relative-time gold SQL carries the literal placeholder `'{as_of}'` (a quoted ISO date, D-10).
`bind_as_of(sql, as_of)` substitutes the snapshot's as-of date; the gold check (Q2.2) calls it
before executing anything.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agentic_analytics.packs import Pack, PackError, load_pack, pack_file
from agentic_analytics.schemas import Probe, Question

QUESTIONS_FILE = "questions.jsonl"
PROBES_FILE = "probes.jsonl"
AS_OF_PLACEHOLDER = "{as_of}"
_BRACES = re.compile(r"\{[^{}]*\}|[{}]")
MAX_REPORTED = 50

M = TypeVar("M", bound=BaseModel)


class BankError(ValueError):
    """A questions or probes file is malformed; the message lists `file:line: problem` entries."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        shown = list(self.problems[:MAX_REPORTED])
        if len(self.problems) > MAX_REPORTED:
            shown.append(f"... and {len(self.problems) - MAX_REPORTED} more")
        super().__init__("\n".join(shown))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_jsonl(text: str, model: type[M], source: str) -> tuple[M, ...]:
    """Validate each non-blank line of `text` into `model`; ids must be unique."""
    problems: list[str] = []
    items: list[M] = []
    first_line: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        where = f"{source}:{lineno}"
        try:
            data = json.loads(
                line, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants
            )
        except ValueError as exc:  # JSONDecodeError is a ValueError
            problems.append(f"{where}: invalid JSON ({exc})")
            continue
        if not isinstance(data, dict):
            problems.append(f"{where}: each line must be a JSON object")
            continue
        label = f" {data['id']}" if isinstance(data.get("id"), str) else ""
        try:
            item = model.model_validate(data)
        except ValidationError as exc:
            problems.append(f"{where}:{label} {_validation_summary(exc)}")
            continue
        item_id = item.id  # type: ignore[attr-defined]
        if item_id in first_line:
            problems.append(
                f"{where}: duplicate id {item_id} (first on line {first_line[item_id]})"
            )
            continue
        first_line[item_id] = lineno
        items.append(item)
    if problems:
        raise BankError(problems)
    return tuple(items)


def parse_questions(text: str, source: str = QUESTIONS_FILE) -> tuple[Question, ...]:
    return parse_jsonl(text, Question, source)


def parse_probes(text: str, source: str = PROBES_FILE) -> tuple[Probe, ...]:
    return parse_jsonl(text, Probe, source)


def _pack(pack: Pack | str | os.PathLike[str]) -> Pack:
    return pack if isinstance(pack, Pack) else load_pack(pack)


def _read(pack: Pack, relpath: str) -> tuple[str, str]:
    target = pack_file(pack, relpath)  # PackError if missing
    source = f"{pack.ref}/{relpath}"
    try:
        return target.read_text(encoding="utf-8"), source
    except (OSError, UnicodeDecodeError) as exc:
        raise PackError(f"{source}: cannot read ({type(exc).__name__})") from exc


def load_questions(pack: Pack | str | os.PathLike[str]) -> tuple[Question, ...]:
    """The pack's `questions.jsonl`, validated. PackError if missing, BankError if malformed."""
    text, source = _read(_pack(pack), QUESTIONS_FILE)
    return parse_questions(text, source)


def load_probes(pack: Pack | str | os.PathLike[str]) -> tuple[Probe, ...]:
    """The pack's `probes.jsonl`, validated. PackError if missing, BankError if malformed."""
    text, source = _read(_pack(pack), PROBES_FILE)
    return parse_probes(text, source)


def placeholder_problem(sql: str) -> str | None:
    """Why `sql`'s placeholders are malformed, or None. The only placeholder is `'{as_of}'`."""
    for m in _BRACES.finditer(sql):
        token = m.group(0)
        if token != AS_OF_PLACEHOLDER:
            return f"unknown or malformed placeholder {token!r} (only '{AS_OF_PLACEHOLDER}')"
        start, end = m.span()
        if sql[start - 1 : start] != "'" or sql[end : end + 1] != "'":
            return f"{AS_OF_PLACEHOLDER} must be quoted as a date literal: '{AS_OF_PLACEHOLDER}'"
    return None


def uses_as_of(sql: str) -> bool:
    return AS_OF_PLACEHOLDER in sql


def bind_as_of(sql: str, as_of: date) -> str:
    """Substitute the as-of date (D-10) for every `'{as_of}'`; malformed placeholders raise."""
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise TypeError("as_of must be a date, not a datetime or string")
    problem = placeholder_problem(sql)
    if problem:
        raise ValueError(problem)
    return sql.replace(AS_OF_PLACEHOLDER, as_of.isoformat())


__all__ = [
    "AS_OF_PLACEHOLDER",
    "PROBES_FILE",
    "QUESTIONS_FILE",
    "BankError",
    "bind_as_of",
    "load_probes",
    "load_questions",
    "parse_jsonl",
    "parse_probes",
    "parse_questions",
    "placeholder_problem",
    "uses_as_of",
]
