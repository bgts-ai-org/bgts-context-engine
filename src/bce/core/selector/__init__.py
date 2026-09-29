"""Post-ranking context selector (Jev decision model). See :mod:`bce.core.selector.selector`."""

from __future__ import annotations

import logging

from bce.core.selector.jev import JevClient, JevError
from bce.core.selector.selector import (
    SYMBOL_LEVELS,
    FileGroup,
    Selector,
    SelectorConfig,
    apply_policy,
    group_files,
)

logger = logging.getLogger("bce.selector")

__all__ = [
    "SYMBOL_LEVELS",
    "FileGroup",
    "JevClient",
    "JevError",
    "Selector",
    "SelectorConfig",
    "apply_policy",
    "group_files",
    "selector_from_settings",
]


def selector_from_settings(settings=None) -> Selector | None:
    """The configured selector, or ``None`` when it is off / not configured (``BCE_SELECTOR``)."""
    if settings is None:
        from bce.config import get_settings

        settings = get_settings()
    mode = (settings.selector or "auto").strip().lower()
    if mode in ("off", "none", "0", "false"):
        return None
    if mode not in ("auto", "jev"):
        logger.warning("selector: unknown BCE_SELECTOR=%r, treating as off", settings.selector)
        return None
    if not settings.openrouter_api_key:
        if mode == "jev":
            logger.warning(
                "selector: BCE_SELECTOR=jev but no OPENROUTER_API_KEY; answers are returned unselected"
            )
        return None
    client = JevClient(
        api_key=settings.openrouter_api_key,
        model=settings.selector_model,
        url=settings.selector_url,
        timeout=settings.selector_timeout,
    )
    cfg = SelectorConfig(
        full_threshold=settings.selector_full_threshold,
        stub_threshold=settings.selector_stub_threshold,
        max_files=settings.selector_max_files,
        symbol_min_score=settings.selector_symbol_min_score,
    )
    return Selector(client, cfg)
