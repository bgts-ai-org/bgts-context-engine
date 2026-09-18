"""Per-file change frequency (git churn) - the "where do diffs land" prior (scoring w11).

For every file the indexer knows, count the commits within a fixed window ending at the indexed
commit that touched it. Deterministic given the commit: ``git log <commit>`` walks ancestors only,
so a snapshot taken at a PR's base commit never sees the PR's own commits, and re-running at the
same commit always yields the same counts. Stored in the relational ``file_churn`` table
(migration 0010) and read by :meth:`GraphRepository.symbol_features`.

The window is a commit count rather than a date so that a slow-moving repository and a busy one
both get a prior on the same scale.
"""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from bce.indexing.parser.symbol_id import make_file_id

logger = logging.getLogger("bce.indexing.churn")

#: Commits walked back from the indexed commit.
CHURN_WINDOW_COMMITS = 500


def file_churn(
    root: str | Path, *, commit: str = "HEAD", window: int = CHURN_WINDOW_COMMITS
) -> dict[str, int]:
    """``repo-relative POSIX path -> commits touching it`` within the window (ancestors only).

    Renames are followed (``-M``) so a file's history survives a move. Returns an empty mapping
    when git is unavailable or the path is not a repository (the prior then stays 0).
    """
    try:
        out = subprocess.run(  # noqa: S603,S607 - fixed executable, arguments are a sha and ints
            [
                "git",
                "-C",
                str(root),
                "log",
                "-M",
                "--name-only",
                "--pretty=format:",
                f"-n{int(window)}",
                commit,
                "--",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.warning("git log for churn failed", extra={"root": str(root), "error": str(exc)})
        return {}
    counts: Counter[str] = Counter()
    for line in out.stdout.splitlines():
        path = line.strip().replace("\\", "/")
        if path:
            counts[path] += 1
    return dict(counts)


def churn_available(conn: Any) -> bool:
    """True when migration 0010 has been applied to this database."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.file_churn') IS NOT NULL")
        row = cur.fetchone()
    return bool(row and row[0])


def write_file_churn(
    conn: Any,
    *,
    repo_id: str,
    counts: dict[str, int],
    commit: str,
    window: int = CHURN_WINDOW_COMMITS,
    only_paths: set[str] | None = None,
) -> int:
    """Upsert churn rows for ``repo_id`` in the caller's transaction. Returns rows written.

    ``only_paths`` restricts the rows to the files that were indexed (history mentions deleted
    files too); every indexed path absent from ``counts`` is written with 0 so a re-run at a
    quieter commit lowers a stale count instead of leaving it.
    """
    if not churn_available(conn):
        return 0
    paths = set(only_paths) if only_paths is not None else set(counts)
    rows = [
        (make_file_id(repo_id, path), repo_id, int(counts.get(path, 0)), int(window), commit)
        for path in sorted(paths)
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO file_churn (file_id, repo_id, commits, window_commits, computed_at_commit) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (file_id) DO UPDATE SET commits = EXCLUDED.commits, "
            "window_commits = EXCLUDED.window_commits, "
            "computed_at_commit = EXCLUDED.computed_at_commit",
            rows,
        )
    return len(rows)


def indexed_paths(conn: Any, repo_id: str) -> set[str]:
    """Repo-relative paths of the files present in ``symbol_fts`` for the repo (one query)."""
    prefix = f"{repo_id}:"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT file_id FROM symbol_fts WHERE repo_id = %s AND file_id IS NOT NULL",
            (repo_id,),
        )
        return {
            str(row[0])[len(prefix) :] for row in cur.fetchall() if str(row[0]).startswith(prefix)
        }
