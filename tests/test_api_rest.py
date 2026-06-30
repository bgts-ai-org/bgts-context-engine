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

from cce.api.rest.app import create_app
from cce.api.rest.deps import get_repository


class _FakeRepository:
    """Minimal stand-in exposing the read methods the Layer-1 tools call."""

    def __init__(self, matches: list[dict[str, Any]], references: list[dict[str, Any]]) -> None:
        self._matches = matches
        self._references = references

    def resolve_symbol(self, name: str, repo_id: str | None = None) -> list[dict[str, Any]]:
        return self._matches

    def find_references(self, symbol_id: str) -> list[dict[str, Any]]:
        return self._references


def _client(repo: _FakeRepository | None = None) -> TestClient:
    app = create_app()
    if repo is not None:
        app.dependency_overrides[get_repository] = lambda: repo
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
    resp = client.post(
        "/v1/index-remote", json={"url": "https://bitbucket.org/onlyworkspace"}
    )
    assert resp.status_code == 400
    assert "message" in resp.json()

