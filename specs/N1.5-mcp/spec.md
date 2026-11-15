# N1.5 — MCP client

**Wave 1. Depends on:** N1.1, N1.6 (uses `ToolSpec`, D-33). **Unblocks:** A2.1.

## Goal
The agents reach ClickHouse only through this client: a Streamable HTTP session to the remote MCP server, and per question a toolbox that offers exactly three read tools, hides and injects `serviceId`, limits `database` to the pack's databases, caps rows shown to the model, and stops at six calls.

## Outputs (`agentic_analytics.mcp`)
- `McpSettings.from_env()` and `basic_auth()`; `McpConnection` (URL with Basic auth via the `mcp` SDK's `streamable_http_client`, or an in-process server for tests) listing server tools once (MCP-01).
- `Toolbox(conn, service_id, databases, max_calls=6, row_cap=50)`: `specs` for the model and `call(name, args) -> ToolOutcome` (MCP-02..05).
- `shape_rows()` for the result shapes a query may return (columns+rows, ClickHouse JSON/JSONCompact, list of objects); unknown shapes pass through as clipped text.
- `tests/mcp/fake_server.py`: an in-process MCP server shaped like the real one, including a control-plane tool (MCP-06). A2.1's live smoke records real payloads.

## Out of scope
The agent loop (A2.1), re-execution (S2.1), the read-only role (N1.2).
