-- Background job queue for async indexing (index / index_remote / reindex).
-- Claimed with FOR UPDATE SKIP LOCKED so multiple workers never pick the same job.

CREATE TABLE IF NOT EXISTS jobs (
    job_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type    TEXT NOT NULL CHECK (job_type IN ('index', 'index_remote', 'reindex')),
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at);
