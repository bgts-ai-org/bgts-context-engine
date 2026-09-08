"""Optional SCIP resolution adapter (feature 3: provenance elevation).

SCIP (SCIP Code Intelligence Protocol) indexers such as ``scip-python`` and ``scip-typescript``
produce *exact*, compiler-grade cross-file and cross-repo symbol resolution. When such a binary is
available for a repository we run it, read the resulting ``index.scip``, and elevate the provenance
of edges it confirms from ``treesitter`` to ``scip`` (scoring trusts ``scip`` most, §6.4).

This is strictly a "use-if-available" enhancement: when no indexer binary is installed the resolver
reports itself unavailable and extraction behaves exactly as before (tree-sitter heuristics only),
so determinism is preserved either way.
"""

from bce.indexing.parser.scip.resolver import (
    NullScipResolver,
    ScipResolution,
    ScipResolver,
    build_scip_resolver,
    moniker_display_name,
)

__all__ = [
    "ScipResolver",
    "ScipResolution",
    "NullScipResolver",
    "build_scip_resolver",
    "moniker_display_name",
]
