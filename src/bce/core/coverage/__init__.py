"""Coverage / Confidence signal (spec section 8 + features 3 & 4).

Every Layer-3 result carries a deterministic coverage object so the consuming agent can decide
whether to proceed, widen, or ask a human. Metrics are computed from the retrieval result, and the
context selector's verdict on the answer's files when it ran, so they are reproducible for a given
task + commit + selector answer.
"""

from bce.core.coverage.confidence import (
    TARGET_MIN_RELEVANCE,
    ConfidenceLevel,
    apply_target_confidence,
    compute_coverage,
    coverage_message,
)

__all__ = [
    "TARGET_MIN_RELEVANCE",
    "apply_target_confidence",
    "compute_coverage",
    "coverage_message",
    "ConfidenceLevel",
]
