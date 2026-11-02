"""The existing tests of what a context answer says to edit, found at query time.

An agent that changes ``check_token`` next looks for the tests that exercise it - a search that
was most of what agents still ran after a BCE answer. Two channels find them, in order of trust:

- the graph: test symbols (:func:`~bce.domain.testness.is_test_symbol`) with a ``CALLS`` or
  ``REFERENCES`` edge into a target symbol;
- the file-name convention of the target file's language
  (:func:`~bce.domain.testness.conventional_test_basenames`: ``test_auth.py``,
  ``auth.test.ts``, ``auth_test.go``, ``AuthTests.cs`` ...), the nearest one by path when a
  name occurs in several directories.

A test file reached through more targets, and through edit targets rather than dependencies,
ranks first; at most :data:`MAX_TEST_FILES` files are returned, each with at most
:data:`MAX_TEST_SYMBOLS` test symbols and their line ranges. Repositories without these reads
(unit-test fakes, older graphs) simply yield fewer or no tests.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bce.core.orchestrator import bulk
from bce.domain.testness import conventional_test_basenames, is_test_symbol

_log = logging.getLogger(__name__)

#: Test files returned per answer.
MAX_TEST_FILES = 2
#: Test symbols listed per test file.
MAX_TEST_SYMBOLS = 2
#: Target symbols whose callers / referrers are read.
MAX_TARGETS = 8
#: A conventional basename occurring in more files than this is matched by path proximity only
#: among the first ones (``test_utils.py`` exists in every package of a monorepo).
_BASENAME_LIMIT = 12
_EDGE_CHANNELS = (
    ("calls", "callers_of", "get_callers"),
    ("references", "referrers_of", "get_referrers"),
)
#: A test function or class by its name (``test_x``, ``TestX``, ``XTest``, ``XTests``, ``XSpec``);
#: a convention-matched file also holds fixtures and fakes, which are not listed.
_TEST_NAME = re.compile(r"^(test|Test)|(Test|Tests|Spec)$")
#: Target names shorter than this are not matched inside test names (``get`` is in every one).
_NAME_MATCH_MIN = 4


def _path(file_id: str) -> str:
    return file_id.split(":", 1)[1] if ":" in file_id else file_id


def _in_scope(file_id: str, scope: set[str]) -> bool:
    return not scope or ":" not in file_id or file_id.split(":", 1)[0] in scope


def _proximity(source_path: str, test_path: str) -> int:
    """Directory names of ``source_path`` occurring in ``test_path`` (``auth`` in
    ``tests/auth_tests/``), the tie-break among same-named test files."""
    parts = [p.lower() for p in source_path.split("/")[:-1] if len(p) > 2]
    low = test_path.lower()
    return sum(1 for p in parts if p in low)


def _qualified(symbol_id: str, name: str) -> str:
    """``TestCheckToken::test_empty`` for a method (the id's container segment), else ``name``."""
    segments = symbol_id.split("::")
    if len(segments) >= 4 and segments[-2] and segments[-2] != name:
        return f"{segments[-2]}::{name}"
    return name


def related_tests(
    repository: Any,
    targets: list[dict[str, Any]],
    *,
    repo_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Test files (with symbols) for ``targets`` (``{symbol_id, file_id, name, role}``, best
    first): ``[{"file_id", "path", "why", "symbols": [{symbol_id, name, qualified, line,
    end_line}]}]``. Never raises: a failing read only drops its channel."""
    scope = set(repo_ids or ())
    targets = [t for t in targets if t.get("symbol_id")][:MAX_TARGETS]
    if not targets:
        return []
    weight = {t["symbol_id"]: (2.0 if t.get("role") == "edit" else 1.0) for t in targets}
    names = {t["symbol_id"]: str(t.get("name") or "") for t in targets}

    score: dict[str, float] = {}
    reached: dict[str, set[str]] = {}
    why: dict[str, str] = {}
    hits: dict[str, dict[str, dict[str, Any]]] = {}
    for verb, bulk_name, single_name in _EDGE_CHANNELS:
        try:
            rows = bulk.neighbours(repository, list(weight), bulk_name, single_name)
        except Exception:  # noqa: BLE001 - an unsupported channel is skipped
            _log.debug("related_tests: %s unavailable", bulk_name, exc_info=True)
            continue
        for target_id, neighbours in rows.items():
            for row in neighbours or []:
                fid = str(row.get("file_id") or "")
                sid = str(row.get("symbol_id") or "")
                if not fid or not sid or not _in_scope(fid, scope):
                    continue
                if not is_test_symbol(sid, row.get("name"), fid):
                    continue
                hit = hits.setdefault(fid, {}).setdefault(sid, {**row, "targets": set()})
                hit["targets"].add(target_id)
                if target_id not in reached.setdefault(fid, set()):
                    reached[fid].add(target_id)
                    score[fid] = score.get(fid, 0.0) + weight.get(target_id, 1.0)
                why.setdefault(fid, f"{verb} {names.get(target_id) or 'a target'}")

    source_files: list[str] = []
    for t in targets:
        fid = str(t.get("file_id") or "")
        if fid and fid not in source_files:
            source_files.append(fid)
    wanted: dict[str, str] = {}
    for fid in source_files:
        for base in conventional_test_basenames(_path(fid)):
            wanted.setdefault(base, fid)
    lookup = getattr(repository, "files_by_basename_pattern", None)
    if wanted and callable(lookup):
        try:
            found = lookup(list(wanted), repo_ids=repo_ids, limit=_BASENAME_LIMIT) or {}
        except Exception:  # noqa: BLE001
            _log.debug("related_tests: basename lookup failed", exc_info=True)
            found = {}
        for base, fids in found.items():
            source = wanted.get(base)
            candidates = [
                f for f in fids or [] if _in_scope(f, scope) and is_test_symbol(None, None, f)
            ]
            if not source or not candidates:
                continue
            best = max(candidates, key=lambda f: _proximity(_path(source), _path(f)))
            score[best] = score.get(best, 0.0) + 1.5
            why.setdefault(best, f"test file of {_path(source)}")

    chosen = sorted(score, key=lambda f: (-score[f], f))[:MAX_TEST_FILES]
    if not chosen:
        return []
    symbols = _test_symbols(repository, chosen, hits, list(names.values()))
    meta: dict[str, dict[str, Any]] = {}
    ids = [s["symbol_id"] for syms in symbols.values() for s in syms]
    if ids:
        try:
            meta = bulk.symbols_meta(repository, ids)
        except Exception:  # noqa: BLE001
            meta = {}
    out: list[dict[str, Any]] = []
    for fid in chosen:
        listed = []
        for sym in symbols.get(fid, []):
            info = meta.get(sym["symbol_id"]) or {}
            name = str(sym.get("name") or info.get("name") or "")
            listed.append(
                {
                    "symbol_id": sym["symbol_id"],
                    "name": name,
                    "qualified": _qualified(sym["symbol_id"], name),
                    "line": info.get("line") or sym.get("line"),
                    "end_line": info.get("end_line"),
                }
            )
        out.append({"file_id": fid, "path": _path(fid), "why": why.get(fid, ""), "symbols": listed})
    return out


def _test_symbols(
    repository: Any,
    files: list[str],
    hits: dict[str, dict[str, dict[str, Any]]],
    target_names: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Per chosen file, the test symbols to list: the graph hits reaching the most targets, or
    for a convention-only file its tests named after a target, else its first tests."""
    out: dict[str, list[dict[str, Any]]] = {}
    convention_only = [f for f in files if not hits.get(f)]
    in_file: dict[str, list[dict[str, Any]]] = {}
    if convention_only:
        try:
            in_file = bulk.symbols_in_files(repository, convention_only)
        except Exception:  # noqa: BLE001
            in_file = {}
    wanted = [n.lower() for n in target_names if len(n) >= _NAME_MATCH_MIN]
    for fid in files:
        if hits.get(fid):
            rows = sorted(
                hits[fid].values(),
                key=lambda r: (-len(r["targets"]), r.get("line") or 0, r["symbol_id"]),
            )
            out[fid] = rows[:MAX_TEST_SYMBOLS]
            continue
        rows = [r for r in in_file.get(fid, []) if r.get("symbol_id")]
        tests = [r for r in rows if _TEST_NAME.search(str(r.get("name") or ""))]
        named = [r for r in tests if any(n in str(r.get("name") or "").lower() for n in wanted)]
        out[fid] = (named or tests)[:MAX_TEST_SYMBOLS]
    return out
