-- pgvector embeddings (spec section 4.3).
-- Used ONLY for anchor finding (P2); never a direct context output.
-- Dimension is pinned (768) together with the embedding model version for reproducibility.

CREATE TABLE IF NOT EXISTS embeddings (
    id                BIGSERIAL PRIMARY KEY,
    kind              TEXT NOT NULL,          -- 'symbol' | 'file'
    ref_id            TEXT NOT NULL,          -- symbol_id | file_id
    repo_id           TEXT NOT NULL,
    content           TEXT,                   -- signature + docstring (+ optional body summary)
    model             TEXT NOT NULL,          -- pinned model identifier (determinism)
    embedding         vector(768),
    indexed_at_commit TEXT,
    UNIQUE (kind, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_embeddings_repo ON embeddings (repo_id);
