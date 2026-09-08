"""Scope filtering + audit logging (spec section 9).

``Principal`` identifies the caller and the repos it may read. ``ScopeFilter.allows(repo_id)`` is the
deterministic predicate the orchestrator applies at stage 4 so a symbol from an inaccessible repo can
never enter the context (RLS is also enforced in the DB; this is the application-side mirror). An
unrestricted principal (``allowed_repo_ids=None``) allows everything - used before Phase-4 auth is
wired and for internal/system calls.

``write_audit_log`` records every context request (who, task, commit, tool, returned symbols) for
traceability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg


@dataclass(slots=True)
class Principal:
    user_id: str | None = None
    #: None = unrestricted (system/pre-auth). Empty list = access to nothing.
    allowed_repo_ids: list[str] | None = None

    @classmethod
    def system(cls) -> Principal:
        return cls(user_id=None, allowed_repo_ids=None)


class ScopeFilter:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self._allowed = (
            None
            if principal.allowed_repo_ids is None
            else set(principal.allowed_repo_ids)
        )

    def allows(self, repo_id: str | None) -> bool:
        """True if the principal may read ``repo_id``. Unknown repo_id on a restricted principal is
        denied (fail-closed)."""
        if self._allowed is None:
            return True
        if repo_id is None:
            return False
        return repo_id in self._allowed

    @classmethod
    def from_scopes(cls, conn: psycopg.Connection, user_id: str | None) -> ScopeFilter:
        """Build a filter from the ``scopes`` table. A ``None`` user is treated as system (allow-all)."""
        if user_id is None:
            return cls(Principal.system())
        from bce.storage.relational.queries import get_scoped_repo_ids

        repo_ids = get_scoped_repo_ids(conn, user_id)
        return cls(Principal(user_id=user_id, allowed_repo_ids=repo_ids))


def write_audit_log(
    conn: psycopg.Connection,
    *,
    user_id: str | None,
    task_id: str | None,
    commit: str | None,
    tool: str,
    locale: str | None,
    returned_symbols: list[str],
) -> None:
    """Append one audit row (spec section 9). Commit is the caller's responsibility."""
    conn.execute(
        "INSERT INTO audit_log (user_id, task_id, commit, tool, locale, returned_symbols) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, task_id, commit, tool, locale, json.dumps(returned_symbols)),
    )
