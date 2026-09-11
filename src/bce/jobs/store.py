"""Job store: CRUD over the ``jobs`` table (DB-backed queue, no extra broker).

All functions take an open ``psycopg`` connection and do **not** commit; callers own the
transaction so enqueue/claim/finish compose with the surrounding work (P5: one database).
The only exception is documented per function.

State machine::

    pending -> running -> succeeded | failed
    pending -> cancelled            (cancel is only valid while pending)
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg

#: Job types the worker knows how to execute (mirrors the CHECK constraint in 0007_jobs.sql).
JOB_TYPES = ("index", "index_remote", "reindex")

_COLUMNS = "job_id, job_type, payload, status, result, error, created_at, started_at, finished_at"


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "job_id": str(row[0]),
        "job_type": row[1],
        "payload": row[2],
        "status": row[3],
        "result": row[4],
        "error": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        "started_at": row[7].isoformat() if row[7] else None,
        "finished_at": row[8].isoformat() if row[8] else None,
    }


def enqueue_job(conn: psycopg.Connection, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Insert a ``pending`` job and return it. Caller commits."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unknown job_type '{job_type}'; expected one of {JOB_TYPES}.")
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO jobs (job_type, payload) VALUES (%s, %s) RETURNING {_COLUMNS}",
            (job_type, json.dumps(payload)),
        )
        row = cur.fetchone()
    assert row is not None
    return _row_to_dict(row)


def _valid_uuid(job_id: str) -> bool:
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def get_job(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    if not _valid_uuid(job_id):
        return None
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(
    conn: psycopg.Connection,
    *,
    status: str | None = None,
    repo: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest-first job list, optionally filtered by status and/or repo name/path in the payload."""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if repo is not None:
        # Matches the logical repo name used by all three job payloads ('name' field).
        clauses.append("payload->>'name' = %s")
        params.append(repo)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM jobs {where} ORDER BY created_at DESC, job_id LIMIT %s",
            params,
        )
        rows = cur.fetchall()
    return [_row_to_dict(row) for row in rows]


def claim_next_job(
    conn: psycopg.Connection, *, job_types: Sequence[str] | None = None
) -> dict[str, Any] | None:
    """Atomically claim the oldest ``pending`` job (pending -> running). Caller commits.

    ``FOR UPDATE SKIP LOCKED`` guarantees two workers can never claim the same row even when
    polling concurrently.

    ``job_types`` narrows what this worker is willing to run. The MCP server indexes local working
    trees only, so it must leave a clone job that REST enqueued to the API server rather than doing
    remote work in an editor-spawned process.
    """
    predicate = "status = 'pending'"
    params: tuple[Any, ...] = ()
    if job_types is not None:
        predicate += " AND job_type = ANY(%s)"
        params = (list(job_types),)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE jobs
               SET status = 'running', started_at = now()
             WHERE job_id = (
                   SELECT job_id FROM jobs
                    WHERE {predicate}
                    ORDER BY created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING {_COLUMNS}""",
            params,
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def finish_job(conn: psycopg.Connection, job_id: str, result: dict[str, Any]) -> None:
    """Mark a running job as succeeded with its result payload. Caller commits."""
    conn.execute(
        "UPDATE jobs SET status = 'succeeded', result = %s, finished_at = now() WHERE job_id = %s",
        (json.dumps(result), job_id),
    )


def fail_job(conn: psycopg.Connection, job_id: str, error: str) -> None:
    """Mark a running job as failed with an error message. Caller commits."""
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = %s, finished_at = now() WHERE job_id = %s",
        (error, job_id),
    )


def cancel_job(conn: psycopg.Connection, job_id: str) -> dict[str, Any] | None:
    """Cancel a job if (and only if) it is still ``pending``. Caller commits.

    Returns the updated job, or ``None`` when the job does not exist or is no longer pending
    (running jobs cannot be interrupted).
    """
    if not _valid_uuid(job_id):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = now() "
            f"WHERE job_id = %s AND status = 'pending' RETURNING {_COLUMNS}",
            (job_id,),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None
