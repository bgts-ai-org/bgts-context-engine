"""Job queue tests (no database required).

Store SQL runs against PostgreSQL in integration environments; here we cover the pieces that are
testable without a live database:

1. store-level validation (job_type whitelist, UUID guard),
2. worker dispatch (``execute_job`` routes each job_type to the right Indexer method),
3. the REST job endpoints (envelope + status codes) with the store monkeypatched.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import cce.api.rest.routes as routes_module
from cce.api.rest.app import create_app
from cce.api.rest.deps import get_repository
from cce.jobs.store import _valid_uuid, enqueue_job
from cce.jobs.worker import execute_job

_JOB_ID = "11111111-2222-3333-4444-555555555555"


def _job(status: str = "pending", job_type: str = "index") -> dict[str, Any]:
    return {
        "job_id": _JOB_ID,
        "job_type": job_type,
        "payload": {"repo_path": "/tmp/repo", "name": "acme/app"},
        "status": status,
        "result": None,
        "error": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": None,
        "finished_at": None,
    }


# --- store ---


def test_enqueue_rejects_unknown_job_type() -> None:
    with pytest.raises(ValueError, match="job_type"):
        enqueue_job(object(), "not-a-type", {})  # type: ignore[arg-type]


def test_uuid_guard() -> None:
    assert _valid_uuid(_JOB_ID)
    assert not _valid_uuid("index")  # path-param collisions must not hit the DB
    assert not _valid_uuid("")


# --- worker dispatch ---


class _RecordingIndexer:
    """Stands in for Indexer; records which method was called with which kwargs."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def index_local_repo(self, **kwargs: Any) -> Any:
        return self._record("index_local_repo", kwargs)

    def index_remote_repo(self, **kwargs: Any) -> Any:
        return self._record("index_remote_repo", kwargs)

    def index_incremental(self, **kwargs: Any) -> Any:
        return self._record("index_incremental", kwargs)

    def _record(self, method: str, kwargs: dict[str, Any]) -> Any:
        from cce.indexing.indexer import IndexSummary

        _RecordingIndexer.calls.append((method, kwargs))
        return IndexSummary(repo_id="r", files=1, nodes=2, edges=3, commit="c1")


@pytest.fixture()
def recording_indexer(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingIndexer]:
    _RecordingIndexer.calls = []
    import cce.indexing.indexer as indexer_module

    monkeypatch.setattr(indexer_module, "Indexer", _RecordingIndexer)
    # execute_job builds a repository from the connection; a bare object suffices with the fake.
    import cce.storage.graph.client as client_module
    import cce.storage.graph.repository as repo_module

    monkeypatch.setattr(client_module, "GraphClient", lambda conn: conn)
    monkeypatch.setattr(repo_module, "GraphRepository", lambda client: client)
    return _RecordingIndexer


def test_execute_job_dispatches_index(recording_indexer: type[_RecordingIndexer]) -> None:
    result = execute_job(object(), _job(job_type="index"))
    method, kwargs = recording_indexer.calls[0]
    assert method == "index_local_repo"
    assert kwargs == {"path": "/tmp/repo", "name": "acme/app", "commit": None}
    assert result["repo_id"] == "r" and result["files"] == 1


def test_execute_job_dispatches_reindex(recording_indexer: type[_RecordingIndexer]) -> None:
    job = _job(job_type="reindex")
    job["payload"] = {"repo_path": "/tmp/repo", "name": "acme/app", "since_commit": "abc"}
    execute_job(object(), job)
    method, kwargs = recording_indexer.calls[0]
    assert method == "index_incremental"
    assert kwargs["since_commit"] == "abc"
    assert kwargs["to_commit"] == "HEAD"


def test_execute_job_remote_uses_env_credentials(
    recording_indexer: type[_RecordingIndexer],
) -> None:
    job = _job(job_type="index_remote")
    # Even if a token leaked into the payload it must be ignored; creds come from settings.
    job["payload"] = {"url": "https://bitbucket.org/w/r", "name": None, "token": "SHOULD-IGNORE"}
    execute_job(object(), job)
    method, kwargs = recording_indexer.calls[0]
    assert method == "index_remote_repo"
    assert kwargs["credentials"].token != "SHOULD-IGNORE"


# --- REST endpoints (store monkeypatched; no DB) ---


class _FakeConn:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class _FakeClient:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn


class _FakeRepository:
    def __init__(self) -> None:
        self.client = _FakeClient(_FakeConn())


def _client(monkeypatch: pytest.MonkeyPatch, **store_fakes: Any) -> TestClient:
    for name, fake in store_fakes.items():
        monkeypatch.setattr(routes_module, name, fake)
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: _FakeRepository()
    return TestClient(app)


def test_job_submit_returns_202_with_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, enqueue_job=lambda conn, job_type, payload: _job())
    resp = client.post(
        "/v1/jobs/index", json={"repo_path": "/tmp/repo", "name": "acme/app"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["tool"] == "job_index"
    assert body["payload"]["job"]["job_id"] == _JOB_ID
    assert set(body) == {"tool", "payload", "message", "locale"}


def test_job_index_remote_never_stores_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_enqueue(conn: Any, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return _job(job_type=job_type)

    client = _client(monkeypatch, enqueue_job=fake_enqueue)
    resp = client.post(
        "/v1/jobs/index-remote",
        json={"url": "https://bitbucket.org/w/r", "token": "s3cret", "username": "me"},
    )
    assert resp.status_code == 202
    assert "token" not in captured and "username" not in captured


def test_job_status_found_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, get_job=lambda conn, job_id: _job(status="running"))
    ok = client.get(f"/v1/jobs/{_JOB_ID}")
    assert ok.status_code == 200
    assert ok.json()["payload"]["job"]["status"] == "running"

    client = _client(monkeypatch, get_job=lambda conn, job_id: None)
    missing = client.get(f"/v1/jobs/{_JOB_ID}")
    assert missing.status_code == 404
    assert missing.json()["tool"] == "job_status"


def test_job_list_with_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_list(conn: Any, *, status: str | None, repo: str | None, limit: int) -> list[dict]:
        seen.update(status=status, repo=repo, limit=limit)
        return [_job()]

    client = _client(monkeypatch, list_jobs=fake_list)
    resp = client.get("/v1/jobs", params={"status": "pending", "repo": "acme/app", "limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()["payload"]["jobs"]) == 1
    assert seen == {"status": "pending", "repo": "acme/app", "limit": 5}


def test_job_cancel_pending_conflict_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = _job(status="cancelled")
    client = _client(monkeypatch, cancel_job=lambda conn, job_id: cancelled)
    ok = client.post(f"/v1/jobs/{_JOB_ID}/cancel")
    assert ok.status_code == 200
    assert ok.json()["payload"]["job"]["status"] == "cancelled"

    running = _job(status="running")
    client = _client(
        monkeypatch,
        cancel_job=lambda conn, job_id: None,
        get_job=lambda conn, job_id: running,
    )
    conflict = client.post(f"/v1/jobs/{_JOB_ID}/cancel")
    assert conflict.status_code == 409

    client = _client(
        monkeypatch,
        cancel_job=lambda conn, job_id: None,
        get_job=lambda conn, job_id: None,
    )
    missing = client.post(f"/v1/jobs/{_JOB_ID}/cancel")
    assert missing.status_code == 404


def test_job_message_is_localized(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, enqueue_job=lambda conn, job_type, payload: _job())
    en = client.post(
        "/v1/jobs/index",
        json={"repo_path": "/tmp/r", "name": "n"},
        params={"locale": "en"},
    ).json()
    tr = client.post(
        "/v1/jobs/index",
        json={"repo_path": "/tmp/r", "name": "n"},
        params={"locale": "tr"},
    ).json()
    assert en["payload"] == tr["payload"]
    assert en["message"] != tr["message"]
    assert "enqueued" in en["message"]
    assert "kuyru" in tr["message"]
