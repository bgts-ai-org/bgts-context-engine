"""Retrieval Orchestrator (spec section 6): the 5-stage deterministic pipeline.

    stage 0  commit pinning        (callers pass the pinned commit; every stage runs against it)
    stage 1  multi-source anchors  (section 6.2)
    stage 2  deterministic expand  (section 6.3, fixed template)
    stage 3  scoring + narrowing   (section 6.4, 1000 -> ~N)
    stage 4  RLS/scope filter      (section 6.5 / 9; applied via an injected predicate, Phase 4)
    stage 5  token-budget assembly (handled by Layer-3 assemble_context; orchestrator returns the
             scored, filtered candidate list so any Layer-3 tool can assemble it)

The orchestrator itself is 100% deterministic: embeddings only ever appear upstream as pre-computed
anchor candidates (P2). The same task + commit + scope always yields the same ranked list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from cce.core.orchestrator.anchors import AnchorResult
from cce.core.orchestrator.expand import expand_from_anchors, to_candidates
from cce.core.scoring.engine import Candidate, ScoreWeights, score_candidates
from cce.storage.graph.repository import GraphRepository


@dataclass(slots=True)
class RetrievalResult:
    commit: str | None
    anchors: AnchorResult
    candidates: list[Candidate]
    task_signals: dict[str, float] = field(default_factory=dict)

    @property
    def anchor_ids(self) -> list[str]:
        return self.anchors.anchor_ids


class RetrievalOrchestrator:
    def __init__(
        self,
        repository: GraphRepository,
        weights: ScoreWeights | None = None,
    ) -> None:
        self.repository = repository
        self.weights = weights or ScoreWeights()

    def retrieve(
        self,
        anchors: AnchorResult,
        *,
        commit: str | None = None,
        task_signals: dict[str, float] | None = None,
        max_candidates: int = 8,
        scope_filter: Callable[[str | None], bool] | None = None,
    ) -> RetrievalResult:
        """Run stages 2-4 from a set of anchors (stage 1) and return the narrowed, ranked list.

        ``scope_filter`` (Phase 4) receives a candidate's repo_id and returns True if it is allowed;
        default allows everything. ``task_signals`` maps symbol_id -> [0,1] task relevance (e.g. name
        matches an error code in the task); it is folded into scoring feature w2.
        """
        task_signals = task_signals or {}

        expansion = expand_from_anchors(self.repository, anchors.anchor_ids)
        candidates = to_candidates(self.repository, expansion, task_signals=task_signals)
        ranked = score_candidates(candidates, self.weights)

        if scope_filter is not None:
            ranked = [c for c in ranked if scope_filter(c.repo_id)]

        narrowed = ranked[:max_candidates]
        return RetrievalResult(
            commit=commit,
            anchors=anchors,
            candidates=narrowed,
            task_signals=task_signals,
        )
