"""Scoring Engine tests (spec section 6.4): the 1000->8 narrowing behaviour + determinism."""

from __future__ import annotations

from cce.core.scoring.engine import Candidate, score_candidates
from cce.domain.enums import Provenance, RefKind


def test_define_outranks_read_carrier():
    definer = Candidate(symbol_id="b_definer", ref_kind=str(RefKind.DEFINE), graph_distance=1, degree=5)
    carrier = Candidate(symbol_id="a_carrier", ref_kind=str(RefKind.READ), graph_distance=1, degree=5, is_leaf=True)
    ranked = score_candidates([carrier, definer])
    assert ranked[0].symbol_id == "b_definer"


def test_anchor_gets_priority_floor():
    anchor = Candidate(symbol_id="anchor", ref_kind=str(RefKind.READ), anchor=True, graph_distance=0)
    strong = Candidate(symbol_id="strong", ref_kind=str(RefKind.DEFINE), graph_distance=1, degree=10)
    ranked = score_candidates([strong, anchor])
    assert ranked[0].symbol_id == "anchor"


def test_exact_provenance_outweighs_heuristic_when_otherwise_equal():
    exact = Candidate(symbol_id="a", provenance=str(Provenance.SCIP), graph_distance=1)
    heuristic = Candidate(symbol_id="b", provenance=str(Provenance.HEURISTIC), graph_distance=1)
    ranked = score_candidates([heuristic, exact])
    assert ranked[0].symbol_id == "a"


def test_task_signal_boosts_candidate():
    signalled = Candidate(symbol_id="z_signal", task_signal=1.0, graph_distance=2)
    plain = Candidate(symbol_id="a_plain", task_signal=0.0, graph_distance=2)
    ranked = score_candidates([plain, signalled])
    assert ranked[0].symbol_id == "z_signal"


def test_scoring_is_deterministic_and_tie_breaks_by_id():
    a = Candidate(symbol_id="aaa", graph_distance=1)
    b = Candidate(symbol_id="bbb", graph_distance=1)
    ranked = score_candidates([b, a])
    # Identical features -> tie broken by symbol_id ascending.
    assert [c.symbol_id for c in ranked] == ["aaa", "bbb"]
