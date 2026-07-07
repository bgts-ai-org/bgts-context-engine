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
