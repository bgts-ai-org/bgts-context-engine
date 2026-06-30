"""FastAPI dependencies: per-request database repository and locale resolution.

These are deliberately tiny so they can be overridden in tests (``app.dependency_overrides``)
without a live database.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, Query

from cce.core.i18n import get_translator
from cce.storage.graph.client import GraphClient
from cce.storage.graph.repository import GraphRepository
from cce.storage.relational.db import connection


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


def get_locale(
    locale: str | None = Query(default=None, description="Force a locale (en/tr)."),
    accept_language: str | None = Header(default=None),
) -> str:
    """Resolve the response locale from ``?locale=`` (wins) then the ``Accept-Language`` header.

    Returns a supported locale; the deterministic payload is unaffected (i18n is presentation only).
    """
    return get_translator().resolve(requested=locale, accept_language=accept_language)
