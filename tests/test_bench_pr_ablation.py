"""Pure-function tests for the PR replay ablation benchmark (no database, no git, no model)."""

from __future__ import annotations

from bce.bench.pr_ablation import (
    AblationReport,
    CaseOutcome,
    PRSpec,
    clean_pr_text,
    compute_metrics,
    innermost_overlapping,
    parse_unified_diff,
    semantic_retention,
    task_variants,
)

_DIFF = """\
diff --git a/src/a.cs b/src/a.cs
index 1..2 100644
--- a/src/a.cs
+++ b/src/a.cs
@@ -10,3 +10,4 @@ class A
-old
+new
@@ -40,0 +42,2 @@ class A
+inserted
diff --git a/src/new.cs b/src/new.cs
new file mode 100644
--- /dev/null
+++ b/src/new.cs
@@ -0,0 +1,3 @@
+brand new
diff --git a/src/old.cs b/src/renamed.cs
similarity index 90%
rename from src/old.cs
rename to src/renamed.cs
--- a/src/old.cs
+++ b/src/renamed.cs
@@ -5 +5 @@
-x
+y
"""


def test_parse_unified_diff_maps_old_side_ranges_and_skips_new_files() -> None:
    ranges = parse_unified_diff(_DIFF)
    assert ranges["src/a.cs"] == [(10, 12), (40, 41)]  # insertion widened by one line
    assert ranges["src/old.cs"] == [(5, 5)]  # renames keyed by the pre-change path
    assert "src/new.cs" not in ranges


def _sym(sid: str, line: int, end: int) -> dict:
    return {"symbol_id": sid, "name": sid, "kind": "method", "line": line, "end_line": end}


def test_innermost_overlapping_prefers_leaf_symbols() -> None:
    symbols = [
        _sym("Class", 1, 100),
        _sym("Class.m1", 10, 20),
        _sym("Class.m2", 30, 40),
        _sym("Other", 200, 210),
    ]
    leaves = innermost_overlapping(symbols, [(12, 12), (35, 60)])
    assert [s["symbol_id"] for s in leaves] == ["Class.m1", "Class.m2"]

    # A change in the class body outside any method credits the class itself.
    leaves = innermost_overlapping(symbols, [(50, 50)])
    assert [s["symbol_id"] for s in leaves] == ["Class"]


def test_compute_metrics_rank_and_file_level() -> None:
    m = compute_metrics(
        ["x", "b", "a", "x"],
        ["a", "b", "c", "d"],
        returned_files=["f1", "f2", "f1", "f1"],
        relevant_files=["f1", "f3"],
    )
    assert m.recall == 0.5
    assert m.precision == 2 / 3  # duplicates collapse: x, b, a
    assert m.mrr == 0.5  # first hit at rank 2
    assert m.hit == 1.0
    assert m.file_recall == 0.5 and m.file_precision == 0.5
    assert round(m.f1, 4) == round(2 * (2 / 3) * 0.5 / ((2 / 3) + 0.5), 4)

    empty = compute_metrics([], ["a"], returned_files=[], relevant_files=["f"])
    assert empty.recall == 0.0 and empty.precision == 0.0 and empty.hit == 0.0


def test_lenient_recall_credits_enclosing_symbols_only() -> None:
    returned_spans = {
        "Class": {"file_id": "f", "line": 1, "end_line": 100},
        "Elsewhere": {"file_id": "g", "line": 1, "end_line": 100},
    }
    relevant_spans = {
        "Class.m": {"file_id": "f", "line": 10, "end_line": 20},
        "Other.m": {"file_id": "h", "line": 10, "end_line": 20},
    }
    m = compute_metrics(
        ["Class", "Elsewhere"],
        ["Class.m", "Other.m"],
        returned_files=["f", "g"],
        relevant_files=["f", "h"],
        returned_spans=returned_spans,
        relevant_spans=relevant_spans,
    )
    assert m.recall == 0.0  # strict: the method itself was not returned
    assert m.recall_lenient == 0.5  # its class was; the unrelated file does not count


def test_task_variants_strip_bitbucket_escapes() -> None:
    spec = PRSpec(number=1, title="fix\\(MSA-1\\): title", description="* body \\_x\\_\u200b")
    variants = task_variants(spec)
    assert variants["title"] == "fix(MSA-1): title"
    assert variants["full"] == "fix(MSA-1): title\n\n* body _x_"
    assert clean_pr_text("") == ""


def _outcome(
    number: int,
    scored: bool,
    voyage_recall: float,
    bce_recall: float,
    split: str = "tune",
) -> CaseOutcome:
    def metrics(recall: float) -> dict:
        return {
            "metrics": {
                "recall": recall,
                "precision": recall,
                "f1": recall,
                "mrr": recall,
                "hit": 1.0 if recall else 0.0,
                "recall_lenient": recall,
                "file_recall": recall,
                "file_precision": recall,
                "returned": 10,
                "relevant": 4,
            },
            "latency_ms": 100.0,
            "returned": [],
            "hits": [],
            "kinds": {"method": 8, "class": 2},
            "semantic_retention": recall,
            "timings": {"expand_ms": 40.0, "narrow_ms": 2.0},
        }

    return CaseOutcome(
        number=number,
        title="t",
        scored=scored,
        split=split,
        variant="full",
        db_name="db",
        relevant_symbols=4,
        relevant_files=2,
        systems={
            "voyage_raw": metrics(voyage_recall),
            "voyage": metrics(voyage_recall),
            "bce": metrics(bce_recall),
        },
    )


def test_aggregate_excludes_unscored_and_reports_gain() -> None:
    report = AblationReport(
        k=10,
        encoder_model="voyage-code-4-1024",
        variants=["full"],
        cases=[
            _outcome(1, True, 0.25, 0.75),
            _outcome(2, True, 0.5, 0.5),
            _outcome(3, False, 0.0, 1.0),  # not scored: must not move the aggregate
        ],
    )
    agg = report.aggregate("full")
    assert agg["cases"] == 2
    assert agg["systems"]["voyage"]["recall"] == 0.375
    assert agg["systems"]["bce"]["recall"] == 0.625
    gain = agg["gain_bce_vs_voyage"]["recall"]
    assert gain["abs_pp"] == 25.0
    assert gain["rel_pct"] == 66.7
    assert (gain["wins"], gain["ties"], gain["losses"]) == (1, 1, 0)
    assert agg["returned_kind_share"]["bce"] == {"class": 0.2, "method": 0.8, "_containers": 0.2}
    assert agg["stage_ms_median"] == {"expand_ms": 40.0, "narrow_ms": 2.0}
    assert report.aggregate("full", scored_only=False)["cases"] == 3


def test_splits_are_aggregated_separately() -> None:
    report = AblationReport(
        k=10,
        encoder_model="voyage-code-4-1024",
        variants=["full"],
        cases=[
            _outcome(1, True, 0.2, 0.4, split="tune"),
            _outcome(2, True, 0.8, 0.6, split="holdout"),
        ],
    )
    tune = report.aggregate("full", split="tune")
    holdout = report.aggregate("full", split="holdout")
    assert (tune["cases"], tune["prs"]) == (1, [1])
    assert (holdout["cases"], holdout["prs"]) == (1, [2])
    # A constant fitted on the tune PR cannot flatter the holdout number.
    assert tune["gain_bce_vs_voyage"]["recall"]["abs_pp"] == 20.0
    assert holdout["gain_bce_vs_voyage"]["recall"]["abs_pp"] == -20.0
    assert report.aggregate("full")["cases"] == 2


def test_semantic_retention_counts_only_the_baseline_truths() -> None:
    relevant = {"a", "b"}
    # Voyage found a and b; the engine kept a and swapped b for a wrong guess.
    assert semantic_retention(["a", "b", "x"], ["a", "y"], relevant) == 0.5
    assert semantic_retention(["a", "b"], ["a", "b"], relevant) == 1.0
    # Nothing to retain when the baseline found nothing: the metric is undefined, not zero.
    assert semantic_retention(["x"], ["a"], relevant) is None
