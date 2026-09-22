"""Layer-3 task-aware orchestration tools (spec section 7).

These compose the deterministic core (orchestrator + scoring + coverage + assembler + scope filter)
into task-level answers. They never call an LLM (P1); the only probabilistic input is the semantic
anchor, which is optional and pre-computed (P2). Every result carries a coverage object (section 8)
so the consumer can proceed / widen / ask a human.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bce.core.assembler import assemble
from bce.core.auth.scope import ScopeFilter
from bce.core.coverage import compute_coverage, coverage_message
from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS
from bce.core.i18n import get_translator
from bce.core.orchestrator import RetrievalOrchestrator, bulk
from bce.core.orchestrator.anchors import AnchorResult, find_anchors
from bce.core.orchestrator.profile import RetrievalProfile, active_profile
from bce.core.orchestrator.text import is_test_symbol
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore

logger = logging.getLogger("bce.tools.layer3")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


#: How many nearest non-test symbols the automatic semantic anchor pulls in (D1; opt-out via
#: auto_semantic). Wider than the answer itself: narrowing reserves slots for the model's best
#: ranks, and the rest of the pool is what the graph stages get to re-rank.
AUTO_SEMANTIC_LIMIT = 30
#: Over-fetch factor: test functions usually dominate the raw nearest neighbours of a task
#: description (they literally spell it out), so fetch more and keep the first non-test hits.
_AUTO_SEMANTIC_OVERFETCH = 4


def _auto_semantic_or_none(
    tool: str,
    store: VectorStore,
    task_text: str,
    repo_ids: list[str] | None,
    repository: GraphRepository | None,
) -> list[str] | None:
    """:func:`_auto_semantic_candidates`, degraded to ``None`` when the embedding server cannot be
    reached. The semantic anchor is one of several signals; losing it lowers coverage/confidence
    (which the caller sees in the report) but the lexical and structural anchors still produce an
    answer. Failing the whole tool call instead would leave the agent with nothing, after having
    waited on a stalled network."""
    try:
        return _auto_semantic_candidates(store, task_text, repo_ids, repository)
    except Exception as exc:
        logger.warning(
            "%s: semantic anchor skipped, embedding query failed", tool, extra={"detail": str(exc)}
        )
        return None


def _auto_semantic_candidates(
    store: VectorStore,
    task_text: str,
    repo_ids: list[str] | None,
    repository: GraphRepository | None = None,
    *,
    include_tests: bool = False,
) -> list[str]:
    """Nearest non-test symbol_ids for the task text, in (distance, ref_id) order (deterministic
    given the pinned encoder + snapshot). Used only when the caller did not supply
    semantic_candidates. Test symbols are skipped (their file/name is checked via the repository
    when available, else from the symbol id) unless ``include_tests``."""
    from bce.indexing.embedder.encoder import build_default_encoder

    vector = build_default_encoder().encode_query(task_text)
    limit = AUTO_SEMANTIC_LIMIT * (1 if include_tests else _AUTO_SEMANTIC_OVERFETCH)
    hits = store.search(vector, limit=limit, repo_ids=repo_ids, kind="symbol")

    ids = [hit["ref_id"] for hit in hits]
    meta = (
        bulk.symbols_meta(repository, sorted(set(ids)))
        if repository is not None and not include_tests
        else {}
    )
    out: list[str] = []
    for sid in ids:
        if not include_tests:
            sym = meta.get(sid) or {}
            if is_test_symbol(sid, sym.get("name"), sym.get("file_id")):
                continue
        out.append(sid)
        if len(out) >= AUTO_SEMANTIC_LIMIT:
            break
    return out


def _build_anchors(
    repository: GraphRepository,
    *,
    task_text: str,
    explicit_symbols: list[str] | None,
    route_paths: list[str] | None,
    history_file_ids: list[str] | None,
    component_repo_ids: list[str] | None,
    repo_ids: list[str] | None,
    semantic_candidates: list[str] | None,
    profile: RetrievalProfile | None = None,
) -> AnchorResult:
    return find_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
        profile=profile,
    )


def get_context_for_task(
    repository: GraphRepository,
    *,
    task_text: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    store: VectorStore | None = None,
    auto_semantic: bool = True,
    scope: ScopeFilter | None = None,
    task_id: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """End-to-end: task -> anchors -> expand -> score -> filter -> assemble + coverage (section 6)."""
    tr = get_translator()
    loc = tr.resolve(locale)
    t0 = time.perf_counter()
    # Engine constants fitted for the configured embedding model (voyage / jina / default).
    profile = active_profile()

    # D1: automatic semantic anchor when the caller did not pre-compute one (opt-out: auto_semantic).
    if semantic_candidates is None and auto_semantic and store is not None:
        stage = time.perf_counter()
        semantic_candidates = _auto_semantic_or_none(
            "get_context_for_task", store, task_text, repo_ids, repository
        )
        logger.debug(
            "get_context_for_task: auto semantic anchors",
            extra={"count": len(semantic_candidates or []), "duration_ms": _elapsed_ms(stage)},
        )

    stage = time.perf_counter()
    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
        profile=profile,
    )
    logger.debug(
        "get_context_for_task: anchors resolved",
        extra={
            "anchor_count": len(anchors.anchor_ids),
            "strong_anchors": len(anchors.strong_ids(0.6)),
            "duration_ms": _elapsed_ms(stage),
        },
    )

    # Single pass: task signals are computed for every expanded candidate inside retrieve().
    orchestrator = RetrievalOrchestrator(repository, profile=profile)
    stage = time.perf_counter()
    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_text=task_text,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
    )
    logger.debug(
        "get_context_for_task: retrieval",
        extra={
            "pool": result.pool_size,
            "candidates": len(result.candidates),
            "task_signals": len(result.task_signals),
            "duration_ms": _elapsed_ms(stage),
        },
    )

    stage = time.perf_counter()
    items = _enrich_items(repository, result.candidates, with_body=True)
    package = assemble(items, max_tokens=max_tokens)
    coverage = compute_coverage(result, repository)
    logger.debug(
        "get_context_for_task: assembled",
        extra={
            "included": package.get("included"),
            "confidence": (coverage or {}).get("confidence"),
            "duration_ms": _elapsed_ms(stage),
            "total_ms": _elapsed_ms(t0),
        },
    )

    message = coverage_message(coverage, loc)
    return {
        "tool": "get_context_for_task",
        "payload": {
            "commit": commit,
            "task_id": task_id,
            "anchors": result.anchors.anchors,
            "context": package,
            "coverage": coverage,
        },
        "message": message,
        "locale": loc,
    }


def _enrich_items(
    repository: GraphRepository, candidates: list, *, with_body: bool = False
) -> list[dict[str, Any]]:
    """Attach symbol metadata + design notes to ranked candidates, preserving rank order.

    ``with_body`` also loads ``docstring`` / ``body`` (via ``get_symbol_detail`` when the
    repository offers it) so the assembler can render near items at FULL detail (section 6.5).
    """
    detail_getter = getattr(repository, "get_symbol_detail", None) if with_body else None
    items: list[dict[str, Any]] = []
    for cand in candidates:
        sym = None
        if callable(detail_getter):
            sym = detail_getter(cand.symbol_id)
        if sym is None:
            sym = repository.get_symbol(cand.symbol_id) or {}
        items.append(
            {
                "symbol_id": cand.symbol_id,
                "repo_id": cand.repo_id,
                "name": sym.get("name"),
                "kind": sym.get("kind"),
                "signature": sym.get("signature"),
                "docstring": sym.get("docstring"),
                "body": sym.get("body"),
                "file_id": sym.get("file_id"),
                "line": sym.get("line"),
                "graph_distance": cand.graph_distance,
                "is_anchor": cand.anchor,
                "anchor_strength": round(cand.effective_anchor_strength, 6),
                "is_test": cand.is_test,
                "score": cand.score,
                "features": cand.features,
                "design_notes": repository.get_design_notes(cand.symbol_id),
            }
        )
    return items


def expand_blast_radius(
    repository: GraphRepository,
    *,
    target_symbols: list[str],
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Impacted surface (files/repos/symbols) reachable from the targets, deterministically.

    Impact channels (D2): incoming CALLS (callers), incoming REFERENCES (referrers), subtype
    impact (INHERITS/IMPLEMENTS), and file-level IMPORTS (files importing the target's file).
    The payload carries a per-edge-type breakdown.
    """
    tr = get_translator()
    loc = tr.resolve(locale)

    impacted_symbols: set[str] = set()
    impacted_files: set[str] = set()
    impacted_repos: set[str] = set()
    # First edge type that reached each newly impacted symbol/file (deterministic given the
    # fixed channel order below), so the breakdown never double-counts.
    edge_counts = {"CALLS": 0, "REFERENCES": 0, "INHERITS_IMPLEMENTS": 0, "IMPORTS": 0}

    visited = set(target_symbols)
    frontier = sorted(set(target_symbols))
    for _ in range(3):  # fixed depth (spec: 2-hop callers + type impact); bounded for determinism.
        next_frontier: list[str] = []
        for sid in frontier:
            neighbours = (
                [("CALLS", n) for n in repository.get_callers(sid)]
                + [("REFERENCES", n) for n in repository.get_referrers(sid)]
                + [("INHERITS_IMPLEMENTS", n) for n in repository.get_subtypes(sid)]
            )
            for edge_type, neighbour in neighbours:
                cid = neighbour.get("symbol_id")
                if not cid or cid in visited:
                    continue
                repo_id = repository.repo_of_symbol(cid)
                if scope is not None and not scope.allows(repo_id):
                    continue
                visited.add(cid)
                impacted_symbols.add(cid)
                edge_counts[edge_type] += 1
                if neighbour.get("file_id"):
                    impacted_files.add(neighbour["file_id"])
                if repo_id:
                    impacted_repos.add(repo_id)
                next_frontier.append(cid)
        frontier = sorted(next_frontier)
        if not frontier:
            break

    # File-level IMPORTS impact: every file importing a target's file is potentially affected.
    target_files = sorted(
        {
            fid
            for fid in (
                (repository.get_symbol(sid) or {}).get("file_id")
                for sid in sorted(set(target_symbols))
            )
            if fid
        }
    )
    for fid in target_files:
        for importer in repository.file_importers(fid):
            ifid = importer.get("file_id")
            irepo = importer.get("repo_id")
            if not ifid:
                continue
            if scope is not None and not scope.allows(irepo):
                continue
            if ifid not in impacted_files:
                impacted_files.add(ifid)
                edge_counts["IMPORTS"] += 1
            if irepo:
                impacted_repos.add(irepo)

    message = tr.translate(
        "tool.blast_radius.summary",
        loc,
        files=len(impacted_files),
        symbols=len(impacted_symbols),
        repos=len(impacted_repos),
    )
    return {
        "tool": "expand_blast_radius",
        "payload": {
            "target_symbols": sorted(set(target_symbols)),
            "impacted_symbols": sorted(impacted_symbols),
            "impacted_files": sorted(impacted_files),
            "impacted_repos": sorted(impacted_repos),
            "counts": {
                "symbols": len(impacted_symbols),
                "files": len(impacted_files),
                "repos": len(impacted_repos),
            },
            "impact_by_edge": edge_counts,
        },
        "message": message,
        "locale": loc,
    }


def suggest_change_sites(
    repository: GraphRepository,
    *,
    task_text: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    store: VectorStore | None = None,
    auto_semantic: bool = True,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
    trace: bool = False,
) -> dict[str, Any]:
    """Scored change-site candidates for a task (does NOT decide; narrows the space, P3).

    ``trace`` additionally returns the whole ranked pool (``payload["ranked_ids"]``, score order,
    before narrowing) so a benchmark can tell *where* a missed symbol was lost: never reached by
    an anchor or the expansion, or reached and then narrowed out.
    """
    tr = get_translator()
    loc = tr.resolve(locale)
    t0 = time.perf_counter()
    # Engine constants fitted for the configured embedding model (voyage / jina / default).
    profile = active_profile()

    timings: dict[str, float] = {}

    # D1: automatic semantic anchor when the caller did not pre-compute one (opt-out: auto_semantic).
    if semantic_candidates is None and auto_semantic and store is not None:
        stage = time.perf_counter()
        semantic_candidates = _auto_semantic_or_none(
            "suggest_change_sites", store, task_text, repo_ids, repository
        )
        timings["semantic_ms"] = _elapsed_ms(stage)
        logger.debug(
            "suggest_change_sites: auto semantic anchors",
            extra={"count": len(semantic_candidates or []), "duration_ms": timings["semantic_ms"]},
        )

    stage = time.perf_counter()
    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
        profile=profile,
    )
    timings["anchors_ms"] = _elapsed_ms(stage)
    logger.debug(
        "suggest_change_sites: anchors resolved",
        extra={"anchor_count": len(anchors.anchor_ids), "duration_ms": timings["anchors_ms"]},
    )
    orchestrator = RetrievalOrchestrator(repository, profile=profile)
    stage = time.perf_counter()
    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_text=task_text,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
        keep_ranked=trace,
    )
    timings.update(result.timings)
    timings["retrieve_ms"] = _elapsed_ms(stage)
    logger.debug(
        "suggest_change_sites: retrieval done",
        extra={
            "pool": result.pool_size,
            "candidates": len(result.candidates),
            "duration_ms": timings["retrieve_ms"],
            "total_ms": _elapsed_ms(t0),
        },
    )

    stage = time.perf_counter()
    sites = _enrich_items(repository, result.candidates)
    coverage = compute_coverage(result, repository)
    timings["enrich_ms"] = _elapsed_ms(stage)
    timings["total_ms"] = _elapsed_ms(t0)
    message = tr.translate("tool.change_sites.count", loc, count=len(sites))
    payload: dict[str, Any] = {
        "commit": commit,
        "anchors": result.anchors.anchors,
        "sites": sites,
        "coverage": coverage,
        "timings": timings,
    }
    if trace:
        payload["ranked_ids"] = [c.symbol_id for c in result.ranked]
    return {
        "tool": "suggest_change_sites",
        "payload": payload,
        "message": message,
        "locale": loc,
    }


def select_repos(
    repository: GraphRepository,
    *,
    task_text: str,
    component_repo_ids: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Candidate repo set for a task from metadata + anchor spread (deterministic union)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=None,
        route_paths=None,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=None,
        semantic_candidates=None,
    )
    repos: set[str] = set(component_repo_ids or [])
    for sid in anchors.anchor_ids:
        repo_id = repository.repo_of_symbol(sid)
        if repo_id:
            repos.add(repo_id)
    if scope is not None:
        repos = {r for r in repos if scope.allows(r)}

    selected = sorted(repos)
    message = tr.translate("tool.select_repos.count", loc, count=len(selected))
    return {
        "tool": "select_repos",
        "payload": {"repos": selected, "anchor_count": len(anchors.anchor_ids)},
        "message": message,
        "locale": loc,
    }


def assemble_context(
    repository: GraphRepository,
    *,
    symbol_ids: list[str],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Deduplicate a caller-supplied symbol list and fit it into a token budget (section 6.5)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in symbol_ids:
        if sid in seen:
            continue
        seen.add(sid)
        sym = repository.get_symbol(sid)
        if sym is None:
            continue
        repo_id = repository.repo_of_symbol(sid)
        if scope is not None and not scope.allows(repo_id):
            continue
        items.append(
            {
                "symbol_id": sid,
                "repo_id": repo_id,
                "name": sym.get("name"),
                "signature": sym.get("signature"),
                "file_id": sym.get("file_id"),
                "line": sym.get("line"),
                "graph_distance": 1,
            }
        )
    package = assemble(items, max_tokens=max_tokens)
    message = tr.translate(
        "tool.assemble_context.summary", loc, count=package["included"], tokens=max_tokens
    )
    return {
        "tool": "assemble_context",
        "payload": {"context": package},
        "message": message,
        "locale": loc,
    }
