"""What the task text spells out, matched against the answer: pins and unresolved identifiers.

A task that names ``SqliteTypeMappingSource`` or ``src/utils/auth.ts`` has told us a file it is
about. Two consumers use that:

* :func:`pinned_files` - before the selector tiers the ranked answer, the files that *define*
  a named identifier (or are the named path) are pinned so the decision model cannot drop them
  (measured: of the expected files the model voted out, the median probability was 0.04 while
  the task text named them outright).
* :func:`unresolved_identifiers` - after assembly, the named identifiers that the context does
  not cover, each with the indexed files that define it. This is the one signal an agent needs
  to decide whether to look further: an empty list means every name the task used is in hand;
  a non-empty one says exactly which file to open next, without a search. Names the index does
  not know at all (a variable, a name the task is about to introduce) are *not* reported -
  searching for them is what the agent must not be sent to do.

Pure string matching over the items; one bulk name lookup against the repository for the
unresolved set. Deterministic.
"""

from __future__ import annotations

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


def task_identifiers(task_text: str) -> list[str]:
    """Identifier-looking names the task text spells out, first-seen order, prose caps removed."""
    out: list[str] = []
    for name in explicit_candidates(task_text or ""):
        if len(name) < MIN_IDENT_LEN or name.upper() in _PROSE_CAPS:
            continue
        if name not in out:
            out.append(name)
    return out


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


def unresolved_identifiers(
    repository: Any, items: list[dict[str, Any]], task_text: str, *, repo_ids: list[str] | None
) -> list[dict[str, Any]]:
    """Task identifiers the context does not cover, each with the indexed files defining it.

    ``[{"name": "SqliteTypeMappingSource", "defined_in": ["efcore:src/.../X.cs"]}]``. Only
    names the index knows are reported; the list is bounded (:data:`_UNRESOLVED_LIMIT`).
    Repositories without name resolution (unit-test fakes) yield nothing.
    """
    missing = [n for n in task_identifiers(task_text) if not _covered(items, n)]
    if not missing:
        return []
    try:
        resolved = bulk.resolve_names(repository, missing)
    except (NotImplementedError, AttributeError):
        return []
    scope = set(repo_ids or ())
    out: list[dict[str, Any]] = []
    for name in missing:
        files: list[str] = []
        for row in resolved.get(name) or []:
            fid = str(row.get("file_id") or "")
            rid = row.get("repo_id") or (fid.split(":", 1)[0] if ":" in fid else None)
            if not fid or fid in files:
                continue
            if scope and rid and rid not in scope:
                continue
            if is_test_symbol(row.get("symbol_id"), row.get("name"), fid):
                continue
            files.append(fid)
        if not files or len(files) > PIN_MAX_FILES:
            continue  # unknown to the index, or a name too common to point anywhere
        out.append({"name": name, "defined_in": sorted(files)[:_DEFINED_IN_LIMIT]})
        if len(out) >= _UNRESOLVED_LIMIT:
            break
    return out
