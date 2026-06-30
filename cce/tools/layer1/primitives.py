"""Layer-1 deterministic primitives: resolve_symbol, find_references.

Each tool returns a dict with:
- ``payload``: the deterministic, language-neutral result (symbols, references, ids, commit).
- ``message``: an optional human-readable, localized string (i18n - presentation only).
- ``locale``: the resolved locale used for ``message``.

The ``payload`` is byte-identical regardless of ``locale``; only ``message`` changes. This is the
i18n boundary that keeps P1 intact.
"""

from __future__ import annotations

from typing import Any

from cce.core.i18n import get_translator
from cce.storage.graph.repository import GraphRepository


def resolve_symbol(
    repository: GraphRepository,
    name: str,
    repo_id: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    matches = repository.resolve_symbol(name, repo_id)
    tr = get_translator()
    loc = tr.resolve(locale)

    if matches:
        message = None
        commit = matches[0].get("indexed_at_commit")
    else:
        message = tr.translate("error.symbol_not_found", loc, name=name, scope=repo_id or "*")
        commit = None

    return {
        "tool": "resolve_symbol",
        "payload": {"matches": matches, "indexed_at_commit": commit},
        "message": message,
        "locale": loc,
    }


def find_references(
    repository: GraphRepository,
    symbol_id: str,
    locale: str | None = None,
) -> dict[str, Any]:
    references = repository.find_references(symbol_id)
    tr = get_translator()
    loc = tr.resolve(locale)
    message = tr.translate("tool.references.count", loc, count=len(references), name=symbol_id)

    return {
        "tool": "find_references",
        "payload": {"symbol_id": symbol_id, "references": references},
        "message": message,
        "locale": loc,
    }
