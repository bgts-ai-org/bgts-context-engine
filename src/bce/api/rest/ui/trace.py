"""Stage-by-stage trace of the get_context_for_task retrieval pipeline (UI layer only).

Replays the exact flow of :func:`bce.tools.layer3.orchestration.get_context_for_task` - same
functions, same order, same single-pass scoring - but records every intermediate stage so a
frontend can animate the retrieval on the code graph:

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
from bce.core.orchestrator.orchestrator import RetrievalResult, narrow
from bce.core.orchestrator.text import content_tokens
from bce.core.scoring.engine import ScoreWeights, score_candidates
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore
from bce.tools.layer3.orchestration import _auto_semantic_candidates, _enrich_items

#: Symbol-id chunk size for the bulk metadata Cypher query.
_META_CHUNK = 500


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
            semantic_candidates = _auto_semantic_candidates(store, task_text, repo_ids, repository)
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

    # Stage 1: multi-source anchors (spec section 6.2), with evidence strength per anchor.
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
            "strength": {sid: anchors.strength_of(sid) for sid in anchors.anchor_ids},
            "task_tokens": content_tokens(task_text),
        }
    )

    # Stage 2: deterministic expansion (section 6.3).
    t = time.perf_counter()
    expansion = expand_from_anchors(repository, anchors)
    stages.append(
        {
            "stage": "expand",
            "duration_ms": _elapsed_ms(t),
            "nodes": [
                {
                    "symbol_id": info.symbol_id,
                    "distance": info.distance,
                    "is_anchor": info.is_anchor,
                    "anchor_strength": info.anchor_strength,
                    "ref_kind": info.ref_kind,
                    "provenance": info.provenance,
                }
                for _, info in sorted(expansion.items())
            ],
        }
    )

    # Stage 3: scoring (section 6.4), single pass exactly like get_context_for_task: features
    # (degree/leaf/test) and task signals are attached to every candidate by to_candidates.
    weights = ScoreWeights()
    t = time.perf_counter()
    candidates = to_candidates(repository, expansion, task_text=task_text)
    ranked = score_candidates(candidates, weights)
    signals = {c.symbol_id: c.task_signal for c in ranked if c.task_signal > 0.0}
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
                    "is_test": c.is_test,
                    "features": c.features,
                }
                for c in ranked
            ],
        }
    )

    # Stage 4: diversity-aware narrowing to top-N (the "1000 -> 8" step).
    t = time.perf_counter()
    narrowed = narrow(ranked, max_candidates)
    stages.append(
        {
            "stage": "narrow",
            "duration_ms": _elapsed_ms(t),
            "selected": [
                {"symbol_id": c.symbol_id, "score": c.score, "rank": i + 1}
                for i, c in enumerate(narrowed)
            ],
        }
    )

    # Stage 5: token-budget assembly + coverage, same helpers as the real endpoint.
    t = time.perf_counter()
    items = _enrich_items(repository, narrowed, with_body=True)
    package = assemble(items, max_tokens=max_tokens)
    result = RetrievalResult(
        commit=None,
        anchors=anchors,
        candidates=narrowed,
        task_signals=signals,
        pool_size=len(ranked),
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
