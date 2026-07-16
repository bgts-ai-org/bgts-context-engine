"""Benchmark runner: drives the deterministic retrieval core and computes POC metrics.

A :class:`BenchCase` describes one task with its anchors, the commit it is pinned to, the ground-truth
set of relevant symbol_ids, and an optional principal scope. :func:`run_benchmark` runs each case,
times it, and aggregates recall / precision / determinism / RLS into a :class:`BenchReport` that
serializes to JSON.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from cce.core.auth.scope import Principal, ScopeFilter
from cce.core.orchestrator import RetrievalOrchestrator
from cce.core.orchestrator.anchors import find_anchors
from cce.storage.graph.repository import GraphRepository


@dataclass(slots=True)
class BenchCase:
    """One benchmark task with its ground truth."""

    name: str
    task_text: str
    #: Ground-truth relevant symbol_ids used to compute recall/precision.
    relevant_symbol_ids: list[str] = field(default_factory=list)
    explicit_symbols: list[str] = field(default_factory=list)
    route_paths: list[str] = field(default_factory=list)
    history_file_ids: list[str] = field(default_factory=list)
    component_repo_ids: list[str] = field(default_factory=list)
    repo_ids: list[str] | None = None
    commit: str | None = None
    max_candidates: int = 8
    #: Repos this task's principal may read (None = unrestricted). Used for the RLS check.
    allowed_repo_ids: list[str] | None = None
    #: Repos that MUST NOT appear in the result for a scoped principal (leak canaries).
    forbidden_repo_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CaseResult:
    name: str
    latency_ms: float
    recall: float
    precision: float
    precision_at_1: float
    mrr: float
    deterministic: bool
    rls_ok: bool
    returned_symbol_ids: list[str]


@dataclass(slots=True)
class BenchReport:
    cases: list[CaseResult]
    latency_median_ms: float
    latency_p95_ms: float
    recall_mean: float
    precision_mean: float
    precision_at_1_mean: float
    mrr_mean: float
    determinism_ok: bool
    rls_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "cases": len(self.cases),
                "latency_median_ms": round(self.latency_median_ms, 3),
                "latency_p95_ms": round(self.latency_p95_ms, 3),
                "recall_mean": round(self.recall_mean, 4),
                "precision_mean": round(self.precision_mean, 4),
                "precision_at_1_mean": round(self.precision_at_1_mean, 4),
                "mrr_mean": round(self.mrr_mean, 4),
                "determinism_ok": self.determinism_ok,
                "rls_ok": self.rls_ok,
            },
            "cases": [
                {
                    "name": c.name,
                    "latency_ms": round(c.latency_ms, 3),
                    "recall": round(c.recall, 4),
                    "precision": round(c.precision, 4),
                    "precision_at_1": round(c.precision_at_1, 4),
                    "mrr": round(c.mrr, 4),
                    "deterministic": c.deterministic,
                    "rls_ok": c.rls_ok,
                    "returned_symbol_ids": c.returned_symbol_ids,
                }
                for c in self.cases
            ],
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _run_once(
    orchestrator: RetrievalOrchestrator, case: BenchCase, scope: ScopeFilter | None
) -> list[str]:
    anchors = find_anchors(
        orchestrator.repository,
        task_text=case.task_text,
        explicit_symbols=case.explicit_symbols or None,
        route_paths=case.route_paths or None,
        history_file_ids=case.history_file_ids or None,
        component_repo_ids=case.component_repo_ids or None,
        repo_ids=case.repo_ids,
    )
    result = orchestrator.retrieve(
        anchors,
        commit=case.commit,
        max_candidates=case.max_candidates,
        scope_filter=(scope.allows if scope is not None else None),
    )
    return [c.symbol_id for c in result.candidates]


def _run_case(
    orchestrator: RetrievalOrchestrator, case: BenchCase, *, determinism_runs: int
) -> CaseResult:
    # Timed primary run (unrestricted principal for recall/precision unless a scope is defined).
    start = time.perf_counter()
    returned = _run_once(orchestrator, case, None)
    latency_ms = (time.perf_counter() - start) * 1000.0

    relevant = set(case.relevant_symbol_ids)
    returned_set = set(returned)
    hit = relevant & returned_set
    recall = len(hit) / len(relevant) if relevant else 1.0
    precision = len(hit) / len(returned_set) if returned_set else (1.0 if not relevant else 0.0)

    # Rank-sensitive metrics (order of `returned` matters).
    if not relevant:
        precision_at_1 = 1.0
        mrr = 1.0
    else:
        precision_at_1 = 1.0 if returned and returned[0] in relevant else 0.0
        mrr = 0.0
        for rank, sid in enumerate(returned, start=1):
            if sid in relevant:
                mrr = 1.0 / rank
                break

    # Determinism: repeat and require identical ordered output.
    deterministic = True
    for _ in range(max(0, determinism_runs - 1)):
        if _run_once(orchestrator, case, None) != returned:
            deterministic = False
            break

    # RLS: a scoped principal must not surface forbidden repos.
    rls_ok = True
    if case.allowed_repo_ids is not None or case.forbidden_repo_ids:
        scope = ScopeFilter(Principal(user_id="bench", allowed_repo_ids=case.allowed_repo_ids))
        scoped = _run_once(orchestrator, case, scope)
        forbidden = set(case.forbidden_repo_ids)
        for sid in scoped:
            repo = orchestrator.repository.repo_of_symbol(sid)
            if repo in forbidden or (
                case.allowed_repo_ids is not None and repo not in set(case.allowed_repo_ids)
            ):
                rls_ok = False
                break

    return CaseResult(
        name=case.name,
        latency_ms=latency_ms,
        recall=recall,
        precision=precision,
        precision_at_1=precision_at_1,
        mrr=mrr,
        deterministic=deterministic,
        rls_ok=rls_ok,
        returned_symbol_ids=returned,
    )


def run_benchmark(
    repository: GraphRepository,
    cases: list[BenchCase],
    *,
    determinism_runs: int = 3,
) -> BenchReport:
    """Run all cases and aggregate a :class:`BenchReport`."""
    orchestrator = RetrievalOrchestrator(repository)
    results = [_run_case(orchestrator, c, determinism_runs=determinism_runs) for c in cases]

    latencies = [r.latency_ms for r in results]
    return BenchReport(
        cases=results,
        latency_median_ms=statistics.median(latencies) if latencies else 0.0,
        latency_p95_ms=_percentile(latencies, 95),
        recall_mean=statistics.mean([r.recall for r in results]) if results else 0.0,
        precision_mean=statistics.mean([r.precision for r in results]) if results else 0.0,
        precision_at_1_mean=statistics.mean([r.precision_at_1 for r in results]) if results else 0.0,
        mrr_mean=statistics.mean([r.mrr for r in results]) if results else 0.0,
        determinism_ok=all(r.deterministic for r in results),
        rls_ok=all(r.rls_ok for r in results),
    )


def load_cases(path: str) -> list[BenchCase]:
    """Load bench cases from a JSON file (a list of case objects)."""
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases: list[BenchCase] = []
    for item in raw:
        cases.append(BenchCase(**item))
    return cases


def cases_to_json(cases: list[BenchCase]) -> list[dict[str, Any]]:
    return [asdict(c) for c in cases]
