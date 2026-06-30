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
    commit: str | None = Field(default=None, description="Override commit sha (else read from git).")


class IndexRemoteRequest(BaseModel):
    url: str = Field(..., description="Bitbucket repository URL (https or ssh form).")
    name: str | None = Field(default=None, description="Logical name (default: workspace/repo).")
    branch: str | None = Field(default=None, description="Branch to index (default: remote HEAD).")
    token: str | None = Field(
        default=None, description="Access token / app password (else CCE_BITBUCKET_TOKEN env)."
    )
    username: str | None = Field(
        default=None, description="Username for app password (else CCE_BITBUCKET_USERNAME env)."
    )


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
