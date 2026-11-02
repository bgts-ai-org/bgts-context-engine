"""The file's own symbol (``kind="module"``): what the file declares at its top level.

Constants tables, registrations, re-exports and settings live outside any function or class, and
a task naming three of them names the *file*, not one of its tiny symbols: on their own each
matches one word, so the file never reaches the top of the lexical ranking. The module symbol
carries the file's outline - every line outside a declaration's body, with the first line of each
declaration kept as its entry in the outline - as one search document, so the words a task uses
about the file add up on one symbol.

Each run of omitted body lines is stored as one marker line naming the run (``⋯ 12-40``), so an
answer can show the outline with the file's own line numbers. Files whose outline has fewer
than :data:`MIN_OUTLINE_LINES` lines beyond their declarations' headers get no module symbol: the
declarations already say everything.
"""

from __future__ import annotations

import re

from bce.domain.enums import EdgeLabel, NodeLabel
from bce.domain.models import GraphEdge, GraphFragment, GraphNode
from bce.indexing.parser._treesitter import SEARCH_TEXT_MAX_CHARS
from bce.indexing.parser.symbol_id import make_symbol_id

MODULE_KIND = "module"
#: Starts the one stored line that stands for a run of omitted body lines: ``⋯ 12-40``.
OMITTED_MARK = "\u22ef"
#: Lines of top-level code (outside every body, imports not counted) a file needs for a module
#: symbol.
MIN_OUTLINE_LINES = 3
#: Declarations that are outline material in full (one-statement declarations).
_INLINE_KINDS = frozenset({"constant", "variable", "field", "property", "type"})
#: File stems that stand for their directory (``pkg/__init__.py`` is module ``pkg``).
_INDEX_STEMS = frozenset({"__init__", "index", "mod", "lib"})
#: Import, package and comment lines: part of the outline, but they do not make a file need one.
_IMPORT_LINE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|#\s*include\b|(?:pub\s+)?use\s|using\s+[\w.]+\s*;"
    r"|package\s+[\w.]+|extern\s+crate\b|\"[^\"]*\"\s*$|[)}\]]+[;,]?\s*$"
    # comments (licence headers, section banners); a C preprocessor directive is code
    r"|//|/\*|\*|--|#(?!\s*(?:define|undef|if|ifdef|ifndef|elif|else|endif|pragma)\b))"
)
_WORD = re.compile(r"\w")


def module_name(path: str) -> str:
    """``src/auth/tokens.py`` -> ``tokens``; ``src/auth/__init__.py`` -> ``auth``."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    stem = parts[-1].split(".", 1)[0]
    if stem in _INDEX_STEMS and len(parts) > 1:
        return parts[-2]
    return stem


def outline_text(source: bytes, spans: list[tuple[int, int, str]]) -> tuple[str, int, int]:
    """``(text, top_level_lines, file_lines)`` of the file's outline (see module docstring).

    ``spans`` are ``(line, end_line, kind)`` of the file's declarations (1-based, inclusive);
    constants, variables and type aliases count as top-level code. ``top_level_lines`` counts
    the lines kept that carry a word and are neither a body's first line nor an import.
    """
    lines = source.decode("utf-8", errors="replace").split("\n")
    covered = [False] * (len(lines) + 2)
    heads: set[int] = set()
    for line, end, kind in spans:
        if line < 1 or end < line or kind in _INLINE_KINDS or kind == MODULE_KIND:
            continue
        heads.add(line)
        for n in range(line, min(end, len(lines)) + 1):
            covered[n] = True
    if lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    top_level = 0
    size = 0
    n = 1
    while n <= len(lines):
        if n not in heads and covered[n]:
            last = n
            while last + 1 <= len(lines) and last + 1 not in heads and covered[last + 1]:
                last += 1
            piece = omitted_marker(n, last)
            n = last + 1
        else:
            piece = lines[n - 1].rstrip("\r")
            if not covered[n] and _WORD.search(piece) and not _IMPORT_LINE.match(piece):
                top_level += 1
            n += 1
        size += len(piece) + 1
        if size > SEARCH_TEXT_MAX_CHARS:
            break
        out.append(piece)
    return "\n".join(out), top_level, len(lines)


def omitted_marker(first: int, last: int) -> str:
    """The stored stand-in for file lines ``first..last`` of a body the outline leaves out."""
    return f"{OMITTED_MARK} {first}-{last}"


def module_fragment(
    fragment: GraphFragment,
    *,
    source: bytes,
    file_id: str,
    path: str,
    language: str,
    package: str | None,
    indexed_at_commit: str,
) -> GraphFragment:
    """The module symbol of one extracted file, or an empty fragment (see module docstring)."""
    out = GraphFragment()
    spans: list[tuple[int, int, str]] = []
    for node in fragment.nodes:
        if node.label is not NodeLabel.SYMBOL:
            continue
        line = node.properties.get("text_line") or node.properties.get("line")
        end = node.properties.get("end_line") or line
        if isinstance(line, int) and isinstance(end, int):
            spans.append((line, end, str(node.properties.get("kind") or "")))
    text, top_level, file_lines = outline_text(source, spans)
    if top_level < MIN_OUTLINE_LINES or not text.strip():
        return out
    name = module_name(path)
    if not name:
        return out
    rel = path.replace("\\", "/")
    # The path enters the hash only: the moniker is repo-independent like every symbol id, and
    # two files of one package (``a.h`` / ``a.c``, a Go directory) must not share an id.
    symbol_id = make_symbol_id(
        language=language,
        package=package,
        namespace="",
        name=name,
        signature=rel,
        kind=MODULE_KIND,
    )
    symbol = GraphNode(
        NodeLabel.SYMBOL,
        symbol_id,
        {
            "name": name,
            "kind": MODULE_KIND,
            "signature": None,
            "visibility": "public",
            "docstring": None,
            "body": None,
            "namespace": package or "",
            "file_id": file_id,
            "line": 1,
            "end_line": file_lines,
            "col": 0,
            "indexed_at_commit": indexed_at_commit,
        },
    )
    symbol.search_text = text
    out.add_node(symbol)
    out.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, file_id))
    return out
