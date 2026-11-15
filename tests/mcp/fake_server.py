"""An in-process MCP server shaped like ClickHouse's remote MCP (MCP-06).

Tool names and arguments follow the published tool table (serviceId on every call, a
control-plane tool alongside). A2.1 replaces the canned payloads with recordings from the real
server.
"""

from __future__ import annotations

import json
import re

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

CALLS: list[tuple[str, dict]] = []


def make_server() -> MCPServer:
    server = MCPServer("fake-clickhouse")

    @server.tool(description="List all databases in a ClickHouse service.")
    def list_databases(serviceId: str) -> str:
        CALLS.append(("list_databases", {"serviceId": serviceId}))
        return json.dumps(["default", "system", "uk", "uk_probes"])

    @server.tool(description="List all tables in a database, including column definitions.")
    def list_tables(serviceId: str, database: str, like: str | None = None) -> str:
        CALLS.append(("list_tables", {"serviceId": serviceId, "database": database}))
        cols = [{"name": "price", "type": "UInt32"}, {"name": "town", "type": "String"}]
        return json.dumps([{"database": database, "name": "uk_price_paid", "columns": cols}])

    @server.tool(description="Run a SELECT query in a ClickHouse service.")
    async def run_select_query(query: str, serviceId: str) -> str:
        CALLS.append(("run_select_query", {"serviceId": serviceId, "query": query}))
        if "SLEEP" in query:  # a slow query, for read-timeout tests
            await anyio.sleep(5)
        if "boom" in query:
            raise ToolError("Code: 47. Unknown identifier boom")
        m = re.search(r"LIMIT (\d+)", query)
        n = int(m.group(1)) if m else 3
        return json.dumps(
            {
                "meta": [{"name": "town"}, {"name": "n"}],
                "data": [{"town": f"T{i}", "n": i} for i in range(n)],
            }
        )

    @server.tool(description="Organization cost (control plane).")
    def get_organization_cost(organizationId: str) -> str:
        CALLS.append(("get_organization_cost", {"organizationId": organizationId}))
        return json.dumps({"cost": 1})

    return server
