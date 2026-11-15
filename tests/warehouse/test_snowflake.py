"""EXE-03 and EXE-05: the Snowflake executor, against a fake connector connection."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from snowflake.connector.errors import DatabaseError, OperationalError, ProgrammingError

from agentic_analytics.schemas import Execution
from agentic_analytics.warehouse import SnowflakeExecutor, WarehouseConfigError
from tests.warehouse.fakes import (
    LIVE,
    FakeConnect,
    FakeConnection,
    FakeCursor,
    StepClock,
    write_rsa_key,
)

SQL = "SELECT TOWN, MEDIAN(PRICE) AS M FROM ANALYTICS.UK.UK_PRICE_PAID GROUP BY TOWN"


@pytest.fixture(scope="module")
def key_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_rsa_key(tmp_path_factory.mktemp("keys") / "ro.p8")


def make(key_path: Path, conn: Any, **kw: Any) -> tuple[SnowflakeExecutor, FakeConnect]:
    connect = FakeConnect(conn)
    kw.setdefault("clock", StepClock(0.25))
    exe = SnowflakeExecutor(
        "acct-x", "RO_USER", key_path, "ANALYTICS_XS", "ANALYTICS_RO", connect=connect, **kw
    )
    return exe, connect


def test_key_pair_connection_with_timeout_and_tag(key_path: Path) -> None:
    conn = FakeConnection([FakeCursor(["X"], [(1,)]), FakeCursor(["X"], [(2,)])])
    exe, connect = make(key_path, conn, database="ANALYTICS")
    assert connect.calls == []  # nothing connects at construction
    exe.execute("SELECT 1 AS X")
    exe.execute("SELECT 2 AS X")
    assert len(connect.calls) == 1  # one connection, reused
    kw = connect.calls[0]
    assert kw["account"] == "acct-x" and kw["user"] == "RO_USER"
    assert kw["authenticator"] == "SNOWFLAKE_JWT"
    assert kw["role"] == "ANALYTICS_RO" and kw["warehouse"] == "ANALYTICS_XS"
    assert kw["database"] == "ANALYTICS"
    assert kw["session_parameters"] == {
        "STATEMENT_TIMEOUT_IN_SECONDS": 30,
        "MULTI_STATEMENT_COUNT": 1,
        "QUERY_TAG": "agentic-analytics",
    }
    assert "password" not in kw
    key = serialization.load_der_private_key(kw["private_key"], password=None)
    assert key.key_size == 2048  # type: ignore[union-attr]


def test_encrypted_key_needs_its_passphrase(tmp_path: Path) -> None:
    path = write_rsa_key(tmp_path / "enc.p8", passphrase="s3cret-phrase")
    conn = FakeConnection([FakeCursor(["X"], [(1,)])])
    exe, _ = make(path, conn, private_key_passphrase="s3cret-phrase")
    assert exe.execute("SELECT 1 AS X").error is None
    exe2, _ = make(path, FakeConnection([]))
    with pytest.raises(WarehouseConfigError):
        exe2.execute("SELECT 1 AS X")


def test_missing_key_file_is_a_config_error(tmp_path: Path) -> None:
    exe, connect = make(tmp_path / "nope.p8", FakeConnection([]))
    with pytest.raises(WarehouseConfigError, match="private key"):
        exe.execute("SELECT 1")
    assert connect.calls == []


def test_success_returns_typed_result_with_sfqid(key_path: Path) -> None:
    rows = [("LEEDS", Decimal("250000.50"), dt.date(2024, 1, 31), b"\x01")]
    cur = FakeCursor(["TOWN", "M", "D", "B"], rows, sfqid="01b-qid-1")
    exe, _ = make(key_path, FakeConnection([cur]))
    ex = exe.execute(SQL)
    assert ex.error is None and ex.engine == "snowflake" and ex.sql == SQL
    assert ex.result is not None and ex.result.columns == ("TOWN", "M", "D", "B")
    assert ex.result.rows == (("LEEDS", 250000.5, "2024-01-31", "AQ=="),)
    assert ex.rows_total == 1 and not ex.truncated
    assert ex.query_id == "01b-qid-1" and ex.elapsed_ms == 250
    assert cur.executed == [SQL] and cur.fetch_sizes == [10_001] and cur.closed
    assert Execution.model_validate_json(ex.model_dump_json()) == ex


def test_truncation_uses_the_exact_rowcount(key_path: Path) -> None:
    cur = FakeCursor(["I"], [(i,) for i in range(50)], rowcount=98_765)
    exe, _ = make(key_path, FakeConnection([cur]))
    ex = exe.execute("SELECT SEQ4() AS I FROM TABLE(GENERATOR(ROWCOUNT => 98765))", row_cap=10)
    assert cur.fetch_sizes == [11]
    assert ex.result is not None and len(ex.result.rows) == 10
    assert ex.truncated and ex.rows_total == 98_765


def test_truncation_without_rowcount_falls_back_to_a_count(key_path: Path) -> None:
    main = FakeCursor(["I"], [(i,) for i in range(20)], rowcount=None)
    count = FakeCursor(["N"], [(Decimal(4321),)])
    exe, _ = make(key_path, FakeConnection([main, count]))
    ex = exe.execute("SELECT I FROM T;", row_cap=10)
    assert ex.truncated and ex.rows_total == 4321
    assert count.executed == ["SELECT count(*) AS n FROM (\nSELECT I FROM T\n) AS aa_counted"]
    assert main.closed and count.closed


def test_truncation_with_failed_count_reports_the_lower_bound(key_path: Path) -> None:
    main = FakeCursor(["I"], [(i,) for i in range(20)], rowcount=None)
    count = FakeCursor(error=ProgrammingError("boom", errno=1))
    exe, _ = make(key_path, FakeConnection([main, count]))
    ex = exe.execute("SELECT I FROM T", row_cap=10)
    assert ex.truncated and ex.rows_total == 11


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (
            ProgrammingError(
                "Statement reached its statement or warehouse timeout",
                errno=630,
                sqlstate="57014",
                sfqid="01b-timeout",
            ),
            "timeout",
        ),
        (ProgrammingError("SQL execution canceled", errno=604, sfqid="01b-timeout"), "timeout"),
        (
            ProgrammingError(
                "Object 'X' does not exist or not authorized.",
                errno=2003,
                sqlstate="42S02",
                sfqid="01b-timeout",
            ),
            "sql_error",
        ),
        (DatabaseError("SQL compilation error", errno=1003, sfqid="01b-timeout"), "sql_error"),
        (OperationalError("connection reset", errno=250003, sfqid="01b-timeout"), "transport"),
    ],
)
def test_errors_become_executions_with_the_sfqid(
    key_path: Path, exc: BaseException, kind: str
) -> None:
    cur = FakeCursor(error=exc)
    exe, _ = make(key_path, FakeConnection([cur]))
    ex = exe.execute(SQL)
    assert ex.error == kind and ex.result is None and ex.rows_total is None
    assert ex.query_id == "01b-timeout" and ex.elapsed_ms == 250
    assert cur.closed


def test_guard_refusal_never_connects(key_path: Path) -> None:
    exe, connect = make(key_path, FakeConnection([]))
    ex = exe.execute("SELECT * FROM @mystage")
    assert ex.error == "guard" and ex.query_id is None and connect.calls == []
    assert exe.execute("COPY INTO t FROM @s").error == "guard"


def test_connect_failures(key_path: Path) -> None:
    exe, _ = make(key_path, OperationalError("could not reach host", errno=250001))
    ex = exe.execute("SELECT 1")
    assert ex.error == "transport" and ex.query_id is None
    exe2, _ = make(key_path, DatabaseError("JWT token is invalid", errno=390144))
    with pytest.raises(WarehouseConfigError):
        exe2.execute("SELECT 1")


def test_statement_without_a_result_set_is_an_sql_error(key_path: Path) -> None:
    exe, _ = make(key_path, FakeConnection([FakeCursor(None, [])]))
    ex = exe.execute("SELECT 1")
    assert ex.error == "sql_error" and ex.query_id is not None


def test_repr_hides_the_key_path_contents(key_path: Path) -> None:
    exe, _ = make(key_path, FakeConnection([]))
    assert "PRIVATE KEY" not in repr(exe)


@pytest.mark.live
@LIVE
def test_live_snowflake_select_one() -> None:  # pragma: no cover - needs a real account
    import os

    exe = SnowflakeExecutor(
        os.environ["SNOWFLAKE_ACCOUNT"],
        os.environ["SNOWFLAKE_RO_USER"],
        os.environ["SNOWFLAKE_RO_PRIVATE_KEY_PATH"],
        os.environ["SNOWFLAKE_WAREHOUSE"],
        "ANALYTICS_RO",
    )
    ex = exe.execute("SELECT 1 AS OK")
    assert ex.error is None and ex.query_id


def test_sqlstate_57014_alone_means_timeout(key_path: Path) -> None:
    exc = ProgrammingError("query canceled", errno=1, sqlstate="57014", sfqid="01b-c")
    exe, _ = make(key_path, FakeConnection([FakeCursor(error=exc)]))
    assert exe.execute(SQL).error == "timeout"
