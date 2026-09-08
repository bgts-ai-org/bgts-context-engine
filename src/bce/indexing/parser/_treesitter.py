"""Thin tree-sitter compatibility helpers.

The tree-sitter Python binding changed its construction API across 0.20 -> 0.21 -> 0.22+. These
helpers isolate that churn so language providers can stay clean and version-agnostic.
"""

from __future__ import annotations

from typing import Any

from tree_sitter import Language, Parser


def language_from(raw: Any, name: str = "lang") -> Language:
    """Wrap a raw grammar handle (PyCapsule or pointer) into a ``Language``."""
    try:
        return Language(raw)  # tree-sitter >= 0.22 (PyCapsule)
    except TypeError:
        return Language(raw, name)  # older binding: Language(ptr, name)


def load_language(grammar_module: Any) -> Language:
    """Build a ``Language`` from a packaged grammar module exposing ``language()``."""
    name = getattr(grammar_module, "__name__", "lang").replace("tree_sitter_", "")
    return language_from(grammar_module.language(), name)


def make_parser(language: Language) -> Parser:
    """Construct a ``Parser`` bound to ``language`` across binding versions."""
    try:
        return Parser(language)  # tree-sitter >= 0.22
    except TypeError:
        parser = Parser()
        set_language = getattr(parser, "set_language", None)
        if callable(set_language):
            set_language(language)  # tree-sitter <= 0.21
        else:  # pragma: no cover - defensive
            parser.language = language
        return parser


def node_text(node: Any, source: bytes) -> str:
    """Return the source text spanned by a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


#: Deterministic cap for the ``body`` symbol property (embedding input; keeps node payloads small).
BODY_SNIPPET_MAX_CHARS = 1200


def body_snippet(node: Any, source: bytes, max_chars: int = BODY_SNIPPET_MAX_CHARS) -> str | None:
    """Trimmed body text for embedding content (deterministic char-based truncation)."""
    if node is None:
        return None
    text = node_text(node, source).strip()
    if not text:
        return None
    return text[:max_chars]
