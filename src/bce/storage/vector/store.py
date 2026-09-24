"""pgvector-backed embedding store (anchor finding only, P2).

Writes symbol/file embeddings in the same connection/transaction as the graph upsert (P5: single
DB) and runs cosine-distance nearest-neighbour search over the HNSW index. Every row records the
pinned ``model`` identifier so a model/version change is detectable and reindex-gated (determinism).

A symbol may be stored as several **chunks** (migration 0011): a long body is split into windows
that each get their own vector, all under the same ``ref_id`` with ``chunk = 0, 1, 2 ...``. Search
returns rows, so a caller that wants symbols collapses duplicates (first = best rank);
:meth:`VectorStore.search_refs` does exactly that.

Search results are ordered by distance, then by ``ref_id`` and chunk, to break ties
deterministically, so the same query vector against the same snapshot yields a stable order.
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
        # Lazily probed: whether the ``chunk`` column exists (migration 0011 applied).
        self._chunks_ready: bool | None = None

    def supports_chunks(self) -> bool:
        """True when ``embeddings.chunk`` exists; older snapshots hold one row per ref."""
        if self._chunks_ready is None:
            try:
                with self.conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'embeddings' AND column_name = 'chunk'"
                    )
                    self._chunks_ready = cur.fetchone() is not None
            except Exception:
                self._chunks_ready = False
        return self._chunks_ready

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
        chunk: int = 0,
    ) -> None:
        if self.supports_chunks():
            self.conn.execute(
                "INSERT INTO embeddings (kind, ref_id, repo_id, content, model, embedding, "
                "indexed_at_commit, chunk) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (kind, ref_id, chunk) DO UPDATE SET "
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
                    int(chunk),
                ),
            )
            return
        if chunk:
            # Pre-0011 snapshot: only the first chunk can be stored.
            return
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

    def trim_chunks(self, kind: str, ref_id: str, keep: int) -> None:
        """Drop chunks ``>= keep`` of a ref (a re-indexed symbol that got shorter)."""
        if not self.supports_chunks():
            return
        self.conn.execute(
            "DELETE FROM embeddings WHERE kind = %s AND ref_id = %s AND chunk >= %s",
            (kind, ref_id, int(keep)),
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
        """Cosine nearest neighbours, deterministic order (distance, then ref_id, then chunk).

        Rows, not refs: a chunked symbol may appear more than once. See :meth:`search_refs`.
        """
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

        chunk_col = "chunk" if self.supports_chunks() else "0"
        sql = (
            f"SELECT kind, ref_id, repo_id, content, indexed_at_commit, {chunk_col} AS chunk, "
            "embedding <=> %s::vector AS distance "
            "FROM embeddings" + where + " ORDER BY distance ASC, ref_id ASC, chunk ASC LIMIT %s"
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
                "chunk": int(r[5] or 0),
                "distance": float(r[6]),
            }
            for r in rows
        ]

    def search_refs(
        self,
        query_vector: list[float],
        *,
        limit: int = 20,
        repo_ids: list[str] | None = None,
        kind: str | None = None,
        overfetch: int = 4,
    ) -> list[dict[str, Any]]:
        """Nearest *refs* (symbols / files): chunks collapsed to their best row, ``limit`` refs.

        Fetches ``limit * overfetch`` rows so that a few long, many-chunked symbols cannot crowd
        the ref list; the returned rows carry the best chunk's distance.
        """
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in self.search(
            query_vector, limit=max(limit, 1) * max(overfetch, 1), repo_ids=repo_ids, kind=kind
        ):
            if row["ref_id"] in seen:
                continue
            seen.add(row["ref_id"])
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM embeddings")
            row = cur.fetchone()
        return int(row[0]) if row else 0
