"""Coverage/Confidence (section 8 + god node) and Token-Budget Assembler (section 6.5) tests."""

from __future__ import annotations

from bce.core.assembler import DetailLevel, assemble
from bce.core.coverage import compute_coverage
from bce.core.coverage.confidence import GOD_NODE_DEGREE
from bce.core.orchestrator.anchors import AnchorResult
from bce.core.orchestrator.orchestrator import RetrievalResult
from bce.core.scoring.engine import Candidate


def _result(candidates, anchors: AnchorResult) -> RetrievalResult:
    return RetrievalResult(commit="c1", anchors=anchors, candidates=candidates)


def test_high_confidence_with_multiple_sources_and_clear_margin():
    anchors = AnchorResult()
    anchors.add("s1", "explicit")
    anchors.add("s1", "jira")
    anchors.add("s2", "history")
    cands = [
        Candidate(symbol_id="s1", graph_distance=0, anchor=True, score=10.0),
        Candidate(symbol_id="s2", graph_distance=1, score=2.0),
    ]
    cov = compute_coverage(_result(cands, anchors))
    assert cov["confidence"] == "high"
    assert cov["anchor_count"] == 2
    assert cov["anchor_source_count"] == 3


def test_low_confidence_single_source_small_margin():
    anchors = AnchorResult()
    anchors.add("s1", "semantic")
    cands = [
        Candidate(symbol_id="s1", graph_distance=3, score=1.0),
        Candidate(symbol_id="s2", graph_distance=3, score=0.9),
    ]
    cov = compute_coverage(_result(cands, anchors))
    assert cov["confidence"] == "low"


def test_god_node_flagged():
    anchors = AnchorResult()
    anchors.add("hub", "explicit")
    cands = [Candidate(symbol_id="hub", graph_distance=0, anchor=True, degree=GOD_NODE_DEGREE + 5)]
    cov = compute_coverage(_result(cands, anchors))
    assert cov["touches_god_node"] is True
    assert cov["max_centrality_in_context"] == GOD_NODE_DEGREE + 5


def test_provenance_distribution_sums_to_one_when_present():
    anchors = AnchorResult()
    anchors.add("s1", "explicit")
    cands = [
        Candidate(symbol_id="s1", provenance="scip"),
        Candidate(symbol_id="s2", provenance="treesitter"),
        Candidate(symbol_id="s3", provenance="heuristic"),
        Candidate(symbol_id="s4", provenance="heuristic"),
    ]
    dist = compute_coverage(_result(cands, anchors))["provenance_distribution"]
    assert abs(sum(dist.values()) - 1.0) < 1e-6
    assert dist["heuristic"] == 0.5


def test_assembler_respects_budget_and_assigns_detail_by_distance():
    items = [
        {
            "symbol_id": "near",
            "graph_distance": 1,
            "name": "near",
            "signature": "()",
            "body": "x" * 40,
        },
        {
            "symbol_id": "mid",
            "graph_distance": 2,
            "name": "mid",
            "signature": "(a, b)",
            "docstring": "d",
        },
        {"symbol_id": "far", "graph_distance": 5, "name": "far", "file_id": "f", "line": 3},
    ]
    pkg = assemble(items, max_tokens=100)
    by_id = {it["symbol_id"]: it for it in pkg["items"]}
    assert by_id["near"]["detail_level"] == str(DetailLevel.FULL)
    assert by_id["mid"]["detail_level"] == str(DetailLevel.SIGNATURE)
    assert by_id["far"]["detail_level"] == str(DetailLevel.REFERENCE)
    assert pkg["used_tokens"] <= 100


def test_assembler_skips_overflow_but_keeps_cheaper_items():
    items = [
        {"symbol_id": "huge", "graph_distance": 1, "name": "huge", "body": "y" * 400},
        {"symbol_id": "tiny", "graph_distance": 5, "name": "tiny", "file_id": "f", "line": 1},
    ]
    pkg = assemble(items, max_tokens=5)
    ids = {it["symbol_id"] for it in pkg["items"]}
    # The tiny reference-only item fits even when the huge one does not (recall > precision, P4).
    assert "tiny" in ids


def test_assembler_is_deterministic():
    items = [
        {"symbol_id": "a", "graph_distance": 1, "name": "a", "signature": "()"},
        {"symbol_id": "b", "graph_distance": 2, "name": "b", "signature": "(x)"},
    ]
    assert assemble(items, max_tokens=50) == assemble(items, max_tokens=50)
