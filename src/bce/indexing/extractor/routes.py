"""Route extraction helpers (feature 1: framework-aware routes).

Shared node/edge builders used by language providers' ``extract_routes``. A Route node captures the
HTTP method + path pattern + framework; a ROUTES_TO edge links it to the handler symbol so a route
can act as an explicit anchor source in retrieval (spec section 6.2).

Provenance: route bindings are inferred from framework conventions (decorators/registration calls),
not exact static resolution, so ROUTES_TO edges are tagged ``heuristic`` with
``synthesized_by='route-binding'``.
"""

from __future__ import annotations

from bce.domain.enums import EdgeLabel, HttpMethod, NodeLabel, Provenance
from bce.domain.models import GraphEdge, GraphFragment, GraphNode
from bce.indexing.parser.symbol_id import make_route_id

_ROUTE_SYNTHESIZED_BY = "route-binding"


def normalize_method(raw: str) -> str:
    """Map a framework method token to a canonical :class:`HttpMethod` value (default ANY)."""
    token = (raw or "").strip().upper()
    try:
        return str(HttpMethod(token))
    except ValueError:
        return str(HttpMethod.ANY)


def add_route(
    frag: GraphFragment,
    *,
    repo_id: str,
    framework: str,
    http_method: str,
    path_pattern: str,
    file_id: str,
    line: int,
    indexed_at_commit: str,
    handler_symbol_id: str | None = None,
) -> str:
    """Add a Route node (+ optional ROUTES_TO edge to the handler). Returns the route_id."""
    method = normalize_method(http_method)
    route_id = make_route_id(repo_id, framework, method, path_pattern)
    frag.add_node(
        GraphNode(
            NodeLabel.ROUTE,
            route_id,
            {
                "http_method": method,
                "path_pattern": path_pattern,
                "framework": framework,
                "file_id": file_id,
                "line": line,
                "indexed_at_commit": indexed_at_commit,
            },
        )
    )
    if handler_symbol_id is not None:
        frag.add_edge(
            GraphEdge(
                EdgeLabel.ROUTES_TO,
                route_id,
                handler_symbol_id,
                provenance=Provenance.HEURISTIC,
                synthesized_by=_ROUTE_SYNTHESIZED_BY,
            )
        )
    return route_id
