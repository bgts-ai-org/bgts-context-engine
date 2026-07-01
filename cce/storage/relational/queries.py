"""Relational reads for retrieval (tasks, task_history, scopes).

Small, typed helpers over the SQL metadata tables (spec section 4.2) used by anchor finding
(sources #2 Jira metadata and #3 task_history) and by the auth/scope filter (Phase 4).
"""

from __future__ import annotations

from typing import Any

import psycopg


def get_task(conn: psycopg.Connection, task_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, description, epic, labels, component, linked_commit, linked_pr "
            "FROM tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "epic": row[3],
        "labels": row[4],
        "component": row[5],
        "linked_commit": row[6],
        "linked_pr": row[7],
    }


def get_task_history_files(conn: psycopg.Connection, task_id: str) -> list[str]:
    """Files touched by past resolved tasks with the same id (anchor source #3)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT touched_file_id FROM task_history WHERE task_id = %s "
            "ORDER BY touched_file_id",
            (task_id,),
        )
        return [r[0] for r in cur.fetchall()]


def get_component_repos(conn: psycopg.Connection, component: str) -> list[str]:
    """Repos whose name matches a Jira component (anchor source #2, deterministic LIKE)."""
    if not component:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo_id FROM repos WHERE name ILIKE %s ORDER BY repo_id",
            (f"%{component}%",),
        )
        return [r[0] for r in cur.fetchall()]


def get_scoped_repo_ids(conn: psycopg.Connection, user_id: str) -> list[str]:
    """Repo ids a user may read (feeds the auth/scope filter, Phase 4)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo_id FROM scopes WHERE user_id = %s ORDER BY repo_id", (user_id,)
        )
        return [r[0] for r in cur.fetchall()]
