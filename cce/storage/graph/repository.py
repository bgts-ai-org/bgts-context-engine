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
            "RETURN caller.symbol_id, caller.file_id, caller.line, type(r), r.ref_kind, r.provenance "
            "ORDER BY caller.symbol_id, caller.line"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        """Return a single symbol's core properties (or None if it does not exist)."""
        columns = ["symbol_id", "name", "kind", "signature", "file_id", "line", "indexed_at_commit"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym}) "
            "RETURN s.symbol_id, s.name, s.kind, s.signature, s.file_id, s.line, "
            "s.indexed_at_commit LIMIT 1"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        if not rows:
            return None
        return dict(zip(columns, rows[0], strict=False))

    def find_implementers(self, symbol_id: str) -> list[dict[str, Any]]:
        """Types that INHERITS/IMPLEMENTS the given type symbol (may be cross-repo)."""
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (impl:Symbol)-[r]->(t:Symbol {symbol_id: $sym}) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN impl.symbol_id, impl.name, impl.kind, impl.file_id, impl.line, "
            "type(r), r.provenance "
            "ORDER BY impl.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_callers(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "file_id", "line", "provenance"]
        query = (
            "MATCH (caller:Symbol)-[r:CALLS]->(t:Symbol {symbol_id: $sym}) "
            "RETURN caller.symbol_id, caller.name, caller.file_id, caller.line, r.provenance "
            "ORDER BY caller.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_callees(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "file_id", "line", "provenance"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[r:CALLS]->(callee:Symbol) "
            "RETURN callee.symbol_id, callee.name, callee.file_id, callee.line, r.provenance "
            "ORDER BY callee.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_supertypes(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[r]->(super:Symbol) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN super.symbol_id, super.name, super.kind, super.file_id, super.line, "
            "type(r), r.provenance "
            "ORDER BY super.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_subtypes(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (sub:Symbol)-[r]->(s:Symbol {symbol_id: $sym}) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN sub.symbol_id, sub.name, sub.kind, sub.file_id, sub.line, "
            "type(r), r.provenance "
            "ORDER BY sub.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_file_imports(self, file_id: str) -> list[dict[str, Any]]:
        """Direct IMPORTS edges out of a file (to modules/files)."""
        columns = ["target_id", "namespace", "provenance"]
        query = (
            "MATCH (f:File {file_id: $fid})-[r:IMPORTS]->(m) "
            "RETURN m.gid, m.namespace, r.provenance "
            "ORDER BY m.gid"
        )
        rows = self.client.cypher(query, {"fid": file_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def find_routes(self, path_substring: str | None = None) -> list[dict[str, Any]]:
        """Route nodes and their handler symbol (anchor source #1: explicit route reference)."""
        columns = ["route_id", "http_method", "path_pattern", "framework", "handler_id", "line"]
        if path_substring:
            query = (
                "MATCH (rt:Route) WHERE rt.path_pattern CONTAINS $q "
                "OPTIONAL MATCH (rt)-[:ROUTES_TO]->(s:Symbol) "
                "RETURN rt.route_id, rt.http_method, rt.path_pattern, rt.framework, s.symbol_id, "
                "rt.line ORDER BY rt.route_id"
            )
            params = {"q": path_substring}
        else:
            query = (
                "MATCH (rt:Route) OPTIONAL MATCH (rt)-[:ROUTES_TO]->(s:Symbol) "
                "RETURN rt.route_id, rt.http_method, rt.path_pattern, rt.framework, s.symbol_id, "
                "rt.line ORDER BY rt.route_id"
            )
            params = {}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_design_notes(self, symbol_id: str) -> list[dict[str, Any]]:
        """DesignNotes that EXPLAINS a symbol (feature 5: the "why")."""
        columns = ["note_id", "kind", "text", "file_id", "line"]
        query = (
            "MATCH (n:DesignNote)-[:EXPLAINS]->(s:Symbol {symbol_id: $sym}) "
            "RETURN n.note_id, n.kind, n.text, n.file_id, n.line ORDER BY n.line"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def symbols_in_file(self, file_id: str) -> list[dict[str, Any]]:
        """All symbols defined in a file (used to expand file anchors to symbols)."""
        columns = ["symbol_id", "name", "kind", "line"]
        query = (
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: $fid}) "
            "RETURN s.symbol_id, s.name, s.kind, s.line ORDER BY s.line, s.symbol_id"
        )
        rows = self.client.cypher(query, {"fid": file_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def symbol_degree(self, symbol_id: str) -> int:
        """Total in+out edge degree for a symbol (structural centrality proxy, spec section 6.4)."""
        columns = ["deg"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym}) "
            "OPTIONAL MATCH (s)-[out]->() WITH s, count(out) AS outd "
            "OPTIONAL MATCH ()-[inc]->(s) RETURN outd + count(inc)"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        if not rows or rows[0][0] is None:
            return 0
        try:
            return int(rows[0][0])
        except (TypeError, ValueError):
            return 0

    def lexical_search(
        self, term: str, *, repo_ids: list[str] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Deterministic keyword match on symbol name (case-insensitive CONTAINS).

        Ordered by name then symbol_id for reproducibility. Feeds hybrid search + anchor source #4
        (the lexical half) without any model.
        """
        columns = ["symbol_id", "name", "kind", "file_id", "line", "indexed_at_commit"]
        term_l = (term or "").lower()
        ret = (
            "RETURN s.symbol_id, s.name, s.kind, s.file_id, s.line, s.indexed_at_commit "
            "ORDER BY s.name, s.symbol_id LIMIT $limit"
        )
        if repo_ids:
            query = (
                "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File) "
                "WHERE toLower(s.name) CONTAINS $term AND f.repo_id IN $repos " + ret
            )
            params: dict[str, Any] = {"term": term_l, "repos": repo_ids, "limit": limit}
        else:
            query = "MATCH (s:Symbol) WHERE toLower(s.name) CONTAINS $term " + ret
            params = {"term": term_l, "limit": limit}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def repo_of_symbol(self, symbol_id: str) -> str | None:
        columns = ["repo_id"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[:DEFINED_IN]->(f:File) "
            "RETURN f.repo_id LIMIT 1"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return rows[0][0] if rows else None

    def counts(self) -> tuple[int, int]:
        nodes = self.client.cypher("MATCH (n) RETURN count(n)", None, ["c"])
        edges = self.client.cypher("MATCH ()-[r]->() RETURN count(r)", None, ["c"])
        n = int(nodes[0][0]) if nodes else 0
        e = int(edges[0][0]) if edges else 0
        return n, e
