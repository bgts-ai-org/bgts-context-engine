"""``inspect_symbol_graph``: one graph question about one symbol, asked by name.

The graph tools of layer 1 take a ``symbol_id`` and answer with ids - a chain of lookups an agent
rarely finishes. This tool takes what an agent has in hand and answers with code locations it can
open:

- ``query``: a symbol name (``check_token``), a member (``Session.refresh`` / ``Session::refresh``),
  a position (``app/auth.py:42``), or for ``mode=impact`` a unified diff or a list of
  ``path:L10-L20`` ranges;
- ``mode``:

  * ``definition`` - the declaration's code, numbered (long ones windowed, classes as skeletons;
    the evidence spans of :mod:`bce.core.render.evidence`);
  * ``callers`` / ``references`` - each calling (referencing) symbol with the lines around the
    call, not just a location;
  * ``callees`` - what the symbol calls, with ranges;
  * ``impact`` - the symbols the change touches, the symbols that call, reference or extend them
    (two hops) and the tests to run.

A name several symbols share answers with the candidates (``path:line kind name``) instead of a
guess, unless exactly one of them is production code. Deterministic; read-only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from bce.core.auth.scope import ScopeFilter
from bce.core.orchestrator import bulk
from bce.core.orchestrator.related_tests import related_tests
from bce.core.render.evidence import path_of, span_for, text_lines
from bce.domain.testness import is_test_symbol
from bce.storage.graph.repository import GraphRepository

MODES = ("definition", "callers", "callees", "references", "impact")
#: Call sites / callees / affected symbols listed per answer.
MAX_SITES = 8
#: Candidates listed for an ambiguous name.
MAX_CANDIDATES = 8
#: Lines shown on each side of a call site.
SITE_RADIUS = 2
#: Changed symbols an impact answer starts from.
MAX_CHANGED = 12
#: Symbols whose dependents one impact hop expands.
FRONTIER_LIMIT = 50

_POSITION = re.compile(r"^(?P<path>[^\s:]+\.[A-Za-z0-9]+):L?(?P<a>\d+)(?:-L?(?P<b>\d+))?$")
_HUNK = re.compile(r"^@@ -(?P<a>\d+)(?:,(?P<n>\d+))? \+\d+(?:,\d+)? @@")
_MEMBER = re.compile(r"^(?P<owner>[A-Za-z_$][\w$]*)(?:\.|::|#)(?P<name>[A-Za-z_$][\w$]*)$")
_CONTAINER_KINDS = frozenset({"class", "interface", "struct", "trait", "enum", "module"})
_DEPENDENT_CHANNELS = (
    ("calls", "callers_of", "get_callers"),
    ("references", "referrers_of", "get_referrers"),
    ("extends", "subtypes_of", "get_subtypes"),
)

Allow = Callable[[str], bool]


def _allow(repo_ids: list[str] | None, scope: ScopeFilter | None) -> Allow:
    """Whether a file id is in the requested repos and the caller's scope."""
    wanted = set(repo_ids or ())

    def allowed(file_id: str) -> bool:
        repo = file_id.split(":", 1)[0] if ":" in file_id else None
        if wanted and repo not in wanted:
            return False
        return scope is None or scope.allows(repo)

    return allowed


def _scoped(rows: list[dict[str, Any]], allow: Allow) -> list[dict[str, Any]]:
    return [r for r in rows if allow(str(r.get("file_id") or ""))]


def _container(symbol_id: str) -> str:
    segments = symbol_id.split("::")
    return segments[-2] if len(segments) >= 4 else ""


def _stem(file_id: str) -> str:
    name = path_of(file_id).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _is_test(row: dict[str, Any]) -> bool:
    return is_test_symbol(row.get("symbol_id"), row.get("name"), row.get("file_id"))


def _meta(repository: GraphRepository, ids: list[str]) -> dict[str, dict[str, Any]]:
    try:
        return bulk.symbols_meta(repository, ids)
    except Exception:  # noqa: BLE001 - a missing property only drops a range
        return {}


def _merged(row: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    return {**row, **{k: v for k, v in (meta or {}).items() if v is not None}}


def _file_ids(
    repository: GraphRepository, paths: list[str], repo_ids: list[str] | None
) -> dict[str, list[str]]:
    lookup = getattr(repository, "files_by_suffix", None)
    if not paths or not callable(lookup):
        return {}
    try:
        return lookup(paths, repo_ids=repo_ids) or {}
    except Exception:  # noqa: BLE001
        return {}


def _symbols_at(
    repository: GraphRepository, file_ids: list[str], a: int, b: int
) -> list[dict[str, Any]]:
    """Symbols of ``file_ids`` whose range overlaps lines ``a..b``, innermost first."""
    if not file_ids:
        return []
    try:
        by_file = bulk.symbols_in_files(repository, file_ids)
    except Exception:  # noqa: BLE001
        return []
    rows = [r for fid in file_ids for r in by_file.get(fid) or [] if r.get("symbol_id")]
    meta = _meta(repository, [str(r["symbol_id"]) for r in rows])
    hits = []
    for row in rows:
        info = _merged(row, meta.get(str(row["symbol_id"])))
        start = int(info.get("text_line") or info.get("line") or 0)
        end = int(info.get("end_line") or start)
        if start and start <= b and end >= a:
            hits.append((end - start, start, info))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [info for _, _, info in hits]


def resolve_query(
    repository: GraphRepository, query: str, allow: Allow, repo_ids: list[str] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """``(matches, how)``: the symbols ``query`` names, production code first."""
    text = (query or "").strip().strip("`")
    pos = _POSITION.match(text)
    if pos:
        a = int(pos.group("a"))
        b = int(pos.group("b") or a)
        fids = _file_ids(repository, [pos.group("path")], repo_ids).get(pos.group("path")) or []
        found = _scoped(_symbols_at(repository, list(fids), a, b), allow)
        # innermost symbol per file: a position names one declaration, not its enclosing class
        first: dict[str, dict[str, Any]] = {}
        for row in found:
            first.setdefault(str(row.get("file_id")), row)
        return list(first.values()), "position"
    member = _MEMBER.match(text)
    name = member.group("name") if member else text
    try:
        rows = bulk.resolve_names(repository, [name]).get(name) or []
    except Exception:  # noqa: BLE001
        rows = []
    rows = _scoped(rows, allow)
    if member:
        owner = member.group("owner")
        owned = [
            r
            for r in rows
            if _container(str(r.get("symbol_id") or "")).split(".")[-1] == owner
            or _stem(str(r.get("file_id") or "")) == owner
        ]
        rows = owned or rows
    rows = sorted(
        rows,
        key=lambda r: (_is_test(r), str(r.get("file_id") or ""), int(r.get("line") or 0)),
    )
    return rows, "member" if member else "name"


def _pick(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one symbol meant, or None when several production symbols share the name."""
    if len(rows) == 1:
        return rows[0]
    production = [r for r in rows if not _is_test(r)]
    return production[0] if len(production) == 1 else None


def _location(row: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    info = _merged(row, meta)
    fid = str(info.get("file_id") or "")
    return {
        "symbol_id": info.get("symbol_id"),
        "name": info.get("name"),
        "kind": info.get("kind"),
        "file_id": fid,
        "path": path_of(fid),
        "line": info.get("line"),
        "end_line": info.get("end_line"),
        "signature": info.get("signature"),
        "test": _is_test(info),
    }


def _bodies(repository: GraphRepository, ids: list[str]) -> dict[str, str]:
    lookup = getattr(repository, "symbol_bodies", None)
    if not ids or not callable(lookup):
        return {}
    try:
        out = lookup(ids)
    except Exception:  # noqa: BLE001
        return {}
    return out if isinstance(out, dict) else {}


def _definition(repository: GraphRepository, target: dict[str, Any]) -> dict[str, Any]:
    sid = str(target["symbol_id"])
    item = dict(target)
    detail = getattr(repository, "get_symbol_detail", None)
    if callable(detail):
        try:
            item = _merged(item, detail(sid))
        except Exception:  # noqa: BLE001
            pass
    item.setdefault("tier", "full")
    item["symbol_relevance"] = 3.0  # asked for by name: the subject, never a dependency skeleton
    text = _bodies(repository, [sid]).get(sid)
    return span_for(item, text if isinstance(text, str) else None)


def _site_lines(
    body: str | None, meta: dict[str, Any], call_line: Any, target_name: str
) -> list[list[Any]]:
    """The lines around the call / reference of ``target_name`` in a caller's stored text."""
    if not body:
        return []
    start = int(meta.get("text_line") or meta.get("line") or 1)
    lines = text_lines(body.rstrip("\n"), meta.get("col"))
    idx = None
    if isinstance(call_line, int) and start <= call_line < start + len(lines):
        idx = call_line - start
    elif target_name:
        pattern = re.compile(rf"(?<![\w$]){re.escape(target_name)}(?![\w$])")
        hits = [i for i, line in enumerate(lines) if pattern.search(line)]
        later = [i for i in hits if i]  # past the caller's own header, when it can be
        idx = (later or hits or [None])[0]
    if idx is None:
        return []
    lo, hi = max(0, idx - SITE_RADIUS), min(len(lines) - 1, idx + SITE_RADIUS)
    return [[start + i, lines[i]] for i in range(lo, hi + 1)]


def _sites(
    repository: GraphRepository,
    target: dict[str, Any],
    *,
    edge_types: frozenset[str],
    allow: Allow,
) -> tuple[list[dict[str, Any]], int]:
    """Calling / referencing symbols of ``target`` with their site lines, production first."""
    sid = str(target["symbol_id"])
    try:
        refs = repository.find_references(sid)
    except Exception:  # noqa: BLE001
        refs = []
    by_caller: dict[str, dict[str, Any]] = {}
    for ref in refs:
        cid = str(ref.get("caller_id") or "")
        if cid and cid != sid and ref.get("edge_type") in edge_types:
            by_caller.setdefault(cid, ref)
    rows = _scoped([{**r, "symbol_id": c} for c, r in by_caller.items()], allow)
    meta = _meta(repository, [r["symbol_id"] for r in rows])
    located = [_location(r, meta.get(r["symbol_id"])) for r in rows]
    located.sort(key=lambda r: (r["test"], r["path"], int(r.get("line") or 0)))
    shown = located[:MAX_SITES]
    bodies = _bodies(repository, [r["symbol_id"] for r in shown])
    for site in shown:
        ref = by_caller[site["symbol_id"]]
        site["call_line"] = ref.get("call_line")
        site["edge_type"] = ref.get("edge_type")
        site["lines"] = _site_lines(
            bodies.get(site["symbol_id"]),
            _merged(site, meta.get(site["symbol_id"])),
            ref.get("call_line"),
            str(target.get("name") or ""),
        )
    return shown, len(located) - len(shown)


def _callees(
    repository: GraphRepository, target: dict[str, Any], allow: Allow
) -> tuple[list[dict[str, Any]], int]:
    sid = str(target["symbol_id"])
    try:
        rows = bulk.neighbours(repository, [sid], "callees_of", "get_callees").get(sid) or []
    except Exception:  # noqa: BLE001
        rows = []
    unique = {str(r["symbol_id"]): r for r in rows if r.get("symbol_id")}
    callees = _scoped(list(unique.values()), allow)
    meta = _meta(repository, [str(r["symbol_id"]) for r in callees])
    located = [_location(r, meta.get(str(r["symbol_id"]))) for r in callees]
    located.sort(key=lambda r: (r["path"], int(r.get("line") or 0)))
    return located[:MAX_SITES], max(0, len(located) - MAX_SITES)


def parse_change(query: str) -> list[tuple[str, int, int]]:
    """``(path, first, last)`` line ranges of the pre-change files that a unified diff or a
    ``path:L10-L20`` list touches (a hunk that only adds lines counts as the line it follows)."""
    text = query or ""
    out: list[tuple[str, int, int]] = []
    if "\n@@ " in text or text.startswith(("--- ", "diff --git")):
        path = None
        for line in text.splitlines():
            if line.startswith("--- "):
                src = line[4:].strip().split("\t")[0]
                path = None if src == "/dev/null" else re.sub(r"^[ab]/", "", src)
            elif line.startswith("+++ ") and path is None:
                dst = line[4:].strip().split("\t")[0]
                path = None if dst == "/dev/null" else re.sub(r"^[ab]/", "", dst)
            else:
                hunk = _HUNK.match(line)
                if hunk and path:
                    a = max(int(hunk.group("a")), 1)
                    n = int(hunk.group("n")) if hunk.group("n") is not None else 1
                    out.append((path, a, a + max(n, 1) - 1))
        return out
    for token in re.split(r"[\s,;]+", text.strip()):
        pos = _POSITION.match(token.strip("`"))
        if pos:
            a = int(pos.group("a"))
            out.append((pos.group("path"), a, int(pos.group("b") or a)))
    return out


def _dependents(repository: GraphRepository, ids: list[str], allow: Allow) -> list[dict[str, Any]]:
    """Symbols that call, reference or extend ``ids`` (depth 1) and those that depend on them
    (depth 2), each once, with how it was reached."""
    seen = set(ids)
    frontier = sorted(seen)
    found: list[dict[str, Any]] = []
    for depth in (1, 2):
        hop: dict[str, dict[str, Any]] = {}
        for via, bulk_name, single_name in _DEPENDENT_CHANNELS:
            try:
                rows = bulk.neighbours(
                    repository, frontier[:FRONTIER_LIMIT], bulk_name, single_name
                )
            except Exception:  # noqa: BLE001
                continue
            for source in sorted(rows):
                for row in rows[source] or []:
                    sid = row.get("symbol_id")
                    if sid and sid not in seen and sid not in hop:
                        hop[sid] = {**row, "via": via, "of": source, "depth": depth}
        hop_rows = _scoped(list(hop.values()), allow)
        found.extend(hop_rows)
        seen.update(hop)
        frontier = sorted(str(r["symbol_id"]) for r in hop_rows)
        if not frontier:
            break
    return found


def _impact(
    repository: GraphRepository, query: str, allow: Allow, repo_ids: list[str] | None
) -> dict[str, Any]:
    changes = parse_change(query)
    changed: list[dict[str, Any]] = []
    if changes:
        fids = _file_ids(repository, sorted({p for p, _, _ in changes}), repo_ids)
        for path, a, b in changes:
            for hit in _scoped(_symbols_at(repository, list(fids.get(path) or []), a, b), allow):
                if hit["symbol_id"] not in {c["symbol_id"] for c in changed}:
                    changed.append(hit)
        # a method's change is not its whole class's: a container counts only when none of its
        # members was hit
        leaves = [c for c in changed if c.get("kind") not in _CONTAINER_KINDS]
        changed = leaves or changed
    else:
        one = _pick(resolve_query(repository, query, allow, repo_ids)[0])
        changed = [one] if one else []
    changed = changed[:MAX_CHANGED]
    ids = [str(c["symbol_id"]) for c in changed]
    dependents = _dependents(repository, ids, allow) if ids else []
    names = {str(c["symbol_id"]): c.get("name") for c in changed}
    names.update({str(d["symbol_id"]): d.get("name") for d in dependents})
    meta = _meta(repository, [str(d["symbol_id"]) for d in dependents])
    affected = []
    for dep in dependents:
        row = _location(dep, meta.get(str(dep["symbol_id"])))
        row.update(via=dep["via"], of=names.get(str(dep["of"])) or "", depth=dep["depth"])
        affected.append(row)
    affected.sort(key=lambda r: (r["test"], r["depth"], r["path"], int(r.get("line") or 0)))
    targets = [
        {"symbol_id": c["symbol_id"], "file_id": c.get("file_id"), "name": c.get("name"),
         "role": "edit"}
        for c in changed
    ]  # fmt: skip
    tests = related_tests(repository, targets, repo_ids=repo_ids)
    seen = {t["file_id"] for t in tests}
    for row in affected:
        if row["test"] and row["file_id"] not in seen:
            seen.add(row["file_id"])
            tests.append(
                {"file_id": row["file_id"], "path": row["path"], "why": f"{row['via']} {row['of']}",
                 "symbols": [{"symbol_id": row["symbol_id"], "name": row["name"],
                              "qualified": row["name"], "line": row.get("line"),
                              "end_line": row.get("end_line")}]}
            )  # fmt: skip
    production = [r for r in affected if not r["test"]]
    return {
        "changes": [{"path": p, "start": a, "end": b} for p, a, b in changes],
        "changed": [_location(c) for c in changed],
        "affected": production[:MAX_SITES],
        "affected_more": max(0, len(production) - MAX_SITES),
        "affected_files": len({r["file_id"] for r in production}),
        "tests": tests[:MAX_SITES],
        "tests_more": max(0, len(tests) - MAX_SITES),
    }


def inspect_symbol_graph(
    repository: GraphRepository,
    *,
    query: str,
    mode: str = "definition",
    repo_ids: list[str] | None = None,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """The answer to one graph question (see module docstring), in the standard envelope."""
    mode = (mode or "definition").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} is not one of: {', '.join(MODES)}")
    if not (query or "").strip():
        raise ValueError("query must not be empty")
    allow = _allow(repo_ids, scope)
    payload: dict[str, Any] = {"query": query, "mode": mode}
    envelope = {
        "tool": "inspect_symbol_graph",
        "payload": payload,
        "message": None,
        "locale": locale,
    }
    if mode == "impact":
        payload["impact"] = _impact(repository, query, allow, repo_ids)
        payload["status"] = "ok" if payload["impact"]["changed"] else "not_found"
        return envelope

    rows, _how = resolve_query(repository, query, allow, repo_ids)
    target = _pick(rows)
    if target is None:
        shown = rows[:MAX_CANDIDATES]
        meta = _meta(repository, [str(r["symbol_id"]) for r in shown])
        payload["status"] = "ambiguous" if rows else "not_found"
        payload["candidates"] = [_location(r, meta.get(str(r["symbol_id"]))) for r in shown]
        payload["candidates_more"] = max(0, len(rows) - len(shown))
        return envelope

    meta = _meta(repository, [str(target["symbol_id"])]).get(str(target["symbol_id"]))
    full = _merged(target, meta)
    payload["status"] = "ok"
    payload["target"] = _location(full)
    if mode == "definition":
        payload["definition"] = _definition(repository, full)
    elif mode == "callees":
        payload["callees"], payload["more"] = _callees(repository, full, allow)
    else:
        edges = frozenset({"CALLS"}) if mode == "callers" else frozenset({"CALLS", "REFERENCES"})
        payload["sites"], payload["more"] = _sites(repository, full, edge_types=edges, allow=allow)
    return envelope
