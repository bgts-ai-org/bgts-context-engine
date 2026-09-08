"""Multi-source anchor finding (spec section 6.2).

Four sources feed the graph entry points, in priority order:

1. Explicit reference - exact symbol names or route paths mentioned in the task text (fully
   deterministic). Route nodes act as anchor sources here (feature 1).
2. Jira metadata - component -> repo mapping, linked commit/PR (deterministic lookup).
3. task_history - files touched by past resolutions of the task (semi-deterministic, past data).
4. Semantic/lexical hybrid - lexical keyword match first, embedding as the lowest-priority "widest
   net" fallback (the plan's "deterministic + lexical first" strategy). This is the only source that
   may touch a model, and only to *find* an anchor (P2).

The result is a deterministic, de-duplicated, ordered set of anchor symbol_ids, each tagged with the
source(s) that produced it - which coverage/confidence later reads (more sources = higher trust).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bce.storage.graph.repository import GraphRepository

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH_RE = re.compile(r"/[A-Za-z0-9_\-{}/:]+")


@dataclass(slots=True)
class AnchorResult:
    #: symbol_id -> sorted list of source tags ("explicit", "jira", "history", "lexical", "semantic")
    anchors: dict[str, list[str]] = field(default_factory=dict)
    #: repo_ids implied by the task metadata (component match, history), for repo scoping.
    repo_hints: list[str] = field(default_factory=list)

    def add(self, symbol_id: str, source: str) -> None:
        sources = self.anchors.setdefault(symbol_id, [])
        if source not in sources:
            sources.append(source)
            sources.sort()

    @property
    def anchor_ids(self) -> list[str]:
        return sorted(self.anchors)

    @property
    def source_count(self) -> int:
        seen = set()
        for sources in self.anchors.values():
            seen.update(sources)
        return len(seen)


def find_anchors(
    repository: GraphRepository,
    *,
    task_text: str,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    repo_ids: list[str] | None = None,
    lexical_terms: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
) -> AnchorResult:
    """Combine the four anchor sources deterministically. Callers supply pre-fetched metadata."""
    result = AnchorResult()
    for rid in sorted(set(component_repo_ids or [])):
        result.repo_hints.append(rid)

    # Source 1a: explicit symbol names (exact name resolution).
    names = list(explicit_symbols or []) + _extract_identifiers(task_text)
    for name in sorted(set(names)):
        for match in repository.resolve_symbol(name, None):
            sid = match.get("symbol_id")
            if sid:
                result.add(sid, "explicit")

    # Source 1b: explicit route paths -> handler symbols (feature 1 as anchor source).
    paths = list(route_paths or []) + _extract_paths(task_text)
    for path in sorted(set(paths)):
        for route in repository.find_routes(path):
            handler = route.get("handler_id")
            if handler:
                result.add(handler, "explicit")

    # Source 3: task_history files -> their symbols.
    for file_id in sorted(set(history_file_ids or [])):
        for sym in repository.symbols_in_file(file_id):
            sid = sym.get("symbol_id")
            if sid:
                result.add(sid, "history")

    # Source 4a: lexical keyword match (deterministic, no model).
    for term in sorted(set(lexical_terms or _extract_identifiers(task_text))):
        for row in repository.lexical_search(term, repo_ids=repo_ids, limit=5):
            sid = row.get("symbol_id")
            if sid:
                result.add(sid, "lexical")

    # Source 4b: semantic candidates (lowest priority; caller supplies pre-ranked symbol_ids).
    for sid in semantic_candidates or []:
        result.add(sid, "semantic")

    return result


def _extract_identifiers(text: str) -> list[str]:
    """Identifier-like tokens from task text, length>=3, de-duplicated (deterministic)."""
    if not text:
        return []
    seen: list[str] = []
    for token in _WORD_RE.findall(text):
        if len(token) >= 3 and token not in seen:
            seen.append(token)
    return seen


def _extract_paths(text: str) -> list[str]:
    return _PATH_RE.findall(text or "")
