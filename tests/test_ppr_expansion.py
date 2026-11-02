"""Personalized PageRank expansion (``expansion="ppr"``): bridges up, hub fan-out down, determinism."""

from __future__ import annotations

from bce.config import Settings
from bce.core.orchestrator.expand import HOP_DECAY, PPR_MAX_MULTIPLIER, expand_from_anchors
from bce.core.orchestrator.profile import active_profile

#: ``bridge`` calls both anchors; ``a`` has one more caller, ``b`` is a hub with five more.
_CALLERS = {
    "a": ["bridge", "a_caller"],
    "b": ["bridge", *(f"b_caller{i}" for i in range(5))],
}


class _Repo:
    def repo_of_symbol(self, sid):
        return "r1"

    def get_symbol(self, sid):
        return None

    def symbols_in_file(self, fid):
        return []

    def get_callers(self, sid):
        return [{"symbol_id": s, "provenance": "treesitter"} for s in _CALLERS.get(sid, [])]

    def get_callees(self, sid):
        return [
            {"symbol_id": a, "provenance": "treesitter"} for a, cs in _CALLERS.items() if sid in cs
        ]

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def find_implementers(self, sid):
        return []


def _strengths(expansion):
    return {sid: info.source_strength for sid, info in sorted(expansion.items())}


def _expand(mode: str):
    return expand_from_anchors(_Repo(), ["a", "b"], strengths={"a": 0.9, "b": 0.9}, expansion=mode)


def test_hops_gives_every_first_hop_the_same_evidence():
    hops = _strengths(_expand("hops"))
    assert hops == _strengths(
        expand_from_anchors(_Repo(), ["a", "b"], strengths={"a": 0.9, "b": 0.9})
    )
    first_hop = round(0.9 * HOP_DECAY, 6)
    assert hops["bridge"] == hops["a_caller"] == hops["b_caller0"] == first_hop


def test_ppr_lifts_a_bridge_above_a_hubs_fan_out():
    hops = _strengths(_expand("hops"))
    ppr = _strengths(_expand("ppr"))
    assert ppr["bridge"] > hops["bridge"]
    assert ppr["bridge"] > ppr["a_caller"] > ppr["b_caller0"]
    # the hub's callers are the typical node here (the median): they keep their hop evidence
    assert ppr["b_caller0"] == hops["b_caller0"]
    assert ppr["bridge"] <= round(hops["bridge"] * PPR_MAX_MULTIPLIER, 6)
    # anchors keep their own evidence
    assert ppr["a"] == hops["a"] == 0.9


def test_ppr_is_byte_identical_across_runs():
    first = _expand("ppr")
    second = _expand("ppr")
    assert repr(sorted(first.items())) == repr(sorted(second.items()))


def test_the_expansion_flag_is_read_from_settings():
    assert active_profile(Settings()).expansion == "hops"
    assert active_profile(Settings(retrieval_expansion="PPR")).expansion == "ppr"
    assert active_profile(Settings(retrieval_expansion="pagerank")).expansion == "hops"
