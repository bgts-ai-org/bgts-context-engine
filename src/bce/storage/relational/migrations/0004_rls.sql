-- Row-Level Security for repo-scoped tables (spec section 9, Phase 4).
--
-- Policy model: a session sets ``SET LOCAL bce.user_id = '<id>'``. When the session is a superuser
-- or ``bce.user_id`` is unset/empty (system/pre-auth calls), all rows are visible; otherwise only
-- rows whose repo is granted to that user via ``scopes`` are visible. The graph (AGE) is filtered in
-- the application (ScopeFilter, section 6.5); this covers the relational + vector side (P5).

-- Helper: is the current session unrestricted (no user pinned)?
CREATE OR REPLACE FUNCTION bce_current_user_id() RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('bce.user_id', true), '')
$$;

-- repos: visible if unrestricted, or the user has a scope row for that repo.
ALTER TABLE repos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS repos_scope ON repos;
CREATE POLICY repos_scope ON repos
    USING (
        bce_current_user_id() IS NULL
        OR EXISTS (
            SELECT 1 FROM scopes s
            WHERE s.repo_id = repos.repo_id AND s.user_id = bce_current_user_id()
        )
    );

-- embeddings: same rule keyed by repo_id (anchor finding must not leak across scope).
ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS embeddings_scope ON embeddings;
CREATE POLICY embeddings_scope ON embeddings
    USING (
        bce_current_user_id() IS NULL
        OR EXISTS (
            SELECT 1 FROM scopes s
            WHERE s.repo_id = embeddings.repo_id AND s.user_id = bce_current_user_id()
        )
    );
