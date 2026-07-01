"""FastAPI dependencies: per-request database repository, vector store, locale, and scope.

These are deliberately tiny so they can be overridden in tests (``app.dependency_overrides``)
without a live database.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, Query

from cce.core.auth.scope import Principal, ScopeFilter
from cce.core.i18n import get_translator
from cce.storage.graph.client import GraphClient
from cce.storage.graph.repository import GraphRepository
from cce.storage.relational.db import connection
from cce.storage.vector.store import VectorStore


def get_repository() -> Iterator[GraphRepository]:
    """Yield a graph repository bound to a fresh connection (closed when the request ends).

    Reads do not commit. Write endpoints (e.g. ``/index``) commit explicitly; on any error we roll
    back so a half-written transaction never lingers on the pooled connection.
    """
    with connection() as conn:
        try:
            yield GraphRepository(GraphClient(conn))
        except Exception:
            conn.rollback()
            raise


def get_vector_store(
    repository: GraphRepository = Depends(get_repository),
) -> VectorStore:
    """Vector store bound to the same request connection as the graph repository (single DB, P5)."""
    return VectorStore(repository.client.conn)


def get_scope(
    repository: GraphRepository = Depends(get_repository),
    user_id: str | None = Header(default=None, alias="X-CCE-User"),
) -> ScopeFilter:
    """Build the scope filter from the ``X-CCE-User`` header via the ``scopes`` table (section 9).

    No header -> system principal (allow-all), matching pre-Phase-4 behaviour. On any lookup error we
    fail closed to an empty scope so a misconfiguration cannot leak inaccessible repos.
    """
    if user_id is None:
        return ScopeFilter(Principal.system())
    try:
        return ScopeFilter.from_scopes(repository.client.conn, user_id)
    except Exception:
        return ScopeFilter(Principal(user_id=user_id, allowed_repo_ids=[]))


def get_locale(
    locale: str | None = Query(default=None, description="Force a locale (en/tr)."),
    accept_language: str | None = Header(default=None),
) -> str:
    """Resolve the response locale from ``?locale=`` (wins) then the ``Accept-Language`` header.

    Returns a supported locale; the deterministic payload is unaffected (i18n is presentation only).
    """
    return get_translator().resolve(requested=locale, accept_language=accept_language)
