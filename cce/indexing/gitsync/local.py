"""Local filesystem source walking for full indexing.

Yields source files in a deterministic (sorted) order so a full index is reproducible. Also exposes
a git-diff helper (:func:`changed_files`) that drives incremental re-indexing (Phase 1).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cce_data",
}


def iter_source_files(root: str | Path, supported_exts: tuple[str, ...]) -> Iterator[tuple[str, Path]]:
    """Yield ``(relative_posix_path, absolute_path)`` for files with a supported extension.

    Iteration order is sorted for determinism. ``supported_exts`` come from the language registry.
    """
    root_path = Path(root).resolve()
    exts = tuple(e.lower() for e in supported_exts)
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root_path).parts[:-1]):
            continue
        if not path.name.lower().endswith(exts):
            continue
        rel = path.relative_to(root_path).as_posix()
        yield rel, path


def current_commit(root: str | Path) -> str:
    """Return the current git commit sha, or ``WORKDIR`` if not a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip() or "WORKDIR"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "WORKDIR"


@dataclass(slots=True)
class FileChange:
    """A single file's change between two commits (deterministic, git-diff derived)."""

    status: str  # "added" | "modified" | "deleted"
    path: str    # repo-relative POSIX path


def changed_files(
    root: str | Path,
    from_commit: str,
    to_commit: str = "HEAD",
) -> list[FileChange]:
    """Return files changed between two commits via ``git diff --name-status``.

    Rename/copy statuses are decomposed into a delete of the old path + an add of the new path, so
    the incremental indexer never has to special-case them. Results are sorted (path, status) for a
    reproducible re-index order (P1). Raises :class:`RuntimeError` if git is unavailable.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-status",
                "-z",
                "--no-renames",
                f"{from_commit}",
                f"{to_commit}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"git diff failed for {root}: {exc}") from exc

    return _parse_name_status(out.stdout)


_STATUS_MAP = {"A": "added", "M": "modified", "D": "deleted"}


def _parse_name_status(raw: str) -> list[FileChange]:
    """Parse ``git diff --name-status -z`` output (NUL-separated status/path fields)."""
    fields = [f for f in raw.split("\0") if f != ""]
    changes: list[FileChange] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        letter = code[:1].upper()
        status = _STATUS_MAP.get(letter)
        if status is None:
            # Unknown status (e.g. type-change) - treat as modified, consume one path.
            status = "modified"
        if i + 1 >= len(fields):
            break
        path = fields[i + 1].replace("\\", "/")
        changes.append(FileChange(status=status, path=path))
        i += 2
    changes.sort(key=lambda c: (c.path, c.status))
    return changes
