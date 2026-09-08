"""Deterministic graph data structures produced by the extractor and consumed by the upserter.

Nodes and edges are intentionally generic (label + id + properties) so the upserter and graph
client stay schema-agnostic, while builder helpers keep the spec property names consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bce.domain.enums import EdgeLabel, NodeLabel, Provenance, RefKind

# Canonical id property name per node label (used as the MERGE key on upsert).
ID_PROPERTY: dict[NodeLabel, str] = {
    NodeLabel.REPO: "repo_id",
    NodeLabel.FILE: "file_id",
    NodeLabel.SYMBOL: "symbol_id",
    NodeLabel.MODULE: "module_id",
    NodeLabel.ROUTE: "route_id",
    NodeLabel.DESIGN_NOTE: "note_id",
}


@dataclass(slots=True)
class GraphNode:
    label: NodeLabel
    node_id: str
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def id_property(self) -> str:
        return ID_PROPERTY[self.label]

    def merged_properties(self) -> dict[str, Any]:
        """Properties including the canonical id property and a generic ``gid``, for Cypher.

        ``gid`` mirrors the node id under a single property name across all labels, so edges can be
        matched uniformly (``MATCH (a {gid: $src})``) without knowing each endpoint's label.
        """
        props = dict(self.properties)
        props[self.id_property] = self.node_id
        props["gid"] = self.node_id
        return props

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (str(self.label), self.node_id)


@dataclass(slots=True)
class GraphEdge:
    label: EdgeLabel
    src_id: str
    dst_id: str
    provenance: Provenance = Provenance.TREESITTER
    synthesized_by: str | None = None
    ref_kind: RefKind | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def merged_properties(self) -> dict[str, Any]:
        props = dict(self.properties)
        props["provenance"] = str(self.provenance)
        if self.synthesized_by is not None:
            props["synthesized_by"] = self.synthesized_by
        if self.ref_kind is not None:
            props["ref_kind"] = str(self.ref_kind)
        return props

    @property
    def dedup_key(self) -> tuple[str, str, str, str | None, str | None]:
        return (
            str(self.label),
            self.src_id,
            self.dst_id,
            str(self.ref_kind) if self.ref_kind is not None else None,
            self.synthesized_by,
        )


@dataclass(slots=True, frozen=True)
class ImportBinding:
    """A local-name binding introduced by an import statement (cross-file linking input).

    ``local_name`` is the identifier usable inside the importing file (alias-aware). A local name of
    ``"*"`` denotes a wildcard/namespace-wide import (``from m import *``, Java ``import a.b.*``,
    C# ``using``). ``module_path`` is the language-normalized absolute module path (relative
    Python/JS specifiers are resolved against the importing file's package before recording).
    ``imported_name`` is the original exported name, or ``None`` for module-object bindings.
    """

    local_name: str
    module_path: str
    imported_name: str | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.local_name, self.module_path, self.imported_name or "")


@dataclass(slots=True, frozen=True)
class UnresolvedRef:
    """A call/reference/inheritance whose target was not defined in the same file.

    Providers record these instead of silently dropping the edge; the repo-wide linker resolves
    them against the global symbol table + import bindings after all files are extracted.
    """

    src_symbol_id: str
    name: str
    qualifier: str | None = None
    kind: str = "call"  # "call" | "reference" | "inherits" | "implements"
    ref_kind: RefKind | None = None
    line: int | None = None

    @property
    def sort_key(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.src_symbol_id,
            self.name,
            self.qualifier or "",
            self.kind,
            str(self.ref_kind) if self.ref_kind is not None else "",
            self.line or 0,
        )


@dataclass(slots=True)
class FragmentLinkData:
    """Per-file linking context carried alongside a fragment (not persisted to the graph).

    ``exports`` maps top-level names (and ``Class.method`` qualified names) defined in the file to
    their symbol ids; the linker unions these into the repo-wide symbol table.
    """

    file_id: str
    package: str | None
    language: str
    exports: dict[str, str] = field(default_factory=dict)
    imports: list[ImportBinding] = field(default_factory=list)
    unresolved: list[UnresolvedRef] = field(default_factory=list)

    def deduped(self) -> FragmentLinkData:
        imports = sorted(set(self.imports), key=lambda b: b.sort_key)
        unresolved = sorted(set(self.unresolved), key=lambda u: u.sort_key)
        return FragmentLinkData(
            file_id=self.file_id,
            package=self.package,
            language=self.language,
            exports=dict(sorted(self.exports.items())),
            imports=imports,
            unresolved=unresolved,
        )


@dataclass(slots=True)
class GraphFragment:
    """A deterministic set of nodes and edges, typically for one file or one repo."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    link_data: FragmentLinkData | None = None

    def add_node(self, node: GraphNode) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)

    def extend(self, other: GraphFragment) -> None:
        self.nodes.extend(other.nodes)
        self.edges.extend(other.edges)
        if self.link_data is None:
            self.link_data = other.link_data

    def deduped(self) -> GraphFragment:
        """Return a fragment with duplicate nodes/edges removed, in a stable order.

        Determinism: nodes are de-duplicated by (label, id) and sorted by that key; edges by their
        dedup_key and sorted. This guarantees a reproducible upsert order regardless of traversal
        order in the extractor. Link data (if any) is deduped/sorted the same way.
        """
        seen_nodes: dict[tuple[str, str], GraphNode] = {}
        for node in self.nodes:
            seen_nodes.setdefault(node.dedup_key, node)

        seen_edges: dict[tuple, GraphEdge] = {}
        for edge in self.edges:
            seen_edges.setdefault(edge.dedup_key, edge)

        nodes = sorted(seen_nodes.values(), key=lambda n: n.dedup_key)
        edges = sorted(seen_edges.values(), key=lambda e: tuple("" if p is None else p for p in e.dedup_key))
        link_data = self.link_data.deduped() if self.link_data is not None else None
        return GraphFragment(nodes=nodes, edges=edges, link_data=link_data)

    def __len__(self) -> int:
        return len(self.nodes) + len(self.edges)
