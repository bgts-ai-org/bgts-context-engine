"""Layer-1 deterministic graph primitives (spec section 7).

The six tools below are thin, deterministic wrappers over the graph repository:
``resolve_symbol``, ``find_references``, ``find_implementers``, ``get_call_graph``,
``get_dependencies``, ``get_type_hierarchy``.

Each tool returns a dict with:
- ``payload``: the deterministic, language-neutral result (symbols, references, ids, commit).
- ``message``: an optional human-readable, localized string (i18n - presentation only).
- ``locale``: the resolved locale used for ``message``.

The ``payload`` is byte-identical regardless of ``locale``; only ``message`` changes. This is the
i18n boundary that keeps P1 intact.
"""

from __future__ import annotations

from typing import Any

from bce.core.i18n import get_translator
from bce.storage.graph.repository import GraphRepository


def resolve_symbol(
    repository: GraphRepository,
    name: str,
    repo_id: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    matches = repository.resolve_symbol(name, repo_id)
    tr = get_translator()
    loc = tr.resolve(locale)

    if matches:
        message = None
        commit = matches[0].get("indexed_at_commit")
    else:
        message = tr.translate("error.symbol_not_found", loc, name=name, scope=repo_id or "*")
        commit = None

    return {
        "tool": "resolve_symbol",
        "payload": {"matches": matches, "indexed_at_commit": commit},
        "message": message,
        "locale": loc,
    }


def find_references(
    repository: GraphRepository,
    symbol_id: str,
    locale: str | None = None,
) -> dict[str, Any]:
    references = repository.find_references(symbol_id)
    tr = get_translator()
    loc = tr.resolve(locale)
    message = tr.translate("tool.references.count", loc, count=len(references), name=symbol_id)

    return {
        "tool": "find_references",
        "payload": {"symbol_id": symbol_id, "references": references},
        "message": message,
        "locale": loc,
    }


def find_implementers(
    repository: GraphRepository,
    symbol_id: str,
    locale: str | None = None,
) -> dict[str, Any]:
    """Types that inherit/implement the given type symbol (may be cross-repo)."""
    implementers = repository.find_implementers(symbol_id)
    tr = get_translator()
    loc = tr.resolve(locale)
    message = tr.translate(
        "tool.implementers.count", loc, count=len(implementers), name=symbol_id
    )
    return {
        "tool": "find_implementers",
        "payload": {"symbol_id": symbol_id, "implementers": implementers},
        "message": message,
        "locale": loc,
    }


def get_call_graph(
    repository: GraphRepository,
    symbol_id: str,
    hops: int = 1,
    direction: str = "both",
    locale: str | None = None,
) -> dict[str, Any]:
    """Callers/callees subgraph around a symbol up to ``hops`` (bounded BFS, deterministic).

    ``direction`` is one of ``callers``, ``callees`` or ``both``. The traversal is a fixed-order,
    visited-set BFS so the same input always yields the same subgraph (spec section 6.3).
    """
    tr = get_translator()
    loc = tr.resolve(locale)
    hops = max(1, min(hops, 10))
    direction = direction if direction in ("callers", "callees", "both") else "both"

    callers = _bfs_calls(repository, symbol_id, hops, repository.get_callers) \
        if direction in ("callers", "both") else []
    callees = _bfs_calls(repository, symbol_id, hops, repository.get_callees) \
        if direction in ("callees", "both") else []

    message = tr.translate(
        "tool.call_graph.summary",
        loc,
        callers=len(callers),
        callees=len(callees),
        name=symbol_id,
    )
    return {
        "tool": "get_call_graph",
        "payload": {
            "symbol_id": symbol_id,
            "hops": hops,
            "direction": direction,
            "callers": callers,
            "callees": callees,
        },
        "message": message,
        "locale": loc,
    }


def _bfs_calls(repository, root_id, hops, neighbors):
    """Deterministic bounded BFS over CALLS edges; returns ``[{symbol, depth}]`` sorted stably."""
    visited: set[str] = {root_id}
    out: list[dict[str, Any]] = []
    frontier = [root_id]
    for depth in range(1, hops + 1):
        next_frontier: list[str] = []
        for node_id in sorted(frontier):
            for neigh in neighbors(node_id):
                nid = neigh.get("symbol_id")
                if not nid or nid in visited:
                    continue
                visited.add(nid)
                out.append({"symbol": neigh, "depth": depth})
                next_frontier.append(nid)
        frontier = next_frontier
        if not frontier:
            break
    out.sort(key=lambda item: (item["depth"], item["symbol"].get("symbol_id") or ""))
    return out


def get_dependencies(
    repository: GraphRepository,
    file_id: str,
    transitive: bool = False,
    locale: str | None = None,
) -> dict[str, Any]:
    """IMPORTS edges out of a file. ``transitive`` follows module->file imports where resolvable."""
    tr = get_translator()
    loc = tr.resolve(locale)

    direct = repository.get_file_imports(file_id)
    dependencies = list(direct)
    if transitive:
        seen = {d.get("target_id") for d in direct}
        frontier = [d.get("target_id") for d in direct if d.get("target_id")]
        while frontier:
            current = sorted(t for t in frontier if t)
            frontier = []
            for target in current:
                for dep in repository.get_file_imports(target):
                    tid = dep.get("target_id")
                    if tid and tid not in seen:
                        seen.add(tid)
                        dependencies.append(dep)
                        frontier.append(tid)

    dependencies.sort(key=lambda d: d.get("target_id") or "")
    message = tr.translate(
        "tool.dependencies.count", loc, count=len(dependencies), file=file_id
    )
    return {
        "tool": "get_dependencies",
        "payload": {
            "file_id": file_id,
            "transitive": transitive,
            "dependencies": dependencies,
        },
        "message": message,
        "locale": loc,
    }


def get_type_hierarchy(
    repository: GraphRepository,
    symbol_id: str,
    locale: str | None = None,
) -> dict[str, Any]:
    """Direct super/sub types of a type symbol (INHERITS/IMPLEMENTS both directions)."""
    tr = get_translator()
    loc = tr.resolve(locale)
    supertypes = repository.get_supertypes(symbol_id)
    subtypes = repository.get_subtypes(symbol_id)
    message = tr.translate(
        "tool.type_hierarchy.summary",
        loc,
        supers=len(supertypes),
        subs=len(subtypes),
        name=symbol_id,
    )
    return {
        "tool": "get_type_hierarchy",
        "payload": {
            "symbol_id": symbol_id,
            "supertypes": supertypes,
            "subtypes": subtypes,
        },
        "message": message,
        "locale": loc,
    }
