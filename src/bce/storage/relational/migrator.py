"""Minimal forward-only SQL migration runner.

Applies ``migrations/*.sql`` in filename order, tracking applied files in ``schema_migrations``.
A dollar-quote-aware splitter lets a single file contain multiple statements including PL/pgSQL
``DO $$ ... $$`` blocks (needed for the AGE graph creation guard).

The one schema detail that depends on configuration is the width of ``embeddings.embedding``:
each embedding model has its own (Voyage 1024, jina-code 1536, ...). Static SQL cannot read
``BCE_EMBEDDING_DIM``, so :func:`align_embedding_dim` re-types the column after the SQL
migrations have run.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg

from bce.storage.relational.db import connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_VECTOR_TYPE_RE = re.compile(r"^vector\((\d+)\)$")


class EmbeddingDimMismatch(RuntimeError):
    """``embeddings.embedding`` holds vectors of another width than ``BCE_EMBEDDING_DIM``.

    Changing the width throws every stored vector away (they belong to another model), which is
    a reindex boundary the caller has to opt into rather than something ``migrate`` does quietly.
    """


def embedding_column_dim(conn: psycopg.Connection) -> int | None:
    """Width of ``embeddings.embedding``; ``None`` if the table or a fixed width is missing."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = to_regclass('embeddings') AND attname = 'embedding' "
            "AND NOT attisdropped"
        )
        row = cur.fetchone()
    if row is None:
        return None
    match = _VECTOR_TYPE_RE.match(str(row[0]))
    return int(match.group(1)) if match else None


def align_embedding_dim(conn: psycopg.Connection, dim: int, *, reset: bool = False) -> bool:
    """Re-type ``embeddings.embedding`` to ``vector(dim)``. Returns True when it changed.

    Stored vectors of another width are only dropped with ``reset=True``; otherwise the mismatch
    is reported as :class:`EmbeddingDimMismatch` so nobody loses an index by editing ``.env``.
    """
    current = embedding_column_dim(conn)
    if current is None or current == dim:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM embeddings")
        stored = int(cur.fetchone()[0])
        if stored and not reset:
            conn.rollback()
            raise EmbeddingDimMismatch(
                f"embeddings.embedding is vector({current}) and holds {stored} vectors, but "
                f"BCE_EMBEDDING_DIM={dim}. Changing the width discards them (they came from "
                f"another model): re-run with --reset-embeddings and then reindex, or set "
                f"BCE_EMBEDDING_DIM={current} to keep the existing index."
            )
        # Same recipe as migration 0005: the HNSW index is bound to the width, so rebuild it.
        cur.execute("DROP INDEX IF EXISTS idx_embeddings_hnsw")
        cur.execute("TRUNCATE TABLE embeddings")
        cur.execute(f"ALTER TABLE embeddings ALTER COLUMN embedding TYPE vector({int(dim)})")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw "
            "ON embeddings USING hnsw (embedding vector_cosine_ops)"
        )
    conn.commit()
    return True


_CREATE_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into statements, respecting dollar-quoted blocks, strings, and comments."""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    dollar_tag: str | None = None
    in_single = False
    in_line_comment = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            end = sql.find("$", i + 1)
            if end != -1:
                inner = sql[i + 1 : end]
                if inner == "" or inner.isidentifier():
                    tag = sql[i : end + 1]
                    dollar_tag = tag
                    buf.append(tag)
                    i = end + 1
                    continue

        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def applied_ids(conn: psycopg.Connection) -> set[str]:
    conn.execute(_CREATE_TRACKING)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def run_migrations(
    conn: psycopg.Connection | None = None, migrations_dir: Path | None = None
) -> list[str]:
    """Apply pending migrations. Returns the ids applied in this run."""
    migrations_dir = migrations_dir or MIGRATIONS_DIR

    def _run(c: psycopg.Connection) -> list[str]:
        done = applied_ids(c)
        newly: list[str] = []
        for path in sorted(migrations_dir.glob("*.sql")):
            mig_id = path.stem
            if mig_id in done:
                continue
            # utf-8-sig, not utf-8: an editor-added BOM would otherwise reach the server as
            # part of the first statement and Postgres rejects it as a syntax error.
            sql = path.read_text(encoding="utf-8-sig")
            with c.cursor() as cur:
                for statement in split_sql_statements(sql):
                    cur.execute(statement)
                cur.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (mig_id,))
            c.commit()
            newly.append(mig_id)
        return newly

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)
