"""Retrieval Orchestrator (spec section 6): anchor -> expand -> score -> filter -> assemble.

The orchestrator is the deterministic heart of retrieval. It wires anchor finding (section 6.2),
fixed-template expansion (section 6.3), and scoring (section 6.4) into a single reproducible pass.
"""

from cce.core.orchestrator.anchors import AnchorResult, find_anchors
from cce.core.orchestrator.expand import expand_from_anchors
from cce.core.orchestrator.orchestrator import RetrievalOrchestrator, RetrievalResult

__all__ = [
    "find_anchors",
    "AnchorResult",
    "expand_from_anchors",
    "RetrievalOrchestrator",
    "RetrievalResult",
]
