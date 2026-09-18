"""Deterministic candidate scoring (spec section 6.4 + feature 3 provenance weight), scoring-v3.

    score(node) =
        w1 * ref_kind_weight(define/write >> read/pass) * (0.5 + 0.5 * evidence)
      + w2 * task_signal_match(name or its parts appear in the task text)
      + w3 * structural_centrality(log-scaled degree)
      + w4 * proximity(1 / (1 + graph_distance)) * evidence
      - w5 * leaf_penalty(consume-only leaf node)
      + w6 * provenance_weight(exact edge > heuristic bridge)   # feature 3
      + w7 * anchor_strength                              # evidence-scaled, not a flat floor
      - w8 * test_penalty(test file / test function)
      + w9 * kind_prior(a method is a change site, a class is a location)
      + w10 * semantic_rank_prior(1 / (1 + rank) for the embedding model's ranked list)
      + w11 * churn_prior(log-saturated commits touching the file in the churn window)

v1 gave every anchor a flat +5.0 floor. With a natural-language task that yields dozens of
single-word anchors, that floor split the pool into two disjoint bands (every anchor >= 7.3,
every graph neighbour <= 4.9) and the top-N became "the highest-degree anchors" regardless of
evidence. v2 scales the anchor bonus by the anchor's evidence strength (section 6.2), so a symbol
matched by several sources keeps a clear lead while a lone stop-word-ish hit is beatable by a
well-connected neighbour with a define edge or a task-name match.

v3 adds the two corrections the PR-replay benchmark exposed. First, *evidence decays along the
graph*: v2 paid a 1-hop neighbour of a throwaway anchor exactly what it paid a 1-hop neighbour of
the best-evidenced one, so a `define` edge off a generic name like `Content` scored like a real
lead. Proximity and the reference kind are therefore both scaled by the evidence that reached the
node (:attr:`Candidate.evidence`). Second, a *kind prior*: the change a task describes almost
always lands in a method or function, while classes, interfaces and constructors are where it
lives. Containers keep a positive prior (they are legitimate answers when nothing inside them is
known) but no longer beat the members that carry the change, and
:func:`demote_redundant_containers` drops a container further once its own members are in the
pool.

v4 makes the model's *rank* an explicit term. The semantic anchor strength decays gently with
rank (it has to: a rank-25 neighbour is still an anchor worth expanding from), so between the
model's #1 and #10 the v3 score differed by a few tenths - less than a define edge - and a graph
neighbour of a generic name routinely displaced the model's best hit from the top of the list.
The rank prior ``1 / (1 + rank)`` is steep where it matters (1.0, 0.5, 0.33 ...) and negligible
past rank 10, so the top of the model's list is respected while its tail still has to earn its
place through evidence.

Unknown ref_kind / provenance are scored *at or below* the weakest known value: an unknown must
never outrank known evidence. Weights are fixed and versioned; ranking is embedding-free - the
model contributes anchor *ranks*, never a similarity number that could reorder candidates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from bce.domain.enums import Provenance, RefKind

SCORE_WEIGHTS_VERSION = "scoring-v4"

#: Kinds that *contain* other symbols. A container named in a task is a location hint: the change
#: site is normally one of its members. Shared with anchor finding and narrowing.
CONTAINER_KINDS: frozenset[str] = frozenset(
    {
        "class",
        "interface",
        "struct",
        "record",
        "enum",
        "type",
        "trait",
        "protocol",
        "namespace",
        "module",
        "object",
        "constructor",
    }
)

#: Containers that *declare* members. The constructor is a container for narrowing purposes (it is
#: a location, not the logic a task describes) but it is itself a member of its class, so name
#: resolution must not treat it as the thing that owns other symbols.
DECLARING_KINDS: frozenset[str] = CONTAINER_KINDS - {"constructor"}


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    w1_ref_kind: float = 3.0
    w2_task_signal: float = 2.5
    w3_centrality: float = 0.6
    w4_proximity: float = 2.0
    w5_leaf_penalty: float = 0.5
    w6_provenance: float = 0.5
    w7_anchor: float = 3.5
    w8_test_penalty: float = 2.0
    w9_kind_prior: float = 2.0
    #: Fitted on the PR-replay tune split: 2.0 made the engine's own picks duplicate the model's
    #: ranks (which narrowing already guarantees a share of) and cost recall at K=5/10; 1.0 and 0
    #: measured the same, 1.0 keeps the model's rank as explicit evidence in the score.
    w10_semantic_rank: float = 1.0
    w11_churn: float = 1.0


DEFAULT_WEIGHTS = ScoreWeights()

#: Commits (within the churn window) at which the churn prior saturates; log-scaled below it.
CHURN_SATURATION_COMMITS = 20


def semantic_rank_prior(rank: int | None) -> float:
    """``1 / (1 + rank)`` for a candidate the embedding model returned (0-based rank); 0 otherwise."""
    if rank is None or rank < 0:
        return 0.0
    return 1.0 / (1.0 + rank)


def churn_prior_of(commits: int) -> float:
    """Log-scaled change frequency of the symbol's file in [0, 1] (0 when unknown / never)."""
    if commits <= 0:
        return 0.0
    return min(math.log1p(commits) / math.log1p(CHURN_SATURATION_COMMITS), 1.0)


_REF_KIND_WEIGHT: dict[str, float] = {
    str(RefKind.DEFINE): 1.0,
    str(RefKind.WRITE): 0.8,
    str(RefKind.READ): 0.3,
    str(RefKind.PASS): 0.2,
}
#: Reached through CALLS / hierarchy / sibling edges (no REFERENCES kind): treated like a read.
_UNKNOWN_REF_KIND_WEIGHT = 0.3

_PROVENANCE_WEIGHT: dict[str, float] = {
    str(Provenance.SCIP): 1.0,
    str(Provenance.TREESITTER): 0.8,
    str(Provenance.HEURISTIC): 0.4,
}
#: No edge provenance (anchors, siblings): never better than a heuristic bridge.
_UNKNOWN_PROVENANCE_WEIGHT = 0.4

#: Degree at which centrality saturates (log-scaled below it).
_CENTRALITY_SATURATION_DEGREE = 20

#: How likely a symbol of this kind *is* the change site rather than its surroundings. Derived
#: from what diffs actually touch, not from any one repository: bodies of methods and functions
#: hold the logic a task describes; declarations, signatures and type shells change less often
#: and, when they do, usually alongside a member that is already in the pool.
_KIND_PRIOR: dict[str, float] = {
    "method": 1.0,
    "function": 1.0,
    "property": 0.5,
    "field": 0.5,
    "variable": 0.5,
    "constant": 0.5,
    "class": 0.5,
    "struct": 0.5,
    "record": 0.5,
    "object": 0.5,
    "module": 0.5,
    "namespace": 0.5,
    "type": 0.5,
    "enum": 0.5,
    "interface": 0.4,
    "protocol": 0.4,
    "trait": 0.4,
    "constructor": 0.4,
}
#: An unindexed or unknown kind must not outrank a known change-site kind.
_UNKNOWN_KIND_PRIOR = 0.5

#: Penalty applied by :func:`demote_redundant_containers` to a container whose own members are
#: already in the ranking - roughly one kind-prior step, enough to sink below them.
CONTAINER_MEMBER_PENALTY = 1.5


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
    #: Evidence behind the anchor in [0, 1] (section 6.2). 0 with ``anchor=True`` means "unknown",
    #: which is treated as fully trusted (explicit callers, legacy inputs).
    anchor_strength: float = 0.0
    #: Anchor evidence that *reached* this node, decayed once per hop (section 6.3). 0 means
    #: "not recorded" and is treated as fully trusted, like ``anchor_strength``.
    source_strength: float = 0.0
    #: Anchor sources that nominated this symbol ("explicit", "lexical", "semantic", ...).
    sources: list[str] = field(default_factory=list)
    #: Position in the embedding model's ranked list, or None when the model did not return it.
    semantic_rank: int | None = None
    #: Commits that touched the symbol's file within the churn window (0 = unknown / none).
    churn: int = 0
    is_test: bool = False
    name: str | None = None
    kind: str | None = None
    file_id: str | None = None
    score: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_anchor_strength(self) -> float:
        if not self.anchor:
            return 0.0
        return self.anchor_strength if self.anchor_strength > 0.0 else 1.0

    @property
    def evidence(self) -> float:
        """Strength of the anchor evidence behind this candidate, in (0, 1].

        An anchor's own strength for distance 0; the reaching anchor's strength decayed per hop
        for a neighbour. 0 from either field means "not recorded" (hand-built candidates, callers
        that predate evidence propagation) and is fully trusted so the unknown never scores below
        real weak evidence.
        """
        strength = max(self.source_strength, self.effective_anchor_strength)
        return strength if strength > 0.0 else 1.0

    @property
    def is_container(self) -> bool:
        return (self.kind or "").lower() in CONTAINER_KINDS


def centrality_of(degree: int) -> float:
    """Log-scaled degree in [0, 1]: a 20-degree hub is 1.0, degree 4 ≈ 0.53, degree 1 ≈ 0.23."""
    if degree <= 0:
        return 0.0
    return min(math.log1p(degree) / math.log1p(_CENTRALITY_SATURATION_DEGREE), 1.0)


def kind_prior_of(kind: str | None) -> float:
    """Change-site prior for a symbol kind in [0, 1] (unknown kinds score mid-range)."""
    return _KIND_PRIOR.get((kind or "").lower(), _UNKNOWN_KIND_PRIOR)


def score_candidate(cand: Candidate, weights: ScoreWeights = DEFAULT_WEIGHTS) -> float:
    ref_kind_weight = _REF_KIND_WEIGHT.get(cand.ref_kind or "", _UNKNOWN_REF_KIND_WEIGHT)
    centrality = centrality_of(cand.degree)
    proximity = 1.0 / (1.0 + max(cand.graph_distance, 0))
    leaf = 1.0 if cand.is_leaf else 0.0
    provenance = _PROVENANCE_WEIGHT.get(cand.provenance or "", _UNKNOWN_PROVENANCE_WEIGHT)
    anchor_strength = cand.effective_anchor_strength
    test = 1.0 if cand.is_test else 0.0
    evidence = cand.evidence
    kind_prior = kind_prior_of(cand.kind)
    rank_prior = semantic_rank_prior(cand.semantic_rank)
    churn_prior = churn_prior_of(cand.churn)

    score = (
        weights.w1_ref_kind * ref_kind_weight * (0.5 + 0.5 * evidence)
        + weights.w2_task_signal * cand.task_signal
        + weights.w3_centrality * centrality
        + weights.w4_proximity * proximity * evidence
        - weights.w5_leaf_penalty * leaf
        + weights.w6_provenance * provenance
        + weights.w7_anchor * anchor_strength
        - weights.w8_test_penalty * test
        + weights.w9_kind_prior * kind_prior
        + weights.w10_semantic_rank * rank_prior
        + weights.w11_churn * churn_prior
    )

    cand.score = round(score, 6)
    cand.features = {
        "ref_kind_weight": round(ref_kind_weight, 6),
        "task_signal": round(cand.task_signal, 6),
        "centrality": round(centrality, 6),
        "proximity": round(proximity, 6),
        "leaf_penalty": leaf,
        "provenance_weight": round(provenance, 6),
        "anchor_strength": round(anchor_strength, 6),
        "test_penalty": test,
        "evidence": round(evidence, 6),
        "kind_prior": round(kind_prior, 6),
        "semantic_rank_prior": round(rank_prior, 6),
        "churn_prior": round(churn_prior, 6),
    }
    return cand.score


def score_candidates(
    candidates: list[Candidate], weights: ScoreWeights = DEFAULT_WEIGHTS
) -> list[Candidate]:
    """Score and return candidates in deterministic order (score desc, then symbol_id asc)."""
    for cand in candidates:
        score_candidate(cand, weights)
    return sorted(candidates, key=lambda c: (-c.score, c.symbol_id))


def member_prefix_of(symbol_id: str) -> str | None:
    """Symbol-id prefix shared by a container's members, or None for a non-container id shape.

    Container ids carry an empty container segment - ``csharp::Acme.Services::::MongoDbService``
    - while their members fill it in: ``csharp::Acme.Services::MongoDbService::GetAsync``. So the
    prefix is the id with the name moved into the container slot.
    """
    segments = symbol_id.split("#", 1)[0].split("::")
    if len(segments) < 4 or segments[-2] != "":
        return None
    return "::".join([*segments[:-2], segments[-1]]) + "::"


def demote_redundant_containers(
    ranked: list[Candidate], *, horizon: int, penalty: float = CONTAINER_MEMBER_PENALTY
) -> list[Candidate]:
    """Re-rank, penalising containers whose own members are already in the top ``horizon``.

    A class in the answer is only useful when nothing inside it is known; once one of its methods
    ranks, the class repeats information and costs a slot. A container with no member in the pool
    keeps its score - it is still the best available pointer at the change.
    """
    if not ranked or horizon <= 0:
        return list(ranked)

    member_ids = [c.symbol_id.split("#", 1)[0] for c in ranked[:horizon]]
    out = list(ranked)
    for cand in out:
        if not cand.is_container:
            continue
        prefix = member_prefix_of(cand.symbol_id)
        if prefix and any(mid.startswith(prefix) for mid in member_ids):
            cand.score = round(cand.score - penalty, 6)
            cand.features = {**cand.features, "container_member_penalty": penalty}
    return sorted(out, key=lambda c: (-c.score, c.symbol_id))
