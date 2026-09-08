"""Offline integration test for remote sync (uses a local bare repo as the 'remote' - no network).

Skipped automatically if git is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bce.indexing.gitsync import GitCredentials, sync_repo
from bce.indexing.gitsync.local import current_commit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_remote(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "tester@example.com", cwd=work)
    _git("config", "user.name", "Tester", cwd=work)
    (work / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "remote.git"
    _git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return work, bare


def test_sync_repo_clones_then_fetches(tmp_path: Path) -> None:
    work, bare = _make_remote(tmp_path)
    dest = tmp_path / "cache" / "repo"

    # First sync: full clone.
    sync_repo(https_url=str(bare), dest=dest, creds=GitCredentials(), branch="main")
    assert (dest / "mod.py").exists()
    sha1 = current_commit(dest)
    assert len(sha1) == 40

    # A clone of a local path leaves no token to scrub; origin should be the clean URL.
    origin = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == str(bare)

    # Second sync after a new upstream commit: fetch + hard reset (fast path).
    (work / "mod2.py").write_text("x = 2\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "second", cwd=work)
    _git("push", str(bare), "HEAD:main", cwd=work)

    sync_repo(https_url=str(bare), dest=dest, creds=GitCredentials(), branch="main")
    assert (dest / "mod2.py").exists()
    assert current_commit(dest) != sha1
