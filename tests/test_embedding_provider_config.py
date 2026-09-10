"""How a half-configured embedding provider surfaces on each entry point.

Selecting Voyage without its key or its optional package used to fail in two different ways: the
indexer raised a bare RuntimeError deep inside a Typer traceback, while the query path quietly
substituted the hashing encoder. Both are misconfiguration, and both must now say so in one line.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bce import cli, config
from bce.api.rest.app import create_app
from bce.api.rest.deps import get_vector_store
from bce.indexing.embedder.encoder import EncoderConfigError


class _UnusedVectorStore:
    """Stands in for pgvector: the encoder fails before any search is attempted."""

    def search(self, *args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("search ran despite an unusable encoder")


def test_semantic_search_answers_503_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage", voyage_api_key=""),
    )

    app = create_app()
    app.dependency_overrides[get_vector_store] = lambda: _UnusedVectorStore()
    resp = TestClient(app).post("/v1/semantic-search", json={"query": "login timeout"})

    assert resp.status_code == 503
    assert "BCE_VOYAGE_API_KEY" in resp.json()["message"]


def test_cli_prints_one_line_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> None:
        raise EncoderConfigError("BCE_EMBEDDING_PROVIDER=voyage needs the 'voyageai' package")

    monkeypatch.setattr(cli, "app", _raise)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "voyageai" in err
