-- Postgres full-text search over symbols (Layer-2 lexical channel, hybrid-v2).
--
-- The 'simple' configuration is language-agnostic and deterministic (no stemming dictionaries),
-- and the default parser splits snake_case identifiers into searchable word parts, so a token
-- query like "cost" matches "calculate_cost". Rows are written by the graph upserter in the same
-- transaction as the graph nodes (P5) and removed with the file subgraph on incremental drops.

CREATE TABLE IF NOT EXISTS symbol_fts (
    symbol_id         TEXT PRIMARY KEY,
    repo_id           TEXT NOT NULL,
    name              TEXT NOT NULL,
    kind              TEXT,
    signature         TEXT,
    docstring         TEXT,
    file_id           TEXT,
    line              INTEGER,
    indexed_at_commit TEXT,
    document          tsvector GENERATED ALWAYS AS (
        to_tsvector('simple',
            coalesce(name, '') || ' ' || coalesce(signature, '') || ' ' || coalesce(docstring, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_symbol_fts_document ON symbol_fts USING gin (document);

CREATE INDEX IF NOT EXISTS idx_symbol_fts_repo ON symbol_fts (repo_id);
