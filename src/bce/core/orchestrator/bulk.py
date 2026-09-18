"""Bulk graph reads with a per-symbol fallback.

Anchor finding and expansion touch hundreds to thousands of symbols per task. Asking the graph
once per symbol turned a retrieval into tens of seconds of round-trips, so every hot read here
prefers the repository's batched method and falls back to the single-symbol one when it is absent
(unit-test fakes) or unimplemented. The fallbacks return exactly the same shapes, so callers never
branch on which path ran.
"""

from __future__ import annotations

from typing import Any

from bce.storage.graph.repository import GraphRepository


def _bulk(repository: GraphRepository, name: str) -> Any | None:
    method = getattr(repository, name, None)
    return method if callable(method) else None


def neighbours(
    repository: GraphRepository, ids: list[str], bulk_name: str, single_name: str
) -> dict[str, list[dict[str, Any]]]:
    """``symbol_id -> neighbour rows`` for one edge channel."""
    if not ids:
        return {}
    bulk = _bulk(repository, bulk_name)
    if bulk is not None:
        try:
            return bulk(ids)
        except NotImplementedError:
            pass
    single = getattr(repository, single_name)
    return {sid: single(sid) for sid in ids}


def symbols_meta(repository: GraphRepository, ids: list[str]) -> dict[str, dict[str, Any]]:
    """``symbol_id -> {name, kind, file_id, line}`` (missing symbols omitted)."""
    if not ids:
        return {}
    bulk = _bulk(repository, "symbols_meta")
    if bulk is not None:
        try:
            return bulk(ids)
        except NotImplementedError:
            pass
    single = _bulk(repository, "get_symbol")
    if single is None:
        # A minimal repository (route-only fakes, scope probes) simply has no metadata to give.
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sid in ids:
        sym = single(sid)
        if sym is not None:
            out[sid] = sym
    return out


def symbols_in_files(
    repository: GraphRepository, file_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """``file_id -> symbols defined in it``, in (line, symbol_id) order."""
    if not file_ids:
        return {}
    bulk = _bulk(repository, "symbols_in_files")
    if bulk is not None:
        try:
            return bulk(file_ids)
        except NotImplementedError:
            pass
    return {fid: repository.symbols_in_file(fid) for fid in file_ids}


def repos_of_symbols(repository: GraphRepository, ids: list[str]) -> dict[str, str | None]:
    """``symbol_id -> repo_id`` (the scope filter reads it for every candidate)."""
    if not ids:
        return {}
    bulk = _bulk(repository, "repos_of_symbols")
    if bulk is not None:
        try:
            return dict(bulk(ids))
        except NotImplementedError:
            pass
    return {sid: repository.repo_of_symbol(sid) for sid in ids}


def resolve_names(repository: GraphRepository, names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """``name -> matching symbol rows`` (exact name match, ordered by symbol_id)."""
    if not names:
        return {}
    bulk = _bulk(repository, "resolve_symbols")
    if bulk is not None:
        try:
            return bulk(names)
        except NotImplementedError:
            pass
    return {name: repository.resolve_symbol(name, None) for name in names}


def lexical_hits(
    repository: GraphRepository,
    terms: list[str],
    *,
    repo_ids: list[str] | None,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    """``term -> best ``limit`` keyword matches``, one query for all terms when supported."""
    if not terms:
        return {}
    bulk = _bulk(repository, "lexical_search_many")
    if bulk is not None:
        try:
            return bulk(terms, repo_ids=repo_ids, limit=limit)
        except NotImplementedError:
            pass
    return {term: repository.lexical_search(term, repo_ids=repo_ids, limit=limit) for term in terms}
