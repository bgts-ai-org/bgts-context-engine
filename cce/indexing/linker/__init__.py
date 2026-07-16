"""Repo-wide cross-file linker (pass 2 of indexing).

Resolves the :class:`~cce.domain.models.UnresolvedRef` entries recorded by the language providers
against a deterministic repo-wide symbol table + per-file import bindings, producing the
cross-file CALLS / REFERENCES / INHERITS / IMPLEMENTS edges that pure intra-file extraction
cannot see.
"""

from cce.indexing.linker.linker import link_fragments, synthesize_scip_edges

__all__ = ["link_fragments", "synthesize_scip_edges"]
