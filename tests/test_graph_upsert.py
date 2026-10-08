"""Write path of ``GraphRepository``: fragment upserts name the endpoint labels in edge MATCHes."""

from __future__ import annotations

from typing import Any

from bce.domain.enums import EdgeLabel, NodeLabel
from bce.domain.models import GraphEdge, GraphFragment, GraphNode
from bce.storage.graph.repository import GraphRepository


class _RecordingClient:
    conn = None  # no relational side tables (symbol_fts probe short-circuits to "absent")

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self.queries.append((query, dict(params or {})))


def _edge_queries(client: _RecordingClient) -> list[str]:
    return [q for q, _ in client.queries if q.startswith("MATCH (")]


def test_fragment_edges_match_endpoints_by_label() -> None:
    client = _RecordingClient()
    repo = GraphRepository(client)  # type: ignore[arg-type]
    frag = GraphFragment()
    frag.add_node(GraphNode(NodeLabel.FILE, "f1", {"path": "a.py"}))
    frag.add_node(GraphNode(NodeLabel.SYMBOL, "s1", {"name": "f"}))
    frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, "s1", "f1"))
    # Target outside the fragment (cross-file call): the edge type fixes it as a Symbol.
    frag.add_edge(GraphEdge(EdgeLabel.CALLS, "s1", "s-elsewhere"))

    repo.upsert_fragment(frag)

    heads = {q.split(" SET ")[0] for q in _edge_queries(client)}
    assert heads == {
        "MATCH (a:Symbol {gid: $src}), (b:File {gid: $dst}) MERGE (a)-[r:DEFINED_IN]->(b)",
        "MATCH (a:Symbol {gid: $src}), (b:Symbol {gid: $dst}) MERGE (a)-[r:CALLS]->(b)",
    }
    params = [p for q, p in client.queries if "CALLS" in q][0]
    assert params["src"] == "s1" and params["dst"] == "s-elsewhere"


def test_edge_only_fragments_take_endpoint_labels_from_the_edge_type() -> None:
    """The linker emits cross-file edges without nodes; every one must still be a labelled MATCH."""
    client = _RecordingClient()
    repo = GraphRepository(client)  # type: ignore[arg-type]
    frag = GraphFragment()
    frag.add_edge(GraphEdge(EdgeLabel.REFERENCES, "s1", "s2"))
    frag.add_edge(GraphEdge(EdgeLabel.INHERITS, "s1", "s3"))
    frag.add_edge(GraphEdge(EdgeLabel.ROUTES_TO, "route-1", "s1"))
    frag.add_edge(GraphEdge(EdgeLabel.EXPLAINS, "note-1", "s1"))
    # Module -> File binding: the Module node is in the fragment, the File is not.
    frag.add_node(GraphNode(NodeLabel.MODULE, "m1", {"namespace": "pkg"}))
    frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, "m1", "f-elsewhere"))

    repo.upsert_fragment(frag)

    heads = {q.split(" SET ")[0] for q in _edge_queries(client)}
    assert heads == {
        "MATCH (a:Symbol {gid: $src}), (b:Symbol {gid: $dst}) MERGE (a)-[r:REFERENCES]->(b)",
        "MATCH (a:Symbol {gid: $src}), (b:Symbol {gid: $dst}) MERGE (a)-[r:INHERITS]->(b)",
        "MATCH (a:Route {gid: $src}), (b:Symbol {gid: $dst}) MERGE (a)-[r:ROUTES_TO]->(b)",
        "MATCH (a:DesignNote {gid: $src}), (b:Symbol {gid: $dst}) MERGE (a)-[r:EXPLAINS]->(b)",
        "MATCH (a:Module {gid: $src}), (b:File {gid: $dst}) MERGE (a)-[r:IMPORTS]->(b)",
    }


class _RecordingConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)


def test_index_run_pins_the_planner_to_point_lookups_and_restores_it() -> None:
    """The label tables are never analyzed inside the indexing transaction, so without this the
    edge MERGE existence check becomes a sort of the whole edge table per edge."""
    from bce.indexing.indexer import Indexer

    conn = _RecordingConn()
    client = _RecordingClient()
    client.conn = conn  # type: ignore[assignment]
    indexer = Indexer(GraphRepository(client), embed=False)  # type: ignore[arg-type]
    with indexer._point_lookup_plans():
        assert conn.statements == ["SET enable_mergejoin = off", "SET enable_hashjoin = off"]
    assert conn.statements[2:] == ["RESET enable_mergejoin", "RESET enable_hashjoin"]

    # Fakes without a connection (unit tests, dry runs): a no-op.
    bare = Indexer(GraphRepository(_RecordingClient()), embed=False)  # type: ignore[arg-type]
    with bare._point_lookup_plans():
        pass


def test_imports_edge_with_unknown_endpoints_keeps_the_unlabelled_match() -> None:
    """IMPORTS runs File <-> Module in both directions; with no endpoint known there is no guess."""
    client = _RecordingClient()
    repo = GraphRepository(client)  # type: ignore[arg-type]
    repo.upsert_edge(GraphEdge(EdgeLabel.IMPORTS, "f1", "m1"))
    assert _edge_queries(client)[0].startswith(
        "MATCH (a {gid: $src}), (b {gid: $dst}) MERGE (a)-[r:IMPORTS]->(b)"
    )
    repo.upsert_edge(GraphEdge(EdgeLabel.IMPORTS, "f1", "m1"), {"f1": NodeLabel.FILE})
    assert _edge_queries(client)[1].startswith(
        "MATCH (a:File {gid: $src}), (b:Module {gid: $dst}) MERGE (a)-[r:IMPORTS]->(b)"
    )
