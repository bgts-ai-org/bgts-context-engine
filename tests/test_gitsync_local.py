"""Source walking for full indexing: skip lists and nested checkouts (no git, no database)."""

from __future__ import annotations

from pathlib import Path

from bce.indexing.gitsync.local import iter_source_files

_EXTS = (".py", ".ts")


def _rel_paths(root: Path) -> list[str]:
    return [rel for rel, _ in iter_source_files(root, _EXTS)]


def _init_checkout(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


def _write(path: Path, text: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_skips_nested_worktree_with_gitlink_file(tmp_path: Path) -> None:
    # `git worktree add` writes a `.git` *file* pointing at the main repository's gitdir.
    _init_checkout(tmp_path)
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / ".claude" / "worktrees" / "feature-x" / ".git", "gitdir: /elsewhere\n")
    _write(tmp_path / ".claude" / "worktrees" / "feature-x" / "src" / "app.py")
    _write(tmp_path / ".claude" / "worktrees" / "feature-x" / "deep" / "util.ts")

    assert _rel_paths(tmp_path) == ["src/app.py"]


def test_skips_nested_clone_with_git_directory(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    _write(tmp_path / "main.py")
    (tmp_path / "vendor" / "other-repo" / ".git").mkdir(parents=True)
    _write(tmp_path / "vendor" / "other-repo" / "lib.py")
    _write(tmp_path / "vendor" / "kept.py")

    assert _rel_paths(tmp_path) == ["main.py", "vendor/kept.py"]


def test_root_checkout_itself_is_still_indexed(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    _write(tmp_path / "pkg" / "mod.py")
    _write(tmp_path / ".git" / "hooks" / "pre-commit.py")

    assert _rel_paths(tmp_path) == ["pkg/mod.py"]


def test_skip_dirs_and_extensions_unchanged(tmp_path: Path) -> None:
    _write(tmp_path / "a.py")
    _write(tmp_path / "node_modules" / "dep" / "index.ts")
    _write(tmp_path / "build" / "out.py")
    _write(tmp_path / "README.md")

    assert _rel_paths(tmp_path) == ["a.py"]


def test_order_is_deterministic(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    for name in ("b.py", "a.ts", "sub/z.py", "sub/a.py"):
        _write(tmp_path / name)
    _write(tmp_path / "wt" / ".git", "gitdir: /elsewhere\n")
    _write(tmp_path / "wt" / "c.py")

    first = _rel_paths(tmp_path)
    assert first == _rel_paths(tmp_path)
    assert first == sorted(first)
    assert "wt/c.py" not in first


def test_non_checkout_root_with_several_clones_is_walked_in_full(tmp_path: Path) -> None:
    # `bce index --repo ~/work/` on a plain folder of clones: every child holds a `.git`, and
    # skipping them would index nothing, so nested checkouts are only skipped inside a checkout.
    for repo in ("repoA", "repoB"):
        (tmp_path / repo / ".git").mkdir(parents=True)
        _write(tmp_path / repo / "main.py")
    skipped: list[str] = []

    rels = [rel for rel, _ in iter_source_files(tmp_path, _EXTS, skipped)]

    assert rels == ["repoA/main.py", "repoB/main.py"]
    assert skipped == []


def test_subdirectory_of_checkout_still_skips_nested_checkouts(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    _write(tmp_path / "pkg" / "mod.py")
    _write(tmp_path / "pkg" / "wt" / ".git", "gitdir: /elsewhere\n")
    _write(tmp_path / "pkg" / "wt" / "mod.py")

    assert _rel_paths(tmp_path / "pkg") == ["mod.py"]


def test_skipped_checkouts_are_reported_once_each(tmp_path: Path) -> None:
    _init_checkout(tmp_path)
    _write(tmp_path / "app.py")
    _write(tmp_path / ".claude" / "worktrees" / "a" / ".git", "gitdir: /elsewhere\n")
    _write(tmp_path / ".claude" / "worktrees" / "a" / "x.py")
    _write(tmp_path / ".claude" / "worktrees" / "a" / "y.py")
    # A checkout inside a skipped checkout is reported as the outer one only.
    (tmp_path / ".claude" / "worktrees" / "a" / "sub" / ".git").mkdir(parents=True)
    _write(tmp_path / ".claude" / "worktrees" / "a" / "sub" / "z.py")
    (tmp_path / "vendor" / "lib" / ".git").mkdir(parents=True)
    _write(tmp_path / "vendor" / "lib" / "lib.py")
    skipped: list[str] = []

    rels = [rel for rel, _ in iter_source_files(tmp_path, _EXTS, skipped)]

    assert rels == ["app.py"]
    assert skipped == [".claude/worktrees/a", "vendor/lib"]
