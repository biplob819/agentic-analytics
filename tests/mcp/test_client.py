import asyncio
import base64
import json
from typing import ClassVar

import pytest

from agentic_analytics.mcp import ALLOWED_TOOLS, McpConnection, McpSettings, Toolbox, basic_auth
from agentic_analytics.mcp.client import McpConfigError, model_schema, shape_rows
from tests.mcp import fake_server
from tests.models.fakes import no_sleep

DBS = ("uk", "uk_probes")


def run(coro):
    return asyncio.run(coro)


async def with_toolbox(fn, **kw):
    async with McpConnection(server=fake_server.make_server()) as conn:
        box = Toolbox(conn, "svc-123", DBS, retry={"sleep": no_sleep}, **kw)
        return await fn(box)


def test_basic_auth_header():
    h = basic_auth("kid", "secret")
    assert h.startswith("Basic ")
    assert base64.b64decode(h[6:]).decode() == "kid:secret"


def test_settings_from_env_name_missing_variables():
    with pytest.raises(McpConfigError, match="CLICKHOUSE_MCP_KEY_SECRET"):
        McpSettings.from_env({"CLICKHOUSE_MCP_KEY_ID": "k", "CLICKHOUSE_SERVICE_ID": "s"})
    s = McpSettings.from_env(
        {
            "CLICKHOUSE_MCP_KEY_ID": " k ",
            "CLICKHOUSE_MCP_KEY_SECRET": "x",
            "CLICKHOUSE_SERVICE_ID": "s",
        }
    )
    assert (s.key_id, s.url) == ("k", "https://mcp.clickhouse.cloud/mcp")
    assert "x" not in repr(s)  # the secret never prints


def test_model_sees_only_allow_listed_tools_without_service_id():
    async def go(box):
        return box.specs

    specs = run(with_toolbox(go))
    assert tuple(s.name for s in specs) == ALLOWED_TOOLS
    for s in specs:
        assert "serviceId" not in s.parameters["properties"]
        assert "serviceId" not in s.parameters.get("required", [])
    tables = next(s for s in specs if s.name == "list_tables")
    assert tables.parameters["properties"]["database"]["enum"] == list(DBS)


def test_service_id_is_injected_and_model_value_ignored():
    fake_server.CALLS.clear()

    async def go(box):
        return await box.call("list_tables", {"database": "uk", "serviceId": "someone-else"})

    out = run(with_toolbox(go))
    assert not out.is_error
    assert fake_server.CALLS[-1] == ("list_tables", {"serviceId": "svc-123", "database": "uk"})


def test_control_plane_tools_never_reach_the_server():
    fake_server.CALLS.clear()

    async def go(box):
        return await box.call("get_organization_cost", {"organizationId": "o"})

    out = run(with_toolbox(go))
    assert out.is_error and out.refused == "not_allowed"
    assert all(name != "get_organization_cost" for name, _ in fake_server.CALLS)


def test_other_databases_are_refused():
    async def go(box):
        return await box.call("list_tables", {"database": "system"})

    out = run(with_toolbox(go))
    assert out.refused == "bad_args"


def test_rows_are_capped_with_total_reported():
    async def go(box):
        return await box.call("run_select_query", {"query": "SELECT town FROM t LIMIT 120"})

    out = run(with_toolbox(go))
    body = json.loads(out.content)
    assert (out.rows_returned, out.rows_total, out.truncated) == (50, 120, True)
    assert (
        len(body["rows"]) == 50 and body["rows_total"] == 120 and body["columns"] == ["town", "n"]
    )


def test_small_results_are_not_truncated():
    async def go(box):
        return await box.call("run_select_query", {"query": "SELECT 1 LIMIT 3"})

    out = run(with_toolbox(go))
    assert (out.rows_returned, out.rows_total, out.truncated) == (3, 3, False)


def test_seventh_call_is_refused_without_reaching_the_server():
    fake_server.CALLS.clear()

    async def go(box):
        outs = [await box.call("list_databases", {}) for _ in range(7)]
        return outs

    outs = run(with_toolbox(go))
    assert all(not o.is_error for o in outs[:6])
    assert outs[6].refused == "budget" and "final_answer" in outs[6].content
    assert len(fake_server.CALLS) == 6


def test_refused_attempts_count_toward_the_budget():
    async def go(box):
        for _ in range(6):
            await box.call("get_organization_cost", {})
        return await box.call("list_databases", {})

    assert run(with_toolbox(go)).refused == "budget"


def test_server_errors_become_tool_errors():
    async def go(box):
        return await box.call("run_select_query", {"query": "SELECT boom"})

    out = run(with_toolbox(go))
    assert out.is_error and "boom" in out.content and out.refused is None


@pytest.mark.parametrize("args", [None, {"query": 5}, {}])
def test_bad_query_arguments(args):
    async def go(box):
        return await box.call("run_select_query", args)

    assert run(with_toolbox(go)).refused == "bad_args"


def test_missing_server_tool_is_a_config_error():
    class Conn:
        server_tools: ClassVar[dict] = {"list_databases": {"description": "", "input_schema": {}}}

    with pytest.raises(McpConfigError, match="run_select_query"):
        Toolbox(Conn(), "s", DBS)  # type: ignore[arg-type]


def test_transport_failure_after_retries_is_a_tool_error():
    class Flaky:
        server_tools: ClassVar[dict] = {
            t: {"description": "", "input_schema": {}} for t in ALLOWED_TOOLS
        }
        calls = 0

        async def call_tool(self, name, args, **kw):
            Flaky.calls += 1
            raise ConnectionError("reset")

    box = Toolbox(Flaky(), "s", DBS, retry={"sleep": no_sleep})  # type: ignore[arg-type]
    out = run(box.call("list_databases", {}))
    assert out.is_error and "ConnectionError" in out.content
    assert Flaky.calls == 6


@pytest.mark.parametrize(
    "payload, cols, n",
    [
        ({"columns": ["a"], "rows": [[1], [2]]}, ["a"], 2),
        ({"meta": [{"name": "a"}], "data": [[1]]}, ["a"], 1),
        ({"meta": [{"name": "a"}], "data": [{"a": 1}]}, ["a"], 1),
        ([{"a": 1, "b": 2}], ["a", "b"], 1),
        ('{"data": [{"a": 1}]}', ["a"], 1),
        ([], [], 0),
    ],
)
def test_shape_rows_handles_known_shapes(payload, cols, n):
    c, rows = shape_rows(payload)
    assert c == cols and rows is not None and len(rows) == n


def test_shape_rows_unwraps_a_result_string():
    c, rows = shape_rows({"result": '{"columns": ["a"], "rows": [[1]]}'})
    assert c == ["a"] and rows == [[1]]


def test_shape_rows_unknown_shapes():
    assert shape_rows("not json") == (None, None)
    assert shape_rows({"x": 1}) == (None, None)


def test_model_schema_handles_missing_parts():
    assert model_schema({}, DBS) == {"type": "object", "properties": {}, "required": []}
    s = model_schema(
        {
            "type": "object",
            "properties": {"serviceId": {}, "query": {}},
            "required": ["query", "serviceId"],
        },
        DBS,
    )
    assert s["required"] == ["query"] and "serviceId" not in s["properties"]
