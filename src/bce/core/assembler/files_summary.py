"""``payload.files``: one line per file of the assembled context, with the reasons it is there.

Agents read a context answer file-first: *which files, how sure, why*. The symbol items carry
that information spread over ``file_id`` / ``tier`` / ``file_relevance`` / anchor sources, so a
consumer had to re-derive it (or read everything). This module folds the assembled items into

``[{"path", "file_id", "tier", "relevance", "symbols", "why": [...]}, ...]``

in the assembler's order (full files first, best first; then stubs). ``why`` is at most
:data:`WHY_LIMIT` short, deterministic reasons: the task named the file or an identifier it
defines (pinned), the engine ranked it first, the anchor sources that reached it (``explicit``,
``path``, ``semantic``, ...), the sibling-file rule that nominated it, the model's probability.
"""

from __future__ import annotations

from typing import Any

WHY_LIMIT = 3

#: Anchor sources in the order they are worth mentioning (strongest evidence first).
_SOURCE_ORDER = ("explicit", "path", "jira", "history", "semantic", "lexical", "usage", "impact")
_SOURCE_LABEL = {
    "explicit": "task names a symbol here",
    "path": "task names this path",
    "jira": "linked in the issue",
    "history": "edited with the task's files before",
    "semantic": "semantic match",
    "lexical": "keyword match",
    "usage": "uses a fragment the task quotes",
    "impact": "uses a symbol the task names",
}
_SIBLING_LABEL = {
    "sibling_file:name_family": "same name family as an anchor file",
    "sibling_file:directory": "same directory as an anchor file",
    "sibling_file:importer": "imports an anchor file",
}


def _path_of(file_id: str, repo_id: str | None) -> str:
    if repo_id and file_id.startswith(f"{repo_id}:"):
        return file_id[len(repo_id) + 1 :]
    return file_id.split(":", 1)[1] if ":" in file_id else file_id


def files_summary(
    assembled: list[dict[str, Any]],
    *,
    ranked: list[dict[str, Any]] | None = None,
    pins: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Fold assembled items into the per-file list (see module docstring).

    ``ranked`` are the pre-assembly items (they carry ``sources`` / ``provenance`` / ``pinned``
    that the assembler does not copy); ``pins`` is :func:`bce.core.selector.pins.pinned_files`.
    """
    meta: dict[str, dict[str, Any]] = {}
    for it in ranked or ():
        sid = str(it.get("symbol_id") or "")
        if sid:
            meta[sid] = it
    pins = pins or {}

    files: dict[str, dict[str, Any]] = {}
    for it in assembled:
        fid = str(it.get("file_id") or "")
        if not fid:
            continue
        entry = files.get(fid)
        if entry is None:
            entry = files[fid] = {
                "path": _path_of(fid, it.get("repo_id")),
                "file_id": fid,
                "tier": it.get("tier")
                or ("stub" if it.get("detail_level") == "reference" else "full"),
                "relevance": it.get("file_relevance"),
                "symbols": [],
                "_sources": set(),
                "_sibling": None,
                "_first": False,
                "_pinned": bool(it.get("pinned")),
            }
        name = it.get("name")
        if name and name not in entry["symbols"] and len(entry["symbols"]) < 6:
            entry["symbols"].append(name)
        src = meta.get(str(it.get("symbol_id") or ""), {})
        entry["_sources"].update(src.get("sources") or ())
        prov = str(src.get("provenance") or "")
        if prov.startswith("sibling_file:") and entry["_sibling"] is None:
            entry["_sibling"] = prov
        if src.get("pinned"):
            entry["_pinned"] = True
        if (src.get("graph_distance") == 0 or it.get("graph_distance") == 0) and not entry[
            "_first"
        ]:
            entry["_first"] = bool(src.get("is_anchor"))

    out: list[dict[str, Any]] = []
    first_full = next((f for f in files.values() if f["tier"] == "full"), None)
    for entry in files.values():
        why: list[str] = []
        for reason in pins.get(entry["file_id"], []):
            why.append(reason)
        if entry is first_full:
            why.append("engine's first file")
        for source in _SOURCE_ORDER:
            if source in entry["_sources"] and len(why) < WHY_LIMIT:
                why.append(_SOURCE_LABEL[source])
        if entry["_sibling"] and len(why) < WHY_LIMIT:
            why.append(_SIBLING_LABEL.get(entry["_sibling"], "sibling of an anchor file"))
        if entry["relevance"] is not None and len(why) < WHY_LIMIT:
            why.append(f"selector p={float(entry['relevance']):.2f}")
        if not why:
            why.append("graph neighbour of an anchor")
        out.append(
            {
                "path": entry["path"],
                "file_id": entry["file_id"],
                "tier": entry["tier"],
                "relevance": entry["relevance"],
                "symbols": entry["symbols"],
                "why": why[:WHY_LIMIT],
            }
        )
    return out
