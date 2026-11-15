"""Fake warehouse drivers built from the real SDK objects where they can be (EK L-17).

ClickHouse answers are real `clickhouse_connect` `QueryResult`s and errors are real
`DatabaseError`/`OperationalError`s with codes; Snowflake column descriptions are the
connector's own `ResultMetadata` and errors are its `ProgrammingError`/`OperationalError`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from clickhouse_connect.driver.models import SettingDef
from clickhouse_connect.driver.query import QueryResult
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from snowflake.connector.cursor import ResultMetadata

Handler = Callable[[str, dict[str, Any] | None], Any]

# Live tests need real accounts: marked `live` and skipped unless AA_LIVE=1 is set explicitly.
LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="live: set AA_LIVE=1")


def ch_result(
    columns: list[str], rows: list[tuple[Any, ...]], *, query_id: str = "ch-q-1"
) -> QueryResult:
    return QueryResult(
        result_set=[list(r) for r in rows],
        column_names=tuple(columns),
        query_id=query_id,
        summary={"read_rows": str(len(rows))},
    )


class FakeCHClient:
    """Answers `query`/`command` from a queue of results or exceptions, or from a handler."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        handler: Handler | None = None,
        server_settings: dict[str, SettingDef] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.handler = handler
        self.server_settings = server_settings or {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def _answer(self, kind: str, sql: str, settings: dict[str, Any] | None) -> Any:
        self.calls.append((kind, sql, settings))
        item = self.handler(sql, settings) if self.handler else self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def query(self, sql: str, settings: dict[str, Any] | None = None) -> Any:
        return self._answer("query", sql, settings)

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> Any:
        return self._answer("command", sql, settings)


def readonly_server_settings() -> dict[str, SettingDef]:
    """What a readonly=1 profile looks like to clickhouse-connect: readonly itself is locked."""
    return {
        "readonly": SettingDef(name="readonly", value="1", readonly=1),
        "max_execution_time": SettingDef(name="max_execution_time", value="30", readonly=0),
        "max_result_rows": SettingDef(name="max_result_rows", value="0", readonly=0),
        "result_overflow_mode": SettingDef(name="result_overflow_mode", value="throw", readonly=0),
    }


def sf_column(name: str, type_code: int = 0) -> ResultMetadata:
    return ResultMetadata(name, type_code, None, None, None, None, True)


class FakeCursor:
    def __init__(
        self,
        columns: list[str] | None = None,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        sfqid: str = "01b2c3d4-0000-0001-0000-000000000001",
        rowcount: int | None = -1,
        error: BaseException | None = None,
    ) -> None:
        self.description = [sf_column(c) for c in columns] if columns is not None else None
        self._rows = list(rows or [])
        self.sfqid: str | None = None
        self._sfqid = sfqid
        self.rowcount = len(self._rows) if rowcount == -1 else rowcount
        self.error = error
        self.executed: list[str] = []
        self.fetch_sizes: list[int | None] = []
        self.closed = False

    def execute(self, sql: str) -> FakeCursor:
        self.executed.append(sql)
        if self.error is not None:
            raise self.error
        self.sfqid = self._sfqid
        return self

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        self.fetch_sizes.append(size)
        return self._rows[: size or 1]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Hands out cursors from a queue, or builds one per SQL with a handler."""

    def __init__(
        self,
        cursors: list[FakeCursor] | None = None,
        *,
        handler: Callable[[str], FakeCursor] | None = None,
    ) -> None:
        self.cursors = list(cursors or [])
        self.handler = handler
        self.handed: list[FakeCursor] = []

    def cursor(self) -> Any:
        if self.handler is not None:
            return _Deferred(self.handler, self.handed)
        cur = self.cursors.pop(0)
        self.handed.append(cur)
        return cur


class _Deferred:
    """A cursor whose behaviour is chosen once the SQL is known."""

    def __init__(self, handler: Callable[[str], FakeCursor], handed: list[FakeCursor]) -> None:
        self._handler = handler
        self._handed = handed
        self._cur: FakeCursor | None = None

    def execute(self, sql: str) -> Any:
        self._cur = self._handler(sql)
        self._handed.append(self._cur)
        return self._cur.execute(sql)

    def __getattr__(self, name: str) -> Any:
        if self._cur is None:
            if name == "close":
                return lambda: None
            raise AttributeError(name)
        return getattr(self._cur, name)


class FakeConnect:
    """Stands in for `snowflake.connector.connect`; records the keyword arguments."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.conn, BaseException):
            raise self.conn
        return self.conn


def write_rsa_key(path: Path, passphrase: str | None = None) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase
        else serialization.NoEncryption()
    )
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    )
    return path


class StepClock:
    """A perf_counter stand-in that advances by `step` seconds per call."""

    def __init__(self, step: float = 0.125, start: float = 100.0) -> None:
        self.t = start - step
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t
