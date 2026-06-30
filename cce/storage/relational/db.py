"""PostgreSQL connection management (psycopg 3).

A single database holds the graph (AGE), embeddings (pgvector), and relational metadata, so one
connection can traverse the graph, search vectors, JOIN metadata, and apply RLS in one place (P5).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from cce.config import Settings, get_settings


@contextmanager
def connection(settings: Settings | None = None) -> Iterator[psycopg.Connection]:
    """Yield a database connection (manual transaction control)."""
    settings = settings or get_settings()
    conn = psycopg.connect(settings.dsn)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(settings: Settings | None = None) -> Iterator[psycopg.Connection]:
    """Yield a connection wrapped in a transaction (commit on success, rollback on error)."""
    with connection(settings) as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
