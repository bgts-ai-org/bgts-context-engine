"""Background job infrastructure: DB-backed queue + in-process worker threads.

Async variants of the indexing operations (``index``, ``index_remote``, ``reindex``) are enqueued
into the ``jobs`` table and executed by :class:`bce.jobs.worker.JobWorkerPool`. Claiming uses
``FOR UPDATE SKIP LOCKED`` so multiple workers never pick the same job.
"""

from bce.jobs.store import (
    JOB_TYPES,
    cancel_job,
    claim_next_job,
    enqueue_job,
    fail_job,
    finish_job,
    get_job,
    list_jobs,
)
from bce.jobs.worker import JobWorkerPool

__all__ = [
    "JOB_TYPES",
    "JobWorkerPool",
    "cancel_job",
    "claim_next_job",
    "enqueue_job",
    "fail_job",
    "finish_job",
    "get_job",
    "list_jobs",
]
