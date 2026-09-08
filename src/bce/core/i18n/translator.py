"""Message catalog loader and translator.

Catalogs are flat ``key -> template`` JSON files in ``catalogs/``. ``translate`` resolves the
locale, looks the key up (falling back to the default locale, then to the key itself), and formats
with the provided parameters. Missing parameters degrade gracefully to the raw template rather than
raising, so a localization gap can never crash a tool call.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from bce.core.i18n.locale import resolve_locale

_CATALOG_DIR = Path(__file__).parent / "catalogs"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


class Translator:
    def __init__(
        self,
        *,
        supported: tuple[str, ...] = ("en", "tr"),
        default: str = "en",
        catalog_dir: Path | None = None,
    ) -> None:
        self.supported = supported
        self.default = default
        self._dir = catalog_dir or _CATALOG_DIR
        self._catalogs: dict[str, dict[str, str]] = {}
        for locale in supported:
            self._catalogs[locale] = self._load(locale)

    def _load(self, locale: str) -> dict[str, str]:
        path = self._dir / f"{locale}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def resolve(self, requested: str | None = None, accept_language: str | None = None) -> str:
        return resolve_locale(
            requested=requested,
            accept_language=accept_language,
            supported=self.supported,
            default=self.default,
        )

    def translate(self, key: str, locale: str | None = None, /, **params: object) -> str:
        loc = locale if locale in self._catalogs else self.default
        template = self._catalogs.get(loc, {}).get(key)
        if template is None:
            template = self._catalogs.get(self.default, {}).get(key, key)
        return template.format_map(_SafeDict(params))

    # Convenience alias.
    t = translate


@lru_cache(maxsize=1)
def get_translator() -> Translator:
    from bce.config import get_settings

    settings = get_settings()
    return Translator(supported=settings.supported_locales, default=settings.default_locale)
