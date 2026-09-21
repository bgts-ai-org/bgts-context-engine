"""Retrieval profiles: the engine constants that depend on the embedding model.

Most of the orchestrator is model-agnostic - names, routes, graph edges - but two knobs encode
an assumption about *where in the model's ranked list the right answers sit*:

* how much semantic anchor strength a rank is worth (:attr:`RetrievalProfile.semantic_rank_scale`),
* how narrowing shares the answer between the model's ranks and the engine's own picks
  (:attr:`RetrievalProfile.semantic_reserve_share`, :attr:`RetrievalProfile.semantic_guard_ranks`).

They were fitted on the PR-replay ``tune`` split with voyage-code-4, whose correct hits sit in
the first 5 ranks or nowhere. jina-code-embeddings-1.5b finds more (recall@20 24.6 % vs 16.9 %
on the same 30 PRs) but spreads them over ranks 1-15, and the voyage constants ranked those mid
hits out of the answer (retention 100 % -> 83 %, holdout recall@20 -3 pp). A profile keeps each
model's constants separate: the voyage numbers are untouched, jina gets its own, and a model
nobody has fitted for falls back to the voyage set.

Selection is automatic from ``BCE_EMBEDDING_MODEL`` (:func:`profile_for_model`), can be forced
with ``BCE_RETRIEVAL_PROFILE=<name>`` and single knobs can be overridden from the environment
for fitting runs (``BCE_RETRIEVAL_SEMANTIC_GUARD_RANKS`` etc., see :class:`bce.config.Settings`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bce.config import Settings


@dataclass(frozen=True, slots=True)
class RetrievalProfile:
    name: str
    #: Narrowing: the model's first ``n`` ranks fill slots 1..n unconditionally (only the
    #: container cap applies). 0 = no guard, the reserve share alone decides.
    semantic_guard_ranks: int
    #: Narrowing: floor on the share of the first ``i`` slots that come from the model's ranks
    #: (an engine pick that is also a model rank counts). See ``orchestrator.narrow``.
    semantic_reserve_share: float
    #: Anchors: semantic strength is ``cap / (1 + rank / scale)``; the rank at which the model's
    #: evidence is worth half its cap. See ``anchors.semantic_strength``.
    semantic_rank_scale: float


#: voyage-code-4 (and the historical defaults every constant was fitted with). Unchanged.
VOYAGE = RetrievalProfile(
    name="voyage",
    semantic_guard_ranks=0,
    semantic_reserve_share=0.5,
    semantic_rank_scale=30.0,
)

#: jina-code-embeddings-1.5b. Fitted with engine-only re-runs (``bench40_jina.py rerun``) over
#: the stored jina lists of ``local_bench/reports/jina_vs_bce_k40.json``; numbers are recall@20
#: on all 30 scored PRs / ``tune`` (16) / ``holdout`` (14), see
#: ``local_bench/benchmark-k40-jina-tr.md``:
#:
#:   guard  0 (voyage constants): 35.7 / 42.7 / 27.8, retention 83 %  - holdout below jina alone
#:   guard  5:                    35.7 / 42.7 / 27.8, retention 83 %  - the lost hits sit at 7-15
#:   guard  7:                    38.7 / 42.7 / 34.1, retention 88 %
#:   guard 10:                    38.2 / 41.1 / 34.9, retention 92 %  <- chosen
#:   guard 15:                    30.2 / 37.0 / 22.4, retention 89 %  - engine left with 5 slots
#:   rank scale 60 (guard 10):    identical to guard 10 at every K
#:
#: 10 is the only setting that beats jina alone on ``holdout`` at every K (11.5 / 20.6 / 34.9
#: vs 11.9 / 19.8 / 30.8) and keeps the most of the model's own hits. Its cost is recall@5 on
#: ``tune`` (13.0 -> 3.6): where jina's top ranks are poor, the engine's anchor finds (#800,
#: #854, #856) move from slots 1-5 to 11-20 - they are still in the K=20 answer.
JINA = RetrievalProfile(
    name="jina",
    semantic_guard_ranks=10,
    semantic_reserve_share=0.5,
    semantic_rank_scale=30.0,
)

PROFILES: dict[str, RetrievalProfile] = {p.name: p for p in (VOYAGE, JINA)}

#: Unknown model (or the hashing fallback): the voyage constants, the only fully validated set.
DEFAULT_PROFILE = VOYAGE

#: model-name prefix -> profile. Matched against the lower-cased model id with any path / org
#: prefix removed (``jinaai/jina-code-embeddings-1.5b`` -> ``jina-code-embeddings-1.5b``).
_MODEL_FAMILIES: tuple[tuple[str, RetrievalProfile], ...] = (
    ("voyage", VOYAGE),
    ("jina", JINA),
)


def profile_for_model(model: str | None) -> RetrievalProfile:
    """The profile fitted for ``model`` (``BCE_EMBEDDING_MODEL`` or a stored ``model_id``)."""
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    for prefix, profile in _MODEL_FAMILIES:
        if name.startswith(prefix):
            return profile
    return DEFAULT_PROFILE


def active_profile(settings: Settings | None = None) -> RetrievalProfile:
    """Resolve the profile from settings: explicit name, else the embedding model, plus overrides.

    Never raises on an unknown ``BCE_RETRIEVAL_PROFILE`` - retrieval must not fail because of a
    tuning knob - it falls back to the model-derived profile.
    """
    if settings is None:
        from bce.config import get_settings

        settings = get_settings()
    chosen = (settings.retrieval_profile or "auto").strip().lower()
    profile = PROFILES.get(chosen) or profile_for_model(settings.embedding_model)
    if settings.retrieval_semantic_guard_ranks is not None:
        profile = replace(
            profile, semantic_guard_ranks=max(0, int(settings.retrieval_semantic_guard_ranks))
        )
    if settings.retrieval_semantic_reserve_share is not None:
        profile = replace(
            profile,
            semantic_reserve_share=min(
                1.0, max(0.0, float(settings.retrieval_semantic_reserve_share))
            ),
        )
    if settings.retrieval_semantic_rank_scale is not None:
        profile = replace(
            profile, semantic_rank_scale=max(1.0, float(settings.retrieval_semantic_rank_scale))
        )
    return profile
