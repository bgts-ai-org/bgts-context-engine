"""MCP tool catalog + dispatcher (SDK-independent).

Declares the agent-facing tool set as MCP-style specs (name, description, JSON input schema) and
maps a tool name + arguments to a call against the deterministic core, using the caller's connection.
This is the single source of truth the SDK server binds to, and it is importable/testable without the
optional ``mcp`` package.

The catalog is deliberately narrower than the REST surface. An agent picks worse when handed a dozen
overlapping retrieval tools, so only the non-redundant ones are exposed: ``hybrid_search`` subsumes
semantic and code-similarity search, and ``get_context_for_task`` subsumes the change-site and
assembly helpers. The rest stay reachable over REST.

Indexing tools are writes, so they are gated behind ``allow_write`` (``BCE_MCP_ALLOW_WRITE``) and
enqueue a job rather than indexing inline: a full index outlives any agent's tool-call timeout. They
cover local working trees only. Cloning a remote repository is a deployment task with its own
credentials, not something an editor-spawned agent should reach for, so it stays on REST and the CLI.

Every tool returns the standard envelope ``{tool, payload, message, locale}`` (P1: payload is
language-neutral; only ``message`` is localized).
"""

from __future__ import annotations

from typing import Any

import psycopg

from bce.core.auth.scope import Principal, ScopeFilter
from bce.core.i18n import get_translator
from bce.jobs.store import enqueue_job, get_job, list_jobs
from bce.storage.graph.client import GraphClient
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore
from bce.tools.layer1 import find_references, get_call_graph, resolve_symbol
from bce.tools.layer2 import hybrid_search
from bce.tools.layer3 import expand_blast_radius, get_context_for_task

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


#: Tools that mutate the graph. Only advertised and dispatchable when ``allow_write`` is set.
WRITE_TOOLS = frozenset({"index_repo", "reindex_repo"})

# Tool catalog: name -> (description, input schema).
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_context_for_task": {
        "description": (
            "Primary tool: assemble the code context for a task description, with anchors and a "
            "coverage/confidence report. Start here for any 'where do I change X' question."
        ),
        "schema": _schema(
            {
                "task_text": _STR,
                "task_id": _STR,
                "max_tokens": _INT,
                "max_candidates": _INT,
                "commit": _STR,
                "repo_ids": _STR_LIST,
                "explicit_symbols": _STR_LIST,
                "route_paths": _STR_LIST,
                "history_file_ids": _STR_LIST,
                "component_repo_ids": _STR_LIST,
                "semantic_candidates": _STR_LIST,
                "auto_semantic": _BOOL,
                "locale": _STR,
            },
            ["task_text"],
        ),
    },
    "hybrid_search": {
        "description": (
            "Search indexed code by natural-language query, blending keyword, vector and structural "
            "signals. Use when you need candidate symbols rather than a full context package."
        ),
        "schema": _schema(
            {"query": _STR, "repo_ids": _STR_LIST, "limit": _INT, "locale": _STR}, ["query"]
        ),
    },
    "resolve_symbol": {
        "description": "Resolve a symbol name to its definition and symbol_id (input to the graph tools).",
        "schema": _schema({"name": _STR, "repo_id": _STR, "locale": _STR}, ["name"]),
    },
    "find_references": {
        "description": "Find references (callsites) to a symbol_id, with file, line and provenance.",
        "schema": _schema({"symbol_id": _STR, "locale": _STR}, ["symbol_id"]),
    },
    "get_call_graph": {
        "description": "Callers/callees subgraph around a symbol_id.",
        "schema": _schema(
            {"symbol_id": _STR, "hops": _INT, "direction": _STR, "locale": _STR}, ["symbol_id"]
        ),
    },
    "expand_blast_radius": {
        "description": "Impacted files, repos and symbols if the given target symbols change.",
        "schema": _schema({"target_symbols": _STR_LIST, "locale": _STR}, ["target_symbols"]),
    },
    "index_repo": {
        "description": (
            "Queue a full index of a local repository. Returns a job immediately; poll it with "
            "get_index_job."
        ),
        "schema": _schema(
            {"repo_path": _STR, "name": _STR, "commit": _STR, "locale": _STR}, ["repo_path", "name"]
        ),
    },
    "reindex_repo": {
        "description": (
            "Queue an incremental re-index of a local repository, covering only files changed since "
            "the last indexed commit (git diff). Returns a job."
        ),
        "schema": _schema(
            {
                "repo_path": _STR,
                "name": _STR,
                "since_commit": _STR,
                "to_commit": _STR,
                "locale": _STR,
            },
            ["repo_path", "name"],
        ),
    },
    "get_index_job": {
        "description": "Status and result of one indexing job by job_id.",
        "schema": _schema({"job_id": _STR, "locale": _STR}, ["job_id"]),
    },
    "list_index_jobs": {
        "description": "Indexing jobs, newest first, optionally filtered by status or repository name.",
        "schema": _schema({"status": _STR, "repo": _STR, "limit": _INT, "locale": _STR}, []),
    },
}


def available_tool_specs(*, allow_write: bool) -> dict[str, dict[str, Any]]:
    """The catalog to advertise: write tools are hidden unless ``allow_write`` is set."""
    if allow_write:
        return dict(TOOL_SPECS)
    return {name: spec for name, spec in TOOL_SPECS.items() if name not in WRITE_TOOLS}


def dispatch_tool(
    conn: psycopg.Connection,
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str | None = None,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Route an MCP tool call to the core, using ``conn`` for graph + vector access (single DB).

    Write tools only enqueue jobs, so they leave the transaction dirty; the caller decides whether to
    commit (see :func:`bce.api.mcp.server.build_server`).
    """
    if name not in TOOL_SPECS:
        raise KeyError(f"unknown tool: {name}")
    if name in WRITE_TOOLS and not allow_write:
        raise PermissionError(
            f"tool '{name}' writes to the index and is disabled; "
            "set BCE_MCP_ALLOW_WRITE=true to enable indexing over MCP"
        )

    args = dict(arguments)
    locale = args.pop("locale", None)

    if name in WRITE_TOOLS or name in {"get_index_job", "list_index_jobs"}:
        return _dispatch_job_tool(conn, name, args, locale)

    repository = GraphRepository(GraphClient(conn))
    store = VectorStore(conn)
    scope = (
        ScopeFilter.from_scopes(conn, user_id)
        if user_id is not None
        else ScopeFilter(Principal.system())
    )

    if name == "get_context_for_task":
        return get_context_for_task(
            repository, store=store, scope=scope, locale=locale, **_l3_kwargs(args)
        )
    if name == "hybrid_search":
        return hybrid_search(
            repository,
            store,
            args["query"],
            repo_ids=args.get("repo_ids"),
            limit=args.get("limit", 10),
            locale=locale,
        )
    if name == "resolve_symbol":
        return resolve_symbol(repository, args["name"], repo_id=args.get("repo_id"), locale=locale)
    if name == "find_references":
        return find_references(repository, args["symbol_id"], locale=locale)
    if name == "get_call_graph":
        return get_call_graph(
            repository,
            args["symbol_id"],
            hops=args.get("hops", 1),
            direction=args.get("direction", "both"),
            locale=locale,
        )
    if name == "expand_blast_radius":
        return expand_blast_radius(
            repository, target_symbols=args["target_symbols"], scope=scope, locale=locale
        )
    raise KeyError(f"unhandled tool: {name}")  # pragma: no cover - guarded above


#: MCP write tool -> queue job_type (the job types the worker pool understands).
_JOB_TYPES = {
    "index_repo": "index",
    "reindex_repo": "reindex",
}

#: The only job types an MCP process should run, derived from the tools it exposes so the two cannot
#: drift. Its worker pool claims nothing else, leaving remote clones to the API server.
MCP_JOB_TYPES: tuple[str, ...] = tuple(dict.fromkeys(_JOB_TYPES.values()))


def _job_payload(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Build the queue payload for a write tool."""
    if name == "index_repo":
        return {
            "repo_path": args["repo_path"],
            "name": args["name"],
            "commit": args.get("commit"),
        }
    return {
        "repo_path": args["repo_path"],
        "name": args["name"],
        "since_commit": args.get("since_commit"),
        "to_commit": args.get("to_commit") or "HEAD",
    }


def _dispatch_job_tool(
    conn: psycopg.Connection, name: str, args: dict[str, Any], locale: str | None
) -> dict[str, Any]:
    """Enqueue an indexing job or report on the queue, in the standard envelope."""
    tr = get_translator()

    if name in WRITE_TOOLS:
        job_type = _JOB_TYPES[name]
        job = enqueue_job(conn, job_type, _job_payload(name, args))
        message = tr.translate("tool.job.enqueued", locale, job_id=job["job_id"], job_type=job_type)
        return {"tool": name, "payload": {"job": job}, "message": message, "locale": locale}

    if name == "get_index_job":
        job_id = args["job_id"]
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(tr.translate("error.job_not_found", locale, job_id=job_id))
        message = tr.translate(
            "tool.job.status", locale, job_id=job["job_id"], status=job["status"]
        )
        return {"tool": name, "payload": {"job": job}, "message": message, "locale": locale}

    jobs = list_jobs(
        conn, status=args.get("status"), repo=args.get("repo"), limit=args.get("limit", 50)
    )
    message = tr.translate("tool.job.list", locale, count=len(jobs))
    return {"tool": name, "payload": {"jobs": jobs}, "message": message, "locale": locale}


def _l3_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    """Filter the task-oriented Layer-3 kwargs out of the raw tool arguments."""
    allowed = {
        "task_text",
        "task_id",
        "max_tokens",
        "max_candidates",
        "commit",
        "repo_ids",
        "explicit_symbols",
        "route_paths",
        "history_file_ids",
        "component_repo_ids",
        "semantic_candidates",
        "auto_semantic",
    }
    return {k: v for k, v in args.items() if k in allowed}
