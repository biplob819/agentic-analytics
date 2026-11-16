"""Shared loader code: fixed, templated SQL (rendering, splitting, allow-list) and results.

Loaders write, so they cannot go through the read-only guard. Instead they only run statements
from the pack's SQL files, rendered with validated identifiers, and every statement must match
the loader's allow-list of statement shapes before anything is sent.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agentic_analytics.schemas import Engine
from agentic_analytics.warehouse.guard import (
    CLICKHOUSE_TABLE_FUNCTION_PREFIXES,
    CLICKHOUSE_TABLE_FUNCTIONS,
    DENIED_FUNCTIONS,
    DENIED_PREFIXES,
    SNOWFLAKE_DENIED_FUNCTIONS,
    SNOWFLAKE_DENIED_PREFIXES,
)
from agentic_analytics.warehouse.roles import _ident

Clock = Callable[[], dt.datetime]
Action = Literal["loaded", "reloaded", "skipped"]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_PATH_SAFE = re.compile(r"^[A-Za-z0-9_./:\\ -]+$")


class LoaderError(RuntimeError):
    """A load could not run safely or did not end as expected. Stops the load."""


def ident(name: str, what: str) -> str:
    """A plain SQL identifier (the same rule as the role SQL, so a pack cannot inject SQL)."""
    return _ident(name, what)


def qualified(name: str, parts: int, what: str) -> tuple[str, ...]:
    """`DATABASE.SCHEMA`-style names: exactly `parts` plain identifiers joined by dots."""
    pieces = name.split(".") if isinstance(name, str) else []
    if len(pieces) != parts:
        raise ValueError(f"{what} {name!r} must have {parts} dot-separated part(s)")
    return tuple(ident(p, what) for p in pieces)


def safe_path(path: str, what: str) -> str:
    """A local file path that can sit inside a quoted SQL literal unchanged."""
    if not path or not _PATH_SAFE.match(path):
        raise ValueError(f"{what} {path!r} has characters that cannot go in a SQL literal")
    return path


def render(template: str, params: Mapping[str, str]) -> str:
    """Replace `{name}` placeholders; a placeholder with no value is an error."""
    missing = sorted({m for m in _PLACEHOLDER.findall(template) if m not in params})
    if missing:
        raise ValueError(f"template placeholders without a value: {missing}")
    return _PLACEHOLDER.sub(lambda m: params[m.group(1)], template)


def split_statements(sql: str) -> list[str]:
    """Split on `;` outside quotes, dropping `--` comments and blank statements."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # doubled quote inside a literal
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch == "-" and sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quote:
        raise ValueError("unterminated quoted literal in SQL")
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def normalise(stmt: str) -> str:
    """Whitespace collapsed to single spaces."""
    return " ".join(stmt.split())


_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_REPLACEABLE = r"(?:OR REPLACE )?"
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("create_database", re.compile(rf"^CREATE DATABASE (?:IF NOT EXISTS )?(?P<t>{_NAME})$", re.I)),
    ("create_schema", re.compile(rf"^CREATE SCHEMA (?:IF NOT EXISTS )?(?P<t>{_NAME})$", re.I)),
    (
        "create_file_format",
        re.compile(
            rf"^CREATE {_REPLACEABLE}FILE FORMAT (?:IF NOT EXISTS )?(?P<t>{_NAME})(?: |$)", re.I
        ),
    ),
    (
        "create_stage",
        re.compile(rf"^CREATE {_REPLACEABLE}STAGE (?:IF NOT EXISTS )?(?P<t>{_NAME})(?: |$)", re.I),
    ),
    (
        "create_table",
        re.compile(rf"^CREATE {_REPLACEABLE}TABLE (?:IF NOT EXISTS )?(?P<t>{_NAME}) ?\(", re.I),
    ),
    ("truncate", re.compile(rf"^TRUNCATE TABLE (?P<t>{_NAME})$", re.I)),
    ("put", re.compile(rf"^PUT 'file://[^']+' @(?P<t>{_NAME})(?: |$)", re.I)),
    ("copy", re.compile(rf"^COPY INTO (?P<t>{_NAME}) FROM @(?P<s>{_NAME})/[^\s/]+ ", re.I)),
    (
        "insert",
        re.compile(rf"^INSERT INTO (?P<t>{_NAME}) (?:\([^)]*\) )?(?:SELECT|VALUES)(?: |$)", re.I),
    ),
)


@dataclass(frozen=True)
class Step:
    """One statement with its kind and the objects it touches (target, then a stage for COPY)."""

    kind: str
    targets: tuple[str, ...]
    sql: str


def classify(stmt: str) -> Step:
    flat = normalise(stmt)
    for kind, pattern in _SHAPES:
        m = pattern.match(flat)
        if m:
            return Step(kind, tuple(v for v in m.groups() if v), stmt)
    raise LoaderError(f"statement shape not allowed for a loader: {flat[:120]!r}")


# Functions that reach outside the warehouse (network, files, other servers, external engines,
# dictionaries, stages, AI): the read-only guard's deny lists, reused for loader bodies.
_EXTERNAL = (
    DENIED_FUNCTIONS
    | CLICKHOUSE_TABLE_FUNCTIONS
    | SNOWFLAKE_DENIED_FUNCTIONS
    | {"table", "identifier"}
) - {"values"}  # the VALUES keyword of an INSERT, not the table function
_EXTERNAL_PREFIXES = (
    *DENIED_PREFIXES,
    *CLICKHOUSE_TABLE_FUNCTION_PREFIXES,
    *SNOWFLAKE_DENIED_PREFIXES,
)
_CALL = re.compile(r"(?<![\w$.])([A-Za-z_][\w$]*)\s*\(")
_QUALIFIED_CALL = re.compile(r"[A-Za-z_][\w$]*\s*\.\s*[A-Za-z_][\w$]*\s*\(")
_CH_ENGINE = re.compile(r"^ENGINE = (?:Replacing|Summing|Aggregating)?MergeTree(?:\(\))?(?: |$)")
_URL_SOURCE = re.compile(
    r"^url\('(?P<url>https?://[^'\s]+)', '[A-Za-z]+', '(?P<structure>[^']*)'\)"
    r"(?: SETTINGS max_http_get_redirects ?= ?\d+)?$"
)
_STAGE_BODY = re.compile(rf"^(?:FILE_FORMAT = {_NAME})?$", re.I)
# Syntax the engines read differently from the masking above, so a call could hide from the
# checks below: a backslash anywhere (both engines treat `\'` inside a literal as an escaped quote,
# the masker ends the literal there), a block or `#` comment between a name and its `(`, a
# backtick or double-quoted function name. None of these appears in a pack's load SQL.
_HIDING_CODE = re.compile(r"/\*|\*/|`|#|\"\s*\(")
_CH_TABLE_OPTIONS = re.compile(r"\b(?:AS|SETTINGS|TTL)\b", re.I)

SelectSource = Literal["url", "table"]


def mask_literals(sql: str) -> str:
    """The statement with the contents of every quoted literal or identifier replaced by `x`,
    keeping its length (so offsets match the original) and so body checks never read data as
    code."""
    out = list(sql)
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote and i + 1 < len(sql) and sql[i + 1] == quote:
                out[i] = out[i + 1] = "x"
                i += 2
                continue
            if ch == quote:
                quote = None
            else:
                out[i] = "x"
        elif ch in ("'", '"'):
            quote = ch
        i += 1
    return "".join(out)


def _closing_paren(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise LoaderError("unbalanced parentheses in a loader statement")


def table_columns(stmt: str) -> list[tuple[str, str]]:
    """(name, type) pairs of a `CREATE TABLE … (…)` statement, in order."""
    flat = normalise(stmt)
    code = mask_literals(flat)
    start = code.index("(")
    end = _closing_paren(code, start)
    body, masked = flat[start + 1 : end], code[start + 1 : end]
    cols: list[tuple[str, str]] = []
    depth, last = 0, 0
    for i, ch in enumerate(masked + ","):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            part = body[last:i].strip()
            name, _, typ = part.partition(" ")
            cols.append((name, typ.strip()))
            last = i + 1
    return cols


def _body_problem(
    st: Step, engine: Engine, select_source: SelectSource | None, sources: set[str]
) -> str | None:
    flat = normalise(st.sql)
    code = mask_literals(flat)
    after = code[code.find(st.targets[0]) + len(st.targets[0]) :] if st.targets else code
    if "\\" in flat:
        return "a backslash (an escape the engines read inside literals)"
    hidden = _HIDING_CODE.search(code)
    if hidden:
        return f"a comment, backtick or quoted name that could hide a call ({hidden.group(0)!r})"
    if st.kind not in ("put", "copy") and "@" in code:
        return "a stage reference (@) outside PUT/COPY"
    if _QUALIFIED_CALL.search(after):
        return "a schema-qualified function call"
    url_calls = 0
    for m in _CALL.finditer(code):
        name = m.group(1).casefold()
        if name == "url" and st.kind == "insert" and select_source == "url":
            url_calls += 1
            continue
        if name in _EXTERNAL or name.startswith(_EXTERNAL_PREFIXES):
            return f"an external table function or engine {m.group(1)}()"
    if st.kind == "create_table":
        start = code.index("(")
        tail = code[_closing_paren(code, start) + 1 :].strip()
        if engine == "clickhouse":
            if not _CH_ENGINE.match(tail) or _CH_TABLE_OPTIONS.search(tail):
                return f"table options other than a MergeTree engine: {tail[:60]!r}"
        elif tail:
            return f"table options after the column list: {tail[:60]!r}"
    if st.kind == "create_stage":
        body = flat.split(st.targets[0], 1)[1].strip()
        if not _STAGE_BODY.match(body):
            return "a stage that is not a plain internal stage"
    if st.kind == "insert":
        words = [w.upper() for w in re.findall(r"\b(SELECT|FROM|JOIN|UNION)\b", code, re.I)]
        if not words:  # INSERT … VALUES
            return "url() inside VALUES" if url_calls else None
        if words != ["SELECT", "FROM"]:
            return "an INSERT … SELECT that is not one SELECT from one source"
        source = flat[re.search(r"\bFROM\b", code, re.I).end() :].strip()  # type: ignore[union-attr]
        if select_source == "url":
            tight = re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", source))
            if url_calls != 1 or not _URL_SOURCE.match(tight):
                return "an INSERT whose source is not one documented url() read"
        elif select_source == "table":
            if source not in sources:
                return f"an INSERT from {source[:60]!r}, not the staging table"
        else:
            return "an INSERT … SELECT where only VALUES are allowed"
    elif url_calls:
        return "url() outside the documented INSERT"
    return None


def plan(
    sql: str,
    allowed: Mapping[str, Iterable[str]],
    *,
    engine: Engine,
    select_source: SelectSource | None = None,
    sources: Iterable[str] = (),
) -> list[Step]:
    """Split rendered SQL into steps. Every step's kind and objects must be in `allowed`
    (kind -> object names), and its body must pass the checks below, or nothing is sent:

    - no external table function, engine or stage read anywhere (the read-only guard's deny
      lists), no schema-qualified calls, no `@` outside `PUT`/`COPY`;
    - ClickHouse tables use a MergeTree engine and nothing else; Snowflake tables have no
      options after the column list; stages are internal (`FILE_FORMAT` only);
    - an `INSERT … SELECT` is one SELECT from one source: with `select_source="url"` exactly
      the documented `url('http(s)://…', '<format>', '<structure>')` (optionally with
      `SETTINGS max_http_get_redirects`), with `"table"` one of `sources`; without it only
      `INSERT … VALUES` is allowed.
    """
    steps = [classify(s) for s in split_statements(sql)]
    sets = {k: set(v) for k, v in allowed.items()}
    srcs = set(sources)
    for st in steps:
        ok = sets.get(st.kind)
        flat = normalise(st.sql)[:120]
        if ok is None or any(t not in ok for t in st.targets):
            raise LoaderError(f"{st.kind} on {', '.join(st.targets)} is not allowed: {flat!r}")
        problem = _body_problem(st, engine, select_source, srcs)
        if problem:
            raise LoaderError(f"{st.kind} is not allowed, it has {problem}: {flat!r}")
    return steps


def _norm_type(engine: Engine, typ: str) -> str:
    t = "".join(typ.split())
    if engine == "clickhouse":
        m = re.fullmatch(r"(Enum(?:8|16))\((.*)\)", t)
        if m:
            items = sorted(m.group(2).split(","))
            return f"{m.group(1)}({','.join(items)})"
        return t
    t = t.upper()
    if t in ("VARCHAR", "TEXT", "STRING"):
        return "VARCHAR(16777216)"
    if t in ("NUMBER", "INT", "INTEGER", "BIGINT"):
        return "NUMBER(38,0)"
    return t


def schema_mismatch(
    engine: Engine, expected: list[tuple[str, str]], actual: list[tuple[str, str]]
) -> str | None:
    """A readable difference between the pack's columns and an existing table's, or None."""

    def norm(cols: list[tuple[str, str]]) -> list[tuple[str, str]]:
        fold = str.casefold if engine == "snowflake" else str
        return [(fold(n), _norm_type(engine, t)) for n, t in cols]

    want, have = norm(expected), norm(actual)
    if want == have:
        return None
    for i, (w, h) in enumerate(zip(want, have, strict=False)):
        if w != h:
            return f"column {i + 1} is {h[0]} {h[1]}, the pack expects {w[0]} {w[1]}"
    return f"the table has {len(have)} columns, the pack expects {len(want)}"


@dataclass(frozen=True)
class LoadResult:
    """What one load did: rows before and after per table, and every statement sent."""

    engine: Engine
    target: str
    action: Action
    rows_before: Mapping[str, int]
    rows_after: Mapping[str, int]
    statements: tuple[str, ...]
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "target": self.target,
            "action": self.action,
            "rows_before": dict(self.rows_before),
            "rows_after": dict(self.rows_after),
            "statements": len(self.statements),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "snapshot": dict(self.snapshot),
        }


__all__ = [
    "Clock",
    "LoadResult",
    "LoaderError",
    "Step",
    "classify",
    "ident",
    "normalise",
    "plan",
    "qualified",
    "render",
    "safe_path",
    "schema_mismatch",
    "split_statements",
    "table_columns",
]
