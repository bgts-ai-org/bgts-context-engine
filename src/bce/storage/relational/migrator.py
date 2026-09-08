"""Minimal forward-only SQL migration runner.

Applies ``migrations/*.sql`` in filename order, tracking applied files in ``schema_migrations``.
A dollar-quote-aware splitter lets a single file contain multiple statements including PL/pgSQL
``DO $$ ... $$`` blocks (needed for the AGE graph creation guard).
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from bce.storage.relational.db import connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

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
            sql = path.read_text(encoding="utf-8")
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
