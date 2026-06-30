"""Typed graph operations built on the AGE Cypher client.

This is the surface the indexing pipeline and the Layer-1 tools depend on. Labels and id-property
names are interpolated from our own enums (never user input), while values flow through agtype
parameters.
"""

from __future__ import annotations

from typing import Any

from cce.domain.models import GraphEdge, GraphFragment, GraphNode
from cce.storage.graph.client import GraphClient

_REFERENCE_EDGE_TYPES = "['CALLS', 'REFERENCES', 'ROUTES_TO']"


def _set_clause(alias: str, props: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build a deterministic ``SET alias.key = $pN`` clause + params for a property map.

    AGE does not accept ``SET n += $mapParam``, so we set each property individually with a scalar
    parameter. Keys come from our schema (safe identifiers); values flow through agtype params.
    """
    parts: list[str] = []
    params: dict[str, Any] = {}
    for i, key in enumerate(sorted(props)):
        pk = f"p{i}"
        parts.append(f"{alias}.{key} = ${pk}")
        params[pk] = props[key]
    return ", ".join(parts), params


class GraphRepository:
    def __init__(self, client: GraphClient) -> None:
        self.client = client

    # --- write path (incremental upsert) ---

    def upsert_node(self, node: GraphNode) -> None:
        set_sql, params = _set_clause("n", node.merged_properties())
        params["id"] = node.node_id
        query = f"MERGE (n:{node.label} {{{node.id_property}: $id}})"
        if set_sql:
            query += f" SET {set_sql}"
        self.client.execute(query, params)

    def upsert_edge(self, edge: GraphEdge) -> None:
        set_sql, params = _set_clause("r", edge.merged_properties())
        params["src"] = edge.src_id
        params["dst"] = edge.dst_id
        query = (
            "MATCH (a {gid: $src}), (b {gid: $dst}) "
            f"MERGE (a)-[r:{edge.label}]->(b)"
        )
        if set_sql:
            query += f" SET {set_sql}"
        self.client.execute(query, params)

    def upsert_fragment(self, fragment: GraphFragment) -> None:
        frag = fragment.deduped()
        for node in frag.nodes:
            self.upsert_node(node)
        for edge in frag.edges:
            self.upsert_edge(edge)

    def delete_file_subgraph(self, file_id: str) -> None:
        """Remove a file's symbols and the file node (for incremental re-index, Phase 1)."""
        self.client.execute(
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: $fid}) DETACH DELETE s",
            {"fid": file_id},
        )
        self.client.execute("MATCH (f:File {file_id: $fid}) DETACH DELETE f", {"fid": file_id})

    # --- read path (Layer-1 primitives) ---

    def resolve_symbol(self, name: str, repo_id: str | None = None) -> list[dict[str, Any]]:
        columns = ["symbol_id", "kind", "signature", "docstring", "file_id", "line", "indexed_at_commit"]
        ret = (
            "RETURN s.symbol_id, s.kind, s.signature, s.docstring, s.file_id, s.line, "
            "s.indexed_at_commit"
        )
        if repo_id:
            query = (
                "MATCH (s:Symbol {name: $name})-[:DEFINED_IN]->(f:File) "
                f"WHERE f.repo_id = $repo {ret}"
            )
            params = {"name": name, "repo": repo_id}
        else:
            query = f"MATCH (s:Symbol {{name: $name}}) {ret}"
            params = {"name": name}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def find_references(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["caller_id", "file_id", "line", "edge_type", "ref_kind", "provenance"]
        query = (
            "MATCH (caller:Symbol)-[r]->(t:Symbol {symbol_id: $sym}) "
            f"WHERE type(r) IN {_REFERENCE_EDGE_TYPES} "
            "RETURN caller.symbol_id, caller.file_id, caller.line, type(r), r.ref_kind, r.provenance"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def counts(self) -> tuple[int, int]:
        nodes = self.client.cypher("MATCH (n) RETURN count(n)", None, ["c"])
        edges = self.client.cypher("MATCH ()-[r]->() RETURN count(r)", None, ["c"])
        n = int(nodes[0][0]) if nodes else 0
        e = int(edges[0][0]) if edges else 0
        return n, e
