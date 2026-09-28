"""PostgreSQL connection management (psycopg 3).

A single database holds the graph (AGE), embeddings (pgvector), and relational metadata, so one
connection can traverse the graph, search vectors, JOIN metadata, and apply RLS in one place (P5).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from bce.config import Settings, get_settings


@contextmanager
def connection(settings: Settings | None = None) -> Iterator[psycopg.Connection]:
    """Yield a database connection (manual transaction control).

    Statements are planned with their actual parameter values every time (``plan_cache_mode =
    force_custom_plan``). psycopg prepares a statement server-side after a few executions, and
    PostgreSQL then settles on a *generic* plan chosen from the row estimates at that moment.
    Indexing runs tens of thousands of identical ``MERGE`` statements inside one transaction,
    starting from empty tables: the generic plan picked while the graph had a hundred vertices
    is never re-planned and degrades to tens of milliseconds per statement on a 20 000-symbol
    graph (measured: 2 ms flat with custom plans against 5 -> 17 ms and still growing at 6 000
    symbols). Custom planning costs a fraction of a millisecond per statement.
    """
    settings = settings or get_settings()
    conn = psycopg.connect(settings.dsn, options="-c plan_cache_mode=force_custom_plan")
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
