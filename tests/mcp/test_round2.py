"""Verifier round 2 for N1.5: retries, result shapes and caps, paging, lifecycle, secrets.

Each test isolates one guarantee (L-2) with exact values where counts matter (L-3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import types
from typing import Any

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from mcp_types import (
    CONNECTION_CLOSED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    REQUEST_TIMEOUT,
)

import mcp
from agentic_analytics.mcp import McpConnection, McpSettings, Toolbox, basic_auth, shape_rows
from agentic_analytics.mcp.client import (
    MAX_SSE_EVENT_BYTES,
    MAX_TEXT_CHARS,
    MAX_TOOL_PAGES,
    McpConfigError,
    mcp_error_kind,
)
from tests.mcp import fake_server
from tests.models.fakes import no_sleep

DBS = ("uk", "uk_probes")
SECRET = "s3cr3t-key-value-XYZ"
SETTINGS = McpSettings("kid-1", SECRET, "svc-123", url="http://localhost:8000/mcp")
HEADER = basic_auth("kid-1", SECRET)

SCHEMAS: dict[str, dict[str, Any]] = {
    "list_databases": {
        "type": "object",
        "properties": {"serviceId": {"type": "string"}},
        "required": ["serviceId"],
    },
    "list_tables": {
        "type": "object",
        "properties": {
            "serviceId": {"type": "string"},
            "database": {"type": "string"},
            "like": {"type": "string"},
        },
        "required": ["serviceId", "database"],
    },
    "run_select_query": {
        "type": "object",
        "properties": {"serviceId": {"type": "string"}, "query": {"type": "string"}},
        "required": ["serviceId", "query"],
    },
}


def run(coro):
    return asyncio.run(coro)


def result(text: str | None = None, structured: Any = None, is_error: bool = False):
    content: list[Any] = [TextContent(type="text", text=text)] if text is not None else []
    return CallToolResult(content=content, structured_content=structured, is_error=is_error)


class ScriptedConn:
    """A connection whose call_tool plays a script; the last item repeats."""

    def __init__(self, *script: Any, settings: McpSettings | None = None, on_call=None):
        self.server_tools = {n: {"description": "", "input_schema": s} for n, s in SCHEMAS.items()}
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[float | None] = []
        self.settings = settings
        self.needs_reopen = False
        self.on_call = on_call
        self.in_flight = self.max_in_flight = 0

    async def call_tool(self, name, args, *, timeout_s=None):
        self.calls.append((name, dict(args)))
        self.timeouts.append(timeout_s)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # let other tasks run, as a real round trip would
            if self.on_call is not None:
                self.on_call()
            item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.in_flight -= 1


def box_for(conn: Any, **kw) -> Toolbox:
    return Toolbox(conn, "svc-123", DBS, retry={"sleep": no_sleep}, **kw)


def query(conn: Any, sql: str = "SELECT 1", **kw):
    return run(box_for(conn, **kw).call("run_select_query", {"query": sql}))


# --- 1. retries ---


@pytest.mark.parametrize(
    "code, kind",
    [
        (CONNECTION_CLOSED, "MCP connection closed (code -32000)"),
        (INTERNAL_ERROR, "MCP server error (HTTP 5xx, rate limit or internal error) (code -32603)"),
    ],
)
def test_transient_mcp_errors_are_retried_six_times(code, kind):
    conn = ScriptedConn(MCPError(code, "Server returned an error response"))
    out = query(conn)
    assert len(conn.calls) == 6
    assert out.is_error and out.refused is None
    assert out.content == f"{kind}: the call failed after 6 attempts."


def test_transient_errors_then_success():
    ok = result(json.dumps({"columns": ["a"], "rows": [[1]]}))
    conn = ScriptedConn(MCPError(CONNECTION_CLOSED, "c"), MCPError(INTERNAL_ERROR, "e"), ok)
    out = query(conn)
    assert len(conn.calls) == 3 and not out.is_error and out.rows_total == 1
    assert conn.needs_reopen is False  # the session recovered; nothing to reopen


TIMEOUT_MESSAGE = (
    "MCP server timeout (code -32001): the call failed after 1 attempt. The query timed out; "
    "simplify it (tighter filters, fewer columns, an aggregate or a smaller LIMIT) and try again."
)


def test_timeouts_are_not_retried_and_tell_the_model_to_simplify():
    conn = ScriptedConn(MCPError(REQUEST_TIMEOUT, "Request 'tools/call' timed out"))
    out = query(conn)
    assert len(conn.calls) == 1 and out.content == TIMEOUT_MESSAGE


def test_python_timeouts_are_not_retried_either():
    conn = ScriptedConn(TimeoutError())
    out = query(conn)
    assert len(conn.calls) == 1
    assert out.content == TIMEOUT_MESSAGE.replace(" (code -32001)", "")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_retries_stop_at_the_wall_clock_budget():
    clock = FakeClock()

    def slow():  # every attempt takes 20 s of wall clock
        clock.now += 20

    conn = ScriptedConn(MCPError(INTERNAL_ERROR, "busy"), on_call=slow)
    out = query(conn, clock=clock, call_budget_s=45.0)
    assert len(conn.calls) == 3  # starts at 0 s, 20 s and 40 s; none at 60 s
    assert conn.timeouts == [45.0, 25.0, 5.0]  # each read timeout capped by the time left
    assert out.content == (
        "MCP server error (HTTP 5xx, rate limit or internal error) (code -32603): the call "
        "failed after 3 attempts. The 45 s retry time limit was reached."
    )


def test_backoff_sleeps_are_capped_by_the_time_left():
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(d):
        slept.append(d)
        clock.now += d

    def slow():
        clock.now += 40

    for seed in range(20):  # backoff jitter is random: seed it and check every outcome (L-4)
        clock.now, slept[:] = 0.0, []
        conn = ScriptedConn(MCPError(INTERNAL_ERROR, "busy"), on_call=slow)
        box = Toolbox(
            conn,  # type: ignore[arg-type]
            "svc-123",
            DBS,
            clock=clock,
            retry={"sleep": sleep, "base": 100.0, "rng": random.Random(seed)},
        )
        out = run(box.call("list_databases", {}))
        assert out.is_error and slept and all(d <= 5.0 for d in slept)
        assert len(conn.calls) <= 2  # a second attempt only if the jittered sleep left time
        assert clock.now <= 45.0 + 40.0  # no attempt starts after the 45 s call budget


def test_default_budgets():
    from agentic_analytics.mcp.client import CALL_BUDGET_S, DEFAULT_READ_TIMEOUT_S

    assert (CALL_BUDGET_S, DEFAULT_READ_TIMEOUT_S) == (45.0, 30.0)
    assert McpConnection(server=object())._timeout == 30.0
    assert box_for(ScriptedConn(result("[]"))).call_budget_s == 45.0


@pytest.mark.parametrize(
    "code, kind",
    [
        (INVALID_PARAMS, "MCP protocol error (invalid params) (code -32602)"),
        (METHOD_NOT_FOUND, "MCP protocol error (method not found) (code -32601)"),
        (INVALID_REQUEST, "MCP protocol error (invalid request) (code -32600)"),
        (PARSE_ERROR, "MCP protocol error (parse error) (code -32700)"),
        (-32099, "MCP protocol error (code -32099)"),
    ],
)
def test_protocol_errors_are_not_retried(code, kind):
    conn = ScriptedConn(MCPError(code, "bad"))
    out = query(conn)
    assert len(conn.calls) == 1
    assert out.content == f"{kind}: the call failed after 1 attempt."


def test_mcp_error_classification():
    assert mcp_error_kind(MCPError(REQUEST_TIMEOUT, "x"))[1] is False
    assert mcp_error_kind(MCPError(CONNECTION_CLOSED, "x"))[1] is True
    assert mcp_error_kind(MCPError(INTERNAL_ERROR, "x"))[1] is True
    for code in (INVALID_PARAMS, METHOD_NOT_FOUND, INVALID_REQUEST, PARSE_ERROR, -1):
        assert mcp_error_kind(MCPError(code, "x"))[1] is False


def test_read_timeout_through_the_real_client_is_not_retried():
    fake_server.CALLS.clear()

    async def go():
        async with McpConnection(server=fake_server.make_server(), read_timeout_s=0.05) as conn:
            return await box_for(conn).call("run_select_query", {"query": "SELECT SLEEP"})

    out = run(go())
    assert out.content == TIMEOUT_MESSAGE
    assert sum(1 for name, _ in fake_server.CALLS if name == "run_select_query") == 1


class Gate(httpx2.AsyncBaseTransport):
    """Answers the first `failures` tools/call POSTs with `status`; the rest go to the app."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, status: int = 503, failures: int = 0):
        self.inner, self.status, self.failures = inner, status, failures
        self.tool_posts = 0
        self.auth: list[str | None] = []

    async def handle_async_request(self, request):
        self.auth.append(request.headers.get("authorization"))
        body = await request.aread()
        if b'"tools/call"' in body:
            self.tool_posts += 1
            if self.failures > 0:
                self.failures -= 1
                return httpx2.Response(self.status, text="busy")
        return await self.inner.handle_async_request(request)


@contextlib.asynccontextmanager
async def url_connection(monkeypatch, gate_kw: dict | None = None, **conn_kw):
    """A real Streamable HTTP connection (URL mode) to the fake server, served in-process."""
    server = fake_server.make_server()
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    made: list[httpx2.AsyncClient] = []
    gates: list[Gate] = []

    def factory(headers=None, timeout=None, auth=None):
        gate = Gate(httpx2.ASGITransport(app=app), **(gate_kw or {}))
        gates.append(gate)
        client = httpx2.AsyncClient(transport=gate, headers=headers)
        made.append(client)
        return client

    monkeypatch.setattr("mcp.shared._httpx_utils.create_mcp_http_client", factory)
    async with server.session_manager.run():
        yield McpConnection(SETTINGS, **conn_kw), made, gates


@pytest.mark.parametrize("status", [503, 429])
def test_http_5xx_and_429_through_the_real_client_are_retried(monkeypatch, status):
    async def go():
        async with url_connection(monkeypatch, {"status": status, "failures": 99}) as (c, _, g):
            async with c as conn:
                out = await box_for(conn).call("list_databases", {})
            return out, g[0]

    out, gate = run(go())
    assert gate.tool_posts == 6
    assert out.content == (
        "MCP server error (HTTP 5xx, rate limit or internal error) (code -32603): "
        "the call failed after 6 attempts."
    )


def test_http_503_twice_then_success_through_the_real_client(monkeypatch):
    async def go():
        async with url_connection(monkeypatch, {"failures": 2}) as (c, _, g):
            async with c as conn:
                out = await box_for(conn).call("list_databases", {})
            return out, g[0]

    out, gate = run(go())
    assert gate.tool_posts == 3 and not out.is_error and "uk_probes" in out.content


# --- 2. shape_rows never raises ---


@pytest.mark.parametrize(
    "payload",
    [
        {"columns": [1, 2], "rows": [[1, 2]]},
        {"columns": [{"x": 1}], "rows": [[1]]},
        {"columns": [None], "rows": [[1]]},
        {"meta": [1], "data": [[1]]},
        {"meta": [{"name": 5}], "data": [[1]]},
        {"columns": ["a"], "rows": [1, 2]},
        {"meta": [{"name": "a"}], "data": [3]},
        {"meta": [{"name": "a"}], "data": ["ab"]},
        {"columns": ["a", "b"], "rows": [[1]]},
        {"data": {"data": [1]}},
        {"result": 5},
        [1, "a", None],
        [[1], {"a": 1}],
        "[" * 100_000 + "]" * 100_000,
        '{"data":' * 50_000 + "[]" + "}" * 50_000,
        json.dumps({"result": json.dumps({"result": json.dumps({"result": "[]"})})}) * 1,
        3.5,
        None,
        True,
    ],
)
def test_shape_rows_unknown_or_malformed_shapes_never_raise(payload):
    cols, rows = shape_rows(payload)
    assert rows is None or isinstance(rows, list)
    assert cols is None or all(isinstance(c, str) for c in cols)


def test_shape_rows_rejects_specific_malformed_shapes():
    assert shape_rows({"columns": [1, 2], "rows": [[1, 2]]}) == (None, None)
    assert shape_rows({"meta": [1], "data": [[1]]}) == (None, None)
    assert shape_rows({"columns": ["a"], "rows": [1, 2]}) == (None, None)
    assert shape_rows("[" * 100_000 + "]" * 100_000) == (None, None)


def test_shape_rows_deeply_nested_python_value_never_raises():
    deep: Any = []
    for _ in range(100_000):
        deep = {"data": [deep]}
    cols, _ = shape_rows(deep)  # one row whose single cell is the nested value
    assert cols == ["data"]
    assert shape_rows({"result": deep})[0] == ["data"]  # D-64: a dict under "result" unwraps
    res = types.SimpleNamespace(content=[], structured_content=deep, is_error=False)
    out = query(ScriptedConn(res))  # json cannot write the cell: raw (empty) text, no exception
    assert (out.is_error, out.content, out.rows_returned) == (False, "", None)


def test_shape_rows_unwrap_depth_is_bounded():
    payload: Any = [{"a": 1}]
    for _ in range(10):
        payload = {"result": json.dumps(payload)}
    assert shape_rows(payload) == (None, None)


def test_huge_result_is_shaped_and_capped():
    rows = [[i, "x" * 10] for i in range(200_000)]
    out = query(ScriptedConn(result(json.dumps({"columns": ["i", "s"], "rows": rows}))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (50, 200_000, True)


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(10**12), 10**12)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda inner: (
        st.lists(inner, max_size=5) | st.dictionaries(st.text(max_size=8), inner, max_size=5)
    ),
    max_leaves=30,
)
shaped_like = st.dictionaries(
    st.sampled_from(
        ["columns", "rows", "meta", "data", "result", "rows_before_limit_at_least", "truncated"]
    ),
    json_values,
    max_size=5,
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(value=st.one_of(json_values, shaped_like, st.lists(shaped_like, max_size=4)))
def test_toolbox_call_never_raises_for_any_json(value):
    structured = value if isinstance(value, dict) else None
    for res in (result(json.dumps(value), structured), result(json.dumps(value))):
        out = query(ScriptedConn(res))
        assert isinstance(out.content, str) and not out.is_error
        assert len(out.content) <= MAX_TEXT_CHARS + 60
        if out.rows_returned is not None:
            assert out.rows_returned <= 50 and out.rows_total is not None
    shape_rows(value)
    shape_rows(json.dumps(value))


# --- 3. character cap on query results ---


def test_a_100kb_cell_is_cut_and_the_result_stays_under_the_cap():
    big = "a" * 100_000
    payload = {"columns": ["k", "v"], "rows": [[1, big], [2, "small"], [3, "s"]]}
    out = query(ScriptedConn(result(json.dumps(payload))))
    body = json.loads(out.content)
    assert len(out.content) <= MAX_TEXT_CHARS
    assert (out.rows_returned, out.rows_total, out.truncated) == (3, 3, True)
    assert body["rows"][0][1] == "a" * 500 + "...[99500 more characters cut]"
    assert body["rows"][1:] == [[2, "small"], [3, "s"]] and body["truncated"] is True


def test_the_cell_limit_halves_until_every_row_fits():
    rows = [[i, "b" * 499] for i in range(50)]  # under 500 each, 25 KB together
    out = query(ScriptedConn(result(json.dumps({"columns": ["i", "s"], "rows": rows}))))
    body = json.loads(out.content)
    assert (out.rows_returned, out.rows_total, out.truncated) == (50, 50, True)
    # 500 and 250 do not fit 50 rows; 125 does
    assert body["rows"][7] == [7, "b" * 125 + "...[374 more characters cut]"]
    assert len(out.content) <= MAX_TEXT_CHARS


def test_trailing_rows_are_dropped_when_cells_cannot_shrink():
    rows = [[123456789] * 30 for _ in range(50)]  # numbers cannot be cut: ~300 chars a row
    cols = [f"c{i}" for i in range(30)]
    out = query(ScriptedConn(result(json.dumps({"columns": cols, "rows": rows}))))
    body = json.loads(out.content)
    n = out.rows_returned
    assert n is not None and 0 < n < 50
    assert (out.rows_total, out.truncated) == (50, True)
    assert body["rows"] == rows[:n] and len(out.content) <= MAX_TEXT_CHARS
    longer = {"columns": cols, "rows": rows[: n + 1], "rows_total": 50, "truncated": True}
    assert len(json.dumps(longer)) > MAX_TEXT_CHARS  # the most rows that fit are shown


def test_column_names_over_the_cap_still_give_valid_json():
    cols = [f"{i:03d}" + "n" * 300 for i in range(100)]  # 30 KB of names (each cut to 500)
    rows = [list(range(100)) for _ in range(5)]
    out = query(ScriptedConn(result(json.dumps({"columns": cols, "rows": rows}))))
    body = json.loads(out.content)
    assert len(out.content) <= MAX_TEXT_CHARS
    assert (out.rows_returned, out.rows_total, out.truncated) == (0, 5, True)
    kept = body["columns"][:-1]
    assert kept == cols[: len(kept)] and 0 < len(kept) < 100
    assert body["columns"][-1] == f"...[{100 - len(kept)} more columns cut]"
    assert body["rows"] == []


lengths = st.integers(0, 3000)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    col_lens=st.lists(lengths, min_size=1, max_size=40),
    n_rows=st.integers(0, 80),
    cell_len=st.integers(0, 1200),
    nested=st.booleans(),
)
def test_query_output_is_always_valid_json_under_the_cap(col_lens, n_rows, cell_len, nested):
    cols = [f"{i}:" + "c" * n for i, n in enumerate(col_lens)]
    cell: Any = ["v" * cell_len] if nested else "v" * cell_len
    rows = [[i, *([cell] * (len(cols) - 1))] for i in range(n_rows)]
    out = query(ScriptedConn(result(json.dumps({"columns": cols, "rows": rows}))))
    body = json.loads(out.content)  # always valid JSON
    assert len(out.content) <= MAX_TEXT_CHARS
    assert out.rows_total == n_rows == body["rows_total"]
    assert out.rows_returned == len(body["rows"]) <= min(n_rows, 50)
    assert body["truncated"] is out.truncated
    if out.rows_returned < n_rows:
        assert out.truncated


def test_results_under_the_cap_are_not_marked_truncated():
    rows = [[i, "c" * 100] for i in range(50)]
    out = query(ScriptedConn(result(json.dumps({"columns": ["i", "s"], "rows": rows}))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (50, 50, False)


# --- 4. list_tools pagination ---


def paged_server(pages: int | None):
    """Tools split over two pages, or (pages=None) a server whose cursor never ends."""
    seen: list[str | None] = []
    names = [*SCHEMAS, "get_organization_cost"]

    async def on_list_tools(ctx, params):
        cursor = params.cursor if params else None
        seen.append(cursor)
        if pages is None:
            return ListToolsResult(tools=[], next_cursor=f"c{len(seen)}")
        if pages == 0:  # a server that hands back the same cursor forever
            return ListToolsResult(tools=[], next_cursor="same")
        half = names[:2] if cursor is None else names[2:]
        tools = [Tool(name=n, input_schema=SCHEMAS.get(n, {"type": "object"})) for n in half]
        return ListToolsResult(tools=tools, next_cursor="page-2" if cursor is None else None)

    async def on_call_tool(ctx, params):
        return CallToolResult(content=[TextContent(type="text", text="[]")])

    return Server("paged", on_list_tools=on_list_tools, on_call_tool=on_call_tool), seen


def test_tool_listing_follows_next_cursor():
    server, seen = paged_server(2)

    async def go():
        async with McpConnection(server=server) as conn:
            box = box_for(conn)
            return set(conn.server_tools), box.specs

    tools, specs = run(go())
    assert seen == [None, "page-2"]
    assert tools == {*SCHEMAS, "get_organization_cost"} and len(specs) == 3


def test_tool_listing_stops_after_the_page_cap():
    server, seen = paged_server(None)

    async def go():
        async with McpConnection(server=server) as conn:
            return conn.server_tools

    assert run(go()) == {}
    assert len(seen) == MAX_TOOL_PAGES == 20


def test_tool_listing_stops_at_a_repeated_cursor():
    server, seen = paged_server(0)

    async def go():
        async with McpConnection(server=server) as conn:
            return conn.server_tools

    assert run(go()) == {}
    assert seen == [None, "same"]  # the cursor came back once: stop, not 20 pages


# --- 5. clipping, structured content, error slices, secrets ---


def test_non_query_text_over_12000_chars_is_clipped_with_a_note():
    text = "x" * 12_001
    out = run(box_for(ScriptedConn(result(text))).call("list_databases", {}))
    assert out.content == "x" * 12_000 + "\n... [1 more characters cut]"


def test_non_query_text_of_exactly_12000_chars_is_unchanged():
    text = "y" * 12_000
    out = run(box_for(ScriptedConn(result(text))).call("list_databases", {}))
    assert out.content == text


def test_structured_content_is_preferred_over_text():
    res = result(
        json.dumps({"columns": ["b"], "rows": [[2], [3]]}),
        {"columns": ["a"], "rows": [[1]]},
    )
    out = query(ScriptedConn(res))
    assert json.loads(out.content)["columns"] == ["a"] and out.rows_total == 1


def test_text_is_used_when_structured_content_has_no_known_shape():
    res = result(json.dumps({"columns": ["b"], "rows": [[2], [3]]}), {"x": 1})
    out = query(ScriptedConn(res))
    assert json.loads(out.content)["columns"] == ["b"] and out.rows_total == 2


def test_tool_error_text_is_sliced_to_12000_chars():
    text = "E" * 12_000 + "TAIL"
    out = query(ScriptedConn(result(text, is_error=True)))
    assert out.is_error and out.content == "E" * 12_000


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(f"401 for Authorization: {HEADER} secret={SECRET}"),
        ConnectionError(f"reset while sending {HEADER}"),
        MCPError(INTERNAL_ERROR, f"bad auth {HEADER} {SECRET}"),
        MCPError(INVALID_PARAMS, f"key {SECRET}", data={"auth": HEADER}),
        result(f"Code: 516. Authentication failed for {HEADER} / {SECRET}", is_error=True),
        result(f"echo {SECRET}"),
    ],
)
def test_secrets_never_reach_the_model_or_the_log(failure, caplog):
    caplog.set_level(logging.DEBUG)
    conn = ScriptedConn(failure, settings=SETTINGS)
    out = run(box_for(conn).call("list_databases", {}))
    token = HEADER.removeprefix("Basic ")
    for leak in (SECRET, token):
        assert leak not in out.content
        assert leak not in repr(out)
        assert leak not in caplog.text
        assert leak not in repr(SETTINGS)


ODD_SECRET = 'se"cr\\et-Q9'  # a quote and a backslash, so JSON escapes it
ODD = McpSettings("kid-2", ODD_SECRET, "svc-123", url="http://localhost:8000/mcp")


def odd_leaks(text: str) -> list[str]:
    header = basic_auth("kid-2", ODD_SECRET)
    forms = {ODD_SECRET, json.dumps(ODD_SECRET)[1:-1], header.removeprefix("Basic ")}
    return [f for f in forms if f in text]


def test_secrets_with_json_escapes_are_redacted_in_query_results():
    payload = {"columns": ["a"], "rows": [[f"value {ODD_SECRET}"]]}
    for res in (result(json.dumps(payload)), result("x", payload)):
        out = query(ScriptedConn(res, settings=ODD))
        assert not out.is_error and out.rows_returned == 1
        assert odd_leaks(out.content) == []
        assert json.loads(out.content)["rows"] == [["value [redacted]"]]


def test_secrets_with_json_escapes_are_redacted_in_tool_errors():
    text = json.dumps({"error": f"auth failed for {ODD_SECRET}"})
    out = query(ScriptedConn(result(text, is_error=True), settings=ODD))
    assert out.is_error and odd_leaks(out.content) == []


def test_cell_cutting_cannot_leave_a_secret_prefix():
    cell = "x" * 495 + SECRET + "y" * 100_000  # a cut at 500 would keep SECRET[:5]
    payload = {"columns": ["a"], "rows": [[cell]]}
    out = query(ScriptedConn(result(json.dumps(payload)), settings=SETTINGS))
    assert json.loads(out.content)["rows"][0][0].startswith("x" * 495 + "[reda")
    assert SECRET[:5] not in out.content


def test_structured_cell_cutting_cannot_leave_a_secret_prefix():
    structured = {"columns": ["a"], "rows": [["x" * 495 + SECRET + "y" * 100_000]]}
    out = query(ScriptedConn(result("plain text", structured), settings=SETTINGS))
    assert json.loads(out.content)["rows"][0][0].startswith("x" * 495 + "[reda")
    assert SECRET[:5] not in out.content


def test_structured_content_is_redacted():
    structured = {"columns": ["a"], "rows": [[SECRET], [HEADER]]}
    out = query(ScriptedConn(result("no secret in the text", structured), settings=SETTINGS))
    assert json.loads(out.content)["rows"] == [["[redacted]"], ["[redacted]"]]
    assert SECRET not in out.content and HEADER.removeprefix("Basic ") not in out.content


# --- 6. row fidelity ---


def test_ragged_rows_are_an_unknown_shape():
    assert shape_rows({"columns": ["a", "b"], "rows": [[1, 2], [3]]}) == (None, None)
    assert shape_rows({"columns": ["a", "b"], "rows": [[1, 2], [3, 4, 5]]}) == (None, None)
    assert shape_rows({"meta": [{"name": "a"}], "data": [[1], [2, 3]]}) == (None, None)


def test_object_rows_inside_columns_rows_map_by_name():
    assert shape_rows({"columns": ["a", "b"], "rows": [{"b": 2, "a": 1}]}) == (
        ["a", "b"],
        [[1, 2]],
    )


def test_string_rows_are_not_split_into_characters():
    assert shape_rows({"columns": ["a"], "rows": ["xyz"]}) == (None, None)
    assert shape_rows({"columns": ["a", "b", "c"], "rows": ["xyz"]}) == (None, None)


def test_list_of_objects_uses_the_union_of_keys_in_first_seen_order():
    assert shape_rows([{"a": 1}, {"b": 2, "a": 3}, {"c": 4}]) == (
        ["a", "b", "c"],
        [[1, None, None], [3, 2, None], [None, None, 4]],
    )


def test_rows_before_limit_is_not_a_server_cut():
    # A complete top-10: ClickHouse counts the rows before the query's own LIMIT.
    payload = {
        "meta": [{"name": "town", "type": "String"}, {"name": "n", "type": "UInt64"}],
        "data": [{"town": f"T{i}", "n": 100 - i} for i in range(10)],
        "rows": 10,
        "rows_before_limit_at_least": 5000,
        "statistics": {"elapsed": 0.01, "rows_read": 5000, "bytes_read": 100},
    }
    out = query(ScriptedConn(result(json.dumps(payload))), "SELECT ... ORDER BY n DESC LIMIT 10")
    body = json.loads(out.content)
    assert (out.rows_returned, out.rows_total, out.truncated) == (10, 10, False)
    assert body["rows_total"] == 10 and body["truncated"] is False
    assert "5000" not in out.content


def test_rows_before_limit_on_a_wrapper_is_ignored_too():
    payload = {"data": [{"a": i} for i in range(3)], "rows_before_limit_at_least": 500}
    out = query(ScriptedConn(result(json.dumps(payload))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (3, 3, False)


def test_clickhouse_rows_equal_to_data_is_not_a_cut():
    payload = {"meta": [{"name": "n"}], "data": [[1], [2]], "rows": 2, "statistics": {}}
    out = query(ScriptedConn(result(json.dumps(payload))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (2, 2, False)


def test_server_truncated_flag_is_reported():
    payload = {"data": [{"a": 1}], "truncated": True}
    out = query(ScriptedConn(result(json.dumps(payload))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (1, 1, True)


def test_server_total_on_a_wrapper_is_reported():
    payload = {"data": [{"a": i} for i in range(60)], "rows": 500}
    out = query(ScriptedConn(result(json.dumps(payload))))
    assert (out.rows_returned, out.rows_total, out.truncated) == (50, 500, True)


# --- 7. lifecycle ---


def test_url_mode_sends_basic_auth_and_closes_its_http_client(monkeypatch):
    async def go():
        async with url_connection(monkeypatch) as (c, made, gates):
            async with c as conn:
                out = await box_for(conn).call("list_databases", {})
                open_while_in_use = not made[0].is_closed
            return out, made, gates, open_while_in_use

    out, made, gates, open_while_in_use = run(go())
    assert not out.is_error and open_while_in_use
    assert len(made) == 1 and made[0].is_closed
    assert gates[0].auth and set(gates[0].auth) == {HEADER}


def test_list_tools_failure_closes_everything_and_reraises(monkeypatch):
    async def broken(self, **kw):
        raise MCPError(INTERNAL_ERROR, "listing failed")

    monkeypatch.setattr(mcp.Client, "list_tools", broken)

    async def go():
        async with url_connection(monkeypatch) as (c, made, _):
            with pytest.raises(MCPError, match="listing failed"):
                await c.__aenter__()
            return c, made

    conn, made = run(go())
    assert made[0].is_closed and conn._client is None and conn._stack is None


def test_max_sse_event_size_is_passed_explicitly(monkeypatch):
    import mcp.client.streamable_http as sh

    seen: dict[str, Any] = {}
    real = sh.streamable_http_client

    def spy(url, **kw):
        seen.update(kw)
        return real(url, **kw)

    monkeypatch.setattr(sh, "streamable_http_client", spy)

    async def go():
        async with url_connection(monkeypatch) as (c, _, _g), c:
            pass

    run(go())
    assert MAX_SSE_EVENT_BYTES == 8 * 1024 * 1024
    assert seen["max_sse_event_size"] == MAX_SSE_EVENT_BYTES


class ClosingConnection(McpConnection):
    """A real in-process connection whose first `closed` calls fail as a dropped stream."""

    def __init__(self, closed: int, **kw):
        super().__init__(server=fake_server.make_server(), **kw)
        self.closed, self.attempts, self.clients = closed, 0, []

    async def call_tool(self, name, arguments, *, timeout_s=None):
        self.attempts += 1
        self.clients.append(self._client)
        if self.closed > 0:
            self.closed -= 1
            raise MCPError(CONNECTION_CLOSED, "Connection closed")
        return await super().call_tool(name, arguments, timeout_s=timeout_s)


CLOSED_6 = "MCP connection closed (code -32000): the call failed after 6 attempts."


def test_toolbox_never_reopens_and_flags_the_connection():
    async def go():
        conn = ClosingConnection(closed=99)
        async with conn:
            out = await box_for(conn).call("list_databases", {})
            return out, conn

    out, conn = run(go())
    assert out.content == CLOSED_6 and conn.attempts == 6
    assert conn.needs_reopen is True and conn.generation == 0
    assert all(c is conn.clients[0] for c in conn.clients)  # the same session throughout


def test_ensure_open_reopens_in_the_owning_task_and_clears_the_flag():
    async def go():
        conn = ClosingConnection(closed=6)
        async with conn:
            box = box_for(conn)
            first = await box.call("list_databases", {})
            old = conn._client
            reopened = await conn.ensure_open()
            again = await conn.ensure_open()  # nothing to do now
            second = await box.call("list_databases", {})
            return first, second, reopened, again, old, conn

    first, second, reopened, again, old, conn = run(go())
    assert first.content == CLOSED_6
    assert (reopened, again, conn.needs_reopen, conn.generation) == (True, False, False, 1)
    assert conn._client is None and old is not conn.clients[-1]  # closed on exit; new session
    assert not second.is_error and "uk_probes" in second.content


def test_ensure_open_reopens_a_connection_that_is_not_open():
    async def go():
        async with McpConnection(server=fake_server.make_server()) as conn:
            await conn._close()
            return await conn.ensure_open(), conn._client is not None

    assert run(go()) == (True, True)


def test_concurrent_calls_from_gathered_tasks_do_not_reopen_and_teardown_is_clean():
    async def go():
        conn = ClosingConnection(closed=99)
        async with conn:
            boxes = [box_for(conn) for _ in range(3)]
            outs = await asyncio.gather(*(b.call("list_databases", {}) for b in boxes))
        return outs, conn

    outs, conn = run(go())  # exiting raised nothing: no cancel scope crossed tasks
    assert [o.content for o in outs] == [CLOSED_6] * 3
    assert conn.generation == 0 and conn.needs_reopen is True


def test_concurrent_calls_on_one_toolbox_are_serialized():
    conn = ScriptedConn(result("[]"))
    box = box_for(conn)

    async def go():
        return await asyncio.gather(*(box.call("list_databases", {}) for _ in range(8)))

    outs = run(go())
    assert conn.max_in_flight == 1
    assert len(conn.calls) == 6 and box.calls_made == 8
    assert sum(o.refused == "budget" for o in outs) == 2


def test_reopen_skips_when_another_caller_already_reopened():
    async def go():
        async with McpConnection(server=fake_server.make_server()) as conn:
            first = conn._client
            await conn.reopen(seen_generation=0)
            second = conn._client
            await conn.reopen(seen_generation=0)  # stale: someone already reopened
            return first, second, conn._client, conn.generation

    first, second, third, gen = run(go())
    assert first is not second and second is third and gen == 1


# --- 8. arguments and settings ---


def test_unknown_arguments_are_dropped_before_the_server():
    conn = ScriptedConn(result("[]"))
    args = {"database": "uk", "like": "uk%", "format": "Native", "serviceId": "other"}
    run(box_for(conn).call("list_tables", args))
    assert conn.calls == [
        ("list_tables", {"database": "uk", "like": "uk%", "serviceId": "svc-123"})
    ]


def test_query_arguments_are_limited_to_the_schema():
    conn = ScriptedConn(result("[]"))
    query(conn, "SELECT 1")
    run(box_for(conn).call("run_select_query", {"query": "SELECT 2", "settings": {"x": 1}}))
    assert conn.calls[-1] == ("run_select_query", {"query": "SELECT 2", "serviceId": "svc-123"})


ENV = {
    "CLICKHOUSE_MCP_KEY_ID": "k",
    "CLICKHOUSE_MCP_KEY_SECRET": "x",
    "CLICKHOUSE_SERVICE_ID": "s",
}


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.clickhouse.cloud/mcp",
        "http://localhost.example.com/mcp",
        "ftp://mcp.clickhouse.cloud/mcp",
        "mcp.clickhouse.cloud/mcp",
        "https:///mcp",
    ],
)
def test_settings_reject_non_https_urls(url):
    with pytest.raises(McpConfigError, match="CLICKHOUSE_MCP_URL"):
        McpSettings.from_env({**ENV, "CLICKHOUSE_MCP_URL": url})


@pytest.mark.parametrize(
    "url",
    ["https://mcp.example.com/mcp", "http://localhost:8000/mcp", "http://127.0.0.1/mcp"],
)
def test_settings_accept_https_and_local_http(url):
    assert McpSettings.from_env({**ENV, "CLICKHOUSE_MCP_URL": url}).url == url


def test_live_clickhouse_remote_mcp_shape_is_read() -> None:
    """D-64: the payload the ClickHouse Cloud remote MCP server returned live."""
    live = {
        "result": {
            "meta": [
                {"name": "roles", "type": "Array(String)"},
                {"name": "readonly", "type": "UInt8"},
            ],
            "data": [{"roles": ["analytics_ro"], "readonly": 2}],
            "rows": 1,
            "statistics": {"elapsed": 0.0013, "rows_read": 1, "bytes_read": 1},
        }
    }
    cols, rows = shape_rows(live)
    assert cols == ["roles", "readonly"]
    assert rows == [[["analytics_ro"], 2]]


def test_live_shape_carries_the_inner_server_cut() -> None:
    """D-64 verifier: a cut stated inside the unwrapped object survives the outer level."""
    inner = {"meta": [{"name": "a"}], "data": [{"a": 1}, {"a": 2}], "rows": 5000}

    def ask(payload: dict[str, Any]) -> Any:
        res = types.SimpleNamespace(content=[], structured_content=payload, is_error=False)
        return query(ScriptedConn(res))

    out = ask({"result": inner})
    assert (out.rows_returned, out.rows_total, out.truncated) == (2, 5000, True)
    out = ask({"result": {**inner, "rows": 2, "truncated": True}})
    assert (out.rows_returned, out.rows_total, out.truncated) == (2, 2, True)
    out = ask({"result": {**inner, "rows": 2}})
    assert (out.rows_returned, out.rows_total, out.truncated) == (2, 2, False)


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"status": "error", "message": "Code: 164. readonly"}},
        {"result": {"error": "Code: 497", "rows": 0}},
        {"result": {"result": {"result": {"result": {"result": {"data": [{"a": 1}]}}}}}},
    ],
)
def test_unwrapped_error_or_too_deep_object_is_not_rows(payload: Any) -> None:
    assert shape_rows(payload) == (None, None)
