"""ClickHouse remote MCP client (N1.5, Tech Spec §2.4 and §8).

What this client limits, and what it does not:

- Transport: Streamable HTTP to `https://mcp.clickhouse.cloud/mcp` (plain http only for
  localhost), API key over HTTP Basic (MCP-01). The tool listing is read once, page by page.
- Tools: only `list_databases`, `list_tables` and `run_select_query` reach the model; any other
  name is refused here and never sent (MCP-02). Control-plane tools stay inside this module.
- `serviceId`: removed from the model-facing schemas; the toolbox always sends its own.
- Arguments: only the properties of the model-facing schema are forwarded (unknown keys are
  dropped), and the `database` argument must be one of the pack's databases.
- SQL: the client does not parse `query`. A query can still name any database or table in its
  SQL text; restricting what SQL can read is the read-only role's job (N1.2), not this client's.
- Results: query results go back as compact JSON with at most `row_cap` rows, at most
  MAX_TEXT_CHARS characters and the exact total (MCP-03). Every call attempt counts toward the
  budget; the one past it is refused (MCP-04).
- Failures: server errors and transport failures become tool errors the model can read, never
  exceptions (MCP-05). Connection-closed and HTTP 5xx/429 errors are retried, up to six attempts
  within a 45 s wall-clock budget per call; timeouts are not retried. A toolbox never reopens
  the connection itself: it flags `needs_reopen` and the task that owns the connection calls
  `ensure_open()`. Error text names the kind of failure, never credentials.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from evalkit.retry import with_retries
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CONNECTION_CLOSED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    REQUEST_TIMEOUT,
)

from agentic_analytics.models.base import ToolSpec

logger = logging.getLogger(__name__)

ALLOWED_TOOLS: tuple[str, ...] = ("list_databases", "list_tables", "run_select_query")
DEFAULT_URL = "https://mcp.clickhouse.cloud/mcp"
MAX_ATTEMPTS = 6
CALL_BUDGET_S = 45.0  # wall-clock cap across all attempts of one call (question budget: 120 s)
DEFAULT_READ_TIMEOUT_S = 30.0
MAX_TEXT_CHARS = 12_000
MAX_CELL_CHARS = 500
MIN_CELL_CHARS = 16
MAX_TOOL_PAGES = 20
# The SDK buffers at most 1 MiB per SSE event by default (DEFAULT_MAX_SSE_EVENT_SIZE) and fails
# the request beyond it. A query result can exceed that before our caps apply, so the limit is
# set explicitly. JSON (non-SSE) responses are not limited by this setting.
MAX_SSE_EVENT_BYTES = 8 * 1024 * 1024
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
Refusal = Literal["not_allowed", "budget", "bad_args"]

# In mcp 2.3 a read timeout is REQUEST_TIMEOUT, a dropped stream is CONNECTION_CLOSED, and any
# HTTP error status other than 404 (5xx and 429 included) is INTERNAL_ERROR "Server returned an
# error response". Only the last two are retried. A timeout is not: a retry would run the same
# slow query on the server again, and 6 x 30 s would overrun the 120 s question budget.
MCP_ERROR_KINDS: dict[int, str] = {
    REQUEST_TIMEOUT: "MCP server timeout",
    CONNECTION_CLOSED: "MCP connection closed",
    INTERNAL_ERROR: "MCP server error (HTTP 5xx, rate limit or internal error)",
    PARSE_ERROR: "MCP protocol error (parse error)",
    INVALID_REQUEST: "MCP protocol error (invalid request)",
    METHOD_NOT_FOUND: "MCP protocol error (method not found)",
    INVALID_PARAMS: "MCP protocol error (invalid params)",
}
TRANSIENT_MCP_CODES = frozenset({CONNECTION_CLOSED, INTERNAL_ERROR})
TIMEOUT_HINT = (
    " The query timed out; simplify it (tighter filters, fewer columns, an aggregate or a"
    " smaller LIMIT) and try again."
)


class McpConfigError(ValueError):
    """Missing settings or a server that lacks a tool the benchmark needs."""


class McpCallFailed(Exception):
    """A classified call failure. `status_code` 503 makes evalkit's `with_retries` retry it."""

    def __init__(self, kind: str, *, transient: bool) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status_code = 503 if transient else 400


def mcp_error_kind(exc: MCPError) -> tuple[str, bool]:
    """(model-facing kind, transient?) for an SDK error. Never includes the error's message."""
    kind = MCP_ERROR_KINDS.get(exc.code, "MCP protocol error")
    return f"{kind} (code {exc.code})", exc.code in TRANSIENT_MCP_CODES


def basic_auth(key_id: str, key_secret: str) -> str:
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode("ascii")
    return f"Basic {token}"


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return
    raise McpConfigError(
        "CLICKHOUSE_MCP_URL must be an https URL (http is allowed only for localhost)"
    )


@dataclass(frozen=True)
class McpSettings:
    key_id: str
    key_secret: str = field(repr=False)
    service_id: str
    url: str = DEFAULT_URL

    def __post_init__(self) -> None:
        _check_url(self.url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> McpSettings:
        env = os.environ if env is None else env
        names = ("CLICKHOUSE_MCP_KEY_ID", "CLICKHOUSE_MCP_KEY_SECRET", "CLICKHOUSE_SERVICE_ID")
        missing = [n for n in names if not env.get(n, "").strip()]
        if missing:
            raise McpConfigError(f"set {', '.join(missing)} (see .env.example)")
        return cls(
            env["CLICKHOUSE_MCP_KEY_ID"].strip(),
            env["CLICKHOUSE_MCP_KEY_SECRET"].strip(),
            env["CLICKHOUSE_SERVICE_ID"].strip(),
            env.get("CLICKHOUSE_MCP_URL", DEFAULT_URL).strip() or DEFAULT_URL,
        )


class McpConnection:
    """One MCP session for a run. `server` is a URL (real) or an in-process server (tests).

    The SDK's transports run inside anyio task groups, so entering, `reopen()`/`ensure_open()`
    and exiting must happen in the task that opened the connection. Calls may come from other
    tasks; they never reopen, they set `needs_reopen` when the session is gone.
    """

    def __init__(
        self,
        settings: McpSettings | None = None,
        *,
        server: Any = None,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        max_sse_event_bytes: int = MAX_SSE_EVENT_BYTES,
    ) -> None:
        if (settings is None) == (server is None):
            raise ValueError("give exactly one of settings or server")
        self.settings, self._server, self._timeout = settings, server, read_timeout_s
        self._max_sse = max_sse_event_bytes
        self._client: Any = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self.generation = 0  # bumped by every successful reopen()
        self.needs_reopen = False  # set by a toolbox that saw the connection closed
        self.server_tools: dict[str, dict[str, Any]] = {}

    async def _open(self) -> None:
        """Open the transport, client and tool listing; on any failure close what was opened."""
        from mcp import Client

        stack = contextlib.AsyncExitStack()
        try:
            if self.settings is not None:
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client

                auth = basic_auth(self.settings.key_id, self.settings.key_secret)
                http = create_mcp_http_client(headers={"Authorization": auth})
                # The transport does not close a client it was given; the stack does.
                await stack.enter_async_context(http)
                target: Any = streamable_http_client(
                    self.settings.url, http_client=http, max_sse_event_size=self._max_sse
                )
            else:
                target = self._server
            client = Client(target, read_timeout_seconds=self._timeout)
            await stack.enter_async_context(client)
            tools = await self._list_all_tools(client)
        except BaseException:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        self._stack, self._client, self.server_tools = stack, client, tools

    @staticmethod
    async def _list_all_tools(client: Any) -> dict[str, dict[str, Any]]:
        """Every page of the tool listing, following `next_cursor` for at most MAX_TOOL_PAGES."""
        tools: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_TOOL_PAGES):
            page = await client.list_tools(cursor=cursor)
            for t in page.tools:
                tools[t.name] = {"description": t.description or "", "input_schema": t.input_schema}
            cursor = page.next_cursor
            if cursor is None or cursor in seen:
                break
            seen.add(cursor)
        return tools

    async def _close(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> McpConnection:
        await self._open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._close()

    async def reopen(self, seen_generation: int | None = None) -> None:
        """Close the session (ignoring close errors) and open a new one.

        With `seen_generation`, do nothing if another caller already reopened since then.
        """
        async with self._lock:
            if seen_generation is not None and seen_generation != self.generation:
                return
            with contextlib.suppress(Exception):
                await self._close()
            await self._open()
            self.generation += 1
            self.needs_reopen = False

    async def ensure_open(self) -> bool:
        """Reopen if a toolbox flagged the connection or it is not open; True if it reopened.

        Call it from the task that owns the connection (A2.1's loop, between turns).
        """
        if not self.needs_reopen and self._client is not None:
            return False
        await self.reopen()
        return True

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> Any:
        if self._client is None:
            raise MCPError(CONNECTION_CLOSED, "connection is not open")
        timeout = self._timeout if timeout_s is None else min(self._timeout, timeout_s)
        return await self._client.call_tool(name, arguments, read_timeout_seconds=timeout)


def model_schema(server_schema: dict[str, Any], databases: tuple[str, ...]) -> dict[str, Any]:
    """The server's schema minus `serviceId`, with `database` limited to the pack's."""
    schema: dict[str, Any] = copy.deepcopy(server_schema) if server_schema else {"type": "object"}
    props: dict[str, Any] = dict(schema.get("properties") or {})
    props.pop("serviceId", None)
    if "database" in props:
        props["database"] = {**props["database"], "enum": list(databases)}
    schema["properties"] = props
    schema["required"] = [r for r in schema.get("required") or [] if r != "serviceId"]
    schema.setdefault("type", "object")
    return schema


# --- result shapes ----------------------------------------------------------------------------
# ASSUMPTION (unconfirmed until A2.1 records the live server's payloads): run_select_query
# returns one of
#   {"columns": [names or {"name": ...}], "rows": [[...] or {...}, ...]}
#   ClickHouse JSON: {"meta": [{"name", "type"}], "data": [...], "rows": n, "statistics": {...}}
#   a list of objects (JSONEachRow), possibly wrapped in {"data" | "result" | "rows": ...}.
# Anything else is an unknown shape and the raw text goes to the model, clipped. Only an
# explicit server-side cut counts: an integer `rows` above the number of rows sent, or
# `"truncated": true`. ClickHouse's `rows_before_limit_at_least` is ignored on purpose: it counts
# rows before the query's own LIMIT, so a complete top-10 can carry 5000 there.

MAX_UNWRAP = 4  # wrapper levels followed; with json.loads guarded, _shape cannot overflow


@dataclass(frozen=True)
class _Shaped:
    columns: list[str] | None
    rows: list[list[Any]]
    server_total: int | None = None
    server_truncated: bool = False


def _column_names(entries: list[Any]) -> list[str] | None:
    names: list[str] = []
    for c in entries:
        if isinstance(c, str):
            names.append(c)
        elif isinstance(c, dict) and isinstance(c.get("name"), str):
            names.append(c["name"])
        else:
            return None
    return names


def _rows_for(cols: list[str], raw: list[Any]) -> list[list[Any]] | None:
    """Rows as lists in column order; objects map by name. None if any row has another shape."""
    rows: list[list[Any]] = []
    for r in raw:
        if isinstance(r, dict):
            rows.append([r.get(c) for c in cols])
        elif isinstance(r, list | tuple) and len(r) == len(cols):
            rows.append(list(r))
        else:  # strings, scalars, rows of the wrong width
            return None
    return rows


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _server_cut(payload: dict[str, Any], sent: int) -> tuple[int | None, bool]:
    rows = _count(payload.get("rows"))
    total = rows if rows is not None and rows > sent else None
    return total, total is not None or payload.get("truncated") is True


def _shape(payload: Any, depth: int = 0) -> _Shaped | None:
    if depth > MAX_UNWRAP:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            return None
    if isinstance(payload, dict):
        shaped: _Shaped | None
        pair = next(
            (
                (c, r)
                for c, r in (("columns", "rows"), ("meta", "data"))
                if isinstance(payload.get(c), list) and isinstance(payload.get(r), list)
            ),
            None,
        )
        if pair is not None:
            cols = _column_names(payload[pair[0]])
            rows = _rows_for(cols, payload[pair[1]]) if cols is not None else None
            if cols is None or rows is None:
                return None
            shaped = _Shaped(cols, rows)
        else:
            key = next((k for k in ("data", "result", "rows") if k in payload), None)
            if key is None or not isinstance(payload[key], list | str):
                return None
            shaped = _shape(payload[key], depth + 1)
            if shaped is None:
                return None
        total, cut = _server_cut(payload, len(shaped.rows))
        if shaped.server_total is not None:
            total = max(shaped.server_total, total or 0)
        return _Shaped(shaped.columns, shaped.rows, total, cut or shaped.server_truncated)
    if isinstance(payload, list):
        if all(isinstance(r, dict) for r in payload):
            names: list[Any] = []
            for r in payload:  # the union of keys, in first-seen order
                names.extend(k for k in r if k not in names)
            if not all(isinstance(c, str) for c in names):
                return None
            return _Shaped(names, [[r.get(c) for c in names] for r in payload])
        if all(isinstance(r, list | tuple) for r in payload):
            return _Shaped(None, [list(r) for r in payload])
    return None


def shape_rows(payload: Any) -> tuple[list[str] | None, list[list[Any]] | None]:
    """Columns and rows from the shapes a query result may take; (None, None) if unknown.

    Never raises: any JSON value, or text that is not JSON, gives a shape or (None, None).
    """
    shaped = _shape(payload)
    if shaped is None:
        return None, None
    return shaped.columns, shaped.rows


# --- outcomes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOutcome:
    content: str  # what the model sees
    is_error: bool
    rows_returned: int | None = None
    rows_total: int | None = None
    truncated: bool = False
    latency_ms: int = 0
    refused: Refusal | None = None


def _result_text(result: Any) -> str:
    parts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    text = "\n".join(p for p in parts if isinstance(p, str) and p)
    if not text and getattr(result, "structured_content", None) is not None:
        try:
            text = json.dumps(result.structured_content, default=str)
        except (ValueError, TypeError, RecursionError):
            text = ""
    return text


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + f"\n... [{len(text) - MAX_TEXT_CHARS} more characters cut]"


def _cut_cell(value: Any, limit: int) -> Any:
    """A cell of at most `limit` characters plus a marker; nested values become text."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list | dict):
        text = json.dumps(value, default=str)
    else:
        return value
    if len(text) <= limit:
        return value
    short = text[:limit] + f"...[{len(text) - limit} more characters cut]"
    return short if len(short) < len(text) else value


def _most_that_fit(n: int, fits: Any) -> int:
    """The largest k in [0, n] with fits(k), for a predicate that only turns false as k grows."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _query_body(
    cols: list[str] | None, rows: list[list[Any]], total: int, cut: bool
) -> tuple[str, int, bool]:
    """(JSON, rows shown, truncated), always valid JSON of at most MAX_TEXT_CHARS characters.

    Over the cap: column names are cut to MAX_CELL_CHARS; if the names alone still do not fit,
    only the leading names that fit are kept, followed by a "...[n more columns cut]" entry, and
    no rows are shown. Otherwise long cells are cut, halving the cell limit from MAX_CELL_CHARS
    down to MIN_CELL_CHARS until every row fits, and then trailing rows are dropped.
    """

    def dump(shown: list[list[Any]], names: list[Any] | None, truncated: bool) -> str:
        body = {"columns": names, "rows": shown, "rows_total": total, "truncated": truncated}
        return json.dumps(body, default=str)

    def fits(text: str) -> bool:
        return len(text) <= MAX_TEXT_CHARS

    text = dump(rows, cols, cut)
    if fits(text):
        return text, len(rows), cut
    names = None if cols is None else [_cut_cell(c, MAX_CELL_CHARS) for c in cols]
    if names is not None and not fits(dump([], names, True)):

        def kept(k: int) -> list[Any]:
            return [*names[:k], f"...[{len(names) - k} more columns cut]"]

        k = _most_that_fit(len(names), lambda k: fits(dump([], kept(k), True)))
        return dump([], kept(k), True), 0, True
    limit = MAX_CELL_CHARS
    while True:
        shrunk = [[_cut_cell(v, limit) for v in r] for r in rows]
        n = _most_that_fit(len(shrunk), lambda k, sh=shrunk: fits(dump(sh[:k], names, True)))
        if n == len(shrunk) or limit <= MIN_CELL_CHARS:
            return dump(shrunk[:n], names, True), n, True
        limit = max(MIN_CELL_CHARS, limit // 2)


class Toolbox:
    """The tools one question may use: allow-listed, budgeted, with `serviceId` injected.

    Calls are serialized, so concurrent `call()`s on one toolbox are safe. A toolbox never
    reopens its connection; see `McpConnection.ensure_open()`.
    """

    def __init__(
        self,
        conn: McpConnection,
        service_id: str,
        databases: tuple[str, ...],
        *,
        max_calls: int = 6,
        row_cap: int = 50,
        retry: dict[str, Any] | None = None,
        call_budget_s: float = CALL_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        missing = [t for t in ALLOWED_TOOLS if t not in conn.server_tools]
        if missing:
            raise McpConfigError(f"the MCP server does not offer {', '.join(missing)}")
        if not databases:
            raise McpConfigError("a toolbox needs at least one database")
        self.conn, self.service_id, self.databases = conn, service_id, databases
        self.max_calls, self.row_cap = max_calls, row_cap
        self.calls_made = 0
        retry = dict(retry or {})
        self._sleep: Callable[[float], Awaitable[object]] = retry.pop("sleep", asyncio.sleep)
        self._retry: dict[str, Any] = {"attempts": MAX_ATTEMPTS, **retry}
        self.call_budget_s, self._clock = call_budget_s, clock
        self._call_lock = asyncio.Lock()
        self.specs: tuple[ToolSpec, ...] = tuple(
            ToolSpec(
                name=name,
                description=conn.server_tools[name]["description"],
                parameters=model_schema(conn.server_tools[name]["input_schema"], databases),
            )
            for name in ALLOWED_TOOLS
        )
        self._params = {s.name: frozenset(s.parameters.get("properties") or {}) for s in self.specs}
        self._secrets = _secret_forms(getattr(conn, "settings", None))

    def _refuse(self, kind: Refusal, message: str) -> ToolOutcome:
        return ToolOutcome(content=message, is_error=True, refused=kind)

    def _scrub(self, text: str) -> str:
        """Text with the key secret and Basic token redacted, raw and JSON-escaped."""
        for s in self._secrets:
            text = text.replace(s, "[redacted]")
        return text

    def _scrub_structured(self, value: Any) -> Any:
        if value is None or not self._secrets:
            return value
        try:
            dumped = json.dumps(value, default=str)
            scrubbed = self._scrub(dumped)
            return value if scrubbed == dumped else json.loads(scrubbed)
        except (ValueError, TypeError, RecursionError):
            return None  # fall back to the (scrubbed) text

    async def call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        async with self._call_lock:
            return await self._call(name, arguments)

    async def _call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        self.calls_made += 1
        if self.calls_made > self.max_calls:
            return self._refuse(
                "budget",
                f"Tool-call budget of {self.max_calls} is used up. Call final_answer now.",
            )
        if name not in ALLOWED_TOOLS:
            return self._refuse("not_allowed", f"Tool {name!r} is not available.")
        if not isinstance(arguments, dict):
            return self._refuse("bad_args", "Arguments must be a JSON object.")
        db = arguments.get("database")
        if db is not None and db not in self.databases:
            return self._refuse(
                "bad_args", f"Database {db!r} is not available; use one of {list(self.databases)}."
            )
        # Only the model-facing schema's properties go to the server; serviceId is ours.
        args = {k: v for k, v in arguments.items() if k in self._params[name] and k != "serviceId"}
        if name == "run_select_query" and not isinstance(args.get("query"), str):
            return self._refuse("bad_args", "run_select_query needs a 'query' string.")
        args["serviceId"] = self.service_id
        t0 = time.perf_counter()
        try:
            result = await self._call_with_retries(name, args)
        except McpCallFailed as exc:  # failures after retries reach the model as text
            latency = int((time.perf_counter() - t0) * 1000)
            message = self._scrub(exc.kind)
            logger.warning("MCP call %s failed: %s", name, message)
            return ToolOutcome(content=message, is_error=True, latency_ms=latency)
        latency = int((time.perf_counter() - t0) * 1000)
        # Redact before any slicing or cell cutting, so no secret prefix can survive a cut.
        text = self._scrub(_result_text(result))
        if getattr(result, "is_error", False):
            return ToolOutcome(
                content=text[:MAX_TEXT_CHARS] or "MCP tool error", is_error=True, latency_ms=latency
            )
        if name != "run_select_query":
            return ToolOutcome(content=_clip(text), is_error=False, latency_ms=latency)
        structured = self._scrub_structured(getattr(result, "structured_content", None))
        return self._query_outcome(structured, text, latency)

    async def _call_with_retries(self, name: str, args: dict[str, Any]) -> Any:
        """The server's result, or McpCallFailed naming the failure and the attempts made.

        Connection-closed and 5xx/429 errors are retried (MAX_ATTEMPTS, within call_budget_s of
        wall clock, each attempt's read timeout capped by the time left); timeouts and protocol
        errors are not. A final connection-closed sets `conn.needs_reopen`.
        """
        attempts = 0
        last_code: int | None = None
        last_kind = ""
        out_of_time = False
        start = self._clock()

        def left() -> float:
            return self.call_budget_s - (self._clock() - start)

        async def sleep(delay: float) -> None:
            await self._sleep(max(0.0, min(delay, left())))

        async def attempt() -> Any:
            nonlocal attempts, last_code, last_kind, out_of_time
            remaining = left()
            if attempts and remaining <= 0:
                out_of_time = True
                raise McpCallFailed("out of time", transient=False)
            attempts += 1
            try:
                return await self.conn.call_tool(name, args, timeout_s=remaining)
            except MCPError as exc:
                last_code = exc.code
                last_kind, transient = mcp_error_kind(exc)
                raise McpCallFailed(last_kind, transient=transient) from exc
            except TimeoutError as exc:
                last_code, last_kind = REQUEST_TIMEOUT, _other_kind(exc)
                raise McpCallFailed(last_kind, transient=False) from exc
            except Exception as exc:  # retried only if evalkit's is_retryable says so
                last_code, last_kind = None, _other_kind(exc)
                raise

        try:
            return await with_retries(attempt, sleep=sleep, **self._retry)
        except Exception as exc:
            kind = last_kind or _other_kind(exc)
            if last_code == CONNECTION_CLOSED:
                self.conn.needs_reopen = True
            plural = "s" if attempts != 1 else ""
            message = f"{kind}: the call failed after {attempts} attempt{plural}."
            if out_of_time:
                message += f" The {self.call_budget_s:g} s retry time limit was reached."
            if last_code == REQUEST_TIMEOUT:
                message += TIMEOUT_HINT
            raise McpCallFailed(message, transient=False) from exc

    def _query_outcome(self, structured: Any, text: str, latency: int) -> ToolOutcome:
        shaped: _Shaped | None = None
        if structured is not None:
            shaped = _shape(structured)
        if shaped is None:
            shaped = _shape(text)
        if shaped is None:
            return ToolOutcome(content=_clip(text), is_error=False, latency_ms=latency)
        rows = shaped.rows
        shown = rows[: self.row_cap]
        total = max(len(rows), shaped.server_total or 0)
        cut = len(rows) > len(shown) or shaped.server_truncated
        try:
            content, n_shown, cut = _query_body(shaped.columns, shown, total, cut)
        except (ValueError, TypeError, RecursionError):  # cells json cannot write
            return ToolOutcome(content=_clip(text), is_error=False, latency_ms=latency)
        return ToolOutcome(
            content=self._scrub(content),
            is_error=False,
            rows_returned=n_shown,
            rows_total=total,
            truncated=cut,
            latency_ms=latency,
        )


def _other_kind(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "MCP server timeout"
    return f"MCP transport error: {type(exc).__name__}"


def _secret_forms(settings: Any) -> tuple[str, ...]:
    """The secret and Basic token as they may appear in text: raw and JSON-escaped."""
    if not isinstance(settings, McpSettings) or not settings.key_secret:
        return ()
    header = basic_auth(settings.key_id, settings.key_secret)
    forms: set[str] = set()
    for s in (header, header.removeprefix("Basic "), settings.key_secret):
        forms.update(
            (s, json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1]),
        )
    return tuple(sorted((f for f in forms if len(f) >= 4), key=len, reverse=True))
