"""Coverage/Confidence (section 8 + god node) and Token-Budget Assembler (section 6.5) tests."""

from __future__ import annotations

import pytest

from bce.core.assembler import DetailLevel, assemble
from bce.core.coverage import apply_target_confidence, compute_coverage
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


FILES = [
    {"file_id": "r:a.py", "tier": "full", "relevance": 0.65},
    {"file_id": "r:b.py", "tier": "stub", "relevance": 0.95},
]


@pytest.mark.parametrize(
    ("files", "level", "best", "unconfirmed", "after"),
    [
        # the best full file (a stub does not count) is under the bar: capped at low
        (FILES, "high", 0.65, True, "low"),
        ([{**FILES[0], "relevance": 0.9}, FILES[1]], "medium", 0.9, False, "medium"),
        ([FILES[1]], "medium", 0.0, True, "low"),  # no full file at all
        (FILES, "unsupported", 0.65, True, "unsupported"),  # the index verdict stands
    ],
)
def test_the_selector_caps_confidence_when_no_full_file_is_a_likely_edit_target(
    files, level, best, unconfirmed, after
):
    cov = {"confidence": level}
    apply_target_confidence(cov, files, {"status": "ok"})
    assert cov == {
        "confidence": after,
        "target_relevance": best,
        "target_unconfirmed": unconfirmed,
    }


@pytest.mark.parametrize("selection", [None, {"status": "error"}, {"status": "timeout"}])
def test_without_a_selector_verdict_confidence_is_left_alone(selection):
    cov = {"confidence": "high"}
    apply_target_confidence(cov, FILES, selection)
    assert cov == {"confidence": "high"}


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
