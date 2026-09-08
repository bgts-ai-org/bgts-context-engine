"""Stage-by-stage trace of the get_context_for_task retrieval pipeline (UI layer only).

Replays the exact flow of :func:`bce.tools.layer3.orchestration.get_context_for_task` - same
functions, same order, same two-pass task-signal handling - but records every intermediate stage
so a frontend can animate the retrieval on the code graph:

    semantic -> anchors -> expand -> score -> narrow -> assemble

Determinism guarantee: the pipeline is embedding-free after the (optional) semantic anchor stage,
so for a given task + snapshot this trace is byte-identical to what the real endpoint computed.
Nothing outside ``bce.api.rest.ui`` is modified; core functions are only *called*.
"""

from __future__ import annotations

import time
from typing import Any

from bce.core.assembler import assemble
from bce.core.coverage import compute_coverage
from bce.core.orchestrator.anchors import find_anchors
from bce.core.orchestrator.expand import expand_from_anchors, to_candidates
from bce.core.orchestrator.orchestrator import RetrievalResult
from bce.core.scoring.engine import Candidate, ScoreWeights, score_candidates
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore
from bce.tools.layer3.orchestration import (
    _auto_semantic_candidates,
    _enrich_items,
    _task_signals,
)

#: Symbol-id chunk size for the bulk metadata Cypher query.
_META_CHUNK = 500

#: Mirrors the prelim-pass multiplier in get_context_for_task (max_candidates * 4).
_PRELIM_FACTOR = 4


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def trace_context_for_task(
    repository: GraphRepository,
    *,
    task_text: str,
    max_candidates: int = 8,
    max_tokens: int = 4000,
    repo_ids: list[str] | None = None,
    store: VectorStore | None = None,
    auto_semantic: bool = True,
) -> dict[str, Any]:
    """Run the retrieval pipeline and return every stage's intermediate result."""
    stages: list[dict[str, Any]] = []

    # Stage: automatic semantic anchor candidates (D1) - the only model-touching step.
    semantic_candidates: list[str] | None = None
    t = time.perf_counter()
    semantic_error: str | None = None
    if auto_semantic and store is not None:
        try:
            semantic_candidates = _auto_semantic_candidates(store, task_text, repo_ids)
        except Exception as exc:  # e.g. embeddings table empty / provider unavailable
            semantic_candidates = []
            semantic_error = str(exc)
    stage: dict[str, Any] = {
        "stage": "semantic",
        "duration_ms": _elapsed_ms(t),
        "candidates": semantic_candidates or [],
    }
    if semantic_error:
        stage["error"] = semantic_error
    stages.append(stage)

    # Stage 1: multi-source anchors (spec section 6.2).
    t = time.perf_counter()
    anchors = find_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=None,
        route_paths=None,
        history_file_ids=None,
        component_repo_ids=None,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
    )
    stages.append(
        {
            "stage": "anchors",
            "duration_ms": _elapsed_ms(t),
            "anchors": {sid: list(sources) for sid, sources in sorted(anchors.anchors.items())},
        }
    )

    # Stage 2: deterministic expansion (section 6.3). Run once and reused for both scoring
    # passes - expand_from_anchors is deterministic, so this equals the real double retrieve.
    t = time.perf_counter()
    expansion = expand_from_anchors(repository, anchors.anchor_ids)
    stages.append(
        {
            "stage": "expand",
            "duration_ms": _elapsed_ms(t),
            "nodes": [
                {
                    "symbol_id": info.symbol_id,
                    "distance": info.distance,
                    "is_anchor": info.is_anchor,
                    "ref_kind": info.ref_kind,
                    "provenance": info.provenance,
                }
                for _, info in sorted(expansion.items())
            ],
        }
    )

    # Stage 3: scoring (section 6.4), two passes exactly like get_context_for_task:
    # prelim (no task signals) -> task signals on prelim top -> final scoring.
    # The second pass reuses the first pass's graph features (degree/leaf/ref_kind do not depend
    # on task signals), so the result is identical to a second to_candidates() at half the cost.
    weights = ScoreWeights()
    t = time.perf_counter()
    base_candidates = _fast_to_candidates(repository, expansion)
    prelim_ranked = score_candidates(base_candidates, weights)
    prelim_top = prelim_ranked[: max_candidates * _PRELIM_FACTOR]
    signals = _task_signals(task_text, [c.symbol_id for c in prelim_top], repository)
    final_candidates = [
        Candidate(
            symbol_id=c.symbol_id,
            repo_id=c.repo_id,
            ref_kind=c.ref_kind,
            provenance=c.provenance,
            graph_distance=c.graph_distance,
            degree=c.degree,
            is_leaf=c.is_leaf,
            task_signal=signals.get(c.symbol_id, 0.0),
            anchor=c.anchor,
        )
        for c in base_candidates
    ]
    ranked = score_candidates(final_candidates, weights)
    stages.append(
        {
            "stage": "score",
            "duration_ms": _elapsed_ms(t),
            "task_signals": signals,
            "ranked": [
                {
                    "symbol_id": c.symbol_id,
                    "repo_id": c.repo_id,
                    "score": c.score,
                    "graph_distance": c.graph_distance,
                    "is_anchor": c.anchor,
                    "features": c.features,
                }
                for c in ranked
            ],
        }
    )

    # Stage 4: narrowing to top-N (the "1000 -> 8" step).
    narrowed = ranked[:max_candidates]
    stages.append(
        {
            "stage": "narrow",
            "duration_ms": 0.0,
            "selected": [
                {"symbol_id": c.symbol_id, "score": c.score, "rank": i + 1}
                for i, c in enumerate(narrowed)
            ],
        }
    )

    # Stage 5: token-budget assembly + coverage, same helpers as the real endpoint.
    t = time.perf_counter()
    items = _enrich_items(repository, narrowed)
    package = assemble(items, max_tokens=max_tokens)
    result = RetrievalResult(
        commit=None, anchors=anchors, candidates=narrowed, task_signals=signals
    )
    coverage = compute_coverage(result, repository)
    stages.append(
        {
            "stage": "assemble",
            "duration_ms": _elapsed_ms(t),
            "context": {"included": package.get("included"), "total": len(narrowed)},
            "coverage": coverage,
        }
    )

    return {
        "task_text": task_text,
        "repo_ids": repo_ids,
        "max_candidates": max_candidates,
        "stages": stages,
        "symbols": _symbol_metadata(repository, expansion.keys()),
    }


def _fast_to_candidates(repository: GraphRepository, expansion: dict) -> list[Candidate]:
    """Bulk-query equivalent of :func:`bce.core.orchestrator.expand.to_candidates`.

    ``to_candidates`` issues 4 Cypher round-trips per symbol (degree x2, callers, callees), which
    is minutes for a broad expansion. This computes the exact same ``degree``/``is_leaf`` features
    with chunked bulk queries. Falls back to the core function when no real client is available
    (unit-test fakes), keeping behaviour identical either way.
    """
    client = getattr(repository, "client", None)
    if client is None or not hasattr(client, "cypher"):
        return to_candidates(repository, expansion)

    ids = sorted(expansion)
    out_deg: dict[str, int] = dict.fromkeys(ids, 0)
    in_deg: dict[str, int] = dict.fromkeys(ids, 0)
    has_callees: set[str] = set()
    has_callers: set[str] = set()

    for i in range(0, len(ids), _META_CHUNK):
        chunk = ids[i : i + _META_CHUNK]
        params = {"ids": chunk}
        # Any-type edge degree, matching GraphRepository.symbol_degree (out + in).
        for (sid,) in client.cypher(
            "MATCH (s:Symbol)-[r]->() WHERE s.symbol_id IN $ids RETURN s.symbol_id",
            params,
            ["sid"],
        ):
            out_deg[sid] = out_deg.get(sid, 0) + 1
        for (sid,) in client.cypher(
            "MATCH ()-[r]->(s:Symbol) WHERE s.symbol_id IN $ids RETURN s.symbol_id",
            params,
            ["sid"],
        ):
            in_deg[sid] = in_deg.get(sid, 0) + 1
        # CALLS presence for the is_leaf feature (leaf = has callers but no callees).
        for (sid,) in client.cypher(
            "MATCH (s:Symbol)-[r:CALLS]->(c:Symbol) WHERE s.symbol_id IN $ids RETURN s.symbol_id",
            params,
            ["sid"],
        ):
            has_callees.add(sid)
        for (sid,) in client.cypher(
            "MATCH (c:Symbol)-[r:CALLS]->(s:Symbol) WHERE s.symbol_id IN $ids RETURN s.symbol_id",
            params,
            ["sid"],
        ):
            has_callers.add(sid)

    candidates: list[Candidate] = []
    for sid in ids:
        info = expansion[sid]
        candidates.append(
            Candidate(
                symbol_id=sid,
                repo_id=info.repo_id,
                ref_kind=info.ref_kind,
                provenance=info.provenance,
                graph_distance=info.distance,
                degree=out_deg.get(sid, 0) + in_deg.get(sid, 0),
                is_leaf=sid not in has_callees and sid in has_callers,
                task_signal=0.0,
                anchor=info.is_anchor,
            )
        )
    return candidates


def _symbol_metadata(repository: GraphRepository, symbol_ids: Any) -> dict[str, dict[str, Any]]:
    """name/kind/file/line per trace symbol, so the frontend can label and place ghost nodes.

    Uses one chunked bulk Cypher query when a real graph client is available (expansions can hold
    thousands of symbols); falls back to per-symbol lookups for fakes/tests without a client.
    """
    ids = sorted(symbol_ids)
    client = getattr(repository, "client", None)
    if client is None or not hasattr(client, "cypher"):
        meta: dict[str, dict[str, Any]] = {}
        for sid in ids:
            sym = repository.get_symbol(sid)
            if sym is not None:
                meta[sid] = {
                    "name": sym.get("name"),
                    "kind": sym.get("kind"),
                    "file_id": sym.get("file_id"),
                    "line": sym.get("line"),
                }
        return meta

    meta = {}
    columns = ["symbol_id", "name", "kind", "file_id", "line"]
    for i in range(0, len(ids), _META_CHUNK):
        chunk = ids[i : i + _META_CHUNK]
        rows = client.cypher(
            "MATCH (s:Symbol) WHERE s.symbol_id IN $ids "
            "RETURN s.symbol_id, s.name, s.kind, s.file_id, s.line",
            {"ids": chunk},
            columns,
        )
        for sid, name, kind, file_id, line in rows:
            meta[sid] = {"name": name, "kind": kind, "file_id": file_id, "line": line}
    return meta
