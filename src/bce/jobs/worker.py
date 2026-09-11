"""In-process worker threads that execute queued indexing jobs.

Each worker polls the ``jobs`` table, claims one job at a time (``FOR UPDATE SKIP LOCKED`` in the
store) and runs the matching :class:`bce.indexing.indexer.Indexer` method. Two connections are
used per job on purpose:

- a *bookkeeping* connection that claims the job and records the outcome (committed immediately so
  other workers and status queries see the transition), and
- a *work* connection that holds the actual graph/vector writes, committed only when the indexing
  run succeeds and rolled back on failure.

Secrets note: ``index_remote`` credentials are **never** stored in the job payload; the worker
reads them from settings (``BCE_BITBUCKET_*``) at execution time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from bce.config import Settings, get_settings

logger = logging.getLogger("bce.jobs.worker")


def execute_job(conn: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Run one job's indexing work on ``conn`` and return the result payload (no commit)."""
    from bce.indexing.gitsync import GitCredentials
    from bce.indexing.indexer import Indexer
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository

    settings = get_settings()
    indexer = Indexer(GraphRepository(GraphClient(conn)))
    payload = job["payload"] or {}
    job_type = job["job_type"]

    if job_type == "index":
        summary = indexer.index_local_repo(
            path=payload["repo_path"], name=payload["name"], commit=payload.get("commit")
        )
    elif job_type == "index_remote":
        creds = GitCredentials(username=settings.bitbucket_username, token=settings.bitbucket_token)
        summary = indexer.index_remote_repo(
            url=payload["url"],
            name=payload.get("name"),
            branch=payload.get("branch"),
            credentials=creds,
        )
    elif job_type == "reindex":
        summary = indexer.index_incremental(
            path=payload["repo_path"],
            name=payload["name"],
            since_commit=payload.get("since_commit"),
            to_commit=payload.get("to_commit") or "HEAD",
        )
    else:  # pragma: no cover - enqueue_job validates job_type
        raise ValueError(f"Unknown job_type '{job_type}'.")
    return asdict(summary)


class JobWorkerPool:
    """Pool of daemon threads polling the jobs queue; sized by ``BCE_JOB_WORKERS``.

    ``job_types`` restricts which jobs this pool will claim (default: all of them). The MCP server
    passes the local-only subset so remote clones stay with the API server.
    """

    def __init__(
        self, settings: Settings | None = None, *, job_types: Sequence[str] | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._job_types = tuple(job_types) if job_types is not None else None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        count = max(1, self._settings.job_workers)
        for i in range(count):
            thread = threading.Thread(
                target=self._run_loop, name=f"bce-job-worker-{i}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        logger.info("job worker pool started", extra={"workers": count})

    def stop(self, timeout: float | None = 30.0) -> None:
        """Signal workers to stop and wait for in-flight jobs to finish."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        logger.info("job worker pool stopped")

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._claim_and_run_one()
            except Exception:
                # Never let an unexpected error (e.g. DB down) kill the worker thread.
                logger.exception("job worker loop error")
                worked = False
            if not worked:
                self._stop.wait(self._settings.job_poll_interval)

    def _claim_and_run_one(self) -> bool:
        """Claim and execute at most one job. Returns True when a job was processed."""
        from bce.jobs.store import claim_next_job, fail_job, finish_job
        from bce.storage.relational.db import connection

        with connection(self._settings) as book:
            job = claim_next_job(book, job_types=self._job_types)
            if job is None:
                return False
            book.commit()

            job_id = job["job_id"]
            logger.info("job started", extra={"job_id": job_id, "job_type": job["job_type"]})
            try:
                with connection(self._settings) as work:
                    result = execute_job(work, job)
                    work.commit()
            except Exception as exc:
                logger.exception(
                    "job failed", extra={"job_id": job_id, "job_type": job["job_type"]}
                )
                fail_job(book, job_id, f"{type(exc).__name__}: {exc}")
                book.commit()
                return True

            finish_job(book, job_id, result)
            book.commit()
            logger.info("job succeeded", extra={"job_id": job_id, "job_type": job["job_type"]})
            return True
