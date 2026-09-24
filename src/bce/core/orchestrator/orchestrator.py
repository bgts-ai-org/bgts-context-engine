"""Retrieval Orchestrator (spec section 6): the 5-stage deterministic pipeline.

    stage 0  commit pinning        (callers pass the pinned commit; every stage runs against it)
    stage 1  multi-source anchors  (section 6.2)
    stage 2  deterministic expand  (section 6.3, fixed template)
    stage 3  scoring + narrowing   (section 6.4, 1000 -> ~N, diversity-aware)
    stage 4  RLS/scope filter      (section 6.5 / 9; applied via an injected predicate, Phase 4)
    stage 5  token-budget assembly (handled by Layer-3 assemble_context; orchestrator returns the
             scored, filtered candidate list so any Layer-3 tool can assemble it)

The orchestrator itself is 100% deterministic: embeddings only ever appear upstream as pre-computed
anchor candidates (P2). The same task + commit + scope always yields the same ranked list.

Retrieval is a single pass: task signals are computed for every expanded candidate inside
:func:`to_candidates` (v1 ran a preliminary pass and only signalled its top slice, which - because
anchors always filled that slice - meant graph neighbours could never receive a task signal).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from bce.core.orchestrator.anchors import AnchorResult
from bce.core.orchestrator.expand import expand_from_anchors, to_candidates
from bce.core.orchestrator.profile import RetrievalProfile
from bce.core.orchestrator.text import query_terms
from bce.core.scoring.engine import (
    CONTAINER_KINDS,
    Candidate,
    ScoreWeights,
    demote_redundant_containers,
    score_candidates,
)
from bce.storage.graph.repository import GraphRepository

#: Diversity rule 1: at most ceil(n * share) of the first n selected symbols may come from one
#: file (engine picks only; the model's own ranks are exempt, see :func:`narrow`). 0.4 blocked
#: the engine's second hit in a file at slots 2-5 (a fix usually lands in one or two files);
#: 0.6 lifted recall@5 on the tune split with no change at K=10/20.
PER_FILE_SHARE = 0.6
#: Diversity rule 1 tapers past this slot. The share above was fitted on recall@5/@10 and holds
#: for the head of the list; beyond it a file earns one more slot per ``1 / PER_FILE_TAIL_SHARE``
#: slots, so a 50-slot answer holds at most 6 + 4 = 10 symbols of one file instead of 30 and a
#: 100-slot answer 15 instead of 60. Long answers are asked for to cover *more places* (an
#: inventory of every ``localStorage`` key, every caller of a helper); the ninth member of a file
#: already in the answer is what they should give up first. Model-stream picks stay exempt.
PER_FILE_TAPER_FROM = 10
PER_FILE_TAIL_SHARE = 0.1
#: Diversity rule 1b: at most ceil(n * share) of the first n selected symbols may be members of
#: the same container (engine picks only). A class rework legitimately puts several methods of
#: one class in the answer; the cap keeps the engine from filling the list with them.
PER_CONTAINER_SHARE = 0.3
#: Reserve: the embedding model's ranks get ``share`` of the slots, interleaved with the engine's
#: picks in a fixed apportionment (model first). The model is the only channel that reads
#: *meaning* rather than names, so the engine must not be able to rank the model's top slice out
#: of the answer - and the guarantee is about *which* ranks: four arbitrary semantic picks do not
#: make up for losing the model's #1, which is why the model's #1 is always position 1.
#:
#: Fitted on the PR-replay ``tune`` split. The share is a *floor*, not an apportionment: an
#: engine pick that is also a model rank counts toward it (see :func:`narrow`), so in practice
#: the model's share is met by agreement most of the time and the floor only bites when the
#: engine's scores disagree with the model. 0.7 pushed the engine's own hits - explicit /
#: lexical anchors the model never returned, sitting at pool ranks 3-9 - to positions 11-16 and
#: lost recall@5/@10; 0.5 and 0.3 tie at K=10/20. 0.3 was better at K=5 on the tune split but
#: that split is where the model is weak (recall 10 %); on the holdout split, where the model is
#: stronger (18 %), 0.3 let three of the model's own hits slip from the top 5 / out of the list.
#: 0.5 is the a-priori choice: half the list is the model's, whatever the engine thinks.
#: This is the voyage-code-4 value; another embedding model may carry its own share and a
#: guard on its top ranks in its :class:`~bce.core.orchestrator.profile.RetrievalProfile`.
SEMANTIC_RESERVE_SHARE = 0.5
#: Diversity rule 3: at most ceil(n * share) container symbols among the first n (a class is a
#: location, not a change site; its members carry the change).
CONTAINER_SHARE = 0.2
#: How far down the ranking `demote_redundant_containers` looks for a container's members,
#: as a multiple of N.
CONTAINER_HORIZON = 2


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


@dataclass(slots=True)
class RetrievalResult:
    commit: str | None
    anchors: AnchorResult
    candidates: list[Candidate]
    task_signals: dict[str, float] = field(default_factory=dict)
    #: Size of the scored pool before narrowing (coverage reads it as a flooding signal).
    pool_size: int = 0
    #: Full ranked pool (post scope filter, pre narrowing). Optional; large - not serialized.
    ranked: list[Candidate] = field(default_factory=list, repr=False)
    #: Per-stage wall clock in milliseconds (expand / candidates / score / narrow).
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def anchor_ids(self) -> list[str]:
        return self.anchors.anchor_ids


class RetrievalOrchestrator:
    def __init__(
        self,
        repository: GraphRepository,
        weights: ScoreWeights | None = None,
        profile: RetrievalProfile | None = None,
    ) -> None:
        self.repository = repository
        self.weights = weights or ScoreWeights()
        #: Model-specific narrowing constants; ``None`` = the voyage defaults.
        self.profile = profile

    def retrieve(
        self,
        anchors: AnchorResult,
        *,
        commit: str | None = None,
        task_text: str | None = None,
        task_signals: dict[str, float] | None = None,
        max_candidates: int = 8,
        scope_filter: Callable[[str | None], bool] | None = None,
        keep_ranked: bool = False,
    ) -> RetrievalResult:
        """Run stages 2-4 from a set of anchors (stage 1) and return the narrowed, ranked list.

        ``scope_filter`` (Phase 4) receives a candidate's repo_id and returns True if it is allowed;
        default allows everything. ``task_text`` drives the task-signal feature (w2) for every
        candidate; ``task_signals`` (symbol_id -> [0,1]) overrides it per symbol.
        """
        task_tokens = set(query_terms(task_text or ""))
        timings: dict[str, float] = {}

        stage = time.perf_counter()
        expansion = expand_from_anchors(self.repository, anchors)
        timings["expand_ms"] = _elapsed_ms(stage)

        stage = time.perf_counter()
        candidates = to_candidates(
            self.repository,
            expansion,
            anchors=anchors,
            task_signals=task_signals,
            task_tokens=task_tokens,
        )
        timings["candidates_ms"] = _elapsed_ms(stage)

        stage = time.perf_counter()
        ranked = score_candidates(candidates, self.weights)
        if scope_filter is not None:
            ranked = [c for c in ranked if scope_filter(c.repo_id)]
        ranked = demote_redundant_containers(ranked, horizon=max_candidates * CONTAINER_HORIZON)
        timings["score_ms"] = _elapsed_ms(stage)

        stage = time.perf_counter()
        narrowed = narrow(ranked, max_candidates, profile=self.profile)
        timings["narrow_ms"] = _elapsed_ms(stage)

        signals = {c.symbol_id: c.task_signal for c in ranked if c.task_signal > 0.0}
        return RetrievalResult(
            commit=commit,
            anchors=anchors,
            candidates=narrowed,
            task_signals=signals,
            pool_size=len(ranked),
            ranked=ranked if keep_ranked else [],
            timings=timings,
        )


def _is_container(cand: Candidate) -> bool:
    return (cand.kind or "").lower() in CONTAINER_KINDS


def file_cap(slot: int) -> int:
    """Symbols one file may hold among the first ``slot`` engine picks (diversity rule 1).

    Linear in the head (``ceil(slot * PER_FILE_SHARE)``), then :data:`PER_FILE_TAIL_SHARE` per
    slot past :data:`PER_FILE_TAPER_FROM`. Non-decreasing in ``slot`` and independent of the
    answer size, which is what keeps :func:`narrow` K-monotonic.
    """
    if slot <= PER_FILE_TAPER_FROM:
        return max(1, math.ceil(slot * PER_FILE_SHARE))
    head = max(1, math.ceil(PER_FILE_TAPER_FROM * PER_FILE_SHARE))
    return head + math.ceil((slot - PER_FILE_TAPER_FROM) * PER_FILE_TAIL_SHARE)


def _container_of(cand: Candidate) -> str | None:
    """Id prefix of the container a member belongs to (None for top-level / container ids)."""
    segments = cand.symbol_id.split("#", 1)[0].split("::")
    if len(segments) < 4 or not segments[-2]:
        return None
    return "::".join(segments[:-1])


def narrow(
    ranked: list[Candidate], max_candidates: int, *, profile: RetrievalProfile | None = None
) -> list[Candidate]:
    """Deterministic, diversity-aware top-N over an already ranked (score desc, id asc) list.

    The list is built one slot at a time from two streams - the embedding model's ranks (rank
    order) and the engine's ranking (score order) - and returned **in selection order**, so
    ``narrow(pool, n) == narrow(pool, n + 1)[:n]``: asking for a longer list never changes what
    the shorter one was (K-monotonic), and a K=20 run can be cut to K=10 exactly.

    ``profile`` supplies the two model-specific constants (default: the voyage values,
    :data:`SEMANTIC_RESERVE_SHARE` and no guard). Slot ``i`` (1-based) goes to:

    0. the model's next rank while ``i <= profile.semantic_guard_ranks`` (the guard: the model's
       first ``n`` ranks *are* slots 1..n, whatever the engine scored - only the container cap
       can skip one). A model whose correct hits spread over ranks 1-15 (jina) needs this;
       without it the engine's agreement with the model's *deeper* ranks satisfies the share
       below and its own picks push the model's ranks 7-15 to positions 11-19 or out;
    1. the model's next rank while fewer than ``i * semantic_reserve_share`` slots came from the
       model stream (so position 1 is always the model's #1, and the model's top ranks can never
       be ranked out of the answer);
    2. else the engine's head (score order). An engine pick that *is* one of the model's ranks
       is taken as is - both channels agree on it - and counts toward the model's share like a
       model pick: the reserve exists to keep the model's ranks in the answer, and an agreed pick
       does exactly that. Without this the two streams alternated even when they agreed, and the
       engine's own hits (explicit / lexical anchors the model never returned, sitting at pool
       ranks 3-6) were pushed from position 5 to positions 10-14 - the whole recall@5 / @10 loss
       measured against the v2 narrowing on the tune split.

    Two gates were tried and dropped, both measured on the PR-replay tune split: a score margin
    an engine pick had to clear over the model's next rank (inert - the engine's heads are
    overwhelmingly the model's own deeper ranks, which the gate exempts) and pre-emption of slot
    1 by strong explicit anchors (a long PR description names dozens of identifiers, so
    "explicit" is cheap there and it flooded the top). Explicit evidence enters through the score.

    Diversity caps grow with the slot index (``ceil(i * share)``) and apply to engine picks only:
    per file (:data:`PER_FILE_SHARE`, tapering past :data:`PER_FILE_TAPER_FROM` - see
    :func:`file_cap`), per container (:data:`PER_CONTAINER_SHARE`) and containers
    overall (:data:`CONTAINER_SHARE`, applied to both streams). A task that reworks one big
    service class is exactly where the model's ranks and the file cap collide, and there the cap
    is the thing that is wrong, so the model stream ignores it. A candidate blocked by a cap at
    slot ``i`` is reconsidered at every later slot.

    Pure function of its inputs; ``max_candidates <= 0`` returns an empty list.
    """
    if max_candidates <= 0 or not ranked:
        return []
    reserve_share = SEMANTIC_RESERVE_SHARE if profile is None else profile.semantic_reserve_share
    guard_ranks = 0 if profile is None else max(0, int(profile.semantic_guard_ranks))

    model_stream = sorted(
        (c for c in ranked if c.semantic_rank is not None),
        key=lambda c: (c.semantic_rank, c.symbol_id),
    )
    selected: list[Candidate] = []
    chosen: set[str] = set()
    per_file: dict[str, int] = {}
    per_container: dict[str, int] = {}
    containers = 0
    model_taken = 0

    def admissible(cand: Candidate, slot: int, *, engine_pick: bool) -> bool:
        if cand.symbol_id in chosen:
            return False
        if _is_container(cand) and containers >= max(1, math.ceil(slot * CONTAINER_SHARE)):
            return False
        if not engine_pick:
            return True
        if cand.file_id and per_file.get(cand.file_id, 0) >= file_cap(slot):
            return False
        owner = _container_of(cand)
        return not (
            owner and per_container.get(owner, 0) >= max(1, math.ceil(slot * PER_CONTAINER_SHARE))
        )

    def head(stream: list[Candidate], slot: int, *, engine_pick: bool) -> Candidate | None:
        for cand in stream:
            if admissible(cand, slot, engine_pick=engine_pick):
                return cand
        return None

    def take(cand: Candidate, *, from_model: bool) -> None:
        nonlocal containers, model_taken
        selected.append(cand)
        chosen.add(cand.symbol_id)
        if cand.file_id:
            per_file[cand.file_id] = per_file.get(cand.file_id, 0) + 1
        owner = _container_of(cand)
        if owner:
            per_container[owner] = per_container.get(owner, 0) + 1
        if _is_container(cand):
            containers += 1
        if from_model or cand.semantic_rank is not None:
            model_taken += 1

    while len(selected) < max_candidates:
        slot = len(selected) + 1
        model_head = head(model_stream, slot, engine_pick=False)
        engine_head = head(ranked, slot, engine_pick=True)
        if model_head is None and engine_head is None:
            break
        if model_head is not None and (
            engine_head is None or slot <= guard_ranks or model_taken < slot * reserve_share
        ):
            take(model_head, from_model=True)
        elif engine_head is not None:
            take(engine_head, from_model=False)

    # Caps can leave slots open when only capped candidates remain (a pool of nothing but
    # containers, say); fill them in score order so the list is never shorter than the pool allows.
    if len(selected) < max_candidates:
        for cand in ranked:
            if len(selected) >= max_candidates:
                break
            if cand.symbol_id not in chosen:
                take(cand, from_model=False)
    return selected
