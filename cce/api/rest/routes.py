"""REST routes (Layer-1 surface).

Every handler is a thin pass-through to ``cce.tools`` / the indexer; it adds transport + locale only.
GET endpoints are read-only and never commit; ``POST /index`` commits explicitly after a successful
run. Responses are the tool envelope ``{tool, payload, message, locale}`` so payloads stay
byte-identical across locales (P1).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from cce.api.rest.deps import get_locale, get_repository
from cce.api.rest.schemas import (
    HealthResponse,
    IndexRemoteRequest,
    IndexRequest,
    LanguagesResponse,
    ToolResponse,
)
from cce.core.i18n import get_translator
from cce.indexing.gitsync import GitCredentials, GitError
from cce.indexing.indexer import Indexer
from cce.indexing.parser.registry import build_default_registry
from cce.storage.graph.repository import GraphRepository
from cce.tools.layer1 import find_references, resolve_symbol

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse, tags=["meta"])
def healthz() -> JSONResponse:
    """Liveness + database reachability (503 if the database cannot be queried)."""
    from cce.storage.relational.db import connection

    try:
        with connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        return JSONResponse(
            status_code=503, content={"status": "degraded", "database": "unavailable"}
        )
    return JSONResponse(content={"status": "ok", "database": "ok"})


@router.get("/v1/languages", response_model=LanguagesResponse, tags=["meta"])
def languages() -> dict[str, list[str]]:
    """List supported languages and file extensions (no database needed)."""
    registry = build_default_registry()
    return {
        "languages": list(registry.languages()),
        "extensions": list(registry.supported_extensions()),
    }


@router.get("/v1/resolve-symbol", response_model=ToolResponse, tags=["layer-1"])
def resolve_symbol_route(
    name: str = Query(..., description="Symbol name to resolve."),
    repo: str | None = Query(default=None, description="Restrict to a repo_id."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return resolve_symbol(repository, name, repo_id=repo, locale=locale)


@router.get("/v1/find-references", response_model=ToolResponse, tags=["layer-1"])
def find_references_route(
    symbol_id: str = Query(..., description="symbol_id to find references for."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return find_references(repository, symbol_id, locale=locale)


@router.post("/v1/index", response_model=ToolResponse, tags=["indexing"])
def index_route(
    body: IndexRequest,
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    """Full-index a local repository, then commit. Returns the index summary + localized message."""
    summary = Indexer(repository).index_local_repo(
        path=body.repo_path, name=body.name, commit=body.commit
    )
    repository.client.conn.commit()

    tr = get_translator()
    message = tr.translate(
        "tool.indexing.done",
        locale,
        files=summary.files,
        nodes=summary.nodes,
        edges=summary.edges,
        commit=summary.commit,
    )
    return {
        "tool": "index",
        "payload": {
            "repo_id": summary.repo_id,
            "files": summary.files,
            "nodes": summary.nodes,
            "edges": summary.edges,
            "commit": summary.commit,
        },
        "message": message,
        "locale": locale,
    }


@router.post("/v1/index-remote", response_model=ToolResponse, tags=["indexing"])
def index_remote_route(
    body: IndexRemoteRequest,
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> Any:
    """Clone a Bitbucket Cloud repository by URL, full-index it, then commit.

    Credentials come from the request body if present, otherwise from ``CCE_BITBUCKET_*`` env vars.
    Returns 400 for an unparseable URL and 502 if the clone/fetch fails (tokens are masked).
    """
    from cce.config import get_settings

    tr = get_translator()
    settings = get_settings()
    creds = GitCredentials(
        username=body.username if body.username is not None else settings.bitbucket_username,
        token=body.token if body.token is not None else settings.bitbucket_token,
    )

    try:
        summary = Indexer(repository).index_remote_repo(
            url=body.url, name=body.name, branch=body.branch, credentials=creds
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"message": tr.translate("error.invalid_repo_url", locale, detail=str(exc))},
        )
    except GitError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "message": tr.translate(
                    "error.remote_index_failed", locale, url=body.url, detail=str(exc)
                )
            },
        )
    repository.client.conn.commit()

    message = tr.translate(
        "tool.indexing.done",
        locale,
        files=summary.files,
        nodes=summary.nodes,
        edges=summary.edges,
        commit=summary.commit,
    )
    return {
        "tool": "index_remote",
        "payload": {
            "repo_id": summary.repo_id,
            "name": body.name or summary.repo_id,
            "remote_url": summary.remote_url,
            "branch": summary.branch,
            "files": summary.files,
            "nodes": summary.nodes,
            "edges": summary.edges,
            "commit": summary.commit,
        },
        "message": message,
        "locale": locale,
    }
