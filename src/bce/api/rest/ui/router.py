"""Read-only ``/v1/ui/*`` endpoints serving visualisation-friendly graph data.

Thin transport over :mod:`bce.api.rest.ui.queries`; reuses the standard per-request
``get_repository`` dependency (same connection handling as every other endpoint) but returns
plain JSON shapes instead of the tool envelope - these endpoints exist for frontends, not agents.

Identifiers (``repo_id``, ``gid``) travel as query parameters, never path segments: gids contain
``/``, ``:`` and ``#`` (e.g. ``demo:src/mod.py``, ``py::pkg::foo#1``) which are hostile to URL
path matching.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from bce import __version__
from bce.api.rest.deps import get_repository, get_vector_store
from bce.api.rest.ui import queries
from bce.api.rest.ui.schemas import (
    UIConfigResponse,
    UIGraphResponse,
    UINeighborsResponse,
    UINodeDetailResponse,
    UIRepoListResponse,
    UISearchResponse,
    UIStatsResponse,
    UITraceRequest,
    UITraceResponse,
)
from bce.config import get_settings
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore

router = APIRouter(prefix="/v1/ui", tags=["ui"])


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


@router.get("/config", response_model=UIConfigResponse)
def ui_config() -> dict[str, Any]:
    """Presentation settings for the frontend. Needs no database, so it is safe to call first."""
    settings = get_settings()
    return {
        "version": __version__,
        "default_locale": settings.default_locale,
        "supported_locales": list(settings.supported_locales),
    }


@router.get("/repos", response_model=UIRepoListResponse)
def ui_list_repos(
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """List indexed repositories (from the SQL ``repos`` table)."""
    return {"repos": queries.list_repos(repository.client.conn)}


@router.get("/stats", response_model=UIStatsResponse)
def ui_repo_stats(
    repo_id: str = Query(..., description="Repository id (see /v1/ui/repos)."),
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Node/edge counts per label/type and the file language distribution of a repo."""
    return queries.repo_stats(repository.client, repo_id)


@router.get("/graph", response_model=UIGraphResponse)
def ui_repo_graph(
    repo_id: str = Query(..., description="Repository id (see /v1/ui/repos)."),
    node_labels: str | None = Query(
        default=None, description="Comma-separated node labels to include (e.g. 'File,Symbol')."
    ),
    edge_types: str | None = Query(
        default=None, description="Comma-separated edge types to include (e.g. 'CALLS,IMPORTS')."
    ),
    node_limit: int = Query(
        default=5000, ge=1, le=20000, description="Max nodes per label (safety bound)."
    ),
    edge_limit: int = Query(default=20000, ge=1, le=100000, description="Max edges (safety bound)."),
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Whole-repo subgraph as ``{nodes, edges}``; edges whose endpoints were clipped are dropped."""
    return queries.repo_graph(
        repository.client,
        repo_id,
        node_labels=_csv(node_labels),
        edge_types=_csv(edge_types),
        node_limit=node_limit,
        edge_limit=edge_limit,
    )


@router.get("/node", response_model=UINodeDetailResponse)
def ui_node_detail(
    gid: str = Query(..., description="Universal node id (gid property)."),
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Full properties of one node (including body/docstring) for the detail panel."""
    detail = queries.node_detail(repository.client, gid)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"node not found: {gid}")
    return detail


@router.get("/neighbors", response_model=UINeighborsResponse)
def ui_node_neighbors(
    gid: str = Query(..., description="Universal node id (gid property)."),
    edge_types: str | None = Query(
        default=None, description="Comma-separated edge types to follow."
    ),
    limit: int = Query(default=200, ge=1, le=2000, description="Max neighbors per direction."),
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Direct neighbors + connecting edges of a node (click-to-expand in the graph view)."""
    return queries.node_neighbors(
        repository.client, gid, edge_types=_csv(edge_types), limit=limit
    )


@router.post("/context-trace", response_model=UITraceResponse)
def ui_context_trace(
    body: UITraceRequest,
    repository: GraphRepository = Depends(get_repository),
    store: VectorStore = Depends(get_vector_store),
) -> dict[str, Any]:
    """Stage-by-stage trace of the get_context_for_task pipeline (for animated visualisation)."""
    from bce.api.rest.ui.trace import trace_context_for_task

    return trace_context_for_task(
        repository,
        task_text=body.task_text,
        max_candidates=body.max_candidates,
        max_tokens=body.max_tokens,
        repo_ids=body.repo_ids,
        store=store,
        auto_semantic=body.auto_semantic,
    )


@router.get("/search", response_model=UISearchResponse)
def ui_search(
    q: str = Query(..., min_length=1, description="Search term (symbol name or file path)."),
    repo_id: str | None = Query(default=None, description="Restrict to one repo_id."),
    limit: int = Query(default=20, ge=1, le=100),
    repository: GraphRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Search symbols (lexical/FTS) and files (path contains) to focus in the graph."""
    results = queries.search_nodes(repository, q, repo_id=repo_id, limit=limit)
    return {"query": q, "results": results}
