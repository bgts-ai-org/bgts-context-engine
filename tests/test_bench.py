"""Tests for the POC benchmark harness (against a deterministic fake repository)."""

from __future__ import annotations

from cce.bench.runner import BenchCase, run_benchmark


class _FakeRepo:
    """A tiny graph: setup -> helper (r1); alpha lives in a separate repo r2."""

    def __init__(self):
        self._repo = {"setup": "r1", "helper": "r1", "alpha": "r2"}

    def resolve_symbol(self, name, repo_id=None):
        if name in self._repo:
            return [{"symbol_id": name, "file_id": f"{name}.py", "line": 1}]
        return []

    def find_routes(self, path_substring=None):
        return []

    def repo_of_symbol(self, sid):
        return self._repo.get(sid)

    def get_symbol(self, sid):
        if sid in self._repo:
            return {"symbol_id": sid, "name": sid, "file_id": f"{sid}.py", "line": 1}
        return None

    def symbols_in_file(self, fid):
        return []

    def get_callers(self, sid):
        return [{"symbol_id": "setup", "provenance": "treesitter"}] if sid == "helper" else []

    def get_callees(self, sid):
        return [{"symbol_id": "helper", "provenance": "treesitter"}] if sid == "setup" else []

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def find_implementers(self, sid):
        return []

    def symbol_degree(self, sid):
        return 1

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []


def test_benchmark_recall_precision_and_determinism():
    cases = [
        BenchCase(
            name="find helper",
            task_text="fix setup which calls helper",
            explicit_symbols=["setup"],
            relevant_symbol_ids=["helper", "setup"],
        )
    ]
    report = run_benchmark(_FakeRepo(), cases, determinism_runs=3)
    assert report.recall_mean == 1.0
    assert report.precision_mean == 1.0
    assert report.precision_at_1_mean == 1.0
    assert report.mrr_mean == 1.0
    assert report.determinism_ok is True
    assert report.rls_ok is True
    data = report.to_dict()
    assert data["summary"]["cases"] == 1
    assert "latency_median_ms" in data["summary"]
    assert "precision_at_1_mean" in data["summary"]
    assert "mrr_mean" in data["summary"]


def test_benchmark_report_serializes_case_fields():
    cases = [
        BenchCase(name="c", task_text="setup", explicit_symbols=["setup"], relevant_symbol_ids=["setup"])
    ]
    report = run_benchmark(_FakeRepo(), cases)
    case = report.to_dict()["cases"][0]
    assert set(case) >= {"name", "latency_ms", "recall", "precision", "deterministic", "rls_ok"}
