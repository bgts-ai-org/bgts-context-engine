"""MCP tool catalog + dispatcher (SDK-independent).

Declares the agent-facing tool set as MCP-style specs (name, description, JSON input schema) and
maps a tool name + arguments to a call against the deterministic core, using the caller's connection.
This is the single source of truth the SDK server binds to, and it is importable/testable without the
optional ``mcp`` package.

The catalog is deliberately narrower than the REST surface. Every advertised tool's schema is in
the agent's context on every turn, and an agent picks worse when handed overlapping retrieval tools,
so the default toolset (``BCE_MCP_TOOLSET=minimal``) is two tools with short descriptions:
``get_context_for_task`` (the context for a task) and ``inspect_symbol_graph`` (one graph question
about one symbol, asked by name). ``full`` adds the id-based graph tools and ``hybrid_search``; the
rest stay reachable over REST. ``BCE_MCP_TOOLS`` names the tools outright and overrides the toolset.

Indexing tools are writes, so they are gated behind ``allow_write`` (``BCE_MCP_ALLOW_WRITE``) and
enqueue a job rather than indexing inline: a full index outlives any agent's tool-call timeout. They
cover local working trees only. Cloning a remote repository is a deployment task with its own
credentials, not something an editor-spawned agent should reach for, so it stays on REST and the CLI.

Every tool returns the standard envelope ``{tool, payload, message, locale}`` (P1: payload is
language-neutral; only ``message`` is localized). What an MCP client receives is
:func:`result_text`: the envelope as JSON, except for ``get_context_for_task`` in the ``compact`` or
``locate`` view (``BCE_MCP_VIEW``), the Markdown of :mod:`bce.core.render.compact`, and
``inspect_symbol_graph`` outside ``view=debug``, the Markdown of :mod:`bce.core.render.graph`; the
token budget applies to that text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from bce.core.agent_mode import workflow, workflow_text
from bce.core.auth.scope import Principal, ScopeFilter
from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS, SELECTED_MAX_CANDIDATES
from bce.core.i18n import get_translator
from bce.core.render import VIEWS, normalize_view, render_compact
from bce.core.render.graph import render_inspect
from bce.jobs.store import enqueue_job, get_job, list_jobs
from bce.storage.graph.client import GraphClient
from bce.storage.graph.repository import GraphRepository
from bce.storage.relational.queries import normalize_file_ids, resolve_repo_ids
from bce.storage.vector.store import VectorStore
from bce.tools.layer1 import find_references, get_call_graph, resolve_symbol
from bce.tools.layer2 import hybrid_search
from bce.tools.layer3 import expand_blast_radius, get_context_for_task
from bce.tools.layer3.inspect import MODES as INSPECT_MODES
from bce.tools.layer3.inspect import inspect_symbol_graph

logger = logging.getLogger("bce.api.mcp")

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


#: Tools that mutate the graph. Only advertised and dispatchable when ``allow_write`` is set.
WRITE_TOOLS = frozenset({"index_repo", "reindex_repo"})
#: The indexing queue's tools; the minimal toolset advertises them only with ``allow_write``.
JOB_TOOLS = frozenset({"index_repo", "reindex_repo", "get_index_job", "list_index_jobs"})
#: ``BCE_MCP_TOOLSET`` -> the read tools it advertises (``None``: all of them).
TOOLSETS: dict[str, tuple[str, ...] | None] = {
    "minimal": ("get_context_for_task", "inspect_symbol_graph"),
    "full": None,
}
DEFAULT_TOOLSET = "minimal"

#: ``get_context_for_task`` description of the minimal toolset (the answer states its workflow).
CONTEXT_DESCRIPTION_SHORT = (
    "The code context for a task: call it first, once, with the full task text. Markdown: the "
    "primary edit targets as numbered code (`line→code`, the file's own numbers), other symbols, "
    "related files and tests with line ranges, and Gaps - what the answer lacks; search for "
    "those as usual. view=locate: locations only; debug: JSON."
)
#: ``inspect_symbol_graph`` description.
INSPECT_DESCRIPTION = (
    "One code-graph question about a symbol: its definition, callers, callees, references, or "
    "the impact of a change (dependents, tests to run). query: a name, Type.member, path:line, "
    "or for impact a diff or path:L1-L2 list. Answers with code lines; an ambiguous name lists "
    "candidates."
)

#: ``get_context_for_task`` description for the compact (Markdown) answer.
CONTEXT_DESCRIPTION = (
    "Primary tool: the code context for a task description. Start here for any 'where do I "
    "change X' question. The answer is Markdown. '## Primary edit targets' holds the code the "
    "task most likely changes as numbered source lines (`<line>→<code>`, the file's own line "
    "numbers and indentation, as a file read prints them), each under a heading "
    "`path:start-end · kind name · edit|dependency · state · hash`; state is complete (the whole "
    "declaration), partial (windows of a long one around what the task names; `⋯ lines a-b "
    "omitted` marks the gaps), skeleton (a class's members with their line ranges) or truncated. "
    "'## Other symbols in primary files' lists further symbols of those files with their ranges; "
    "'## Related files' names files probably involved, without code; '## Related tests' the "
    "existing tests of the edit targets with their ranges; '## Gaps' lists what the engine knows "
    "the answer does not cover (an identifier the task names with the indexed files defining it, "
    "names or files the index does not have, missing tests) - search for those as usual. "
    "view=debug returns the full JSON envelope instead."
)
#: ``get_context_for_task`` description for the JSON (debug) answer.
CONTEXT_DESCRIPTION_JSON = (
    "Primary tool: assemble the code context for a task description, with a "
    "coverage/confidence report. Start here for any 'where do I change X' question. "
    "payload.files lists every file of the answer (path, tier full|stub, relevance, "
    "why); payload.context.items carry the symbols and their content; payload.evidence the "
    "numbered code spans; payload.tests the existing tests of the edit targets; "
    "payload.candidates are further ranked files the selector left out. "
    "coverage.unresolved_identifiers names identifiers from the task the answer does "
    "not cover, each with the indexed file defining it - open those directly; coverage.gaps "
    "lists what the index does not have (unknown names, unindexed files, missing tests) and "
    "coverage.state is unknown when the task reaches outside the index."
)

# Tool catalog: name -> (description, input schema).
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_context_for_task": {
        "description": CONTEXT_DESCRIPTION,
        "schema": _schema(
            {
                "task_text": {
                    **_STR,
                    "description": "The task in prose; pass the full request text, once.",
                },
                "task_id": _STR,
                "max_tokens": {
                    **_INT,
                    "default": DEFAULT_MAX_TOKENS,
                    "description": (
                        "Token budget of the whole answer text (code, lists and gaps). Leave "
                        "it unset unless the answer is too short for the task."
                    ),
                },
                "view": {
                    **_STR,
                    "enum": list(VIEWS),
                    "description": (
                        "compact (default): the Markdown answer; locate: the same without code; "
                        "debug: the full JSON envelope."
                    ),
                },
                "max_candidates": {
                    **_INT,
                    "description": (
                        f"How many ranked symbols to consider (K). Omit it: the server uses "
                        f"{SELECTED_MAX_CANDIDATES} when its context selector cuts the answer "
                        f"down, {DEFAULT_MAX_CANDIDATES} otherwise."
                    ),
                },
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
    "inspect_symbol_graph": {
        "description": INSPECT_DESCRIPTION,
        "schema": _schema(
            {
                "query": _STR,
                "mode": {**_STR, "enum": list(INSPECT_MODES), "default": "definition"},
                "repo_ids": _STR_LIST,
                "max_tokens": _INT,
            },
            ["query"],
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


#: ``get_context_for_task`` description of the minimal toolset when the server answers in JSON.
CONTEXT_DESCRIPTION_SHORT_JSON = (
    "The code context for a task: call it first, once, with the full task text. JSON: "
    "payload.files, payload.evidence (numbered code spans with ranges), payload.tests, and "
    "coverage.gaps - what the answer lacks; search for those as usual."
)


def normalize_toolset(value: str | None) -> str:
    """The toolset named by ``value`` (empty means :data:`DEFAULT_TOOLSET`); unknown raises."""
    name = (value or "").strip().lower() or DEFAULT_TOOLSET
    if name not in TOOLSETS:
        raise ValueError(
            f"BCE_MCP_TOOLSET={value!r} is not a toolset; known: {', '.join(TOOLSETS)}"
        )
    return name


def _minimal_context_spec(view: str | None) -> dict[str, Any]:
    compact = normalize_view(view) != "debug"
    return {
        "description": CONTEXT_DESCRIPTION_SHORT if compact else CONTEXT_DESCRIPTION_SHORT_JSON,
        "schema": _schema(
            {
                "task_text": _STR,
                "max_tokens": _INT,
                "view": {**_STR, "enum": list(VIEWS)},
                "repo_ids": _STR_LIST,
            },
            ["task_text"],
        ),
    }


def available_tool_specs(
    *,
    allow_write: bool,
    allowlist: frozenset[str] | None = None,
    agent_mode: str | None = None,
    view: str | None = None,
    toolset: str = "full",
) -> dict[str, dict[str, Any]]:
    """The catalog to advertise.

    ``toolset`` (``BCE_MCP_TOOLSET``) picks the read tools: ``minimal`` is
    ``get_context_for_task`` and ``inspect_symbol_graph`` with short descriptions and only the
    arguments an agent sets (the dispatcher still accepts the rest), plus the indexing tools when
    ``allow_write`` is set; ``full`` is every tool, write tools only with ``allow_write``. An
    ``allowlist`` (``BCE_MCP_TOOLS``) names the tools outright instead; unknown names in it raise,
    so a typo does not silently publish nothing. Outside the minimal toolset, with ``agent_mode``
    (``BCE_AGENT_MODE``) the ``get_context_for_task`` description ends with that mode's workflow,
    worded for the answer ``view`` the server returns by default (``BCE_MCP_VIEW``); the minimal
    one leaves it to the answer and the server instructions.
    """
    minimal = False
    if allowlist is not None:
        unknown = sorted(allowlist - set(TOOL_SPECS))
        if unknown:
            raise ValueError(
                f"BCE_MCP_TOOLS names unknown tools {unknown}; known: {sorted(TOOL_SPECS)}"
            )
        names = set(allowlist)
    else:
        chosen = TOOLSETS[normalize_toolset(toolset)]
        minimal = chosen is not None
        names = (
            set(TOOL_SPECS)
            if chosen is None
            else set(chosen) | (JOB_TOOLS if allow_write else set())
        )
    specs = {
        name: spec
        for name, spec in TOOL_SPECS.items()
        if (allow_write or name not in WRITE_TOOLS) and name in names
    }
    if "get_context_for_task" in specs and minimal:
        specs["get_context_for_task"] = _minimal_context_spec(view)
    elif "get_context_for_task" in specs and (agent_mode is not None or view is not None):
        spec = specs["get_context_for_task"]
        compact = normalize_view(view) != "debug"
        desc = CONTEXT_DESCRIPTION if compact else CONTEXT_DESCRIPTION_JSON
        if agent_mode is not None:
            style = (
                "json"
                if not compact
                else ("locate" if normalize_view(view) == "locate" else "compact")
            )
            desc = f"{desc}\n\n{workflow_text(agent_mode, style=style)}"
        specs["get_context_for_task"] = {**spec, "description": desc}
    return specs


def result_text(
    name: str,
    result: dict[str, Any],
    *,
    view: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """The text content an MCP client receives for ``result``.

    ``get_context_for_task`` in the ``compact`` / ``locate`` view and ``inspect_symbol_graph``
    outside ``view=debug`` are Markdown, ``max_tokens`` applied to it; every other result is the
    envelope as JSON.
    """
    payload = result.get("payload")
    shown = normalize_view(view)
    if isinstance(payload, dict) and shown != "debug" and name in RENDERED_TOOLS:
        try:
            if name == "inspect_symbol_graph":
                return render_inspect(payload, max_tokens=max_tokens)
            return render_compact(payload, max_tokens=max_tokens, view=shown)
        except Exception:  # noqa: BLE001 - a rendering fault must not cost the agent its answer
            logger.exception("Markdown rendering failed; answering with the JSON envelope")
    return json.dumps(result, ensure_ascii=False, default=str)


#: Tools whose MCP answer is Markdown outside ``view=debug``.
RENDERED_TOOLS = frozenset({"get_context_for_task", "inspect_symbol_graph"})


def dispatch_tool(
    conn: psycopg.Connection,
    name: str,
    arguments: dict[str, Any],
    *,
    user_id: str | None = None,
    allow_write: bool = False,
    agent_mode: str | None = None,
) -> dict[str, Any]:
    """Route an MCP tool call to the core, using ``conn`` for graph + vector access (single DB).

    Write tools only enqueue jobs, so they leave the transaction dirty; the caller decides whether to
    commit (see :func:`bce.api.mcp.server.build_server`). With ``agent_mode`` a
    ``get_context_for_task`` answer carries that mode's ``payload.workflow``.
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
    # Agents name repositories the way they see them; the index stores the id it was given.
    for key in ("repo_ids", "component_repo_ids"):
        if args.get(key):
            args[key] = resolve_repo_ids(conn, list(args[key]))
    if args.get("repo_id"):
        args["repo_id"] = (resolve_repo_ids(conn, [str(args["repo_id"])]) or [args["repo_id"]])[0]
    if args.get("history_file_ids"):
        args["history_file_ids"] = normalize_file_ids(
            list(args["history_file_ids"]), args.get("repo_ids")
        )

    if name == "get_context_for_task":
        result = _compact_context(
            get_context_for_task(
                repository, store=store, scope=scope, locale=locale, **_l3_kwargs(args)
            )
        )
        payload = result.get("payload")
        if agent_mode is not None and isinstance(payload, dict):
            payload["workflow"] = workflow(agent_mode)
        return result
    if name == "inspect_symbol_graph":
        return inspect_symbol_graph(
            repository,
            query=str(args.get("query") or ""),
            mode=str(args.get("mode") or "definition"),
            repo_ids=args.get("repo_ids"),
            scope=scope,
            locale=locale,
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


def _compact_context(result: dict[str, Any]) -> dict[str, Any]:
    """Trim the ``get_context_for_task`` envelope for an agent's context window.

    The ``anchors`` map (every anchor symbol id -> its sources) is diagnostic: it was 40-60 % of the
    answer on large repositories and pushed it past the ~40 KB at which Cursor stops inlining a
    tool result and writes it to a file the agent must then open. ``coverage.anchor_count`` and
    ``anchor_sources`` keep the summary; the REST API still returns the full map.
    """
    payload = result.get("payload")
    if isinstance(payload, dict) and "anchors" in payload:
        anchors = payload.pop("anchors")
        payload["anchor_count"] = len(anchors) if isinstance(anchors, dict) else None
    return result


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
