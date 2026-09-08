"""Maps file extensions to language providers.

Adding a language is a one-line registration here (plus the provider module). The rest of the
engine discovers languages exclusively through this registry.
"""

from __future__ import annotations

from bce.indexing.parser.base import LanguageProvider


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
    """Default registry: Python + JS/TS always; Java/C#/Go when their grammars are installed.

    Java/C#/Go grammars are optional (``pip install -e '.[langs]'``). Each optional provider is
    registered only if its grammar import succeeds, so a minimal install still works and the set of
    supported languages is a deterministic function of the environment.
    """
    from bce.indexing.parser.languages.jsts_provider import (
        JavaScriptProvider,
        TsxProvider,
        TypeScriptProvider,
    )
    from bce.indexing.parser.languages.python_provider import PythonProvider

    registry = LanguageRegistry()
    registry.register(PythonProvider())
    registry.register(JavaScriptProvider())
    registry.register(TypeScriptProvider())
    registry.register(TsxProvider())
    _register_optional(registry)
    return registry


#: Optional providers by ecosystem: (module path, class name). Registered if the grammar imports.
_OPTIONAL_PROVIDERS = (
    ("bce.indexing.parser.languages.java_provider", "JavaProvider"),
    ("bce.indexing.parser.languages.csharp_provider", "CSharpProvider"),
    ("bce.indexing.parser.languages.go_provider", "GoProvider"),
)


def _register_optional(registry: LanguageRegistry) -> None:
    import importlib

    for module_path, class_name in _OPTIONAL_PROVIDERS:
        try:
            module = importlib.import_module(module_path)
            provider_cls = getattr(module, class_name)
            registry.register(provider_cls())
        except Exception:
            # Grammar (or provider) unavailable: skip. Behaviour degrades gracefully.
            continue
