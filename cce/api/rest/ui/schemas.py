"""Response models for the UI layer endpoints (OpenAPI documentation only).

Kept inside ``cce.api.rest.ui`` so the UI layer can be removed without touching the main
``schemas`` module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UIRepo(BaseModel):
    repo_id: str
    name: str
    default_branch: str | None = None
    remote_url: str | None = None
    last_indexed_commit: str | None = None
    created_at: str | None = None


class UIRepoListResponse(BaseModel):
    repos: list[UIRepo]


class UINode(BaseModel):
    """Graph-view node: ``id`` is the universal ``gid``; heavy fields (body/docstring) excluded."""

    id: str
    label: str = Field(description="Node label: Repo | File | Symbol | Module | Route | DesignNote.")
    display: str = Field(description="Human-friendly caption for rendering.")
    properties: dict[str, Any] = Field(default_factory=dict)


class UIEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str = Field(description="Edge type: DEFINED_IN | BELONGS_TO | IMPORTS | CALLS | ...")
    properties: dict[str, Any] = Field(default_factory=dict)


class UIGraphResponse(BaseModel):
    repo_id: str
    nodes: list[UINode]
    edges: list[UIEdge]
    truncated: bool = Field(description="True when a node/edge limit clipped the result.")


class UINodeDetailResponse(BaseModel):
    """Full node properties (including body/docstring) for the detail panel."""

    id: str
    label: str
    display: str
    properties: dict[str, Any]


class UINeighborsResponse(BaseModel):
    gid: str
    nodes: list[UINode]
    edges: list[UIEdge]


class UIStatsResponse(BaseModel):
    repo_id: str
    node_counts: dict[str, int]
    edge_counts: dict[str, int]
    total_nodes: int
    total_edges: int
    languages: dict[str, int]


class UISearchResult(BaseModel):
    id: str
    label: str
    display: str
    kind: str | None = None
    file_id: str | None = None
    line: int | None = None
    path: str | None = None
    language: str | None = None


class UISearchResponse(BaseModel):
    query: str
    results: list[UISearchResult]


class UITraceRequest(BaseModel):
    """Request for the pipeline trace (mirrors get_context_for_task's core inputs)."""

    task_text: str = Field(..., min_length=1, description="Task title + description text.")
    max_candidates: int = Field(default=8, ge=1, le=100, description="Top-N narrowing size.")
    max_tokens: int = Field(default=4000, ge=1, le=200000, description="Assembly token budget.")
    repo_ids: list[str] | None = Field(default=None, description="Restrict retrieval to these repos.")
    auto_semantic: bool = Field(
        default=True, description="Run the automatic semantic anchor stage (D1)."
    )


class UITraceStage(BaseModel):
    """One pipeline stage snapshot; the payload key depends on the stage name."""

    model_config = {"extra": "allow"}

    stage: str = Field(description="semantic | anchors | expand | score | narrow | assemble.")
    duration_ms: float


class UITraceSymbol(BaseModel):
    name: str | None = None
    kind: str | None = None
    file_id: str | None = None
    line: int | None = None


class UITraceResponse(BaseModel):
    task_text: str
    repo_ids: list[str] | None = None
    max_candidates: int
    stages: list[UITraceStage]
    symbols: dict[str, UITraceSymbol] = Field(
        description="Metadata for every symbol appearing in the trace (for labels/ghost nodes)."
    )
