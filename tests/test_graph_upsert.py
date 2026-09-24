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
    # Target outside the fragment (cross-file call): label unknown -> unlabelled fallback.
    frag.add_edge(GraphEdge(EdgeLabel.CALLS, "s1", "s-elsewhere"))

    repo.upsert_fragment(frag)

    heads = {q.split(" SET ")[0] for q in _edge_queries(client)}
    assert heads == {
        "MATCH (a:Symbol {gid: $src}), (b:File {gid: $dst}) MERGE (a)-[r:DEFINED_IN]->(b)",
        "MATCH (a:Symbol {gid: $src}), (b {gid: $dst}) MERGE (a)-[r:CALLS]->(b)",
    }
    params = [p for q, p in client.queries if "CALLS" in q][0]
    assert params["src"] == "s1" and params["dst"] == "s-elsewhere"


def test_upsert_edge_without_labels_keeps_the_unlabelled_match() -> None:
    client = _RecordingClient()
    repo = GraphRepository(client)  # type: ignore[arg-type]
    repo.upsert_edge(GraphEdge(EdgeLabel.IMPORTS, "f1", "m1"))
    assert _edge_queries(client)[0].startswith(
        "MATCH (a {gid: $src}), (b {gid: $dst}) MERGE (a)-[r:IMPORTS]->(b)"
    )
