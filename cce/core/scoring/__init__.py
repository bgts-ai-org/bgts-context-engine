"""Scoring Engine (spec section 6.4).

Deterministic ranking of candidate symbols. No embedding score is used here (P2): the ranking is
computed purely from structural + task features so the same candidates always rank the same way.
"""

from cce.core.scoring.engine import (
    SCORE_WEIGHTS_VERSION,
    Candidate,
    ScoreWeights,
    score_candidate,
    score_candidates,
)

__all__ = [
    "Candidate",
    "ScoreWeights",
    "score_candidate",
    "score_candidates",
    "SCORE_WEIGHTS_VERSION",
]
