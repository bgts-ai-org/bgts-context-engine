"""Language-agnostic extraction front door.

The extractor selects a :class:`LanguageProvider` from the registry by file extension, parses, and
returns a de-duplicated, deterministically ordered :class:`GraphFragment`. It is the single place
the rest of the pipeline calls; it never contains language-specific logic itself.

Provenance: AST-derived edges default to ``treesitter`` (set on the edge model). SCIP-resolved
edges and synthesized heuristic bridges (cross-language, route bindings) override provenance in
later phases.
"""

from __future__ import annotations

from cce.domain.enums import NodeLabel
from cce.domain.models import GraphFragment
from cce.indexing.extractor.designnote import extract_design_notes
from cce.indexing.parser.base import ParseContext
from cce.indexing.parser.registry import LanguageRegistry, build_default_registry
from cce.indexing.parser.symbol_id import make_file_id


class Extractor:
    def __init__(self, registry: LanguageRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def supports(self, path: str) -> bool:
        return self.registry.get_for_path(path) is not None

    def extract_file(
        self,
        *,
        repo_id: str,
        path: str,
        source: bytes,
        indexed_at_commit: str = "WORKDIR",
    ) -> GraphFragment | None:
        """Extract a fragment for one file, or ``None`` if no provider handles the extension.

        Runs three deterministic passes: base AST extraction (symbols + edges), route extraction
        (feature 1), and design-note extraction (feature 5). The combined fragment is de-duplicated
        and stably ordered so upsert and any hashing are reproducible (P1).
        """
        provider = self.registry.get_for_path(path)
        if provider is None:
            return None

        file_id = make_file_id(repo_id, path)
        ctx = ParseContext(
            repo_id=repo_id,
            file_id=file_id,
            path=path,
            language=provider.language,
            source=source,
            indexed_at_commit=indexed_at_commit,
        )
        ctx.package = provider.derive_package(ctx)

        tree = provider.parse(source)
        fragment = provider.extract(tree, ctx)

        symbol_lines = self._symbol_lines(fragment)
        fragment.extend(provider.extract_routes(tree, ctx, symbol_lines))
        fragment.extend(
            extract_design_notes(
                source=source,
                file_id=file_id,
                indexed_at_commit=indexed_at_commit,
                comment_markers=provider.line_comment_markers,
                symbol_lines=[(line, sid) for line, sid in symbol_lines.items()],
            )
        )
        return fragment.deduped()

    @staticmethod
    def _symbol_lines(fragment: GraphFragment) -> dict[int, str]:
        """Map a symbol's 1-based definition line to its ``symbol_id`` (for route/note binding)."""
        out: dict[int, str] = {}
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                line = node.properties.get("line")
                if isinstance(line, int):
                    out.setdefault(line, node.node_id)
        return out
