"""REST adapter tests (no database required).

The DB-backed dependency is overridden with an in-memory fake so we can assert the two contracts
that matter at the transport boundary:

1. ``/v1/languages`` works with no database.
2. The deterministic ``payload`` is byte-identical across locales; only ``message`` is localized
   (the i18n / P1 boundary).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from bce.api.rest.app import create_app
from bce.api.rest.deps import get_repository, get_scope, get_vector_store
from bce.core.auth.scope import Principal, ScopeFilter


class _FakeRepository:
    """Minimal stand-in exposing the read methods the Layer-1/2/3 tools call."""

    def __init__(
        self,
        matches: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
    ) -> None:
        self._matches = matches or []
        self._references = references or []

    def resolve_symbol(self, name: str, repo_id: str | None = None) -> list[dict[str, Any]]:
        return self._matches

    def find_references(self, symbol_id: str) -> list[dict[str, Any]]:
        return self._references

    # Broad no-op read surface so Layer-2/3 tools run without a database.
    def find_implementers(self, symbol_id):
        return []

    def get_callers(self, symbol_id):
        return []

    def get_callees(self, symbol_id):
        return []

    def get_supertypes(self, symbol_id):
        return []

    def get_subtypes(self, symbol_id):
        return []

    def get_file_imports(self, file_id):
        return []

    def find_routes(self, path):
        return []

    def get_design_notes(self, symbol_id):
        return []

    def symbols_in_file(self, file_id):
        return []

    def symbol_degree(self, symbol_id):
        return 0

    def repo_of_symbol(self, symbol_id):
        return None

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def get_symbol(self, symbol_id):
        return None


class _FakeVectorStore:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self._hits = hits or []

    def search(self, vector, *, limit=20, repo_ids=None, kind=None):
        return self._hits[:limit]


def _client(
    repo: _FakeRepository | None = None,
    store: _FakeVectorStore | None = None,
) -> TestClient:
    app = create_app()
    if repo is not None:
        app.dependency_overrides[get_repository] = lambda: repo
    if store is not None:
        app.dependency_overrides[get_vector_store] = lambda: store
    app.dependency_overrides[get_scope] = lambda: ScopeFilter(Principal.system())
    return TestClient(app)


def test_languages_needs_no_database() -> None:
    resp = _client().get("/v1/languages")
    assert resp.status_code == 200
    body = resp.json()
    assert "python" in body["languages"]
    assert ".py" in body["extensions"]


def test_find_references_payload_is_locale_invariant() -> None:
    refs = [
        {
            "caller_id": "py::pkg::mod::caller#abc",
            "file_id": "repo:file:mod.py",
            "line": 10,
            "edge_type": "CALLS",
            "ref_kind": "call",
            "provenance": "treesitter",
        }
    ]
    client = _client(_FakeRepository(matches=[], references=refs))

    en = client.get("/v1/find-references", params={"symbol_id": "py::x#1", "locale": "en"}).json()
    tr = client.get("/v1/find-references", params={"symbol_id": "py::x#1", "locale": "tr"}).json()

    # Payload is byte-identical regardless of locale; only the message changes.
    assert json.dumps(en["payload"], sort_keys=True) == json.dumps(tr["payload"], sort_keys=True)
    assert en["locale"] == "en" and tr["locale"] == "tr"
    assert en["message"] != tr["message"]
    assert "reference" in en["message"]
    assert "referans" in tr["message"]


def test_resolve_symbol_not_found_is_localized() -> None:
    client = _client(_FakeRepository(matches=[], references=[]))

    en = client.get("/v1/resolve-symbol", params={"name": "Missing", "locale": "en"}).json()
    tr = client.get("/v1/resolve-symbol", params={"name": "Missing", "locale": "tr"}).json()

    assert en["payload"] == tr["payload"] == {"matches": [], "indexed_at_commit": None}
    assert "not found" in en["message"]
    assert "bulunamad" in tr["message"]


def test_accept_language_header_selects_locale() -> None:
    client = _client(_FakeRepository(matches=[], references=[]))
    resp = client.get(
        "/v1/resolve-symbol",
        params={"name": "Missing"},
        headers={"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
    )
    assert resp.json()["locale"] == "tr"


def test_index_remote_rejects_invalid_url() -> None:
    # An unparseable URL fails before any clone/DB access -> localized 400.
    client = _client(_FakeRepository(matches=[], references=[]))
    resp = client.post("/v1/index-remote", json={"url": "https://bitbucket.org/onlyworkspace"})
    assert resp.status_code == 400
    assert "message" in resp.json()


def test_all_layers_are_registered() -> None:
    app = create_app()
    paths = set(app.openapi()["paths"])
    for path in (
        "/v1/resolve-symbol",
        "/v1/find-references",
        "/v1/find-implementers",
        "/v1/get-call-graph",
        "/v1/get-dependencies",
        "/v1/get-type-hierarchy",
        "/v1/semantic-search",
        "/v1/hybrid-search",
        "/v1/find-similar-code",
        "/v1/get-context-for-task",
        "/v1/suggest-change-sites",
        "/v1/expand-blast-radius",
        "/v1/select-repos",
        "/v1/assemble-context",
        "/v1/index",
        "/v1/index-remote",
        "/v1/reindex",
        "/v1/jobs/index",
        "/v1/jobs/index-remote",
        "/v1/jobs/reindex",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/cancel",
    ):
        assert path in paths, f"missing endpoint: {path}"


def test_semantic_search_payload_is_locale_invariant() -> None:
    hits = [
        {
            "ref_id": "py::a#1",
            "repo_id": "r",
            "content": "a",
            "indexed_at_commit": "c1",
            "distance": 0.1,
        },
    ]
    client = _client(_FakeRepository(), _FakeVectorStore(hits))

    en = client.post("/v1/semantic-search", json={"query": "login"}, params={"locale": "en"}).json()
    tr = client.post("/v1/semantic-search", json={"query": "login"}, params={"locale": "tr"}).json()

    assert json.dumps(en["payload"], sort_keys=True) == json.dumps(tr["payload"], sort_keys=True)
    assert en["message"] != tr["message"]
    assert en["payload"]["candidates"][0]["symbol_id"] == "py::a#1"


def test_get_context_for_task_returns_coverage() -> None:
    client = _client(_FakeRepository(), _FakeVectorStore())
    resp = client.post(
        "/v1/get-context-for-task", json={"task_text": "fix the thing", "max_tokens": 100}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "get_context_for_task"
    assert "coverage" in body["payload"]
    assert "context" in body["payload"]


def test_find_implementers_endpoint_ok() -> None:
    client = _client(_FakeRepository())
    resp = client.get("/v1/find-implementers", params={"symbol_id": "py::T#1"})
    assert resp.status_code == 200
    assert resp.json()["tool"] == "find_implementers"


def test_assemble_context_endpoint_ok() -> None:
    client = _client(_FakeRepository())
    resp = client.post("/v1/assemble-context", json={"symbol_ids": [], "max_tokens": 100})
    assert resp.status_code == 200
    assert resp.json()["payload"]["context"]["included"] == 0
