"""EXE-02 and EXE-05: the ClickHouse executor, against a fake clickhouse-connect client."""

from __future__ import annotations

import datetime as dt
import ipaddress
import uuid
from decimal import Decimal
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError, ProgrammingError

from agentic_analytics.schemas import Execution
from agentic_analytics.warehouse import ClickHouseExecutor, WarehouseConfigError
from agentic_analytics.warehouse import clickhouse as ch_mod
from tests.warehouse.fakes import (
    LIVE,
    FakeCHClient,
    StepClock,
    ch_result,
    readonly_server_settings,
)

SQL = "SELECT town, medianExact(price) AS m FROM uk.uk_price_paid GROUP BY town"


def make(client: FakeCHClient, **kw: Any) -> ClickHouseExecutor:
    kw.setdefault("clock", StepClock(0.125))
    kw.setdefault("query_id_factory", lambda: "aa-fixed")
    return ClickHouseExecutor("h.example", "ro", "pw-not-shown", client=client, **kw)


def test_success_returns_typed_result_with_query_id_and_elapsed() -> None:
    rows = [
        (
            "LEEDS",
            Decimal("250000.00"),
            dt.date(2024, 5, 1),
            dt.datetime(2024, 5, 1, 9, 30),
            uuid.UUID(int=7),
            ipaddress.IPv4Address("10.0.0.1"),
            float("nan"),
        )
    ]
    cols = ["town", "m", "d", "ts", "id", "ip", "avg_empty"]
    client = FakeCHClient([ch_result(cols, rows, query_id="server-qid")])
    ex = make(client).execute(SQL)
    assert ex.error is None and ex.engine == "clickhouse" and ex.sql == SQL
    assert ex.result is not None
    assert ex.result.columns == tuple(cols)
    assert ex.result.rows == (
        (
            "LEEDS",
            250000,
            "2024-05-01",
            "2024-05-01T09:30:00",
            "00000000-0000-0000-0000-000000000007",
            "10.0.0.1",
            None,
        ),
    )
    assert ex.rows_total == 1 and ex.truncated is False
    assert ex.query_id == "server-qid"
    assert ex.elapsed_ms == 125  # one clock step between start and finish
    assert Execution.model_validate_json(ex.model_dump_json()) == ex


def test_settings_are_readonly_with_limits_and_our_query_id() -> None:
    client = FakeCHClient([ch_result(["x"], [(1,)])])
    make(client).execute("SELECT 1 AS x", row_cap=50)
    kind, sql, settings = client.calls[0]
    assert (kind, sql) == ("query", "SELECT 1 AS x")
    assert settings == {
        "max_execution_time": 30,
        "max_result_rows": 51,
        "result_overflow_mode": "break",
        "readonly": 1,
        "query_id": "aa-fixed",
    }
    assert (
        settings is not None and list(settings)[-2] == "readonly"
    )  # applied after the limits it would otherwise lock


def test_settings_locked_by_the_server_profile_are_left_to_it() -> None:
    client = FakeCHClient([ch_result(["x"], [(1,)])], server_settings=readonly_server_settings())
    make(client).execute("SELECT 1 AS x")
    settings = client.calls[0][2] or {}
    assert "readonly" not in settings  # clickhouse-connect refuses to send a locked setting
    assert settings["max_execution_time"] == 30 and settings["max_result_rows"] == 10_001


def test_guard_refusal_never_reaches_the_server() -> None:
    client = FakeCHClient([])
    ex = make(client).execute("SELECT * FROM url('http://x', CSV)")
    assert ex.error == "guard" and ex.result is None and ex.query_id is None
    assert client.calls == []
    assert isinstance(ex.elapsed_ms, int) and ex.elapsed_ms >= 0


def test_guard_runs_before_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    real = ch_mod.check_sql

    def spy(sql: str, engine: Any) -> Any:
        order.append("guard")
        return real(sql, engine)

    class Recording(FakeCHClient):
        def query(self, sql: str, settings: dict[str, Any] | None = None) -> Any:
            order.append("query")
            return super().query(sql, settings)

    monkeypatch.setattr(ch_mod, "check_sql", spy)
    make(Recording([ch_result(["x"], [(1,)])])).execute("SELECT 1 AS x")
    assert order == ["guard", "query"]


def test_blank_sql_is_a_guard_refusal_with_visible_sql() -> None:
    ex = make(FakeCHClient([])).execute("   ")
    assert ex.error == "guard" and ex.sql == "<blank>"


def test_truncation_keeps_the_cap_and_counts_the_rest() -> None:
    cap = 5
    block = [(i,) for i in range(8)]  # 'break' returns whole blocks, so more than cap + 1
    client = FakeCHClient([ch_result(["i"], block, query_id="q-main"), ch_result(["n"], [(1234,)])])
    ex = make(client).execute("SELECT number AS i FROM numbers(1234);", row_cap=cap)
    assert ex.result is not None and len(ex.result.rows) == cap
    assert ex.truncated is True and ex.rows_total == 1234
    assert ex.query_id == "q-main"
    count_sql = client.calls[1][1]
    assert count_sql.startswith("SELECT count(*) AS n FROM (\nSELECT number AS i FROM numbers")
    assert ";" not in count_sql


def test_truncation_with_failed_count_reports_the_lower_bound() -> None:
    client = FakeCHClient(
        [ch_result(["i"], [(i,) for i in range(4)]), DatabaseError("boom", code=241)]
    )
    ex = make(client).execute("SELECT number AS i FROM numbers(100)", row_cap=3)
    assert ex.truncated is True and ex.rows_total == 4
    assert ex.result is not None and len(ex.result.rows) == 3


def test_exactly_the_cap_is_not_truncated_and_needs_no_count() -> None:
    client = FakeCHClient([ch_result(["i"], [(i,) for i in range(3)])])
    ex = make(client).execute("SELECT number AS i FROM numbers(3)", row_cap=3)
    assert ex.truncated is False and ex.rows_total == 3 and len(client.calls) == 1


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (DatabaseError("Code: 159. TIMEOUT_EXCEEDED", code=159), "timeout"),
        (DatabaseError("Code: 160. TOO_SLOW", code=160), "timeout"),
        (DatabaseError("Code: 62. Syntax error", code=62), "sql_error"),
        (DatabaseError("Code: 164. READONLY", code=164), "sql_error"),
        (DatabaseError("Code: 47. UNKNOWN_IDENTIFIER", code=47), "sql_error"),
        (OperationalError("Network Error"), "transport"),
        (ConnectionResetError("reset"), "transport"),
        (TimeoutError("read timed out"), "timeout"),
        (RuntimeError("driver bug"), "transport"),
    ],
)
def test_errors_become_executions_with_the_query_id(exc: BaseException, kind: str) -> None:
    ex = make(FakeCHClient([exc])).execute(SQL)
    assert ex.error == kind and ex.result is None and ex.rows_total is None
    assert ex.truncated is False
    assert ex.query_id == "aa-fixed"  # generated and sent, so recorded even on failure
    assert ex.elapsed_ms == 125


def test_client_side_errors_are_sql_errors() -> None:
    ex = make(FakeCHClient([ProgrammingError("Setting x is readonly")])).execute(SQL)
    assert ex.error == "sql_error"


def test_rejected_credentials_raise_a_config_error() -> None:
    with pytest.raises(WarehouseConfigError):
        make(FakeCHClient([DatabaseError("AUTHENTICATION_FAILED", code=516)])).execute(SQL)


def test_lazy_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get_client(**kw: Any) -> Any:
        seen.update(kw)
        return FakeCHClient([ch_result(["x"], [(1,)])])

    import clickhouse_connect

    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    exe = ClickHouseExecutor("h.example", "ro", "pw-not-shown")
    assert seen == {}  # nothing connects at construction
    assert exe.execute("SELECT 1 AS x").error is None
    assert seen["host"] == "h.example" and seen["username"] == "ro" and seen["secure"] is True
    assert seen["query_limit"] == 0  # the client must never append LIMIT to our SQL
    assert "pw-not-shown" not in repr(exe)


def test_connection_failure_is_transport_and_auth_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse_connect

    def down(**_: Any) -> Any:
        raise OperationalError("Network Error: connection refused")

    monkeypatch.setattr(clickhouse_connect, "get_client", down)
    ex = ClickHouseExecutor("h", "ro", "pw").execute("SELECT 1")
    assert ex.error == "transport" and ex.query_id is None

    def denied(**_: Any) -> Any:
        raise DatabaseError("AUTHENTICATION_FAILED", code=516)

    monkeypatch.setattr(clickhouse_connect, "get_client", denied)
    with pytest.raises(WarehouseConfigError):
        ClickHouseExecutor("h", "ro", "pw").execute("SELECT 1")


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_bad_row_cap_is_a_programming_error(cap: Any) -> None:
    with pytest.raises(ValueError):
        make(FakeCHClient([])).execute("SELECT 1", row_cap=cap)


def test_default_cap_is_ten_thousand() -> None:
    client = FakeCHClient([ch_result(["x"], [(1,)])])
    make(client).execute("SELECT 1 AS x")
    assert (client.calls[0][2] or {})["max_result_rows"] == 10_001


@pytest.mark.live
@LIVE
def test_live_clickhouse_select_one() -> None:  # pragma: no cover - needs a real account
    import os

    exe = ClickHouseExecutor(
        os.environ["CLICKHOUSE_HOST"],
        os.environ["CLICKHOUSE_RO_USER"],
        os.environ["CLICKHOUSE_RO_PASSWORD"],
    )
    ex = exe.execute("SELECT 1 AS ok")
    assert ex.error is None and ex.query_id


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        # free text echoed by the server never decides the kind (verifier round 1)
        (DatabaseError("Code: 47. Unknown identifier AUTHENTICATION_FAILED", code=47), "sql_error"),
        (DatabaseError("Code: 62. Syntax error near TIMEOUT_EXCEEDED", code=62), "sql_error"),
        (DatabaseError("Code: 47. Unknown identifier AUTHENTICATION_FAILED"), "sql_error"),
        (DatabaseError("Code: 62. Syntax error: ... Code: 159 ... TIMEOUT_EXCEEDED"), "sql_error"),
        # without a code attribute, the code at the start of the message is used
        (
            DatabaseError("Received ClickHouse exception, code: 159, server response: ..."),
            "timeout",
        ),
        (DatabaseError("Code: 160. DB::Exception: TOO_SLOW"), "timeout"),
    ],
)
def test_errors_are_classified_by_numeric_code_only(exc: BaseException, kind: str) -> None:
    assert make(FakeCHClient([exc])).execute(SQL).error == kind


def test_config_error_needs_the_auth_code_not_the_word() -> None:
    with pytest.raises(WarehouseConfigError):
        make(FakeCHClient([DatabaseError("Code: 516. DB::Exception: bad password")])).execute(SQL)
    ex = make(FakeCHClient([DatabaseError("Code: 47. AUTHENTICATION_FAILED", code=47)])).execute(
        "SELECT AUTHENTICATION_FAILED FROM uk.uk_price_paid"
    )
    assert ex.error == "sql_error"


def test_truncated_query_with_trailing_comment_still_gets_an_exact_count() -> None:
    client = FakeCHClient([ch_result(["i"], [(i,) for i in range(5)]), ch_result(["n"], [(77,)])])
    ex = make(client).execute("SELECT number AS i FROM numbers(77); -- c", row_cap=3)
    assert ex.rows_total == 77 and ex.truncated
    assert client.calls[1][1] == (
        "SELECT count(*) AS n FROM (\nSELECT number AS i FROM numbers(77)\n) AS aa_counted"
    )
