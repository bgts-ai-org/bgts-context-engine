"""MCP tool catalog + dispatcher (SDK-independent).

Declares every Layer 1-2-3 tool as an MCP-style spec (name, description, JSON input schema) and
maps a tool name + arguments to a call against the deterministic core, using the caller's connection.
This is the single source of truth the SDK server binds to, and it is importable/testable without the
optional ``mcp`` package.

Every tool returns the standard envelope ``{tool, payload, message, locale}`` (P1: payload is
language-neutral; only ``message`` is localized).
"""

from __future__ import annotations

from typing import Any

import psycopg

from cce.core.auth.scope import Principal, ScopeFilter
from cce.storage.graph.client import GraphClient
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore
from cce.tools.layer1 import (
    find_implementers,
    find_references,
    get_call_graph,
    get_dependencies,
    get_type_hierarchy,
    resolve_symbol,
)
from cce.tools.layer2 import find_similar_code, hybrid_search, semantic_search
from cce.tools.layer3 import (
    assemble_context,
    expand_blast_radius,
    get_context_for_task,
    select_repos,
    suggest_change_sites,
)

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_INT = {"type": "integer"}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


# Tool catalog: name -> (description, input schema). Mirrors the REST surface 1:1.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "resolve_symbol": {
        "description": "Layer 1: resolve a symbol by name to its definition + symbol_id.",
        "schema": _schema({"name": _STR, "repo_id": _STR, "locale": _STR}, ["name"]),
    },
    "find_references": {
        "description": "Layer 1: find references (callsites) to a symbol_id.",
        "schema": _schema({"symbol_id": _STR, "locale": _STR}, ["symbol_id"]),
    },
    "find_implementers": {
        "description": "Layer 1: types that inherit/implement a type symbol.",
        "schema": _schema({"symbol_id": _STR, "locale": _STR}, ["symbol_id"]),
    },
    "get_call_graph": {
        "description": "Layer 1: callers/callees subgraph around a symbol.",
        "schema": _schema(
            {"symbol_id": _STR, "hops": _INT, "direction": _STR, "locale": _STR}, ["symbol_id"]
        ),
    },
    "get_dependencies": {
        "description": "Layer 1: IMPORTS edges out of a file (optionally transitive).",
        "schema": _schema(
            {"file_id": _STR, "transitive": {"type": "boolean"}, "locale": _STR}, ["file_id"]
        ),
    },
    "get_type_hierarchy": {
        "description": "Layer 1: super/sub types of a type symbol.",
        "schema": _schema({"symbol_id": _STR, "locale": _STR}, ["symbol_id"]),
    },
    "semantic_search": {
        "description": "Layer 2: nearest symbols for a natural-language query (anchor finding).",
        "schema": _schema(
            {"query": _STR, "repo_ids": _STR_LIST, "limit": _INT, "locale": _STR}, ["query"]
        ),
    },
    "hybrid_search": {
        "description": "Layer 2: keyword + semantic + structural blended search.",
        "schema": _schema(
            {"query": _STR, "repo_ids": _STR_LIST, "limit": _INT, "locale": _STR}, ["query"]
        ),
    },
    "find_similar_code": {
        "description": "Layer 2: nearest symbols to a code fragment.",
        "schema": _schema(
            {"code": _STR, "repo_ids": _STR_LIST, "limit": _INT, "locale": _STR}, ["code"]
        ),
    },
    "get_context_for_task": {
        "description": "Layer 3: assembled context package + coverage for a task.",
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
                "locale": _STR,
            },
            ["task_text"],
        ),
    },
    "suggest_change_sites": {
        "description": "Layer 3: scored change-site candidates for a task.",
        "schema": _schema(
            {"task_text": _STR, "max_candidates": _INT, "commit": _STR, "locale": _STR},
            ["task_text"],
        ),
    },
    "expand_blast_radius": {
        "description": "Layer 3: impacted files/repos/symbols for target symbols.",
        "schema": _schema({"target_symbols": _STR_LIST, "locale": _STR}, ["target_symbols"]),
    },
    "select_repos": {
        "description": "Layer 3: candidate repo set for a task.",
        "schema": _schema({"task_text": _STR, "locale": _STR}, ["task_text"]),
    },
    "assemble_context": {
        "description": "Layer 3: dedup a symbol list and fit it into a token budget.",
        "schema": _schema({"symbol_ids": _STR_LIST, "max_tokens": _INT, "locale": _STR}, ["symbol_ids"]),
    },
}


def dispatch_tool(
    conn: psycopg.Connection,
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Route an MCP tool call to the core, using ``conn`` for graph + vector access (single DB)."""
    if name not in TOOL_SPECS:
        raise KeyError(f"unknown tool: {name}")

    repository = GraphRepository(GraphClient(conn))
    store = VectorStore(conn)
    args = dict(arguments)
    locale = args.pop("locale", None)
    scope = (
        ScopeFilter.from_scopes(conn, user_id)
        if user_id is not None
        else ScopeFilter(Principal.system())
    )

    if name == "resolve_symbol":
        return resolve_symbol(repository, args["name"], repo_id=args.get("repo_id"), locale=locale)
    if name == "find_references":
        return find_references(repository, args["symbol_id"], locale=locale)
    if name == "find_implementers":
        return find_implementers(repository, args["symbol_id"], locale=locale)
    if name == "get_call_graph":
        return get_call_graph(
            repository,
            args["symbol_id"],
            hops=args.get("hops", 1),
            direction=args.get("direction", "both"),
            locale=locale,
        )
    if name == "get_dependencies":
        return get_dependencies(
            repository, args["file_id"], transitive=args.get("transitive", False), locale=locale
        )
    if name == "get_type_hierarchy":
        return get_type_hierarchy(repository, args["symbol_id"], locale=locale)
    if name == "semantic_search":
        return semantic_search(
            store, args["query"], repo_ids=args.get("repo_ids"),
            limit=args.get("limit", 10), locale=locale,
        )
    if name == "hybrid_search":
        return hybrid_search(
            repository, store, args["query"], repo_ids=args.get("repo_ids"),
            limit=args.get("limit", 10), locale=locale,
        )
    if name == "find_similar_code":
        return find_similar_code(
            store, args["code"], repo_ids=args.get("repo_ids"),
            limit=args.get("limit", 10), locale=locale,
        )
    if name == "get_context_for_task":
        return get_context_for_task(repository, scope=scope, locale=locale, **_l3_kwargs(args))
    if name == "suggest_change_sites":
        return suggest_change_sites(repository, scope=scope, locale=locale, **_l3_kwargs(args))
    if name == "expand_blast_radius":
        return expand_blast_radius(
            repository, target_symbols=args["target_symbols"], scope=scope, locale=locale
        )
    if name == "select_repos":
        return select_repos(
            repository,
            task_text=args["task_text"],
            component_repo_ids=args.get("component_repo_ids"),
            history_file_ids=args.get("history_file_ids"),
            scope=scope,
            locale=locale,
        )
    if name == "assemble_context":
        return assemble_context(
            repository, symbol_ids=args["symbol_ids"],
            max_tokens=args.get("max_tokens", 4000), scope=scope, locale=locale,
        )
    raise KeyError(f"unhandled tool: {name}")  # pragma: no cover - guarded above


def _l3_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    """Filter task-oriented Layer-3 kwargs (shared by context/change-sites)."""
    allowed = {
        "task_text", "task_id", "max_tokens", "max_candidates", "commit", "repo_ids",
        "explicit_symbols", "route_paths", "history_file_ids", "component_repo_ids",
        "semantic_candidates",
    }
    return {k: v for k, v in args.items() if k in allowed}
