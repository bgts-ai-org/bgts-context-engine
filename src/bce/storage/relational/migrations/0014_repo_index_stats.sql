-- Index quality report per repository (``bce doctor``, ``bce index --report``).
--
-- The indexer writes one row per repository at the end of every full or incremental run: how many
-- of the repository's code files each language has and how many of them were parsed, parse
-- errors, the symbol count, how many cross-file references the linker resolved, whether SCIP ran
-- and which embedding provider wrote the vectors. ``get_context_for_task`` reads it to tell an
-- agent when part of the repository is code the index cannot see.
--
-- One JSON document rather than columns: the report grows with the engine, and a database indexed
-- before this migration simply has no row (no coverage line, nothing else changes).

CREATE TABLE IF NOT EXISTS repo_index_stats (
    repo_id            TEXT PRIMARY KEY,
    indexed_at_commit  TEXT,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    stats              JSONB NOT NULL
);

-- Directory names are repository content: the same scope rule as repos / embeddings (0004).
ALTER TABLE repo_index_stats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS repo_index_stats_scope ON repo_index_stats;
CREATE POLICY repo_index_stats_scope ON repo_index_stats
    USING (
        bce_current_user_id() IS NULL
        OR EXISTS (
            SELECT 1 FROM scopes s
            WHERE s.repo_id = repo_index_stats.repo_id AND s.user_id = bce_current_user_id()
        )
    );
