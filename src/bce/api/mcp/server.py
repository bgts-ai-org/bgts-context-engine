"""MCP server wiring (optional ``mcp`` SDK).

Binds the SDK-independent tool catalog (:mod:`bce.api.mcp.tools`) to a real MCP server. The ``mcp``
package is an optional dependency; importing this module without it raises a clear error only when
you actually try to build/run the server, so the rest of the package (and tests) stay importable.

Each tool call opens a fresh database connection (single DB: graph + vector, P5), dispatches to the
core, and returns the deterministic envelope as JSON text content.
"""

from __future__ import annotations

import json
from typing import Any


def build_server(name: str = "bgts-context-engine") -> Any:
    """Construct an MCP ``Server`` exposing every BCE tool. Requires the ``mcp`` package."""
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "The 'mcp' package is required for the MCP server. Install it with: pip install mcp"
        ) from exc

    from bce.api.mcp.tools import TOOL_SPECS, dispatch_tool
    from bce.storage.relational.db import connection

    server = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pragma: no cover - requires SDK runtime
        return [
            Tool(name=tool, description=spec["description"], inputSchema=spec["schema"])
            for tool, spec in TOOL_SPECS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:  # pragma: no cover
        with connection() as conn:
            try:
                result = dispatch_tool(conn, name, arguments)
                conn.rollback()  # read-only tools never persist
            except Exception:
                conn.rollback()
                raise
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    return server


async def run_stdio(name: str = "bgts-context-engine") -> None:  # pragma: no cover - SDK runtime
    """Run the MCP server over stdio (the common agent transport)."""
    from mcp.server.stdio import stdio_server

    server = build_server(name)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
