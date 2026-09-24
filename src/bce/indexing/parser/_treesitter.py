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


#: Deterministic cap for the ``body`` symbol property (context assembly; keeps node payloads small).
BODY_SNIPPET_MAX_CHARS = 1200
#: Cap for a symbol's *search text* (FTS body + chunked embeddings). Wide enough for a whole page
#: component or service class; anything longer is a generated file and its tail carries nothing
#: a task description would name.
SEARCH_TEXT_MAX_CHARS = 24_000
#: A doc comment must end within this many lines above the declaration it documents.
_DOC_COMMENT_MAX_GAP_LINES = 1


def body_snippet(node: Any, source: bytes, max_chars: int = BODY_SNIPPET_MAX_CHARS) -> str | None:
    """Trimmed body text for the node property (deterministic char-based truncation)."""
    if node is None:
        return None
    text = node_text(node, source).strip()
    if not text:
        return None
    return text[:max_chars]


def search_text(node: Any, source: bytes, max_chars: int = SEARCH_TEXT_MAX_CHARS) -> str | None:
    """The whole declaration's text for the search indexes (deterministic truncation).

    Unlike :func:`body_snippet` this spans the *declaration* - name, parameters, type members,
    initializer - so an interface's fields, a type union's literals and a constant's value are
    searchable, and a long function body is kept far beyond the display snippet.
    """
    if node is None:
        return None
    text = node_text(node, source).strip()
    if not text:
        return None
    return text[:max_chars]


def leading_doc_comment(
    node: Any, source: bytes, *, markers: tuple[str, ...] = ("/**",)
) -> str | None:
    """The doc comment immediately above ``node`` (``/** ... */`` by default), cleaned.

    Only a comment whose *last* line sits directly above the declaration (at most one blank line
    between them) counts, so a file header or a comment on an unrelated earlier statement is
    never attached. Leading ``*`` decoration is stripped; the text is joined with newlines.
    """
    if node is None:
        return None
    sibling = node.prev_sibling
    # Skip decorators / modifiers tree-sitter may place between the comment and the declaration.
    while sibling is not None and sibling.type in ("decorator", "export", "default"):
        sibling = sibling.prev_sibling
    if sibling is None or sibling.type != "comment":
        return None
    if node.start_point[0] - sibling.end_point[0] > _DOC_COMMENT_MAX_GAP_LINES + 1:
        return None
    raw = node_text(sibling, source).strip()
    if not any(raw.startswith(m) for m in markers):
        return None
    return _clean_block_comment(raw)


def _clean_block_comment(raw: str) -> str | None:
    text = raw
    for opener in ("/**", "/*!", "/*"):
        if text.startswith(opener):
            text = text[len(opener) :]
            break
    if text.endswith("*/"):
        text = text[:-2]
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("*"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    cleaned = "\n".join(lines).strip()
    return cleaned or None
