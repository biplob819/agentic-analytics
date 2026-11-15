"""Read-only Snowflake executor over snowflake-connector-python with key-pair auth (EXE-03).

The connection uses `SNOWFLAKE_JWT` with the private key loaded by `cryptography`, the read-only
role and warehouse, and session parameters `STATEMENT_TIMEOUT_IN_SECONDS=30`,
`MULTI_STATEMENT_COUNT=1` and a `QUERY_TAG` for cost allocation. Every statement passes
`check_sql` first. Rows are fetched with `fetchmany(row_cap + 1)`; `cursor.rowcount` is the exact
size of the full result, so `rows_total` is exact without a second query (a count query is the
fallback when the driver does not report it). `query_id` is the statement's `sfqid`, also taken
from the error when a statement fails. Warehouse errors come back as `Execution(error=...)`;
unreadable keys and rejected credentials raise `WarehouseConfigError`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from snowflake.connector import errors as sf_errors

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

TIMEOUT_ERRNOS = frozenset({604, 630})  # SQL execution canceled; statement timeout reached
TIMEOUT_SQLSTATES = frozenset({"57014"})
QUERY_TAG = "agentic-analytics"


def classify(exc: BaseException) -> ErrorKind:
    errno = getattr(exc, "errno", None)
    if (
        errno in TIMEOUT_ERRNOS
        or getattr(exc, "sqlstate", None) in TIMEOUT_SQLSTATES
        or isinstance(exc, TimeoutError)
    ):
        return "timeout"
    if isinstance(exc, sf_errors.OperationalError | sf_errors.InterfaceError | OSError):
        return "transport"
    if isinstance(exc, sf_errors.Error):
        return "sql_error"
    return "transport"


def _is_config(exc: BaseException) -> bool:
    errno = getattr(exc, "errno", None)
    return isinstance(errno, int) and 390100 <= errno < 390200  # authentication and login errors


def load_private_key(path: str | Path, passphrase: str | None = None) -> bytes:
    """Read a PEM private key and return unencrypted PKCS#8 DER bytes for the connector."""
    from cryptography.hazmat.primitives import serialization

    try:
        data = Path(path).read_bytes()
        key = serialization.load_pem_private_key(
            data, password=passphrase.encode("utf-8") if passphrase else None
        )
    except (OSError, ValueError, TypeError) as e:
        raise WarehouseConfigError(
            f"cannot load the Snowflake private key at {path} ({type(e).__name__})"
        ) from e
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class SnowflakeExecutor:
    engine: Engine = "snowflake"

    def __init__(
        self,
        account: str,
        user: str,
        private_key_path: str | Path,
        warehouse: str,
        role: str,
        *,
        connect: Callable[..., Any] | None = None,
        database: str | None = None,
        private_key_passphrase: str | None = None,
        statement_timeout: int = STATEMENT_TIMEOUT_S,
        clock: Clock = time.perf_counter,
    ) -> None:
        self._account = account
        self._user = user
        self._key_path = private_key_path
        self._passphrase = private_key_passphrase
        self.warehouse = warehouse
        self.role = role
        self._database = database
        self.statement_timeout = statement_timeout
        self._connect = connect
        self._clock = clock
        self._conn: Any | None = None

    def __repr__(self) -> str:
        return (
            f"SnowflakeExecutor(account={self._account!r}, user={self._user!r}, role={self.role!r})"
        )

    def connect_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "account": self._account,
            "user": self._user,
            "authenticator": "SNOWFLAKE_JWT",
            "private_key": load_private_key(self._key_path, self._passphrase),
            "warehouse": self.warehouse,
            "role": self.role,
            "login_timeout": 60,
            "session_parameters": {
                "STATEMENT_TIMEOUT_IN_SECONDS": self.statement_timeout,
                "MULTI_STATEMENT_COUNT": 1,
                "QUERY_TAG": QUERY_TAG,
            },
        }
        if self._database:
            kw["database"] = self._database
        return kw

    @property
    def connection(self) -> Any:
        """The connector connection, opened on first use."""
        if self._conn is None:
            kwargs = self.connect_kwargs()  # key problems raise WarehouseConfigError here
            connect = self._connect
            if connect is None:
                import snowflake.connector

                connect = snowflake.connector.connect
            try:
                self._conn = connect(**kwargs)
            except Exception as e:
                if _is_config(e):
                    raise WarehouseConfigError(
                        f"Snowflake rejected key-pair login for user {self._user!r}"
                    ) from e
                raise
        return self._conn

    def execute(self, sql: str, *, row_cap: int = ROW_CAP) -> Execution:
        check_row_cap(row_cap)
        timer = Timer(self._clock)
        verdict = check_sql(sql, "snowflake")
        if not verdict.ok:
            log.info("guard refused SQL: %s", verdict.reason)
            return failed("snowflake", sql, "guard", timer, None)
        try:
            conn = self.connection
        except WarehouseConfigError:
            raise
        except Exception as e:
            log.warning("Snowflake connection failed: %s", type(e).__name__)
            return failed("snowflake", sql, classify(e), timer, None)
        cur = None
        query_id: str | None = None
        try:
            cur = conn.cursor()
            cur.execute(sql)
            query_id = getattr(cur, "sfqid", None)
            columns = tuple(str(d[0]) for d in (cur.description or ()))
            rows = list(cur.fetchmany(row_cap + 1))
            rowcount = getattr(cur, "rowcount", None)
        except Exception as e:
            if _is_config(e):
                raise WarehouseConfigError("Snowflake rejected the session") from e
            query_id = getattr(e, "sfqid", None) or query_id or getattr(cur, "sfqid", None)
            log.info("Snowflake query %s failed: %s", query_id, e)
            return failed("snowflake", sql, classify(e), timer, query_id)
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    log.debug("cursor close failed", exc_info=True)
        if not columns:
            log.info("Snowflake statement %s returned no result set", query_id)
            return failed("snowflake", sql, "sql_error", timer, query_id)
        total: int | None = None
        if len(rows) > row_cap:
            exact = isinstance(rowcount, int) and not isinstance(rowcount, bool)
            if exact and rowcount is not None and rowcount >= len(rows):
                total = rowcount
            else:
                total = self._count(conn, sql)
        return succeeded(
            "snowflake",
            sql,
            columns,
            rows,
            row_cap=row_cap,
            rows_total=total,
            timer=timer,
            query_id=query_id,
        )

    def _count(self, conn: Any, sql: str) -> int | None:
        wrapped = count_sql(sql, "snowflake")
        if not check_sql(wrapped, "snowflake").ok:
            return None
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(wrapped)
            row = cur.fetchone()
            n = row[0] if row else None
            if isinstance(n, Decimal) and n == n.to_integral_value():
                return int(n)
            return int(n) if isinstance(n, int) and not isinstance(n, bool) else None
        except Exception as e:
            log.info("row count after truncation failed (%s); rows_total is a lower bound", e)
            return None
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    log.debug("cursor close failed", exc_info=True)


__all__ = ["SnowflakeExecutor", "classify", "load_private_key"]
