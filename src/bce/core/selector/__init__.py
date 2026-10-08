"""Post-ranking context selector (decision model). See :mod:`bce.core.selector.selector`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bce.core.selector.jev import Decision, JevClient, JevError
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
    "PRESETS",
    "SYMBOL_LEVELS",
    "Decision",
    "FileGroup",
    "JevClient",
    "JevError",
    "Selector",
    "SelectorConfig",
    "SelectorPreset",
    "apply_policy",
    "describe_selector",
    "group_files",
    "selector_from_settings",
]

DECIDER_LOCAL_URL = "http://127.0.0.1:8000/v1/systemone"


@dataclass(frozen=True)
class SelectorPreset:
    """Defaults of one ``BCE_SELECTOR`` value; ``BCE_SELECTOR_MODEL`` / ``_URL`` / ``_TIMEOUT``
    override them."""

    model: str
    url: str
    timeout: float
    #: Jev is a paid API and refuses anonymous requests; a decider server takes no key.
    needs_key: bool
    #: Substring the server's reported model name must contain (``None``: not checked).
    expect_served: str | None = None


PRESETS: dict[str, SelectorPreset] = {
    # Pinned release: the thresholds were fitted on it, so a new one is a re-fit, not a drop-in.
    "jev": SelectorPreset(
        model="typesafe/jev-1.13",
        url="https://openrouter.ai/api/alpha/decisions",
        timeout=3.0,
        needs_key=True,
    ),
    # https://huggingface.co/Mapika/decider-2b - the server ignores ``model`` and answers with the
    # weights it loaded; the name only labels coverage.selector.
    "decider-2b": SelectorPreset(
        model="decider-2b",
        url=DECIDER_LOCAL_URL,
        timeout=10.0,
        needs_key=False,
        expect_served="decider-2b",
    ),
    # https://huggingface.co/Mapika/decider-4b
    "decider-4b": SelectorPreset(
        model="decider-4b",
        url=DECIDER_LOCAL_URL,
        timeout=10.0,
        needs_key=False,
        expect_served="decider-4b",
    ),
}

_OFF = ("", "off", "none", "0", "false", "no")


def _mode(settings) -> str:
    return (settings.selector or "").strip().lower().replace("_", "-")


def selector_from_settings(settings=None) -> Selector | None:
    """The configured selector, or ``None`` when it is off / not configured (``BCE_SELECTOR``)."""
    if settings is None:
        from bce.config import get_settings

        settings = get_settings()
    mode = _mode(settings)
    if mode in _OFF:
        return None
    preset = PRESETS.get(mode)
    if preset is None:
        logger.warning(
            "selector: unknown BCE_SELECTOR=%r (expected off, %s), treating as off",
            settings.selector,
            ", ".join(PRESETS),
        )
        return None
    api_key = settings.selector_api_key or (settings.openrouter_api_key if mode == "jev" else "")
    if preset.needs_key and not api_key:
        logger.warning(
            "selector: BCE_SELECTOR=%s needs an API key (OPENROUTER_API_KEY, or "
            "BCE_SELECTOR_API_KEY for TypeSafe's own API); answers are returned unselected",
            mode,
        )
        return None
    timeout = settings.selector_timeout
    client = JevClient(
        api_key=api_key,
        model=settings.selector_model or preset.model,
        url=settings.selector_url or preset.url,
        timeout=preset.timeout if timeout is None or timeout <= 0 else timeout,
    )
    cfg = SelectorConfig(
        full_threshold=settings.selector_full_threshold,
        stub_threshold=settings.selector_stub_threshold,
        max_files=settings.selector_max_files,
        symbol_min_score=settings.selector_symbol_min_score,
        full_content_bytes=int(getattr(settings, "selector_full_content_bytes", 12_000)),
    )
    return Selector(client, cfg, method=mode, expect_served=preset.expect_served)


def describe_selector(settings=None) -> str:
    """One line for startup output: which selector runs, against which endpoint."""
    sel = selector_from_settings(settings)
    if sel is None:
        if settings is None:
            from bce.config import get_settings

            settings = get_settings()
        mode = _mode(settings)
        return "off" if mode in _OFF else f"off ({settings.selector!r} is not usable, see log)"
    c = sel.client
    return f"{sel.method} (model {c.model} at {c.url}, timeout {c.timeout:g} s)"
