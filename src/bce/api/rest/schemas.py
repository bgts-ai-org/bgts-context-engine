"""Request/response models for the REST adapter.

Only request bodies need validation models; tool results are returned as-is (already a
deterministic ``{tool, payload, message, locale}`` dict). Response models below document the shape
in OpenAPI without re-deriving any logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IndexRequest(BaseModel):
    repo_path: str = Field(..., description="Path to a local repository to index.")
    name: str = Field(..., description="Logical repository name (stable across clones).")
    commit: str | None = Field(
        default=None, description="Override commit sha (else read from git)."
    )


class IndexRemoteRequest(BaseModel):
    url: str = Field(..., description="Bitbucket repository URL (https or ssh form).")
    name: str | None = Field(default=None, description="Logical name (default: workspace/repo).")
    branch: str | None = Field(default=None, description="Branch to index (default: remote HEAD).")
    token: str | None = Field(
        default=None, description="Access token / app password (else BCE_BITBUCKET_TOKEN env)."
    )
    username: str | None = Field(
        default=None, description="Username for app password (else BCE_BITBUCKET_USERNAME env)."
    )


class ReindexRequest(BaseModel):
    """Incremental re-index request (git-diff driven, Phase 1)."""

    repo_path: str = Field(..., description="Path to the local git working tree.")
    name: str = Field(..., description="Logical repository name (must match the prior index).")
    since_commit: str | None = Field(
        default=None, description="Baseline commit (default: repo's last_indexed_commit)."
    )
    to_commit: str = Field(default="HEAD", description="Target commit/ref (default: HEAD).")


class SearchRequest(BaseModel):
    """Layer-2 search request (semantic / hybrid)."""

    query: str = Field(..., description="Natural-language query.")
    repo_ids: list[str] | None = Field(default=None, description="Restrict to these repo_ids.")
    limit: int = Field(default=10, ge=1, le=100, description="Max candidates to return.")


class SimilarCodeRequest(BaseModel):
    code: str = Field(..., description="Code fragment to find lookalikes for.")
    repo_ids: list[str] | None = Field(default=None, description="Restrict to these repo_ids.")
    limit: int = Field(default=10, ge=1, le=100)


class ContextForTaskRequest(BaseModel):
    """Layer-3 get_context_for_task / suggest_change_sites request."""

    task_text: str = Field(..., description="Task title + description text.")
    task_id: str | None = Field(
        default=None, description="Optional Jira task id (audit + history)."
    )
    max_tokens: int = Field(default=4000, ge=1, le=200000, description="Token budget for assembly.")
    max_candidates: int = Field(
        default=8, ge=1, le=100, description="Narrow to at most N candidates."
    )
    commit: str | None = Field(default=None, description="Pinned commit sha (stage 0).")
    repo_ids: list[str] | None = Field(
        default=None, description="Restrict retrieval to these repos."
    )
    explicit_symbols: list[str] | None = Field(
        default=None, description="Known symbol names (anchor #1)."
    )
    route_paths: list[str] | None = Field(
        default=None, description="Known route paths (anchor #1)."
    )
    history_file_ids: list[str] | None = Field(
        default=None, description="task_history files (anchor #3)."
    )
    component_repo_ids: list[str] | None = Field(
        default=None, description="Jira component repos (anchor #2)."
    )
    semantic_candidates: list[str] | None = Field(
        default=None,
        description="Pre-ranked semantic anchor symbol_ids (anchor #4, lowest priority).",
    )
    auto_semantic: bool = Field(
        default=True,
        description="Auto-run semantic_search(task_text) for anchor #4 when semantic_candidates is omitted.",
    )


class BlastRadiusRequest(BaseModel):
    target_symbols: list[str] = Field(..., description="Symbols whose impact surface to compute.")


class SelectReposRequest(BaseModel):
    task_text: str = Field(..., description="Task text.")
    component_repo_ids: list[str] | None = Field(default=None)
    history_file_ids: list[str] | None = Field(default=None)


class AssembleContextRequest(BaseModel):
    symbol_ids: list[str] = Field(
        ..., description="Symbols to deduplicate and fit into the budget."
    )
    max_tokens: int = Field(default=4000, ge=1, le=200000)


class ToolResponse(BaseModel):
    """Envelope shared by every tool: language-neutral ``payload`` + localized ``message``."""

    tool: str
    payload: dict[str, Any]
    message: str | None = None
    locale: str


class LanguagesResponse(BaseModel):
    languages: list[str]
    extensions: list[str]


class HealthResponse(BaseModel):
    status: str
    database: str
