"""POC benchmark harness (deterministic quality + performance metrics).

Measures the properties the architecture promises and turns them into a JSON report:

- **latency**: end-to-end retrieval time per task (median / p95), incl. a deep-traversal probe.
- **recall**: fraction of each task's known-relevant symbols surfaced in the ranked candidates.
- **precision**: fraction of returned candidates that are relevant (the 1000 -> N narrowing quality).
- **determinism**: same task + commit + scope must yield a byte-identical payload across N runs.
- **rls**: a scoped principal must never see out-of-scope repos (a leak fails the check).

The harness is transport-agnostic: it drives the same deterministic core the API/CLI use, so a report
reflects real engine behaviour, not a mock.
"""

from bce.bench.runner import BenchCase, BenchReport, run_benchmark

__all__ = ["BenchCase", "BenchReport", "run_benchmark"]
