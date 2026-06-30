-- Relational metadata tables (spec section 4.2).

CREATE TABLE IF NOT EXISTS repos (
    repo_id             TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    default_branch      TEXT NOT NULL DEFAULT 'main',
    remote_url          TEXT,
    last_indexed_commit TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    description    TEXT,
    epic           TEXT,
    labels         JSONB NOT NULL DEFAULT '[]'::jsonb,
    component      TEXT,
    linked_commit  TEXT,
    linked_pr      TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Anchor source #3: past resolved task -> files that were touched.
CREATE TABLE IF NOT EXISTS task_history (
    id              BIGSERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL,
    touched_file_id TEXT NOT NULL,
    commit          TEXT,
    touched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history (task_id);

CREATE TABLE IF NOT EXISTS users (
    id    TEXT PRIMARY KEY,
    name  TEXT,
    email TEXT
);

-- Repo-level access used to feed RLS policies (enforced in Phase 4).
CREATE TABLE IF NOT EXISTS scopes (
    id      BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    repo_id TEXT NOT NULL REFERENCES repos (repo_id) ON DELETE CASCADE,
    access  TEXT NOT NULL DEFAULT 'read',
    UNIQUE (user_id, repo_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id          TEXT,
    task_id          TEXT,
    commit           TEXT,
    tool             TEXT,
    locale           TEXT,
    returned_symbols JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log (task_id);
