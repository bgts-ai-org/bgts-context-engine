-- Widen embeddings to Voyage voyage-code-3 dimension (1024).
--
-- The vector column width is fixed per model (P2). Switching model/dimension invalidates existing
-- vectors, so we clear them and rebuild the HNSW index at the new width; a reindex re-populates them
-- with the pinned model. This is the determinism-regression boundary the plan calls out.

DROP INDEX IF EXISTS idx_embeddings_hnsw;

TRUNCATE TABLE embeddings;

ALTER TABLE embeddings ALTER COLUMN embedding TYPE vector(1024);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops);
