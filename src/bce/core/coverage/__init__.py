"""Coverage / Confidence signal (spec section 8 + features 3 & 4).

Every Layer-3 result carries a deterministic coverage object so the consuming agent can decide
whether to proceed, widen, or ask a human. Metrics are computed from the retrieval result only, so
they are reproducible for a given task + commit.
"""

from bce.core.coverage.confidence import (
    ConfidenceLevel,
    compute_coverage,
    coverage_message,
)

__all__ = ["compute_coverage", "coverage_message", "ConfidenceLevel"]
