"""Low-level Apache AGE Cypher client.

Wraps a psycopg connection and runs ``cypher(graph, $$ ... $$, params)`` calls. Parameters are
passed as a single agtype map (JSON) and referenced as ``$name`` inside the Cypher body. Scalar
results come back as agtype text and are normalized to Python values.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from cce.config import get_settings


def _parse_agtype(value: Any) -> Any:
    """Normalize an agtype cell to a Python value.

    Scalars arrive as JSON text (``"foo"``, ``1``, ``null``). Vertices/edges carry a ``::vertex``/
    ``::edge`` suffix; we strip it defensively, though Layer-1 queries return scalars only.
    """
    if not isinstance(value, str):
        return value
    text = value
    for suffix in ("::vertex", "::edge", "::path"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return value


class GraphClient:
    def __init__(self, conn: psycopg.Connection, graph_name: str | None = None) -> None:
        self.conn = conn
        self.graph_name = graph_name or get_settings().graph_name
        self._age_ready = False

    def _ensure_age(self) -> None:
        if self._age_ready:
            return
        with self.conn.cursor() as cur:
            cur.execute("LOAD 'age';")
            cur.execute('SET search_path = ag_catalog, "$user", public;')
        self._age_ready = True

    def cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Execute a Cypher query and return normalized rows.

        ``columns`` lists the result column names; each is declared as ``agtype`` in the AS clause.
        If omitted, a single ``result`` column is assumed.
        """
        self._ensure_age()
        col_defs = ", ".join(f"{c} agtype" for c in (columns or ["result"]))

        if params:
            sql = (
                f"SELECT * FROM ag_catalog.cypher('{self.graph_name}', $cy${query}$cy$, %s::agtype) "
                f"AS ({col_defs});"
            )
            args: tuple[Any, ...] = (json.dumps(params),)
        else:
            sql = (
                f"SELECT * FROM ag_catalog.cypher('{self.graph_name}', $cy${query}$cy$) "
                f"AS ({col_defs});"
            )
            args = ()

        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [tuple(_parse_agtype(cell) for cell in row) for row in rows]

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        """Run a write Cypher statement that returns nothing meaningful."""
        self.cypher(query, params, columns=["_"])
