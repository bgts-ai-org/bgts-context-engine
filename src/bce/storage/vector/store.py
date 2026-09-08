"""pgvector-backed embedding store (anchor finding only, P2).

Writes symbol/file embeddings in the same connection/transaction as the graph upsert (P5: single
DB) and runs cosine-distance nearest-neighbour search over the HNSW index. Every row records the
pinned ``model`` identifier so a model/version change is detectable and reindex-gated (determinism).

Search results are ordered by distance, then by ``ref_id`` to break ties deterministically, so the
same query vector against the same snapshot yields a stable order.
"""

from __future__ import annotations

from typing import Any

import psycopg

_VECTOR_LITERAL = "[{}]"


def _to_vector_literal(vector: list[float]) -> str:
    return _VECTOR_LITERAL.format(",".join(f"{v:.8f}" for v in vector))


class VectorStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def upsert(
        self,
        *,
        kind: str,
        ref_id: str,
        repo_id: str,
        content: str,
        model: str,
        embedding: list[float],
        indexed_at_commit: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO embeddings (kind, ref_id, repo_id, content, model, embedding, "
            "indexed_at_commit) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, ref_id) DO UPDATE SET "
            "repo_id = EXCLUDED.repo_id, content = EXCLUDED.content, model = EXCLUDED.model, "
            "embedding = EXCLUDED.embedding, indexed_at_commit = EXCLUDED.indexed_at_commit",
            (
                kind,
                ref_id,
                repo_id,
                content,
                model,
                _to_vector_literal(embedding),
                indexed_at_commit,
            ),
        )

    def delete_for_file(self, file_id: str, symbol_ids: list[str]) -> None:
        """Remove a file's own embedding and its symbols' embeddings (incremental reindex)."""
        self.conn.execute("DELETE FROM embeddings WHERE kind = 'file' AND ref_id = %s", (file_id,))
        if symbol_ids:
            self.conn.execute(
                "DELETE FROM embeddings WHERE kind = 'symbol' AND ref_id = ANY(%s)", (symbol_ids,)
            )

    def search(
        self,
        query_vector: list[float],
        *,
        limit: int = 20,
        repo_ids: list[str] | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cosine nearest neighbours, deterministic order (distance, then ref_id)."""
        vector_literal = _to_vector_literal(query_vector)
        # Bind order matches the placeholders left-to-right: SELECT distance, then WHERE, then LIMIT.
        params: list[Any] = [vector_literal]
        clauses: list[str] = []
        if repo_ids:
            clauses.append("repo_id = ANY(%s)")
            params.append(repo_ids)
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        sql = (
            "SELECT kind, ref_id, repo_id, content, indexed_at_commit, "
            "embedding <=> %s::vector AS distance "
            "FROM embeddings" + where + " ORDER BY distance ASC, ref_id ASC LIMIT %s"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {
                "kind": r[0],
                "ref_id": r[1],
                "repo_id": r[2],
                "content": r[3],
                "indexed_at_commit": r[4],
                "distance": float(r[5]),
            }
            for r in rows
        ]

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM embeddings")
            row = cur.fetchone()
        return int(row[0]) if row else 0
