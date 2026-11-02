"""What the task text spells out, matched against the answer: pins and unresolved identifiers.

A task that names ``SqliteTypeMappingSource`` or ``src/utils/auth.ts`` has told us a file it is
about. Two consumers use that:

* :func:`pinned_files` - before the selector tiers the ranked answer, the files that *define*
  a named identifier (or are the named path) are pinned so the decision model cannot drop them
  (measured: of the expected files the model voted out, the median probability was 0.04 while
  the task text named them outright).
* :func:`identifier_gaps` - after assembly, the named identifiers that the context does not
  cover: ``unresolved`` ones each with the indexed files that define it (open those, no search
  needed), and ``unknown`` ones the index has no symbol for at all (a name the task is about to
  introduce, or code in a file or language the index does not cover). Both are gaps the agent
  is told about; only it can tell which unknown name is worth a search.

Pure string matching over the items; one bulk name lookup against the repository for the
unresolved set. Deterministic.
"""

from __future__ import annotations

import re
from typing import Any

from bce.core.orchestrator import bulk
from bce.core.orchestrator.text import explicit_candidates, file_mentions, is_test_symbol

#: An identifier matching more distinct files than this among the ranked items (``toString``,
#: ``Dispose``) names nothing in particular and pins none of them.
PIN_MAX_FILES = 4
#: Shorter identifier-looking tokens are abbreviations (``std``, ``ctx``), not names.
MIN_IDENT_LEN = 4
#: ALL-CAPS tokens that pass the identifier heuristic but are prose in a task description.
_PROSE_CAPS = frozenset(
    {
        "JSON", "HTML", "HTTP", "HTTPS", "YAML", "TOML", "TODO", "NULL", "TRUE", "FALSE", "NONE",
        "REST", "GRPC", "CSV", "XML", "UUID", "GUID", "NOTE", "FIXME", "ASCII", "UTF8", "SQL",
        "CPU", "GPU", "RAM", "URL", "URI", "API", "SDK", "CLI", "IDE", "OSS", "AWS", "GCP",
        "MIT", "BSD", "GPL", "README", "WIP", "LGTM", "PTAL", "RFC", "ETA", "IMHO", "FYI",
    }
)  # fmt: skip
#: Defining files reported per unresolved identifier.
_DEFINED_IN_LIMIT = 3
#: Unresolved identifiers reported in total (a long PR description can name dozens).
_UNRESOLVED_LIMIT = 8
#: Identifiers unknown to the index reported in total.
_UNKNOWN_LIMIT = 5


def task_identifiers(task_text: str) -> list[str]:
    """Identifier-looking names the task text spells out, first-seen order, prose caps removed."""
    out: list[str] = []
    for name in explicit_candidates(task_text or ""):
        if len(name) < MIN_IDENT_LEN or name.upper() in _PROSE_CAPS:
            continue
        if name not in out:
            out.append(name)
    return out


_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
#: Product and language names written in identifier case in prose.
_PROSE_NAMES = frozenset(
    {
        "javascript", "typescript", "github", "gitlab", "bitbucket", "pypi", "macos", "nodejs",
        "postgresql", "mysql", "sqlite", "mongodb", "graphql", "openapi", "websocket", "youtube",
        "powershell", "vscode", "intellij", "jetbrains", "dockerfile", "kubernetes", "webpack",
        "eslint", "prettier", "jquery", "reactjs", "nextjs", "vuejs", "angularjs", "dotnet",
    }
)  # fmt: skip


def _worth_reporting_unknown(prose: str, name: str) -> bool:
    """Whether a name no indexed symbol has is a gap worth telling the agent about.

    Only names the task's prose uses as a name of their own. Not: the names an issue's
    reproduction snippet defines, a member written after a dot (``chain.from_iterable``,
    ``x.setter``: it belongs to something else), a markup tag of the prompt
    (``<issue_description>``), an all-caps word without an underscore (a file name or emphasis,
    ``AUTHORS``), a product name (``JavaScript``) or a test name (the user's own test, or one to
    write) - none of them is code the index should have had.
    """
    if name.isupper() and "_" not in name:
        return False
    if name.lower() in _PROSE_NAMES or is_test_symbol(None, name, None):
        return False
    # a name right after a backslash is the tail of an escape (``\nThe`` in a JSON-quoted text)
    return re.search(rf"(?<![\w.</\\]){re.escape(name)}(?![\w>])", prose) is not None


def _basename_stem(file_id: str) -> str:
    name = file_id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _path_matches(file_id: str, mention: str) -> bool:
    fid = file_id.replace("\\", "/").lower()
    m = mention.replace("\\", "/").strip("/").lower()
    return bool(m) and (fid.endswith("/" + m) or fid.endswith(":" + m) or fid == m)


def _defining_files(items: list[dict[str, Any]], name: str) -> list[str]:
    """Files among ``items`` that define ``name`` (a symbol of that name, or the file's stem)."""
    folded = name.casefold()
    out: list[str] = []
    for it in items:
        fid = str(it.get("file_id") or "")
        if not fid or fid in out:
            continue
        sym = str(it.get("name") or "")
        if sym == name or sym.casefold() == folded or _basename_stem(fid).casefold() == folded:
            out.append(fid)
    return out


def pinned_files(items: list[dict[str, Any]], task_text: str) -> dict[str, list[str]]:
    """``file_id -> reasons`` for the ranked items that the task text names directly.

    Reasons are human-readable (``"defines SqliteTypeMappingSource"``, ``"named path
    src/utils/auth.ts"``) and are surfaced in ``payload.files[].why``.
    """
    pins: dict[str, list[str]] = {}

    def add(fid: str, reason: str) -> None:
        reasons = pins.setdefault(fid, [])
        if reason not in reasons:
            reasons.append(reason)

    for name in task_identifiers(task_text):
        files = _defining_files(items, name)
        if 0 < len(files) <= PIN_MAX_FILES:
            for fid in files:
                add(fid, f"defines {name}")
    for mention in file_mentions(task_text or ""):
        hits = sorted({str(it.get("file_id") or "") for it in items if it.get("file_id")})
        hits = [f for f in hits if _path_matches(f, mention)]
        if 0 < len(hits) <= PIN_MAX_FILES:
            for fid in hits:
                add(fid, f"named path {mention}")
    return pins


def _covered(items: list[dict[str, Any]], name: str) -> bool:
    """Whether the assembled context defines or shows ``name`` (symbol name, file stem, or the
    text of a full item)."""
    if _defining_files(items, name):
        return True
    for it in items:
        if it.get("tier") == "stub" or it.get("detail_level") == "reference":
            continue
        text = it.get("content") or it.get("body") or ""
        if name in str(text):
            return True
    return False


def identifier_gaps(
    repository: Any, items: list[dict[str, Any]], task_text: str, *, repo_ids: list[str] | None
) -> dict[str, list[Any]]:
    """Task identifiers the context does not cover: ``{"unresolved": [...], "unknown": [...]}``.

    ``unresolved`` is ``[{"name": "SqliteTypeMappingSource", "defined_in": ["efcore:..."]}]``:
    names the index defines outside the answer, with their (non-test) files. ``unknown`` lists
    names with no symbol in scope at all that the task's prose uses as names
    (:func:`_worth_reporting_unknown`). A name defined in more than :data:`PIN_MAX_FILES` files
    is too common to point anywhere and is in neither list. Both lists are bounded.
    Repositories without name resolution (unit-test fakes) yield nothing.
    """
    empty: dict[str, list[Any]] = {"unresolved": [], "unknown": []}
    missing = [n for n in task_identifiers(task_text) if not _covered(items, n)]
    if not missing:
        return empty
    try:
        resolved = bulk.resolve_names(repository, missing)
    except (NotImplementedError, AttributeError):
        return empty
    scope = set(repo_ids or ())
    prose = _FENCE_RE.sub(" ", task_text or "")
    out: dict[str, list[Any]] = {"unresolved": [], "unknown": []}
    for name in missing:
        files: list[str] = []
        in_scope = False
        for row in resolved.get(name) or []:
            fid = str(row.get("file_id") or "")
            rid = row.get("repo_id") or (fid.split(":", 1)[0] if ":" in fid else None)
            if not fid or fid in files:
                continue
            if scope and rid and rid not in scope:
                continue
            in_scope = True
            if is_test_symbol(row.get("symbol_id"), row.get("name"), fid):
                continue
            files.append(fid)
        if not in_scope:
            if len(out["unknown"]) < _UNKNOWN_LIMIT and _worth_reporting_unknown(prose, name):
                out["unknown"].append(name)
            continue
        if not files or len(files) > PIN_MAX_FILES:
            continue  # only tests define it, or a name too common to point anywhere
        if len(out["unresolved"]) < _UNRESOLVED_LIMIT:
            out["unresolved"].append(
                {"name": name, "defined_in": sorted(files)[:_DEFINED_IN_LIMIT]}
            )
    return out


def unresolved_identifiers(
    repository: Any, items: list[dict[str, Any]], task_text: str, *, repo_ids: list[str] | None
) -> list[dict[str, Any]]:
    """The ``unresolved`` part of :func:`identifier_gaps`."""
    return identifier_gaps(repository, items, task_text, repo_ids=repo_ids)["unresolved"]
