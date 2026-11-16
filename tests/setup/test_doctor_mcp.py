"""SET-07: `doctor` proves that an MCP `run_select_query` runs as `analytics_ro`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from agentic_analytics import cli
from agentic_analytics.warehouse.doctor import (
    ENV_VARS,
    MCP_CHECK,
    MCP_ROLE_SQL,
    mcp_checks,
    mcp_query,
    mcp_query_from_env,
    run_doctor,
)
from tests.setup.fakes import KEY_SECRET

SERVICE = "svc-1"


def role_server(
    roles: Any, readonly: Any = "1", calls: list[dict[str, str]] | None = None
) -> MCPServer:
    """An in-process MCP server shaped like ClickHouse's, answering the role query."""
    server = MCPServer("fake-clickhouse-roles")

    @server.tool(description="List all databases in a ClickHouse service.")
    def list_databases(serviceId: str) -> str:
        return json.dumps(["uk", "uk_probes"])

    @server.tool(description="List all tables in a database.")
    def list_tables(serviceId: str, database: str) -> str:
        return json.dumps([])

    @server.tool(description="Run a SELECT query in a ClickHouse service.")
    def run_select_query(query: str, serviceId: str) -> str:
        if calls is not None:
            calls.append({"query": query, "serviceId": serviceId})
        return json.dumps(
            {
                "meta": [{"name": "roles"}, {"name": "readonly"}],
                "data": [{"roles": roles, "readonly": readonly}],
            }
        )

    return server


def _report(query: Any, env: dict[str, str] | None = None) -> Any:
    return run_doctor(env or {}, mcp=query, check_mcp=True)


def _check(report: Any) -> Any:
    return next(c for c in report.checks if c.name == MCP_CHECK)


def _fixed(content: Any, is_error: bool = False) -> Any:
    text = content if isinstance(content, str) else json.dumps(content)
    return lambda sql: (is_error, text)


def _rows(roles: Any, readonly: Any = "1") -> dict[str, Any]:
    return {"columns": ["roles", "readonly"], "rows": [[roles, readonly]], "rows_total": 1}


@pytest.mark.parametrize(
    ("roles", "readonly", "status"),
    [
        (["analytics_ro"], "1", "pass"),
        (["sql_console_read_only"], "1", "fail"),
        (["sql_console_admin"], "0", "fail"),
        (["analytics_ro", "sql_console_admin"], "1", "fail"),
        ([], "1", "fail"),
        (["analytics_ro"], "0", "fail"),
        (["analytics_ro"], None, "fail"),
        (["analytics_ro"], "2", "pass"),  # D-64: the remote MCP server runs with readonly = 2
        (["analytics_ro"], 2, "pass"),  # the live UInt8 cell is a JSON number
        (["analytics_ro"], "3", "fail"),
        (["analytics_ro"], "", "fail"),
        (["sql_console_read_only"], "2", "fail"),
    ],
)
def test_mcp_role_check_passes_only_for_analytics_ro(
    roles: Any, readonly: Any, status: str
) -> None:
    check = _check(_report(_fixed(_rows(roles, readonly))))
    assert check.status == status
    assert str(roles) in check.detail
    if status == "fail":
        assert "Query API endpoint" in check.detail


def test_mcp_query_runs_through_the_mcp_client_against_a_fake_server() -> None:
    calls: list[dict[str, str]] = []
    query = mcp_query(service_id=SERVICE, server=role_server(["analytics_ro"], 1, calls))
    report = _report(query)
    assert _check(report).status == "pass"
    assert calls == [{"query": MCP_ROLE_SQL, "serviceId": SERVICE}]
    bad = mcp_query(service_id=SERVICE, server=role_server(["sql_console_read_only"]))
    assert _check(_report(bad)).status == "fail"


@pytest.mark.parametrize(
    "cell", [["analytics_ro"], '["analytics_ro"]', "['analytics_ro']", "analytics_ro"]
)
def test_role_cell_formats_parse_the_same(cell: Any) -> None:
    assert _check(_report(_fixed(_rows(cell)))).status == "pass"


@pytest.mark.parametrize(
    "content",
    ["not json", {"columns": ["x"], "rows": [[1]]}, {"columns": ["roles"], "rows": []}, [1, 2]],
)
def test_unreadable_mcp_result_fails(content: Any) -> None:
    check = _check(_report(_fixed(content)))
    assert check.status == "fail" and "could not read" in check.detail


def test_mcp_tool_error_and_exceptions_fail_without_crashing() -> None:
    assert "MCP query failed" in _check(_report(_fixed("Code: 497", is_error=True))).detail

    def boom(sql: str) -> tuple[bool, str]:
        raise ConnectionError("unreachable")

    check = _check(_report(boom))
    assert check.status == "fail" and "ConnectionError" in check.detail


@pytest.mark.safety
def test_mcp_check_detail_never_holds_secrets() -> None:
    env = {"CLICKHOUSE_MCP_KEY_SECRET": KEY_SECRET}
    leak = _fixed(f"denied for key {KEY_SECRET}", is_error=True)
    report = _report(leak, env)
    assert KEY_SECRET not in report.render() and KEY_SECRET not in report.to_json()

    def boom(sql: str) -> tuple[bool, str]:
        raise RuntimeError(f"auth {KEY_SECRET}")

    assert KEY_SECRET not in _report(boom, env).render()


def test_mcp_check_is_absent_unless_asked_and_skipped_without_env() -> None:
    assert all(c.name != MCP_CHECK for c in run_doctor({}).checks)
    check = _check(run_doctor({}, check_mcp=True))
    assert check.status == "skip"
    assert mcp_query_from_env({}) is None
    full = {
        "CLICKHOUSE_MCP_KEY_ID": "id",
        "CLICKHOUSE_MCP_KEY_SECRET": "secret-value",
        "CLICKHOUSE_SERVICE_ID": SERVICE,
    }
    assert callable(mcp_query_from_env(full))  # builds a client lazily; nothing connects


def test_mcp_checks_sends_exactly_the_role_query() -> None:
    sent: list[str] = []

    def q(sql: str) -> tuple[bool, str]:
        sent.append(sql)
        return False, json.dumps(_rows(["analytics_ro"]))

    from agentic_analytics.warehouse.doctor import _Scrubber

    assert mcp_checks(q, scrub=_Scrubber({}))[0].status == "pass"
    assert sent == ["SELECT currentRoles() AS roles, getSetting('readonly') AS readonly"]


def test_cli_doctor_reports_mcp_skip_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    mcp = [c for c in report["checks"] if c["name"] == MCP_CHECK]
    assert mcp and mcp[0]["status"] == "skip"
