"""Shared executor contract: value conversion, row capping, timing (EXE-02, EXE-03, EXE-05)."""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from agentic_analytics.schemas import Engine, Execution, ResultSet

ROW_CAP = 10_000  # Tech Spec §8: 10,000 rows in the executor
STATEMENT_TIMEOUT_S = 30

ErrorKind = Literal["sql_error", "guard", "timeout", "transport"]
Clock = Callable[[], float]


class WarehouseConfigError(RuntimeError):
    """Bad credentials or key material. Stops the run instead of being scored (EK L-21)."""


class Executor(Protocol):
    """Tech Spec §4. The SQL guard runs first; warehouse errors come back in the Execution."""

    engine: Engine

    def execute(self, sql: str, *, row_cap: int = ROW_CAP) -> Execution: ...


def to_json_value(value: Any) -> Any:
    """Convert one driver value to a JSON-safe value, deterministically.

    - `None`, `bool`, `str` and `int` pass through.
    - Finite floats pass through; NaN and infinities become `None` (ClickHouse `avg` of nothing is
      `nan` where Snowflake returns NULL, so this also keeps the engines comparable).
    - `Decimal`: integral values become `int` (exact); others become the nearest `float`, which
      keeps ~15-17 significant digits, far inside every scoring tolerance (rel 1e-6). Non-finite
      decimals become `None`.
    - `date` -> `YYYY-MM-DD`; naive `datetime` -> ISO 8601 as returned; aware `datetime` ->
      converted to UTC, ISO 8601 with `+00:00`; `time` -> ISO 8601.
    - `timedelta` -> seconds as a number.
    - `UUID`, IPv4/IPv6 addresses -> their canonical string.
    - `bytes` -> standard base64 text.
    - Arrays and tuples -> lists; maps -> lists of `[key, value]` pairs sorted by the key's JSON
      text (so keys 1 and '1' never collide); sets -> sorted lists; all recursive.
    - Anything else -> `str(value)`.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(dt.UTC).isoformat()
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        secs = value.total_seconds()
        return int(secs) if secs == int(secs) else secs
    if isinstance(value, uuid.UUID | ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        pairs = [[to_json_value(k), to_json_value(v)] for k, v in value.items()]
        return sorted(pairs, key=lambda kv: json.dumps(kv[0], sort_keys=True))
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=repr) if isinstance(value, set | frozenset) else value
        return [to_json_value(v) for v in items]
    return str(value)


def make_result(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ResultSet:
    return ResultSet(
        columns=tuple(str(c) for c in columns),
        rows=tuple(tuple(to_json_value(v) for v in r) for r in rows),
    )


def count_sql(sql: str, engine: Engine) -> str:
    """Wrap a guarded query to count its rows.

    The body ends at the last real token, so trailing semicolons and comments (`SELECT 1; -- c`)
    are dropped; the newlines keep any comment left inside the body inert.
    """
    return f"SELECT count(*) AS n FROM (\n{query_body(sql, engine)}\n) AS aa_counted"


def query_body(sql: str, engine: Engine) -> str:
    """`sql` up to its last real token: trailing semicolons and comments dropped, for wrapping."""
    import sqlglot
    from sqlglot.tokenizer_core import TokenType

    body = sql.rstrip()
    try:
        tokens = [
            t for t in sqlglot.tokenize(sql, read=engine) if t.token_type != TokenType.SEMICOLON
        ]
        if tokens:
            body = sql[: tokens[-1].end + 1]
    except Exception:  # the guard already parsed it; keep the plain strip as a fallback
        while body.endswith(";"):
            body = body[:-1].rstrip()
    return body


def recorded_sql(sql: str) -> str:
    """`Execution.sql` must be non-blank; blank input is refused by the guard and kept visible."""
    return sql if isinstance(sql, str) and sql.strip() else "<blank>"


class Timer:
    def __init__(self, clock: Clock = time.perf_counter) -> None:
        self._clock = clock
        self._t0 = clock()

    def ms(self) -> int:
        return max(0, round((self._clock() - self._t0) * 1000))


def failed(
    engine: Engine, sql: str, kind: ErrorKind, timer: Timer, query_id: str | None
) -> Execution:
    return Execution(
        engine=engine,
        sql=recorded_sql(sql),
        result=None,
        rows_total=None,
        truncated=False,
        query_id=query_id,
        elapsed_ms=timer.ms(),
        error=kind,
    )


def succeeded(
    engine: Engine,
    sql: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    row_cap: int,
    rows_total: int | None,
    timer: Timer,
    query_id: str | None,
) -> Execution:
    """Cap the rows at `row_cap`. `rows_total` is the exact count when known.

    When more than `row_cap` rows came back and no exact count is available, `rows_total` is the
    lower bound `row_cap + 1` (documented in the N1.2 spec).
    """
    kept = list(rows[:row_cap])
    if len(rows) <= row_cap:
        total = len(rows)
    else:
        total = rows_total if rows_total is not None and rows_total > row_cap else row_cap + 1
    return Execution(
        engine=engine,
        sql=sql,
        result=make_result(columns, kept),
        rows_total=total,
        truncated=total > len(kept),
        query_id=query_id,
        elapsed_ms=timer.ms(),
        error=None,
    )


def check_row_cap(row_cap: int) -> None:
    if isinstance(row_cap, bool) or not isinstance(row_cap, int) or row_cap < 1:
        raise ValueError(f"row_cap must be a positive int, got {row_cap!r}")


__all__ = [
    "ROW_CAP",
    "STATEMENT_TIMEOUT_S",
    "Executor",
    "Timer",
    "WarehouseConfigError",
    "count_sql",
    "make_result",
    "query_body",
    "to_json_value",
]
