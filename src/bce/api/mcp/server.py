"""MCP server wiring (optional ``mcp`` SDK).

Binds the SDK-independent tool catalog (:mod:`bce.api.mcp.tools`) to a real MCP server. The ``mcp``
package is an optional dependency; importing this module without it raises a clear error only when
you actually try to build/run the server, so the rest of the package (and tests) stay importable.

Each tool call opens a fresh database connection (single DB: graph + vector, P5), dispatches to the
core, and returns the deterministic envelope as JSON text content. Read tools roll back; the indexing
tools enqueue a job and commit.

Dispatch is synchronous (psycopg queries, and an HTTP call to the embedding provider), so it runs on a
worker thread rather than on the event loop that drives the stdio transport.

Indexing over MCP is opt-in via ``BCE_MCP_ALLOW_WRITE``. When it is on, this process also runs a job
worker pool, so queued indexing progresses without a separate ``bce serve``; the queue claims rows
with ``FOR UPDATE SKIP LOCKED``, so running both is safe. The pool claims only the job types the MCP
tools can enqueue, leaving remote clones to the API server.

Logging is configured against stderr. Without it the worker's failures fell back to Python's
last-resort handler, which prints a bare traceback; with it they are the same JSON lines the API
server emits, and they reach the log file too.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def build_server(name: str = "bgts-context-engine") -> Any:
    """Construct an MCP ``Server`` exposing the BCE tool catalog. Requires the ``mcp`` package."""
    try:
        import anyio.to_thread  # installed with mcp
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "The 'mcp' package is required for the MCP server. Install it with: pip install mcp"
        ) from exc

    from bce.api.mcp.tools import WRITE_TOOLS, available_tool_specs, dispatch_tool
    from bce.config import get_settings
    from bce.storage.relational.db import connection

    settings = get_settings()
    allow_write = settings.mcp_allow_write
    specs = available_tool_specs(allow_write=allow_write, allowlist=settings.mcp_tool_allowlist)
    server = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pragma: no cover - requires SDK runtime
        return [
            Tool(name=tool, description=spec["description"], inputSchema=spec["schema"])
            for tool, spec in specs.items()
        ]

    def _dispatch_blocking(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in specs:
            raise KeyError(f"tool '{name}' is not advertised by this server (BCE_MCP_TOOLS)")
        with connection() as conn:
            try:
                result = dispatch_tool(conn, name, arguments, allow_write=allow_write)
                if name in WRITE_TOOLS:
                    conn.commit()  # persist the queued job
                else:
                    conn.rollback()  # read-only tools never persist
            except Exception:
                conn.rollback()
                raise
        return result

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:  # pragma: no cover
        result = await anyio.to_thread.run_sync(_dispatch_blocking, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    return server


def _warm_encoder() -> None:  # pragma: no cover - SDK runtime
    """Build the embedding encoder once, before serving, so no tool call pays for it.

    The Voyage SDK imports numpy, and loading that extension lazily from inside a tool call stalls
    for minutes on Windows, where the same import costs seconds at process start. A provider that is
    misconfigured is not fatal: only the search tools need an encoder, and they already raise a clear
    error per call, so the graph tools stay usable.
    """
    from bce.indexing.embedder.encoder import build_default_encoder

    try:
        build_default_encoder()
    except Exception as exc:
        logging.getLogger("bce.api.mcp").warning(
            "embedding encoder unavailable; search tools will fail", extra={"detail": str(exc)}
        )


async def run_stdio(name: str = "bgts-context-engine") -> None:  # pragma: no cover - SDK runtime
    """Run the MCP server over stdio (the common agent transport)."""
    from mcp.server.stdio import stdio_server

    from bce.api.mcp.tools import MCP_JOB_TYPES
    from bce.config import get_settings
    from bce.core.logging import setup_logging

    # stdout carries the protocol, so logs go to stderr, which editors surface as server logs.
    setup_logging(stream=sys.stderr)

    server = build_server(name)
    _warm_encoder()
    pool = None
    if get_settings().mcp_allow_write:
        from bce.jobs.worker import JobWorkerPool

        pool = JobWorkerPool(job_types=MCP_JOB_TYPES)
        pool.start()
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if pool is not None:
            pool.stop()
