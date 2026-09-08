"""Locale resolution.

Resolution priority (deterministic): explicit request parameter > ``Accept-Language`` header >
configured default. Region subtags are dropped (``tr-TR`` -> ``tr``). Unknown locales fall back to
the default. This module has no side effects on the payload.
"""

from __future__ import annotations


def normalize_locale(locale: str | None) -> str | None:
    if not locale:
        return None
    base = locale.strip().lower().replace("_", "-").split("-", 1)[0]
    return base or None


def _parse_accept_language(header: str) -> list[str]:
    """Return locales from an Accept-Language header, ordered by descending q-value (stable)."""
    items: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        token = part.strip()
        if not token:
            continue
        lang, _, params = token.partition(";")
        quality = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                quality = float(params[2:])
            except ValueError:
                quality = 0.0
        base = normalize_locale(lang)
        if base:
            # index keeps original order stable for equal q-values.
            items.append((-quality, index, base))
    items.sort()
    seen: set[str] = set()
    ordered: list[str] = []
    for _, _, base in items:
        if base not in seen:
            seen.add(base)
            ordered.append(base)
    return ordered


def resolve_locale(
    *,
    requested: str | None = None,
    accept_language: str | None = None,
    supported: tuple[str, ...] = ("en", "tr"),
    default: str = "en",
) -> str:
    requested_base = normalize_locale(requested)
    if requested_base and requested_base in supported:
        return requested_base

    if accept_language:
        for base in _parse_accept_language(accept_language):
            if base in supported:
                return base

    return default
