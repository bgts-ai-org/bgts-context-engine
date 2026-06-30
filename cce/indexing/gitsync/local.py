"""Local filesystem source walking for full indexing.

Yields source files in a deterministic (sorted) order so a full index is reproducible.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
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
