"""Token-Budget Assembler (spec section 6.5).

Deterministic packing: iterate the already-ranked candidates from the top, assign a summary level by
graph distance (near = full body, mid = signature, far = reference-only), estimate the token cost of
that level, and include the item while the running total fits the budget. Items that would overflow
are skipped (not reordered), so the result is stable and reproducible for a given budget.

Token estimation is a fixed heuristic (≈4 chars/token) applied to fields already present on the
candidate/symbol - no model, no network - keeping the assembler fully deterministic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

_CHARS_PER_TOKEN = 4
# Distance thresholds for the summary level (near/mid/far).
_NEAR_MAX_DISTANCE = 1
_MID_MAX_DISTANCE = 2


class DetailLevel(StrEnum):
    FULL = "full"  # full body
    SIGNATURE = "signature"  # signature + docstring
    REFERENCE = "reference"  # reference only (id + location)


def _detail_for_distance(distance: int) -> DetailLevel:
    if distance <= _NEAR_MAX_DISTANCE:
        return DetailLevel.FULL
    if distance <= _MID_MAX_DISTANCE:
        return DetailLevel.SIGNATURE
    return DetailLevel.REFERENCE


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


#: Distance assumed when an item carries none (far -> reference-only rendering).
_UNKNOWN_DISTANCE = 3


def _distance_of(item: dict[str, Any]) -> int:
    """``graph_distance`` (or ``distance``) as an int; 0 is a valid value (anchors), only a
    missing/None entry falls back to the far default."""
    for key in ("graph_distance", "distance"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return _UNKNOWN_DISTANCE


#: Distance assumed when an item carries none (far -> reference-only rendering).
_UNKNOWN_DISTANCE = 3


def _distance_of(item: dict[str, Any]) -> int:
    """``graph_distance`` (or ``distance``) as an int; 0 is a valid value (anchors), only a
    missing/None entry falls back to the far default."""
    for key in ("graph_distance", "distance"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return _UNKNOWN_DISTANCE


def _render(item: dict[str, Any], level: DetailLevel) -> tuple[str, int]:
    """Return the (content, token_cost) for an item at a detail level."""
    sig = str(item.get("signature") or "")
    doc = str(item.get("docstring") or "")
    body = str(item.get("body") or "")
    name = str(item.get("name") or item.get("symbol_id") or "")

    if level is DetailLevel.FULL and body:
        content = body
    elif level in (DetailLevel.FULL, DetailLevel.SIGNATURE):
        content = "\n".join(p for p in (f"{name}{sig}", doc) if p)
    else:
        loc = f"{item.get('file_id') or ''}:{item.get('line') or ''}"
        content = f"{name} @ {loc}".strip()
    return content, _estimate_tokens(content or name)


def assemble(
    items: list[dict[str, Any]],
    *,
    max_tokens: int,
) -> dict[str, Any]:
    """Fit ranked ``items`` into ``max_tokens`` deterministically. Each item needs at least
    ``symbol_id`` and ``graph_distance``; optional ``signature``/``docstring``/``body``/``score``.

    Returns ``{items, used_tokens, budget, included, skipped}``. Items are consumed top-down; an item
    that would overflow the budget is skipped (later, cheaper items may still fit).
    """
    assembled: list[dict[str, Any]] = []
    used = 0
    skipped = 0

    for item in items:
        distance = _distance_of(item)
        level = _detail_for_distance(distance)
        content, cost = _render(item, level)
        if used + cost > max_tokens:
            # Try a cheaper reference-only rendering before giving up (recall > precision, P4).
            content, cost = _render(item, DetailLevel.REFERENCE)
            if used + cost > max_tokens:
                skipped += 1
                continue
            level = DetailLevel.REFERENCE
        used += cost
        assembled.append(
            {
                "symbol_id": item.get("symbol_id"),
                "repo_id": item.get("repo_id"),
                "detail_level": str(level),
                "graph_distance": distance,
                "score": item.get("score"),
                "tokens": cost,
                "content": content,
            }
        )

    return {
        "items": assembled,
        "used_tokens": used,
        "budget": max_tokens,
        "included": len(assembled),
        "skipped": skipped,
    }
