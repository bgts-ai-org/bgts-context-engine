-- Per-file change frequency (git churn) as a scoring prior.
--
-- Diffs land where diffs have landed before: a file touched by twenty of the last few hundred
-- commits is a far more likely change site than one nobody has opened in a year. The indexer
-- counts, for every indexed file, the commits that touched it within a fixed window ending at the
-- indexed commit (deterministic given the commit; ancestors only, so a snapshot taken at a PR's
-- base never sees the PR's own commits) and scoring reads it through symbol_features().
--
-- Kept relational rather than as a File-node property so it can be recomputed without touching
-- the graph (``bce churn``) and so a database indexed before this migration simply scores with
-- churn 0 until it is backfilled.

CREATE TABLE IF NOT EXISTS file_churn (
    file_id            TEXT PRIMARY KEY,
    repo_id            TEXT NOT NULL,
    commits            INTEGER NOT NULL,
    window_commits     INTEGER NOT NULL,
    computed_at_commit TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_churn_repo ON file_churn (repo_id);
