"""MCP server wiring (optional ``mcp`` SDK).

Binds the SDK-independent tool catalog (:mod:`bce.api.mcp.tools`) to a real MCP server, served over
stdio (:func:`run_stdio`) or streamable HTTP (:func:`run_http`, :mod:`bce.api.mcp.http`). The ``mcp``
package is an optional dependency; importing this module without it raises a clear error only when
you actually try to build/run the server, so the rest of the package (and tests) stay importable.

Each tool call opens a fresh database connection (single DB: graph + vector, P5), dispatches to the
core, and returns :func:`bce.api.mcp.tools.result_text` as text content: the compact Markdown answer
for ``get_context_for_task`` (``view``, default ``BCE_MCP_VIEW``), the deterministic envelope as
JSON otherwise. Read tools roll back; the indexing tools enqueue a job and commit. Every call logs
one ``mcp tool call`` line (tool, view, duration, answer size, the selector's time and cost, and for
``get_context_for_task`` the time of each pipeline stage).

Dispatch is synchronous (psycopg queries, and an HTTP call to the embedding provider), so it runs on a
worker thread rather than on the event loop that drives the transport.

Indexing over MCP is opt-in via ``BCE_MCP_ALLOW_WRITE``. When it is on, this process also runs a job
worker pool, so queued indexing progresses without a separate ``bce serve``; the queue claims rows
with ``FOR UPDATE SKIP LOCKED``, so running both is safe. The pool claims only the job types the MCP
tools can enqueue, leaving remote clones to the API server.

``BCE_AGENT_MODE`` (``hint`` by default, or ``trust``; :mod:`bce.core.agent_mode`) is read once at
startup: the server instructions, the ``get_context_for_task`` description and every answer's
``payload.workflow`` state that mode's workflow. An unknown mode fails the start.

Logging is configured against stderr. Without it the worker's failures fell back to Python's
last-resort handler, which prints a bare traceback; with it they are the same JSON lines the API
server emits, and they reach the log file too.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

_log = logging.getLogger("bce.api.mcp")


def _call_record(
    name: str,
    view: str,
    result: dict[str, Any] | None,
    text: str | None,
    t0: float,
    render_ms: float | None = None,
) -> dict[str, Any]:
    """The ``mcp tool call`` log fields of one call (``text`` is None when it failed).

    ``timings`` is the engine's per-stage wall time (``get_context_for_task``) plus the time spent
    rendering the answer text."""
    record: dict[str, Any] = {
        "tool": name,
        "view": view,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "chars": len(text) if text is not None else None,
        "status": "ok" if text is not None else "error",
    }
    timings = (result or {}).get("timings")
    if isinstance(timings, dict):
        record["timings"] = {**timings, "render_ms": render_ms}
    payload = (result or {}).get("payload")
    if name == "inspect_symbol_graph" and isinstance(payload, dict):
        record["mode"] = payload.get("mode")
        record["result"] = payload.get("status")
    coverage = payload.get("coverage") if isinstance(payload, dict) else None
    selector = coverage.get("selector") if isinstance(coverage, dict) else None
    if isinstance(selector, dict):
        record["selector_status"] = selector.get("status")
        record["selector_ms"] = selector.get("ms")
        record["selector_usd"] = selector.get("cost_usd")
    return record


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

    from bce.api.mcp.tools import (
        WRITE_TOOLS,
        available_tool_specs,
        dispatch_tool,
        normalize_toolset,
        result_text,
    )
    from bce.config import get_settings
    from bce.core.agent_mode import normalize_agent_mode, workflow_text
    from bce.core.render import normalize_view
    from bce.storage.relational.db import connection

    settings = get_settings()
    allow_write = settings.mcp_allow_write
    agent_mode = normalize_agent_mode(settings.agent_mode)
    default_view = normalize_view(settings.mcp_view)
    specs = available_tool_specs(
        allow_write=allow_write,
        allowlist=settings.mcp_tool_allowlist,
        agent_mode=agent_mode,
        view=default_view,
        toolset=normalize_toolset(settings.mcp_toolset),
    )
    style = {"compact": "compact", "locate": "locate"}.get(default_view, "json")
    instructions = (
        "Call get_context_for_task first, once, with the full task text.\n\n"
        + workflow_text(agent_mode, style=style)
    )
    if "inspect_symbol_graph" in specs:
        instructions += (
            "\n\nFor one symbol's definition, callers, callees or references, or the impact of a "
            "change, ask inspect_symbol_graph by name first."
        )
    try:
        server = Server(name, instructions=instructions)
    except TypeError:  # pragma: no cover - mcp releases before server instructions
        server = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pragma: no cover - requires SDK runtime
        return [
            Tool(name=tool, description=spec["description"], inputSchema=spec["schema"])
            for tool, spec in specs.items()
        ]

    def _dispatch_blocking(name: str, arguments: dict[str, Any]) -> str:
        t0 = time.perf_counter()
        view = default_view
        result: dict[str, Any] | None = None
        text: str | None = None
        render_ms: float | None = None
        try:
            if name not in specs:
                raise KeyError(f"tool '{name}' is not advertised by this server (BCE_MCP_TOOLS)")
            view = normalize_view(arguments.get("view") or default_view)
            with connection() as conn:
                try:
                    result = dispatch_tool(
                        conn, name, arguments, allow_write=allow_write, agent_mode=agent_mode
                    )
                    if name in WRITE_TOOLS:
                        conn.commit()  # persist the queued job
                    else:
                        conn.rollback()  # read-only tools never persist
                except Exception:
                    result = None
                    conn.rollback()
                    raise
            rendered = time.perf_counter()
            text = result_text(name, result, view=view, max_tokens=arguments.get("max_tokens"))
            render_ms = round((time.perf_counter() - rendered) * 1000, 1)
            return text
        finally:
            _log.info("mcp tool call", extra=_call_record(name, view, result, text, t0, render_ms))

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:  # pragma: no cover
        text = await anyio.to_thread.run_sync(_dispatch_blocking, name, arguments)
        return [TextContent(type="text", text=text)]

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


def _start(name: str) -> tuple[Any, Any]:  # pragma: no cover - SDK runtime
    """Build the server, warm the encoder and, with writes on, start the job workers."""
    from bce.api.mcp.tools import MCP_JOB_TYPES
    from bce.config import get_settings
    from bce.core.agent_mode import normalize_agent_mode
    from bce.core.selector import describe_selector

    server = build_server(name)
    _warm_encoder()
    log = logging.getLogger("bce.api.mcp")
    log.info("context selector: %s", describe_selector())
    log.info("agent mode: %s", normalize_agent_mode(get_settings().agent_mode))
    pool = None
    if get_settings().mcp_allow_write:
        from bce.jobs.worker import JobWorkerPool

        pool = JobWorkerPool(job_types=MCP_JOB_TYPES)
        pool.start()
    return server, pool


async def run_stdio(name: str = "bgts-context-engine") -> None:  # pragma: no cover - SDK runtime
    """Run the MCP server over stdio (the common agent transport)."""
    from mcp.server.stdio import stdio_server

    from bce.core.logging import setup_logging

    # stdout carries the protocol, so logs go to stderr, which editors surface as server logs.
    setup_logging(stream=sys.stderr)

    server, pool = _start(name)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if pool is not None:
            pool.stop()


async def run_http(
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
    name: str = "bgts-context-engine",
) -> None:  # pragma: no cover - SDK runtime
    """Run the MCP server over streamable HTTP (:mod:`bce.api.mcp.http` states the security)."""
    import uvicorn

    from bce.api.mcp.http import build_http_app, check_bind, parse_list
    from bce.config import get_settings
    from bce.core.logging import setup_logging
    from bce.integrations.precontext import server_hook_response

    setup_logging()
    settings = get_settings()
    tokens = parse_list(settings.mcp_http_token)
    check_bind(host, tokens)  # before the encoder warms up, so a refused bind fails fast
    server, pool = _start(name)
    app = build_http_app(
        server,
        host=host,
        path=path,
        tokens=tokens,
        allowed_hosts=parse_list(settings.mcp_http_allowed_hosts),
        allowed_origins=parse_list(settings.mcp_http_allowed_origins),
        precontext=lambda body, repo_ids, mode: server_hook_response(
            body, repo_ids=repo_ids, mode=mode
        ),
    )
    logging.getLogger("bce.api.mcp").info(
        "MCP over HTTP on http://%s:%d%s (auth: %s)",
        host,
        port,
        path,
        "bearer token" if tokens else "none, loopback only",
    )
    try:
        await uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        ).serve()
    finally:
        if pool is not None:
            pool.stop()
