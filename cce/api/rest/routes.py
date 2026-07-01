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

from cce.api.rest.deps import get_locale, get_repository, get_scope, get_vector_store
from cce.api.rest.schemas import (
    AssembleContextRequest,
    BlastRadiusRequest,
    ContextForTaskRequest,
    HealthResponse,
    IndexRemoteRequest,
    IndexRequest,
    LanguagesResponse,
    SearchRequest,
    SelectReposRequest,
    SimilarCodeRequest,
    ToolResponse,
)
from cce.core.auth.scope import ScopeFilter
from cce.core.i18n import get_translator
from cce.indexing.gitsync import GitCredentials, GitError
from cce.indexing.indexer import Indexer
from cce.indexing.parser.registry import build_default_registry
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore
from cce.tools.layer1 import (
    find_implementers,
    find_references,
    get_call_graph,
    get_dependencies,
    get_type_hierarchy,
    resolve_symbol,
)
from cce.tools.layer2 import find_similar_code, hybrid_search, semantic_search
from cce.tools.layer3 import (
    assemble_context,
    expand_blast_radius,
    get_context_for_task,
    select_repos,
    suggest_change_sites,
)

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


@router.get("/v1/find-implementers", response_model=ToolResponse, tags=["layer-1"])
def find_implementers_route(
    symbol_id: str = Query(..., description="Type symbol_id to find implementers for."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return find_implementers(repository, symbol_id, locale=locale)


@router.get("/v1/get-call-graph", response_model=ToolResponse, tags=["layer-1"])
def get_call_graph_route(
    symbol_id: str = Query(..., description="Root symbol_id."),
    hops: int = Query(default=1, ge=1, le=10, description="BFS depth."),
    direction: str = Query(default="both", description="callers | callees | both."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return get_call_graph(repository, symbol_id, hops=hops, direction=direction, locale=locale)


@router.get("/v1/get-dependencies", response_model=ToolResponse, tags=["layer-1"])
def get_dependencies_route(
    file_id: str = Query(..., description="file_id whose IMPORTS to return."),
    transitive: bool = Query(default=False, description="Follow imports transitively."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return get_dependencies(repository, file_id, transitive=transitive, locale=locale)


@router.get("/v1/get-type-hierarchy", response_model=ToolResponse, tags=["layer-1"])
def get_type_hierarchy_route(
    symbol_id: str = Query(..., description="Type symbol_id."),
    repository: GraphRepository = Depends(get_repository),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return get_type_hierarchy(repository, symbol_id, locale=locale)


# --- Layer 2: hybrid retrieval (spec section 7) ---


@router.post("/v1/semantic-search", response_model=ToolResponse, tags=["layer-2"])
def semantic_search_route(
    body: SearchRequest,
    store: VectorStore = Depends(get_vector_store),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return semantic_search(
        store, body.query, repo_ids=body.repo_ids, limit=body.limit, locale=locale
    )


@router.post("/v1/hybrid-search", response_model=ToolResponse, tags=["layer-2"])
def hybrid_search_route(
    body: SearchRequest,
    repository: GraphRepository = Depends(get_repository),
    store: VectorStore = Depends(get_vector_store),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return hybrid_search(
        repository, store, body.query, repo_ids=body.repo_ids, limit=body.limit, locale=locale
    )


@router.post("/v1/find-similar-code", response_model=ToolResponse, tags=["layer-2"])
def find_similar_code_route(
    body: SimilarCodeRequest,
    store: VectorStore = Depends(get_vector_store),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return find_similar_code(
        store, body.code, repo_ids=body.repo_ids, limit=body.limit, locale=locale
    )


# --- Layer 3: task-aware orchestration (spec section 7) ---


@router.post("/v1/get-context-for-task", response_model=ToolResponse, tags=["layer-3"])
def get_context_for_task_route(
    body: ContextForTaskRequest,
    repository: GraphRepository = Depends(get_repository),
    scope: ScopeFilter = Depends(get_scope),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return get_context_for_task(
        repository,
        task_text=body.task_text,
        task_id=body.task_id,
        max_tokens=body.max_tokens,
        max_candidates=body.max_candidates,
        commit=body.commit,
        repo_ids=body.repo_ids,
        explicit_symbols=body.explicit_symbols,
        route_paths=body.route_paths,
        history_file_ids=body.history_file_ids,
        component_repo_ids=body.component_repo_ids,
        semantic_candidates=body.semantic_candidates,
        scope=scope,
        locale=locale,
    )


@router.post("/v1/suggest-change-sites", response_model=ToolResponse, tags=["layer-3"])
def suggest_change_sites_route(
    body: ContextForTaskRequest,
    repository: GraphRepository = Depends(get_repository),
    scope: ScopeFilter = Depends(get_scope),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return suggest_change_sites(
        repository,
        task_text=body.task_text,
        max_candidates=body.max_candidates,
        commit=body.commit,
        repo_ids=body.repo_ids,
        explicit_symbols=body.explicit_symbols,
        route_paths=body.route_paths,
        history_file_ids=body.history_file_ids,
        component_repo_ids=body.component_repo_ids,
        semantic_candidates=body.semantic_candidates,
        scope=scope,
        locale=locale,
    )


@router.post("/v1/expand-blast-radius", response_model=ToolResponse, tags=["layer-3"])
def expand_blast_radius_route(
    body: BlastRadiusRequest,
    repository: GraphRepository = Depends(get_repository),
    scope: ScopeFilter = Depends(get_scope),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return expand_blast_radius(
        repository, target_symbols=body.target_symbols, scope=scope, locale=locale
    )


@router.post("/v1/select-repos", response_model=ToolResponse, tags=["layer-3"])
def select_repos_route(
    body: SelectReposRequest,
    repository: GraphRepository = Depends(get_repository),
    scope: ScopeFilter = Depends(get_scope),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return select_repos(
        repository,
        task_text=body.task_text,
        component_repo_ids=body.component_repo_ids,
        history_file_ids=body.history_file_ids,
        scope=scope,
        locale=locale,
    )


@router.post("/v1/assemble-context", response_model=ToolResponse, tags=["layer-3"])
def assemble_context_route(
    body: AssembleContextRequest,
    repository: GraphRepository = Depends(get_repository),
    scope: ScopeFilter = Depends(get_scope),
    locale: str = Depends(get_locale),
) -> dict[str, Any]:
    return assemble_context(
        repository,
        symbol_ids=body.symbol_ids,
        max_tokens=body.max_tokens,
        scope=scope,
        locale=locale,
    )


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
