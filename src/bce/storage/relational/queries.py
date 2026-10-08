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


def _repo_key(ref: str) -> str:
    """Fold the ways a caller may spell one repository: case, ``-`` / ``_`` / spaces, a trailing
    ``.git``, a leading path (``org/repo``, ``C:\\src\\repo``)."""
    s = str(ref).strip().replace("\\", "/").rstrip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    if s.lower().endswith(".git"):
        s = s[:-4]
    return "".join(ch for ch in s.casefold() if ch not in "-_ .")


def resolve_repo_ids(conn: psycopg.Connection, ids: list[str] | None) -> list[str] | None:
    """Map caller-supplied repo ids onto the ids the index stores.

    Agents pass the repository by the name they see - ``PowerShell``, ``my-repo``, ``org/repo`` -
    while the indexer stores the id it was given (``powershell``, ``my_repo``); the SQL filters
    compare exactly, so such a mismatch silently emptied the lexical and vector searches (24 of
    50 PowerShell tasks returned nothing). Matching is by exact id first, then case-insensitive
    on id and display name, then with separators and a path prefix folded away. An id that
    matches no indexed repo is kept as given, so the filter still excludes everything rather
    than widening the scope.
    """
    if not ids:
        return ids
    wanted = [str(i).strip() for i in ids if str(i).strip()]
    if not wanted:
        return ids
    with conn.cursor() as cur:
        cur.execute("SELECT repo_id, name FROM repos")
        rows = cur.fetchall()
    exact = {r[0] for r in rows}
    folded: dict[str, str] = {}
    keyed: dict[str, str] = {}
    for repo_id, name in rows:
        folded.setdefault(str(repo_id).casefold(), str(repo_id))
        keyed.setdefault(_repo_key(repo_id), str(repo_id))
        if name:
            folded.setdefault(str(name).casefold(), str(repo_id))
            keyed.setdefault(_repo_key(name), str(repo_id))
    out: list[str] = []
    for i in wanted:
        if i in exact:
            hit = i
        else:
            hit = folded.get(i.casefold()) or keyed.get(_repo_key(i)) or i
        if hit not in out:
            out.append(hit)
    return out


def normalize_file_ids(ids: list[str] | None, repo_ids: list[str] | None) -> list[str] | None:
    """Normalise caller-supplied file ids (``history_file_ids``) to the index's ``repo:path`` form.

    Backslashes become slashes, ``./`` and a leading ``/`` go, and a bare path gets the ``repo:``
    prefix when exactly one repository is in scope. Ids already carrying a prefix keep it.
    """
    if not ids:
        return ids
    prefix = f"{repo_ids[0]}:" if repo_ids and len(repo_ids) == 1 else None
    out: list[str] = []
    for raw in ids:
        s = str(raw).strip().replace("\\", "/")
        if not s:
            continue
        repo, sep, path = s.partition(":")
        if sep and "/" not in repo and repo:  # already ``repo:path``
            path = path.lstrip("/")
            while path.startswith("./"):
                path = path[2:]
            s = f"{repo}:{path}"
        else:
            s = s.lstrip("/")
            while s.startswith("./"):
                s = s[2:]
            if prefix:
                s = prefix + s
        if s not in out:
            out.append(s)
    return out


def get_scoped_repo_ids(conn: psycopg.Connection, user_id: str) -> list[str]:
    """Repo ids a user may read (feeds the auth/scope filter, Phase 4)."""
    with conn.cursor() as cur:
        cur.execute("SELECT repo_id FROM scopes WHERE user_id = %s ORDER BY repo_id", (user_id,))
        return [r[0] for r in cur.fetchall()]
