"""Retrieval profiles: model-specific engine constants (voyage untouched, jina gets its own)."""

from __future__ import annotations

from bce.config import Settings
from bce.core.orchestrator.anchors import SEMANTIC_RANK_SCALE, semantic_strength
from bce.core.orchestrator.orchestrator import SEMANTIC_RESERVE_SHARE, narrow
from bce.core.orchestrator.profile import (
    DEFAULT_PROFILE,
    JINA,
    PROFILES,
    VOYAGE,
    RetrievalProfile,
    active_profile,
    profile_for_model,
)
from bce.core.scoring.engine import Candidate


def _pool(*cands: Candidate) -> list[Candidate]:
    return sorted(cands, key=lambda c: (-c.score, c.symbol_id))


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --- selection --------------------------------------------------------------------------------


def test_voyage_profile_is_the_historical_constants():
    # The voyage numbers are what every constant was fitted with; they must not move.
    assert VOYAGE.semantic_guard_ranks == 0
    assert VOYAGE.semantic_reserve_share == SEMANTIC_RESERVE_SHARE == 0.5
    assert VOYAGE.semantic_rank_scale == SEMANTIC_RANK_SCALE == 30
    assert DEFAULT_PROFILE is VOYAGE


def test_profile_for_model_matches_the_family_prefix_and_ignores_org_paths():
    assert profile_for_model("voyage-code-4") is VOYAGE
    assert profile_for_model("voyage-code-3") is VOYAGE
    assert profile_for_model("jina-code-embeddings-1.5b") is JINA
    assert profile_for_model("jinaai/jina-code-embeddings-0.5b") is JINA
    assert profile_for_model("JINA-code-embeddings-1.5b-1536") is JINA  # stored model_id form
    # Unknown model / the hashing fallback: the only fully validated set.
    assert profile_for_model("text-embedding-3-large") is VOYAGE
    assert profile_for_model("") is VOYAGE
    assert profile_for_model(None) is VOYAGE


def test_active_profile_is_derived_from_the_embedding_model_by_default():
    assert active_profile(_settings(embedding_model="voyage-code-4")) is VOYAGE
    assert active_profile(_settings(embedding_model="jina-code-embeddings-1.5b")) is JINA
    assert active_profile(_settings(embedding_provider="hashing")) is VOYAGE


def test_active_profile_can_be_forced_and_falls_back_on_an_unknown_name():
    s = _settings(embedding_model="jina-code-embeddings-1.5b", retrieval_profile="voyage")
    assert active_profile(s) is VOYAGE
    s = _settings(embedding_model="voyage-code-4", retrieval_profile="JINA")
    assert active_profile(s) is JINA
    s = _settings(embedding_model="voyage-code-4", retrieval_profile="no-such-profile")
    assert active_profile(s) is VOYAGE
    assert set(PROFILES) == {"voyage", "jina"}


def test_active_profile_applies_single_knob_overrides_without_touching_the_named_profiles():
    s = _settings(
        embedding_model="jina-code-embeddings-1.5b",
        retrieval_semantic_guard_ranks=4,
        retrieval_semantic_reserve_share=0.7,
        retrieval_semantic_rank_scale=60,
    )
    p = active_profile(s)
    assert p == RetrievalProfile("jina", 4, 0.7, 60.0)
    assert JINA.semantic_guard_ranks == 10  # frozen module constant untouched
    # Clamped, not rejected: a tuning knob must never break retrieval.
    s = _settings(retrieval_semantic_guard_ranks=-3, retrieval_semantic_reserve_share=2.0)
    p = active_profile(s)
    assert p.semantic_guard_ranks == 0
    assert p.semantic_reserve_share == 1.0


# --- semantic anchor strength ------------------------------------------------------------------


def test_semantic_strength_scale_comes_from_the_profile():
    assert semantic_strength(30) == semantic_strength(30, scale=VOYAGE.semantic_rank_scale)
    assert semantic_strength(30, scale=30) == 0.45  # half the 0.9 cap at rank == scale
    assert semantic_strength(60, scale=60) == 0.45
    assert semantic_strength(10, scale=60) > semantic_strength(10, scale=30)


# --- narrowing guard ---------------------------------------------------------------------------


def _jina_like_pool() -> list[Candidate]:
    """Engine scores agree with the model's *deep* ranks, disagree with its mid ranks.

    That is the jina pattern from the PR replay: the engine's heads are the model's ranks 15-25
    (they count toward the reserve as agreed picks), so the model's ranks 7-14 - where jina's
    correct hits sit - fall out of a 10-long answer under the voyage constants.
    """
    cands: list[Candidate] = []
    # model ranks 0..14; engine scores the deep ranks (10..14) high and the mid ranks (5..9) low
    for rank in range(15):
        score = 9.0 - rank if rank < 5 else (8.0 + rank if rank >= 10 else 0.5 - rank * 0.01)
        cands.append(
            Candidate(symbol_id=f"m{rank:02d}", score=score, file_id=f"f{rank}", semantic_rank=rank)
        )
    # engine-only picks (explicit / lexical anchors the model never returned)
    for i in range(8):
        cands.append(
            Candidate(symbol_id=f"e{i}", score=7.0 - i * 0.1, file_id=f"g{i}", anchor=True)
        )
    return _pool(*cands)


def test_without_a_guard_agreed_deep_ranks_push_the_models_mid_ranks_out():
    ids = [c.symbol_id for c in narrow(_jina_like_pool(), 10, profile=VOYAGE)]
    # Voyage behaviour, unchanged: the deep ranks the engine likes satisfy the model's share, so
    # the model's ranks 5-9 do not make the first ten.
    assert "m00" == ids[0]
    assert not {"m05", "m06", "m07", "m08", "m09"} & set(ids)
    assert ids == [c.symbol_id for c in narrow(_jina_like_pool(), 10)]  # default == voyage


def test_guard_makes_the_models_first_n_ranks_the_first_n_slots():
    profile = RetrievalProfile(
        "t", semantic_guard_ranks=10, semantic_reserve_share=0.5, semantic_rank_scale=30
    )
    ids = [c.symbol_id for c in narrow(_jina_like_pool(), 20, profile=profile)]
    assert ids[:10] == [f"m{r:02d}" for r in range(10)]
    # Slots 11-20 are the engine's: its own anchor finds are still in the answer.
    assert {"e0", "e1", "e2"} <= set(ids[10:])
    # ...and the guard is exactly the jina profile's behaviour.
    assert ids == [c.symbol_id for c in narrow(_jina_like_pool(), 20, profile=JINA)]


def test_guard_stays_k_monotonic_and_respects_the_container_cap():
    pool = _jina_like_pool()
    lists = [[c.symbol_id for c in narrow(pool, n, profile=JINA)] for n in range(1, 24)]
    for shorter, longer in zip(lists, lists[1:], strict=False):
        assert longer[: len(shorter)] == shorter
    # A guarded model rank that is a container still yields to the container cap (ceil(i*0.2)).
    classes = _pool(
        *[
            Candidate(symbol_id=f"c{r}", score=1.0, file_id="f", kind="class", semantic_rank=r)
            for r in range(5)
        ],
        Candidate(symbol_id="method", score=9.0, file_id="g", kind="method"),
    )
    ids = [c.symbol_id for c in narrow(classes, 3, profile=JINA)]
    assert ids[0] == "c0"
    assert ids.count("method") == 1
    assert sum(1 for i in ids if i.startswith("c")) <= 2


def test_guard_shorter_than_the_pool_falls_back_to_the_share_rule():
    profile = RetrievalProfile("t", 2, 0.5, 30)
    ids = [c.symbol_id for c in narrow(_jina_like_pool(), 6, profile=profile)]
    assert ids[:2] == ["m00", "m01"]
    # after the guard: the 0.5 floor is already met by the two guarded picks + agreed deep ranks,
    # so the engine's heads take over
    assert ids[2] == "m14"  # engine head: highest score in the pool
