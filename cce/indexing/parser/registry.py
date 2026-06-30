"""Maps file extensions to language providers.

Adding a language is a one-line registration here (plus the provider module). The rest of the
engine discovers languages exclusively through this registry.
"""

from __future__ import annotations

from cce.indexing.parser.base import LanguageProvider


class LanguageRegistry:
    def __init__(self) -> None:
        self._by_extension: dict[str, LanguageProvider] = {}
        self._by_language: dict[str, LanguageProvider] = {}

    def register(self, provider: LanguageProvider) -> None:
        self._by_language[provider.language] = provider
        for ext in provider.extensions():
            self._by_extension[ext.lower()] = provider

    def get_for_path(self, path: str) -> LanguageProvider | None:
        path_l = path.lower()
        # Match the longest extension first so ".d.ts" beats ".ts" if ever registered.
        for ext in sorted(self._by_extension, key=len, reverse=True):
            if path_l.endswith(ext):
                return self._by_extension[ext]
        return None

    def get(self, language: str) -> LanguageProvider | None:
        return self._by_language.get(language)

    def supported_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_extension))

    def languages(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_language))


def build_default_registry() -> LanguageRegistry:
    """Phase 0 default: Python + JS/TS. Later phases register more providers here."""
    from cce.indexing.parser.languages.jsts_provider import (
        JavaScriptProvider,
        TsxProvider,
        TypeScriptProvider,
    )
    from cce.indexing.parser.languages.python_provider import PythonProvider

    registry = LanguageRegistry()
    registry.register(PythonProvider())
    registry.register(JavaScriptProvider())
    registry.register(TypeScriptProvider())
    registry.register(TsxProvider())
    return registry
