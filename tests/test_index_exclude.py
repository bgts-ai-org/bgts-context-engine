"""File exclusion for indexing: built-in minified patterns, ``.bceignore``, config and heuristic."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bce.config import get_settings
from bce.domain.enums import NodeLabel
from bce.domain.models import GraphFragment
from bce.indexing.gitsync import (
    DEFAULT_EXCLUDES,
    ExcludeRules,
    build_rules,
    iter_source_files,
    load_rules,
    looks_minified,
)
from bce.indexing.gitsync.exclude import Match, parse_patterns
from bce.indexing.indexer import Indexer

#: One long line, as a minified bundle has (well above the 300-byte mean and the 1 KiB floor).
MINIFIED = b"var a=function(){return 1};" * 200 + b"\n"
NORMAL = b"export function loadBuilds(project) {\n  return fetch(project);\n}\n"


def _rules(*patterns: str, threshold: int = 300) -> ExcludeRules:
    return build_rules(None, patterns, threshold)


# --- pattern semantics ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "public/pdf.worker.min.mjs",
        "a/b/jquery.min.js",
        "lib.min.cjs",
        "static/app.bundle.js",
    ],
)
def test_default_patterns_exclude_minified_and_bundled_files(path: str) -> None:
    assert _rules().path_excluded(path)


@pytest.mark.parametrize("path", ["src/main.js", "minimal.js", "src/min.js", "bundle.ts"])
def test_default_patterns_keep_ordinary_sources(path: str) -> None:
    assert not _rules().path_excluded(path)


def test_unanchored_name_matches_at_any_depth_and_excludes_directory_contents() -> None:
    rules = _rules("vendor")
    assert rules.path_excluded("vendor/x.js")
    assert rules.path_excluded("web/vendor/deep/x.js")
    assert not rules.path_excluded("src/vendored.js")


def test_directory_only_pattern_does_not_match_a_file_of_that_name() -> None:
    rules = _rules("gen/")
    assert rules.path_excluded("src/gen/a.ts")
    assert not rules.path_excluded("src/gen")


def test_slash_anchors_the_pattern_to_the_repository_root() -> None:
    rules = _rules("/build.js", "public/*.mjs")
    assert rules.path_excluded("build.js")
    assert not rules.path_excluded("tools/build.js")
    assert rules.path_excluded("public/worker.mjs")
    assert not rules.path_excluded("web/public/worker.mjs")
    assert not rules.path_excluded("public/sub/worker.mjs")  # ``*`` never crosses a slash


def test_double_star_spans_any_number_of_directories() -> None:
    rules = _rules("**/generated/**", "docs/**/*.py")
    assert rules.path_excluded("generated/a.ts")
    assert rules.path_excluded("x/y/generated/z/a.ts")
    assert rules.path_excluded("docs/conf.py")
    assert rules.path_excluded("docs/a/b/conf.py")
    assert not rules.path_excluded("src/docs/conf.py")


def test_question_mark_and_character_classes() -> None:
    rules = _rules("file?.js", "v[0-9].ts", "x[!a].py")
    assert rules.path_excluded("file1.js")
    assert not rules.path_excluded("file10.js")
    assert rules.path_excluded("v3.ts")
    assert not rules.path_excluded("vx.ts")
    assert rules.path_excluded("xb.py")
    assert not rules.path_excluded("xa.py")


def test_comments_blanks_and_escapes() -> None:
    rules = _rules("# a comment", "", "   ", r"\#literal.js", r"\!bang.js")
    assert rules.path_excluded("#literal.js")
    assert rules.path_excluded("!bang.js")
    assert not rules.path_excluded("a comment")


def test_last_matching_pattern_wins_and_negation_reincludes() -> None:
    rules = _rules("*.generated.ts", "!keep.generated.ts")
    assert rules.match("a.generated.ts") is Match.EXCLUDED
    assert rules.match("keep.generated.ts") is Match.INCLUDED
    assert rules.match("src/other.ts") is Match.NONE
    # A negation in .bceignore (after the defaults) re-includes a built-in exclusion.
    assert not build_rules("!vendor/keep.min.js", ()).path_excluded("vendor/keep.min.js")


def test_a_file_inside_an_excluded_directory_cannot_be_reincluded() -> None:
    """Same rule as git: only the directory itself can be negated."""
    assert _rules("vendor/", "!vendor/keep.js").path_excluded("vendor/keep.js")
    assert not _rules("vendor/", "!vendor/").path_excluded("vendor/keep.js")


def test_parse_patterns_splits_on_commas_and_newlines() -> None:
    assert parse_patterns(" a.js, b/ ,\n**/c.ts,,") == ("a.js", "b/", "**/c.ts")
    assert parse_patterns("") == ()


# --- minification heuristic ----------------------------------------------------------------


def test_looks_minified_flags_long_line_content_only() -> None:
    assert looks_minified(MINIFIED)
    assert not looks_minified(NORMAL * 100)
    assert not looks_minified(b"x" * 500)  # under the size floor: a one-liner is not a bundle
    assert not looks_minified(MINIFIED, max_avg_line_length=0)  # 0 disables


def test_excluded_combines_patterns_with_the_heuristic() -> None:
    rules = _rules("!public/keep.js")
    assert rules.excluded("public/pdf.worker.mjs", MINIFIED)  # no pattern: heuristic decides
    assert not rules.excluded("src/app.js", NORMAL)
    assert not rules.excluded("public/keep.js", MINIFIED)  # explicit re-include wins
    assert not _rules(threshold=0).excluded("public/pdf.worker.mjs", MINIFIED)


# --- walking and configuration ------------------------------------------------------------


def _tree(root: Path) -> None:
    files = {
        "src/app.js": NORMAL,
        "src/util.py": b"def f():\n    return 1\n",
        "public/pdf.worker.min.mjs": MINIFIED,
        "public/legacy.js": MINIFIED,  # minified without the ``.min`` name
        "third_party/lib/x.js": NORMAL,
        "gen/schema.ts": NORMAL,
    }
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_iter_source_files_skips_path_excluded_files(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / ".bceignore").write_text("# vendored\nthird_party/\n", encoding="utf-8")
    exts = (".js", ".mjs", ".py", ".ts")
    rules = load_rules(tmp_path, ["gen/*.ts"])
    paths = [rel for rel, _ in iter_source_files(tmp_path, exts, exclude=rules)]
    assert paths == ["public/legacy.js", "src/app.js", "src/util.py"]
    assert [rel for rel, _ in iter_source_files(tmp_path, exts)] == [
        "gen/schema.ts",
        "public/legacy.js",
        "public/pdf.worker.min.mjs",
        "src/app.js",
        "src/util.py",
        "third_party/lib/x.js",
    ]


def test_load_rules_order_is_defaults_then_bceignore_then_extra(tmp_path: Path) -> None:
    (tmp_path / ".bceignore").write_text("gen/\n", encoding="utf-8")
    rules = load_rules(tmp_path, ["!gen/"])
    assert rules.patterns == (*DEFAULT_EXCLUDES, "gen/", "!gen/")
    assert not rules.path_excluded("gen/schema.ts")


def test_env_patterns_reach_the_indexer_before_explicit_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BCE_INDEX_EXCLUDE", "gen/,*.pb.go")
    monkeypatch.setenv("BCE_INDEX_MINIFIED_LINE_LENGTH", "500")
    get_settings.cache_clear()
    try:
        indexer = Indexer(_FakeRepository(), embed=False, use_scip=False, exclude=["!gen/"])  # type: ignore[arg-type]
    finally:
        get_settings.cache_clear()
    assert indexer.exclude_patterns == ("gen/", "*.pb.go", "!gen/")
    assert indexer.minified_line_length == 500


# --- indexer integration (fake graph, real files) -----------------------------------------


class _FakeRepository:
    client = SimpleNamespace(conn=None)

    def __init__(self) -> None:
        self.dropped: list[str] = []

    def counts(self) -> tuple[int, int]:
        return 0, 0

    def symbols_in_file(self, file_id: str) -> list[dict[str, Any]]:
        return []

    def delete_file_subgraph(self, file_id: str) -> None:
        self.dropped.append(file_id)


class _RecordingUpserter:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def ensure_repo_node(self, *args: Any, **kwargs: Any) -> None:
        pass

    def upsert_file_fragment(self, fragment: GraphFragment) -> None:
        for node in fragment.nodes:
            if node.label is NodeLabel.FILE:
                self.paths.append(node.properties["path"])


def _indexer(**kwargs: Any) -> tuple[Indexer, _RecordingUpserter, _FakeRepository]:
    repository = _FakeRepository()
    indexer = Indexer(repository, embed=False, use_scip=False, **kwargs)  # type: ignore[arg-type]
    upserter = _RecordingUpserter()
    indexer.upserter = upserter  # type: ignore[assignment]
    indexer._record_repo_row = lambda *a, **k: None  # type: ignore[method-assign]
    return indexer, upserter, repository


def test_full_index_skips_excluded_and_minified_files_deterministically(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / ".bceignore").write_text("third_party/\n", encoding="utf-8")

    runs = []
    for _ in range(2):
        indexer, upserter, repository = _indexer(exclude=["gen/"])
        summary = indexer.index_local_repo(path=tmp_path, name="demo", commit="c1")
        runs.append((upserter.paths, summary.files, summary.excluded, repository.dropped))

    assert runs[0] == runs[1]
    paths, files, excluded, dropped = runs[0]
    assert paths == ["src/app.js", "src/util.py"]
    assert (files, excluded) == (2, 4)
    # A full index only upserts, so files indexed before a rule existed are dropped explicitly.
    assert dropped == [
        "demo:gen/schema.ts",
        "demo:public/legacy.js",
        "demo:public/pdf.worker.min.mjs",
        "demo:third_party/lib/x.js",
    ]


git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "tester@example.com", cwd=work)
    _git("config", "user.name", "Tester", cwd=work)
    _tree(work)
    (work / ".bceignore").write_text("third_party/\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    return work


@git_required
def test_reindex_skips_excluded_files_in_the_diff(tmp_path: Path) -> None:
    work = _git_repo(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=work)
    (work / "public" / "pdf.worker.min.mjs").write_bytes(MINIFIED + b"//2\n")
    (work / "public" / "legacy.js").write_bytes(MINIFIED + b"//2\n")
    (work / "src" / "app.js").write_bytes(NORMAL + b"// changed\n")
    _git("commit", "-am", "touch", cwd=work)

    indexer, upserter, repository = _indexer()
    summary = indexer.index_incremental(path=work, name="demo", since_commit=base)

    assert (summary.modified, summary.excluded) == (1, 2)
    # Every supported file the full index would see is re-linked, but only app.js is re-upserted.
    assert upserter.paths == ["src/app.js"]
    # Excluded changes are still dropped, so a file indexed before the rule existed disappears.
    assert {fid.rsplit(":", 1)[-1] for fid in repository.dropped} >= {
        "public/legacy.js",
        "public/pdf.worker.min.mjs",
    }


@git_required
def test_reindex_applies_a_changed_bceignore_to_unchanged_files(tmp_path: Path) -> None:
    work = _git_repo(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=work)
    # Re-include third_party/ and exclude src/util.py; neither file itself changes.
    (work / ".bceignore").write_text("src/util.py\n", encoding="utf-8")
    _git("commit", "-am", "ignore rules", cwd=work)

    indexer, upserter, repository = _indexer()
    summary = indexer.index_incremental(path=work, name="demo", since_commit=base)

    assert (summary.added, summary.modified, summary.excluded) == (1, 0, 1)
    assert upserter.paths == ["third_party/lib/x.js"]
    dropped = [fid for fid in repository.dropped if fid.endswith("src/util.py")]
    assert dropped, repository.dropped


def test_full_index_drops_only_excluded_files_the_graph_still_holds(tmp_path: Path) -> None:
    """With a graph lookup, files never indexed (or already gone) cost no delete statements."""
    _tree(tmp_path)

    class _GraphWithFiles(_FakeRepository):
        client = SimpleNamespace(conn=SimpleNamespace(execute=lambda *a, **k: None))

        def indexed_file_ids(self, repo_id: str) -> set[str]:
            # Left over from an earlier run: the bundle's routes outlived its file node.
            return {"demo:public/pdf.worker.min.mjs", "demo:src/app.js"}

    repository = _GraphWithFiles()
    indexer = Indexer(repository, embed=False, use_scip=False)  # type: ignore[arg-type]
    indexer.upserter = _RecordingUpserter()  # type: ignore[assignment]
    indexer._record_repo_row = lambda *a, **k: None  # type: ignore[method-assign]
    indexer._record_churn = lambda *a, **k: None  # type: ignore[method-assign]
    summary = indexer.index_local_repo(path=tmp_path, name="demo", commit="c1")

    assert summary.excluded == 2  # pdf.worker.min.mjs by name, legacy.js by content
    assert repository.dropped == ["demo:public/pdf.worker.min.mjs"]
