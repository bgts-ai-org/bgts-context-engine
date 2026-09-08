"""Language-agnostic extraction front door.

The extractor selects a :class:`LanguageProvider` from the registry by file extension, parses, and
returns a de-duplicated, deterministically ordered :class:`GraphFragment`. It is the single place
the rest of the pipeline calls; it never contains language-specific logic itself.

Provenance: AST-derived edges default to ``treesitter`` (set on the edge model). SCIP-resolved
edges and synthesized heuristic bridges (cross-language, route bindings) override provenance in
later phases.
"""

from __future__ import annotations

from bce.domain.enums import EdgeLabel, NodeLabel, Provenance
from bce.domain.models import GraphFragment
from bce.indexing.extractor.designnote import extract_design_notes
from bce.indexing.parser.base import ParseContext
from bce.indexing.parser.registry import LanguageRegistry, build_default_registry
from bce.indexing.parser.scip import ScipResolution
from bce.indexing.parser.symbol_id import make_file_id

# Edge kinds whose provenance SCIP can elevate (relationship edges, not membership edges).
_SCIP_ELEVATABLE = {EdgeLabel.CALLS, EdgeLabel.REFERENCES, EdgeLabel.INHERITS, EdgeLabel.IMPLEMENTS}


class Extractor:
    def __init__(
        self,
        registry: LanguageRegistry | None = None,
        *,
        scip_resolution: ScipResolution | None = None,
    ) -> None:
        self.registry = registry or build_default_registry()
        # Optional repo-level exact resolution (feature 3). When present, relationship edges it
        # confirms are elevated to Provenance.SCIP; otherwise behaviour is unchanged.
        self.scip_resolution = scip_resolution

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
        if self.scip_resolution is not None:
            self._elevate_provenance(fragment)
        return fragment.deduped()

    def _elevate_provenance(self, fragment: GraphFragment) -> None:
        """Raise relationship-edge provenance to SCIP where the resolution confirms the pair.

        Endpoints are mapped to SCIP monikers by symbol *name* (a deterministic heuristic that works
        without per-language moniker plumbing). Only relationship edges are eligible; membership
        edges (DEFINED_IN/BELONGS_TO/IMPORTS) are untouched.
        """
        names: dict[str, str] = {}
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                name = node.properties.get("name")
                if isinstance(name, str):
                    names[node.node_id] = name
        for edge in fragment.edges:
            if edge.label not in _SCIP_ELEVATABLE:
                continue
            src_name = names.get(edge.src_id)
            dst_name = names.get(edge.dst_id)
            if src_name and dst_name and self.scip_resolution.confirms(src_name, dst_name):
                edge.provenance = Provenance.SCIP

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
