"""Read-only ClickHouse executor over clickhouse-connect (EXE-02, EXE-05, D-3).

Every statement passes `check_sql` first. Queries run with `readonly=1`, `max_execution_time=30`
and `max_result_rows=row_cap + 1` with `result_overflow_mode='break'`, so the server stops after
just over the cap; the client keeps `row_cap` rows and reports `truncated`. When rows were cut, a
second guarded query (`SELECT count(*) FROM (<sql>)`) gives the exact `rows_total`; if that count
fails, `rows_total` is the lower bound `row_cap + 1`. `elapsed_ms` is wall-clock time for the whole
call, count included. The executor generates the `query_id` and sends it, so the id is recorded
even when the query fails; it is `None` only when nothing was sent (guard refusal, no connection).
Warehouse errors come back as `Execution(error=...)`; bad credentials raise `WarehouseConfigError`.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError, DatabaseError, OperationalError

from agentic_analytics.schemas import Engine, Execution
from agentic_analytics.warehouse._common import (
    ROW_CAP,
    STATEMENT_TIMEOUT_S,
    Clock,
    ErrorKind,
    Timer,
    WarehouseConfigError,
    check_row_cap,
    count_sql,
    failed,
    succeeded,
)
from agentic_analytics.warehouse.guard import check_sql

log = logging.getLogger(__name__)

TIMEOUT_CODES = frozenset({159, 160, 209, 394})  # TIMEOUT_EXCEEDED, TOO_SLOW, SOCKET_TIMEOUT, ...
CONFIG_CODES = frozenset({192, 194, 516})  # UNKNOWN_USER, REQUIRED_PASSWORD, AUTH_FAILED


# Only the code at the very start of the message counts: clickhouse-connect writes
# "Received ClickHouse exception, code: NNN, ..." and the server body starts "Code: NNN.".
# Anything later is free text the server may echo from the query (EXE-02).
_CODE_PREFIX = re.compile(r"^\s*(?:Received ClickHouse exception, code: |Code: )(\d+)")


def error_code(exc: BaseException) -> int | None:
    """The numeric ClickHouse error code: the driver's `code` attribute, else the message prefix."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    m = _CODE_PREFIX.match(str(exc))
    return int(m.group(1)) if m else None


def classify(exc: BaseException) -> ErrorKind:
    """Map a clickhouse-connect (or socket) exception to an Execution error kind, by code only."""
    if error_code(exc) in TIMEOUT_CODES or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OperationalError | ConnectionError | OSError):
        return "transport"
    if isinstance(exc, ClickHouseError):
        return "sql_error"
    return "transport"


def _is_config(exc: BaseException) -> bool:
    return error_code(exc) in CONFIG_CODES


class ClickHouseExecutor:
    engine: Engine = "clickhouse"

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        client: Any | None = None,
        port: int = 8443,
        secure: bool = True,
        max_execution_time: int = STATEMENT_TIMEOUT_S,
        clock: Clock = time.perf_counter,
        query_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._secure = secure
        self._client = client
        self.max_execution_time = max_execution_time
        self._clock = clock
        self._new_query_id = query_id_factory or (lambda: f"aa-{uuid.uuid4()}")

    def __repr__(self) -> str:  # never shows the password
        return f"ClickHouseExecutor(host={self._host!r}, user={self._user!r})"

    @property
    def client(self) -> Any:
        """The clickhouse-connect client, created on first use."""
        if self._client is None:
            import clickhouse_connect

            try:
                self._client = clickhouse_connect.get_client(
                    host=self._host,
                    username=self._user,
                    password=self._password,
                    port=self._port,
                    secure=self._secure,
                    query_limit=0,  # never append a LIMIT to the SQL
                    connect_timeout=10,
                    send_receive_timeout=self.max_execution_time + 30,
                    autogenerate_session_id=False,
                )
            except DatabaseError as e:
                if _is_config(e):
                    raise WarehouseConfigError(
                        f"ClickHouse rejected the credentials for user {self._user!r}"
                    ) from e
                raise
        return self._client

    def settings(self, row_cap: int, query_id: str) -> dict[str, Any]:
        """Per-query settings. Ones the server marks read-only for this user are left to the
        server profile (clickhouse-connect would refuse to send them); `readonly` goes last."""
        wanted: dict[str, Any] = {
            "max_execution_time": self.max_execution_time,
            "max_result_rows": row_cap + 1,
            "result_overflow_mode": "break",
            "readonly": 1,
        }
        server = getattr(self._client, "server_settings", None) or {}
        out = {k: v for k, v in wanted.items() if not getattr(server.get(k), "readonly", False)}
        out["query_id"] = query_id
        return out

    def execute(self, sql: str, *, row_cap: int = ROW_CAP) -> Execution:
        check_row_cap(row_cap)
        timer = Timer(self._clock)
        verdict = check_sql(sql, "clickhouse")
        if not verdict.ok:
            log.info("guard refused SQL: %s", verdict.reason)
            return failed("clickhouse", sql, "guard", timer, None)
        try:
            client = self.client
        except WarehouseConfigError:
            raise
        except Exception as e:
            log.warning("ClickHouse connection failed: %s", type(e).__name__)
            return failed("clickhouse", sql, classify(e), timer, None)
        query_id = self._new_query_id()
        try:
            result = client.query(sql, settings=self.settings(row_cap, query_id))
            columns = tuple(result.column_names)
            rows = list(result.result_rows)
            query_id = getattr(result, "query_id", None) or query_id
        except Exception as e:
            if _is_config(e):
                raise WarehouseConfigError("ClickHouse rejected the credentials") from e
            log.info("ClickHouse query %s failed: %s", query_id, e)
            return failed("clickhouse", sql, classify(e), timer, query_id)
        total = self._count(client, sql, row_cap) if len(rows) > row_cap else None
        return succeeded(
            "clickhouse",
            sql,
            columns,
            rows,
            row_cap=row_cap,
            rows_total=total,
            timer=timer,
            query_id=query_id,
        )

    def _count(self, client: Any, sql: str, row_cap: int) -> int | None:
        wrapped = count_sql(sql, "clickhouse")
        if not check_sql(wrapped, "clickhouse").ok:
            return None
        try:
            res = client.query(wrapped, settings=self.settings(1, self._new_query_id()))
            n = res.result_rows[0][0]
            return int(n) if isinstance(n, int) and not isinstance(n, bool) else None
        except Exception as e:
            log.info("row count after truncation failed (%s); rows_total is a lower bound", e)
            return None


__all__ = ["ClickHouseExecutor", "classify"]
