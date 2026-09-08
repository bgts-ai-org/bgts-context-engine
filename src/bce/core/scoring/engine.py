"""Deterministic candidate scoring (spec section 6.4 + feature 3 provenance weight).

    score(node) =
        w1 * ref_kind_weight(define/write >> read/pass)
      + w2 * task_signal_match(error code, "expired", "500", ...)
      + w3 * structural_centrality(degree)
      + w4 * (1 / graph_distance_to_anchor)
      - w5 * leaf_penalty(consume-only leaf node)
      + w6 * provenance_weight(exact edge > heuristic bridge)   # feature 3

The 1000->8 narrowing works because a type that is merely *carried* (read/pass) in 900+ files scores
near zero on w1 and gets a leaf penalty, while the handful that *define/write/handle* it float to the
top. Weights are fixed and versioned; ranking is embedding-free (P2) so results are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bce.domain.enums import Provenance, RefKind

SCORE_WEIGHTS_VERSION = "scoring-v1"


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    w1_ref_kind: float = 3.0
    w2_task_signal: float = 2.0
    w3_centrality: float = 1.0
    w4_proximity: float = 2.0
    w5_leaf_penalty: float = 1.5
    w6_provenance: float = 0.5


DEFAULT_WEIGHTS = ScoreWeights()

_REF_KIND_WEIGHT: dict[str, float] = {
    str(RefKind.DEFINE): 1.0,
    str(RefKind.WRITE): 0.8,
    str(RefKind.READ): 0.3,
    str(RefKind.PASS): 0.2,
}

_PROVENANCE_WEIGHT: dict[str, float] = {
    str(Provenance.SCIP): 1.0,
    str(Provenance.TREESITTER): 0.8,
    str(Provenance.HEURISTIC): 0.4,
}


@dataclass(slots=True)
class Candidate:
    """A scoring candidate: a symbol with the deterministic features scoring needs."""

    symbol_id: str
    repo_id: str | None = None
    ref_kind: str | None = None
    provenance: str | None = None
    graph_distance: int = 1
    degree: int = 0
    is_leaf: bool = False
    task_signal: float = 0.0
    anchor: bool = False
    score: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)


def score_candidate(cand: Candidate, weights: ScoreWeights = DEFAULT_WEIGHTS) -> float:
    ref_kind_weight = _REF_KIND_WEIGHT.get(cand.ref_kind or "", 0.5)
    centrality = min(cand.degree / 20.0, 1.0)
    proximity = 1.0 / max(cand.graph_distance, 1)
    leaf = 1.0 if cand.is_leaf else 0.0
    provenance = _PROVENANCE_WEIGHT.get(cand.provenance or "", 0.6)

    score = (
        weights.w1_ref_kind * ref_kind_weight
        + weights.w2_task_signal * cand.task_signal
        + weights.w3_centrality * centrality
        + weights.w4_proximity * proximity
        - weights.w5_leaf_penalty * leaf
        + weights.w6_provenance * provenance
    )
    # Anchors are guaranteed a floor so a correct entry point never gets pruned by narrowing (P3).
    if cand.anchor:
        score += 5.0

    cand.score = round(score, 6)
    cand.features = {
        "ref_kind_weight": round(ref_kind_weight, 6),
        "task_signal": round(cand.task_signal, 6),
        "centrality": round(centrality, 6),
        "proximity": round(proximity, 6),
        "leaf_penalty": leaf,
        "provenance_weight": round(provenance, 6),
    }
    return cand.score


def score_candidates(
    candidates: list[Candidate], weights: ScoreWeights = DEFAULT_WEIGHTS
) -> list[Candidate]:
    """Score and return candidates in deterministic order (score desc, then symbol_id asc)."""
    for cand in candidates:
        score_candidate(cand, weights)
    return sorted(candidates, key=lambda c: (-c.score, c.symbol_id))
