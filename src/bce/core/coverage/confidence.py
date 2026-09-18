"""Coverage / Confidence metrics (spec section 8) + god-node (feature 4) + provenance (feature 3).

Deterministic metrics over a retrieval result:

- anchor_count / anchor_sources    - how many anchors, from how many distinct sources
- strong_anchor_count              - anchors whose evidence strength >= STRONG_ANCHOR_STRENGTH
- corroborated_anchor_count        - anchors backed by >= 2 independent sources
- anchor_agreement_ratio           - share of *selected* candidates that are strong/corroborated
                                     anchors or their direct (1-hop) neighbours
- max_anchor_strength              - strongest evidence in the selection
- weak_anchor_ratio                - share of selected candidates that are weak, single-source
                                     anchors (a flooding signal)
- test_ratio                       - share of selected candidates that are test symbols
- pool_size                        - scored candidates before narrowing (very large = flooded)
- connected_component_ratio        - are candidates one connected component or scattered
- cross_repo_edge_ratio            - breadth across repos (candidates spanning repos)
- top_candidate_margin             - score gap between the #1 and #2 candidate (big = clear)
- orphan_ratio                     - candidates not attached to any anchor (weak retrieval)
- provenance_distribution          - {scip, treesitter, heuristic} share of candidate edges (F3)
- touches_god_node / max_centrality_in_context - hub-symbol warning (F4)
- commit_mismatch / commit_mismatch_count - result symbols indexed at a different commit than the
  pinned one (minimal commit-consistency signal; full pinning enforcement is out of scope)

The confidence *level* (high/medium/low) maps to the consumer behaviour in the spec table. v1
derived it from "how many source *tags* exist anywhere" - a natural-language task trivially has
explicit+lexical+semantic tags, so a result made entirely of noise anchors was reported as
*high*. v2 asks whether the selected candidates agree on a well-evidenced anchor.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from bce.core.orchestrator.orchestrator import RetrievalResult

# A symbol with degree >= this is treated as a "god node" hub (feature 4).
GOD_NODE_DEGREE = 20

#: Anchor evidence strength considered "strong" (section 6.2 noisy-OR scale).
STRONG_ANCHOR_STRENGTH = 0.6
#: Below this an anchor is "weak" (typically one common lexical term).
WEAK_ANCHOR_STRENGTH = 0.35


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def compute_coverage(result: RetrievalResult, repository: Any | None = None) -> dict[str, Any]:
    candidates = result.candidates
    n = len(candidates)

    anchors = result.anchors
    anchor_ids = set(result.anchor_ids)
    anchor_count = len(anchor_ids)
    source_count = anchors.source_count

    strengths = getattr(anchors, "strength", {}) or {}
    sources_of = anchors.anchors

    def strength_of(cand) -> float:
        s = strengths.get(cand.symbol_id)
        if s is None:
            s = getattr(cand, "anchor_strength", 0.0) or 0.0
            if s <= 0.0 and (cand.anchor or cand.symbol_id in anchor_ids):
                s = 1.0  # legacy / explicit callers: anchor without a recorded strength
        return float(s)

    def is_corroborated(sid: str) -> bool:
        srcs = sources_of.get(sid, [])
        return len(srcs) >= 2 or "explicit" in srcs or "history" in srcs

    strong_anchor_count = sum(
        1 for sid in anchor_ids if _anchor_strength(strengths, sid) >= STRONG_ANCHOR_STRENGTH
    )
    corroborated_anchor_count = sum(1 for sid in anchor_ids if is_corroborated(sid))

    anchored = [c for c in candidates if c.anchor or c.symbol_id in anchor_ids]
    orphan_ratio = _ratio(n - len(anchored), n)

    trusted = [
        c
        for c in candidates
        if (c.anchor or c.symbol_id in anchor_ids)
        and (strength_of(c) >= STRONG_ANCHOR_STRENGTH or is_corroborated(c.symbol_id))
    ]
    near_trusted = [
        c
        for c in candidates
        if not (c.anchor or c.symbol_id in anchor_ids) and c.graph_distance <= 1
    ]
    # Neighbours only count as agreement when there is at least one trusted anchor to agree with.
    agreement = len(trusted) + (len(near_trusted) if trusted else 0)
    anchor_agreement_ratio = _ratio(min(agreement, n), n)

    weak_anchors = [
        c
        for c in candidates
        if (c.anchor or c.symbol_id in anchor_ids)
        and strength_of(c) < WEAK_ANCHOR_STRENGTH
        and not is_corroborated(c.symbol_id)
    ]
    weak_anchor_ratio = _ratio(len(weak_anchors), n)
    max_anchor_strength = round(max((strength_of(c) for c in anchored), default=0.0), 6)
    test_ratio = _ratio(sum(1 for c in candidates if getattr(c, "is_test", False)), n)

    repos = {c.repo_id for c in candidates if c.repo_id}
    cross_repo_edge_ratio = _ratio(max(len(repos) - 1, 0), max(n, 1))

    # Single connected component proxy: share of candidates that are anchors or within 2 hops.
    connected = [c for c in candidates if c.graph_distance <= 2]
    connected_component_ratio = _ratio(len(connected), n)

    # The narrowed list is in selection order (model / engine interleaved), not score order, so
    # the margin is taken between the two best scores rather than the first two positions.
    scores = sorted((c.score for c in candidates), reverse=True)
    top_candidate_margin = 0.0
    if n >= 2:
        top_candidate_margin = round(scores[0] - scores[1], 6)
    elif n == 1:
        top_candidate_margin = round(scores[0], 6)
    top_is_trusted = bool(candidates) and candidates[0] in trusted

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
        candidate_count=n,
        strong_anchor_count=strong_anchor_count,
        corroborated_anchor_count=corroborated_anchor_count,
        anchor_agreement_ratio=anchor_agreement_ratio,
        weak_anchor_ratio=weak_anchor_ratio,
        max_anchor_strength=max_anchor_strength,
        top_is_trusted=top_is_trusted,
        top_candidate_margin=top_candidate_margin,
        test_ratio=test_ratio,
        orphan_ratio=orphan_ratio,
        source_count=source_count,
    )

    return {
        "confidence": str(level),
        "anchor_count": anchor_count,
        "anchor_sources": sorted({s for srcs in sources_of.values() for s in srcs}),
        "anchor_source_count": source_count,
        "strong_anchor_count": strong_anchor_count,
        "corroborated_anchor_count": corroborated_anchor_count,
        "anchor_agreement_ratio": anchor_agreement_ratio,
        "weak_anchor_ratio": weak_anchor_ratio,
        "max_anchor_strength": max_anchor_strength,
        "test_ratio": test_ratio,
        "pool_size": int(getattr(result, "pool_size", 0) or 0),
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


def _anchor_strength(strengths: dict[str, float], sid: str) -> float:
    s = strengths.get(sid)
    return 1.0 if s is None else float(s)


def _confidence_level(
    *,
    candidate_count: int,
    strong_anchor_count: int,
    corroborated_anchor_count: int,
    anchor_agreement_ratio: float,
    weak_anchor_ratio: float,
    max_anchor_strength: float,
    top_is_trusted: bool,
    top_candidate_margin: float,
    test_ratio: float,
    orphan_ratio: float,
    source_count: int,
) -> ConfidenceLevel:
    if candidate_count == 0:
        return ConfidenceLevel.LOW

    # LOW: nothing well-evidenced to stand on, or the selection is mostly weak/noise anchors, or
    # the top of the ranking is a coin flip between unrelated single-source hits.
    if max_anchor_strength < WEAK_ANCHOR_STRENGTH and corroborated_anchor_count == 0:
        return ConfidenceLevel.LOW
    if weak_anchor_ratio > 0.5 or test_ratio > 0.5:
        return ConfidenceLevel.LOW
    if source_count <= 1 and (top_candidate_margin < 0.5 or orphan_ratio > 0.5):
        return ConfidenceLevel.LOW

    # HIGH: the selection agrees on a well-evidenced anchor - the #1 is itself trusted, most of
    # the selection is trusted anchors or their direct neighbours, and the top is not a tie.
    if (
        top_is_trusted
        and (strong_anchor_count >= 1 or corroborated_anchor_count >= 1)
        and anchor_agreement_ratio >= 0.6
        and weak_anchor_ratio <= 0.25
        and top_candidate_margin >= 0.5
    ):
        return ConfidenceLevel.HIGH
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
    from bce.core.i18n import get_translator

    tr = get_translator()
    level = coverage.get("confidence", "medium")
    key = {
        "high": "coverage.high",
        "medium": "coverage.medium",
        "low": "coverage.low",
    }.get(level, "coverage.medium")
    return tr.translate(
        key,
        locale,
        anchor_count=coverage.get("anchor_count", 0),
        strong_anchor_count=coverage.get("strong_anchor_count", 0),
    )
