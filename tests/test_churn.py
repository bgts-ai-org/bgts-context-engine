"""Git churn prior: counting, determinism at a commit, and the relational write path."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bce.indexing.churn import file_churn, write_file_churn
from bce.indexing.gitsync.local import current_commit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "tester@example.com", cwd=work)
    _git("config", "user.name", "Tester", cwd=work)
    (work / "hot.py").write_text("a = 1\n", encoding="utf-8")
    (work / "cold.py").write_text("b = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    for i in range(3):
        (work / "hot.py").write_text(f"a = {i + 2}\n", encoding="utf-8")
        _git("commit", "-am", f"touch hot {i}", cwd=work)
    return work


def test_file_churn_counts_commits_per_file_within_the_window(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    counts = file_churn(work)
    assert counts == {"hot.py": 4, "cold.py": 1}
    # The window bounds the walk.
    assert file_churn(work, window=2) == {"hot.py": 2}


def test_file_churn_at_a_commit_sees_ancestors_only(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    base = current_commit(work)
    # Later work (a PR branch, say) must not leak into the count taken at ``base``.
    (work / "cold.py").write_text("b = 2\n", encoding="utf-8")
    _git("commit", "-am", "later", cwd=work)
    assert file_churn(work, commit=base) == {"hot.py": 4, "cold.py": 1}
    assert file_churn(work, commit=base) == file_churn(work, commit=base)  # deterministic


def test_file_churn_is_empty_outside_a_repository(tmp_path: Path) -> None:
    assert file_churn(tmp_path / "nowhere") == {}


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append((sql, params))

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self.conn.rows.extend(rows)

    def fetchone(self) -> tuple[bool]:
        return (self.conn.available,)


class _Conn:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.executed: list[tuple[str, Any]] = []
        self.rows: list[Any] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_write_file_churn_restricts_to_indexed_paths_and_zero_fills() -> None:
    conn = _Conn()
    counts = {"src/hot.py": 7, "gone.py": 3}
    written = write_file_churn(
        conn,  # type: ignore[arg-type]
        repo_id="acme",
        counts=counts,
        commit="abc",
        window=500,
        only_paths={"src/hot.py", "src/quiet.py"},
    )
    assert written == 2
    assert conn.rows == [
        ("acme:src/hot.py", "acme", 7, 500, "abc"),
        ("acme:src/quiet.py", "acme", 0, 500, "abc"),
    ]


def test_write_file_churn_is_a_no_op_before_the_migration() -> None:
    conn = _Conn(available=False)
    assert write_file_churn(conn, repo_id="acme", counts={"a.py": 1}, commit="abc") == 0  # type: ignore[arg-type]
    assert conn.rows == []
