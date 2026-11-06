"""Read path of ``GraphRepository.symbol_features``: edge features come from raw SQL over the AGE
edge label tables, and AGE only creates a label's table once an edge carries that label."""

from __future__ import annotations

from typing import Any

import psycopg.errors

from bce.storage.graph.repository import GraphRepository

GID = "844424930131969"


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.statements.append(sql)
        self.rows = self.conn.answer(sql, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class _Conn:
    """Answers the edge-feature SQL for one symbol with three edges in each direction."""

    def __init__(self, *, calls_table: bool) -> None:
        self.calls_table = calls_table
        self.statements: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def answer(self, sql: str, params: object) -> list[tuple[Any, ...]]:
        if "to_regclass(%s)" in sql:
            assert params == ('"code_graph"."CALLS"',)
            return [(params[0] if self.calls_table else None,)]
        if "to_regclass('file_churn')" in sql:
            return [(None,)]
        if '"CALLS"' in sql:
            if not self.calls_table:
                # What PostgreSQL answers when no edge with this label was ever written.
                raise psycopg.errors.UndefinedTable('relation "code_graph.CALLS" does not exist')
            return [(GID,)]
        if "_ag_label_edge" in sql:
            return [(GID, 3)]
        raise AssertionError(sql)


class _Client:
    graph_name = "code_graph"

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def cypher(self, query: str, params: Any = None, columns: Any = None) -> list[tuple[Any, ...]]:
        return [(GID, "sym-a", "run", "function", "r:a.py", 3)]


def _features(conn: _Conn) -> dict[str, Any]:
    repo = GraphRepository(_Client(conn))  # type: ignore[arg-type]
    return repo.symbol_features(["sym-a"])["sym-a"]


def test_index_without_call_edges_has_no_callers_or_callees() -> None:
    """An index whose repositories yield no call edge has no CALLS table; that is not an error."""
    conn = _Conn(calls_table=False)
    feats = _features(conn)
    assert feats["degree"] == 6  # three edges out, three in, read from the common edge parent
    assert feats["has_callers"] is False and feats["has_callees"] is False
    assert not [s for s in conn.statements if 'FROM "code_graph"."CALLS"' in s]


def test_call_flags_are_read_once_the_calls_table_exists() -> None:
    conn = _Conn(calls_table=True)
    feats = _features(conn)
    assert feats["degree"] == 6
    assert feats["has_callers"] is True and feats["has_callees"] is True
    assert len([s for s in conn.statements if 'FROM "code_graph"."CALLS"' in s]) == 2


def test_the_calls_probe_is_asked_again_on_every_call() -> None:
    """The table appears with the first indexed call edge; a running process has to notice it."""
    conn = _Conn(calls_table=False)
    repo = GraphRepository(_Client(conn))  # type: ignore[arg-type]
    assert repo.symbol_features(["sym-a"])["sym-a"]["has_callers"] is False
    conn.calls_table = True
    assert repo.symbol_features(["sym-a"])["sym-a"]["has_callers"] is True
