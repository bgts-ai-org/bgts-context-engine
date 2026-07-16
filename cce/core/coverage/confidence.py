"""Coverage / Confidence metrics (spec section 8) + god-node (feature 4) + provenance (feature 3).

Deterministic metrics over a retrieval result:

- anchor_count / anchor_sources    - how many anchors, from how many distinct sources
- connected_component_ratio        - are candidates one connected component or scattered
- cross_repo_edge_ratio            - breadth across repos (candidates spanning repos)
- top_candidate_margin             - score gap between the #1 and #2 candidate (big = clear)
- orphan_ratio                     - candidates not attached to any anchor (weak retrieval)
- provenance_distribution          - {scip, treesitter, heuristic} share of candidate edges (F3)
- touches_god_node / max_centrality_in_context - hub-symbol warning (F4)
- commit_mismatch / commit_mismatch_count - result symbols indexed at a different commit than the
  pinned one (minimal commit-consistency signal; full pinning enforcement is out of scope)

The confidence *level* (high/medium/low) maps to the consumer behaviour in the spec table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from cce.core.orchestrator.orchestrator import RetrievalResult

# A symbol with degree >= this is treated as a "god node" hub (feature 4).
GOD_NODE_DEGREE = 20


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def compute_coverage(result: RetrievalResult, repository: Any | None = None) -> dict[str, Any]:
    candidates = result.candidates
    n = len(candidates)

    anchor_ids = set(result.anchor_ids)
    anchor_count = len(anchor_ids)
    source_count = result.anchors.source_count

    anchored = [c for c in candidates if c.anchor or c.symbol_id in anchor_ids]
    orphan_ratio = _ratio(n - len(anchored), n)

    repos = {c.repo_id for c in candidates if c.repo_id}
    cross_repo_edge_ratio = _ratio(max(len(repos) - 1, 0), max(n, 1))

    # Single connected component proxy: share of candidates that are anchors or within 2 hops.
    connected = [c for c in candidates if c.graph_distance <= 2]
    connected_component_ratio = _ratio(len(connected), n)

    top_candidate_margin = 0.0
    if n >= 2:
        top_candidate_margin = round(candidates[0].score - candidates[1].score, 6)
    elif n == 1:
        top_candidate_margin = round(candidates[0].score, 6)

    provenance_distribution = _provenance_distribution(candidates)

    max_centrality = max((c.degree for c in candidates), default=0)
    touches_god_node = max_centrality >= GOD_NODE_DEGREE

    # Commit consistency (D4): warn when result symbols were indexed at a different commit than
    # the requested pin. Needs the repository to look up indexed_at_commit per symbol.
    commit_mismatch_count = 0
    if result.commit and repository is not None:
        for cand in candidates:
            indexed_at = (repository.get_symbol(cand.symbol_id) or {}).get("indexed_at_commit")
            if indexed_at and indexed_at != result.commit:
                commit_mismatch_count += 1

    level = _confidence_level(
        source_count=source_count,
        connected_component_ratio=connected_component_ratio,
        top_candidate_margin=top_candidate_margin,
        orphan_ratio=orphan_ratio,
    )

    return {
        "confidence": str(level),
        "anchor_count": anchor_count,
        "anchor_sources": sorted({s for srcs in result.anchors.anchors.values() for s in srcs}),
        "anchor_source_count": source_count,
        "candidate_count": n,
        "connected_component_ratio": connected_component_ratio,
        "cross_repo_edge_ratio": cross_repo_edge_ratio,
        "top_candidate_margin": top_candidate_margin,
        "orphan_ratio": orphan_ratio,
        "provenance_distribution": provenance_distribution,
        "touches_god_node": touches_god_node,
        "max_centrality_in_context": max_centrality,
        "commit_mismatch": commit_mismatch_count > 0,
        "commit_mismatch_count": commit_mismatch_count,
    }


def _confidence_level(
    *,
    source_count: int,
    connected_component_ratio: float,
    top_candidate_margin: float,
    orphan_ratio: float,
) -> ConfidenceLevel:
    if source_count >= 3 and connected_component_ratio >= 0.7 and top_candidate_margin >= 1.0:
        return ConfidenceLevel.HIGH
    if source_count <= 1 and (top_candidate_margin < 0.5 or orphan_ratio > 0.5):
        return ConfidenceLevel.LOW
    return ConfidenceLevel.MEDIUM


def _provenance_distribution(candidates: list) -> dict[str, float]:
    counts = {"scip": 0, "treesitter": 0, "heuristic": 0}
    total = 0
    for cand in candidates:
        prov = cand.provenance
        if prov in counts:
            counts[prov] += 1
            total += 1
    if total == 0:
        return {k: 0.0 for k in counts}
    return {k: round(v / total, 6) for k, v in counts.items()}


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(num / den, 6)


def coverage_message(coverage: dict[str, Any], locale: str) -> str:
    """Localized one-line summary of the confidence level (i18n presentation only, P1)."""
    from cce.core.i18n import get_translator

    tr = get_translator()
    level = coverage.get("confidence", "medium")
    key = {
        "high": "coverage.high",
        "medium": "coverage.medium",
        "low": "coverage.low",
    }.get(level, "coverage.medium")
    return tr.translate(key, locale, anchor_count=coverage.get("anchor_count", 0))
