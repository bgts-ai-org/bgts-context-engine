"""Language-agnostic extraction front door.

The extractor selects a :class:`LanguageProvider` from the registry by file extension, parses, and
returns a de-duplicated, deterministically ordered :class:`GraphFragment`. It is the single place
the rest of the pipeline calls; it never contains language-specific logic itself.

Provenance: AST-derived edges default to ``treesitter`` (set on the edge model). SCIP-resolved
edges and synthesized heuristic bridges (cross-language, route bindings) override provenance in
later phases.
"""

from __future__ import annotations

from cce.domain.models import GraphFragment
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
        """Extract a fragment for one file, or ``None`` if no provider handles the extension."""
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
        # Deterministic order/de-dup so upsert and any hashing are reproducible (P1).
        return fragment.deduped()
