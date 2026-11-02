"""What a context answer is known *not* to cover (``coverage.gaps``, ``coverage.state``).

An agent cannot tell an answer that is complete from one that is merely confident; these are the
signals that say which part of the task the index did not see, so the agent searches for exactly
that and trusts the rest:

- ``unknown_identifiers`` - names the task spells out that no indexed symbol has (new names, or
  code the index does not cover);
- ``unsupported_paths`` - source files the task names that the index has no symbol for, with
  ``reason`` ``language`` (a programming language no installed parser handles) or ``missing``
  (not indexed, or a file the task is about to create). Documentation, configuration and data
  files are not code the graph is meant to hold and are never reported;
- ``truncated_evidence`` - shown declarations cut by the indexer's text cap;
- ``missing_tests`` - no test of the primary targets was found;
- ``semantic_degraded`` - why the semantic anchor did not contribute, if it did not.

``coverage.state`` is ``unknown`` when the task reaches into code the index cannot see (a source
file in an unparsed language) or retrieval ran without its semantic channel, else ``known``.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from bce.core.orchestrator.text import file_mentions
from bce.domain.code_files import CODE_EXTENSIONS

_log = logging.getLogger(__name__)

#: Paths named by the task that are checked / reported.
_MAX_PATHS = 3
#: Hashing embeddings are a test double: they carry no meaning, so the semantic channel is off.
_DEGRADED_PROVIDERS = frozenset({"hashing"})


@functools.lru_cache(maxsize=1)
def indexed_extensions() -> frozenset[str]:
    """Extensions (``.py``) some installed parser handles; empty if the registry cannot load."""
    try:
        from bce.indexing.parser.registry import build_default_registry

        return frozenset(build_default_registry().supported_extensions())
    except Exception:  # noqa: BLE001
        _log.debug("gaps: parser registry unavailable", exc_info=True)
        return frozenset()


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def unsupported_paths(
    repository: Any,
    task_text: str,
    answer_file_ids: list[str],
    *,
    repo_ids: list[str] | None,
) -> list[dict[str, str]]:
    """Paths the task names that the index has no symbol for (see module docstring)."""
    known = [f.replace("\\", "/").lower() for f in answer_file_ids]
    extensions = indexed_extensions()
    pending: list[str] = []
    out: list[dict[str, str]] = []
    for path in file_mentions(task_text or ""):
        low = path.lower()
        if any(f.endswith("/" + low) or f.endswith(":" + low) for f in known):
            continue
        ext = _ext(path)
        if ext not in CODE_EXTENSIONS:
            continue
        if extensions and ext not in extensions:
            out.append({"path": path, "reason": "language", "extension": ext})
        elif "/" in path:
            pending.append(path)
        if len(out) + len(pending) >= _MAX_PATHS:
            break
    check = getattr(repository, "missing_paths", None)
    if pending and callable(check):
        try:
            missing = set(check(pending, repo_ids=repo_ids) or [])
        except Exception:  # noqa: BLE001
            _log.debug("gaps: missing_paths failed", exc_info=True)
            missing = set()
        out.extend({"path": p, "reason": "missing"} for p in pending if p in missing)
    return out[:_MAX_PATHS]


def semantic_degraded(
    *, semantic_error: str | None, semantic_ran: bool, embedding_provider: str | None
) -> str | None:
    if semantic_error:
        return semantic_error
    if semantic_ran and (embedding_provider or "").lower() in _DEGRADED_PROVIDERS:
        return f"the index uses {embedding_provider} embeddings, which carry no meaning"
    return None


def coverage_gaps(
    repository: Any,
    *,
    task_text: str,
    answer_file_ids: list[str],
    evidence: list[dict[str, Any]],
    tests: list[dict[str, Any]],
    unknown_identifiers: list[str],
    repo_ids: list[str] | None,
    semantic_error: str | None,
    semantic_ran: bool,
    embedding_provider: str | None,
) -> dict[str, Any]:
    """The ``coverage.gaps`` block (see module docstring)."""
    truncated = [
        f"{sp.get('path')}:{sp.get('start')}-{sp.get('end') or '?'}"
        for sp in evidence
        if sp.get("truncated")
    ]
    has_targets = any(sp.get("role") == "edit" for sp in evidence)
    return {
        "unknown_identifiers": list(unknown_identifiers),
        "unsupported_paths": unsupported_paths(
            repository, task_text, answer_file_ids, repo_ids=repo_ids
        ),
        "truncated_evidence": truncated,
        "missing_tests": bool(has_targets and not tests),
        "semantic_degraded": semantic_degraded(
            semantic_error=semantic_error,
            semantic_ran=semantic_ran,
            embedding_provider=embedding_provider,
        ),
    }


def coverage_state(gaps: dict[str, Any]) -> str:
    """``unknown`` when the answer is known to miss part of what the task reaches, else ``known``."""
    unparsed = any(p.get("reason") == "language" for p in gaps.get("unsupported_paths") or [])
    return "unknown" if unparsed or gaps.get("semantic_degraded") else "known"
