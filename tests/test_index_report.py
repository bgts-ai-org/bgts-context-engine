"""Index quality report: the census, the coverage an answer reports, ``bce doctor``'s text."""

from __future__ import annotations

from pathlib import Path

from bce.core.render.compact import render_compact
from bce.doctor import render
from bce.indexing.index_stats import IndexStatsBuilder, index_coverage
from bce.indexing.indexer import Indexer
from bce.storage.graph.repository import GraphRepository
from bce.tools.layer3.orchestration import _apply_index_coverage


class _NoConnClient:
    conn = None

    def execute(self, query, params=None):
        return None


def _report(files: dict[str, tuple[int, int]], dirs: dict[str, list[str]] | None = None) -> dict:
    """A stored report with ``language -> (files, indexed)``."""
    return {
        "files": sum(f for f, _ in files.values()),
        "indexed": sum(i for _, i in files.values()),
        "languages": {
            lang: {"files": f, "indexed": i, "parse_errors": 0} for lang, (f, i) in files.items()
        },
        "unparsed_dirs": dirs or {},
        "symbols": 10,
        "references": {"cross_file": 4, "resolved": 3},
        "scip": False,
        "embedding": {"enabled": True, "provider": "openai", "model": "m"},
    }


def test_the_census_counts_parsed_and_unparsed_code_and_ignores_other_files(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "broken.py").write_text("def g(:\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    for i in range(3):
        (tmp_path / "tools" / f"t{i}.rb").write_text("puts 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")

    from bce.indexing.gitsync import iter_source_files

    indexer = Indexer(GraphRepository(_NoConnClient()), embed=False)  # type: ignore[arg-type]
    census = IndexStatsBuilder()
    for rel, abs_path in iter_source_files(tmp_path, indexer._census_extensions()):
        indexer._extract_counted(census, "r", rel, abs_path, "c1")
    stats = indexer._write_stats(census, {"refs": 4, "resolved": 1}, "r", "c1")

    assert stats["files"] == 5 and stats["indexed"] == 2
    assert stats["languages"]["python"] == {"files": 2, "indexed": 2, "parse_errors": 1}
    assert stats["languages"]["ruby"] == {"files": 3, "indexed": 0, "parse_errors": 0}
    assert stats["unparsed_dirs"] == {"ruby": ["tools"]}
    assert stats["symbols"] >= 1
    assert stats["references"] == {"cross_file": 4, "resolved": 1}


def test_index_coverage_lists_only_meaningful_unparsed_languages():
    coverage = index_coverage(
        {
            "r1": {"stats": _report({"python": (90, 90), "go": (60, 0)}, {"go": ["cmd", "pkg"]})},
            "r2": {"stats": _report({"python": (49, 49), "lua": (1, 0)})},
        }
    )
    assert coverage == {
        "files": 200,
        "indexed": 139,
        "ratio": 0.695,
        "unparsed": [{"language": "go", "files": 60, "indexed": 0, "dirs": ["cmd", "pkg"]}],
    }
    assert index_coverage({}) is None


class _StatsRepo:
    def __init__(self, stats):
        self._stats = stats

    def index_stats(self, repo_ids=None):
        return self._stats


def test_a_mostly_unparsed_repository_is_unsupported():
    coverage = {"confidence": "high", "state": "known"}
    _apply_index_coverage(
        coverage, _StatsRepo({"r": {"stats": _report({"python": (5, 5), "go": (95, 0)})}}), None
    )
    assert coverage["confidence"] == "unsupported"
    assert coverage["state"] == "unknown"
    assert coverage["index"]["partial"] is True

    full = {"confidence": "high", "state": "known"}
    _apply_index_coverage(full, _StatsRepo({"r": {"stats": _report({"python": (5, 5)})}}), None)
    assert full["confidence"] == "high" and full["state"] == "known"
    assert full["index"]["partial"] is False

    untouched = {"confidence": "high"}
    _apply_index_coverage(untouched, _StatsRepo({}), None)
    assert "index" not in untouched


def _payload(coverage: dict) -> dict:
    return {"commit": "abc1234", "context": {"items": []}, "files": [], "coverage": coverage}


def test_the_compact_view_says_how_much_of_the_code_the_index_parses():
    coverage = {"confidence": "medium", "state": "known"}
    stats = _report({"python": (60, 60), "go": (40, 0)}, {"go": ["server", "cmd"]})
    _apply_index_coverage(coverage, _StatsRepo({"r": {"stats": stats}}), None)
    text = render_compact(_payload(coverage), max_tokens=2000)
    head = text.splitlines()[0]
    assert "coverage unknown" in head and "index parses 60 % of the code" in head
    assert (
        "- The index parses 60 % of this repository's code files; not parsed: go (40 files, in "
        "`server`, `cmd`). Search those parts with your own tools." in text
    )

    unsupported = {"confidence": "medium", "state": "known"}
    stats = _report({"python": (1, 1), "go": (99, 0)})
    _apply_index_coverage(unsupported, _StatsRepo({"r": {"stats": stats}}), None)
    text = render_compact(_payload(unsupported), max_tokens=2000)
    assert "confidence unsupported" in text.splitlines()[0]
    assert "Rely on your own search" in text

    complete = {"confidence": "high", "state": "known"}
    _apply_index_coverage(complete, _StatsRepo({"r": {"stats": _report({"python": (9, 9)})}}), None)
    assert "index parses" not in render_compact(_payload(complete), max_tokens=2000)


def test_doctor_text_reports_each_repository():
    report = {
        "database": {"ok": True, "target": "bce@localhost:5432/bce", "error": None},
        "migrations": {"applied": 13, "pending": ["0014_repo_index_stats"]},
        "parsers": {"installed": ["python", "rust"], "missing": ["cpp"]},
        "embedding": {"provider": "openai", "model": "m", "dim": 1024},
        "retrieval": {"profile": "voyage", "expansion": "hops"},
        "repos": [
            {
                "repo_id": "r",
                "name": "acme/api",
                "commit": "0123456789abcdef",
                "computed_at": None,
                "stats": _report({"python": (8, 8), "go": (92, 0)}, {"go": ["cmd"]}),
            },
            {
                "repo_id": "old",
                "name": "acme/old",
                "commit": None,
                "computed_at": None,
                "stats": None,
            },
        ],
    }
    text = "\n".join(render(report))
    assert "pending: 0014_repo_index_stats (run `bce migrate`)" in text
    assert "not installed: cpp" in text
    assert "repo acme/api @ 0123456" in text
    assert "code files   100, parsed 8 (8.0 %)" in text
    assert "not parsed: cmd" in text
    assert "verdict      unsupported" in text
    assert "repo acme/old @ ?" in text and "no index report" in text
