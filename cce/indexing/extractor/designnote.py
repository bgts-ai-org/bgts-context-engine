"""DesignNote extractor (feature 5: the "why" inference).

Scans a file's line comments for design-rationale markers (``NOTE``/``WHY``/``HACK``/``TODO``/
``FIXME``) and emits :class:`DesignNote` nodes plus ``EXPLAINS`` edges to the nearest enclosing
symbol. This is deterministic and language-agnostic: it works on raw source lines using the
provider's comment markers, so no per-language AST walk is required.

Provenance: notes are extracted from comments via regex over tree-sitter-known comment syntax, so
edges are ``treesitter`` provenance (AST-adjacent), not synthesized heuristics.
"""

from __future__ import annotations

import re

from cce.domain.enums import DesignNoteKind, EdgeLabel, NodeLabel, Provenance
from cce.domain.models import GraphEdge, GraphFragment, GraphNode
from cce.indexing.parser.symbol_id import make_note_id

# Marker -> kind. Order matters only for the compiled alternation; matching is case-insensitive.
_MARKER_KIND: dict[str, DesignNoteKind] = {
    "WHY": DesignNoteKind.WHY,
    "NOTE": DesignNoteKind.NOTE,
    "HACK": DesignNoteKind.HACK,
    "XXX": DesignNoteKind.HACK,
    "TODO": DesignNoteKind.TODO,
    "FIXME": DesignNoteKind.FIXME,
}

_MARKER_RE = re.compile(
    r"\b(" + "|".join(sorted(_MARKER_KIND, key=len, reverse=True)) + r")\b[:\s-]*(.*)$",
    re.IGNORECASE,
)


def _strip_comment_prefix(line: str, markers: tuple[str, ...]) -> str | None:
    """Return the comment text (after the marker) if ``line`` is a line comment, else ``None``."""
    stripped = line.strip()
    for marker in markers:
        if stripped.startswith(marker):
            return stripped[len(marker):].strip()
    return None


def extract_design_notes(
    *,
    source: bytes,
    file_id: str,
    indexed_at_commit: str,
    comment_markers: tuple[str, ...],
    symbol_lines: list[tuple[int, str]],
) -> GraphFragment:
    """Extract DesignNote nodes + EXPLAINS edges from a file's line comments.

    ``symbol_lines`` is ``[(line_no, symbol_id)]`` (1-based) for the file's symbols, used to bind a
    note to the nearest symbol that starts on or after the note (its documentation target); if none
    follows, the nearest preceding symbol is used.
    """
    frag = GraphFragment()
    text = source.decode("utf-8", errors="replace")
    ordered_symbols = sorted(symbol_lines)

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        comment = _strip_comment_prefix(raw_line, comment_markers)
        if comment is None:
            continue
        match = _MARKER_RE.search(comment)
        if match is None:
            continue
        kind = _MARKER_KIND[match.group(1).upper()]
        note_text = match.group(2).strip() or comment.strip()
        note_id = make_note_id(file_id, idx, str(kind), note_text)
        frag.add_node(
            GraphNode(
                NodeLabel.DESIGN_NOTE,
                note_id,
                {
                    "kind": str(kind),
                    "text": note_text,
                    "file_id": file_id,
                    "line": idx,
                    "indexed_at_commit": indexed_at_commit,
                },
            )
        )
        target = _nearest_symbol(idx, ordered_symbols)
        if target is not None:
            frag.add_edge(
                GraphEdge(
                    EdgeLabel.EXPLAINS,
                    note_id,
                    target,
                    provenance=Provenance.TREESITTER,
                )
            )
    return frag


def _nearest_symbol(note_line: int, ordered_symbols: list[tuple[int, str]]) -> str | None:
    """Nearest following symbol (a note usually documents what comes next); else nearest preceding."""
    following = [sid for line, sid in ordered_symbols if line >= note_line]
    if following:
        return following[0]
    preceding = [sid for line, sid in ordered_symbols if line < note_line]
    return preceding[-1] if preceding else None
