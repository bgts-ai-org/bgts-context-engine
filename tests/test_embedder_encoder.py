"""Determinism + shape tests for the embedding encoder (P2)."""

from __future__ import annotations

import math

import pytest

from cce.indexing.embedder.encoder import (
    HashingEncoder,
    VoyageEncoder,
    build_default_encoder,
)


def test_encoder_is_deterministic():
    enc = HashingEncoder(dim=64)
    a = enc.encode("def get_user(id): return id")
    b = enc.encode("def get_user(id): return id")
    assert a == b


def test_encoder_dimension_and_unit_norm():
    enc = HashingEncoder(dim=128)
    vec = enc.encode("class Service handles authentication")
    assert len(vec) == 128
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-6


def test_encoder_empty_text_is_zero_vector():
    enc = HashingEncoder(dim=32)
    assert enc.encode("") == [0.0] * 32


def test_similar_text_closer_than_unrelated():
    enc = HashingEncoder(dim=256)

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=False))

    q = enc.encode("authenticate user login token")
    near = enc.encode("user authentication login")
    far = enc.encode("matrix multiplication numeric solver")
    assert cos(q, near) > cos(q, far)


@pytest.fixture
def hashing_settings(monkeypatch):
    """Force the deterministic hashing provider regardless of the ambient .env."""
    from cce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="hashing", embedding_dim=1024),
    )
    yield


def test_default_encoder_pins_model_id(hashing_settings):
    enc = build_default_encoder(dim=64, model_id="unset")
    assert enc.model_id == "cce-hashing-v1"
    enc2 = build_default_encoder(dim=64, model_id="my-pinned-model")
    assert enc2.model_id == "my-pinned-model"


def test_default_encoder_falls_back_to_hashing_without_key(monkeypatch):
    from cce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage", voyage_api_key=""),
    )
    enc = build_default_encoder()
    assert isinstance(enc, HashingEncoder)


def test_default_encoder_selects_voyage_when_ready(monkeypatch):
    pytest.importorskip("voyageai")
    from cce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage", voyage_api_key="k"),
    )

    captured = {}

    class _FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key

    monkeypatch.setattr("voyageai.Client", _FakeClient, raising=False)

    enc = build_default_encoder()
    assert isinstance(enc, VoyageEncoder)
    assert enc.model_id == "voyage-code-3-1024"
    assert enc.dim == 1024
    assert captured["api_key"] == "k"


def test_voyage_encoder_input_types(monkeypatch):
    pytest.importorskip("voyageai")

    calls = []

    class _FakeResult:
        embeddings = [[0.1] * 8]

    class _FakeBatchResult:
        embeddings = [[0.1] * 8, [0.2] * 8]

    class _FakeClient:
        def __init__(self, api_key):
            pass

        def embed(self, texts, model, input_type, output_dimension):
            calls.append((tuple(texts), input_type, output_dimension))
            return _FakeBatchResult() if len(texts) > 1 else _FakeResult()

    monkeypatch.setattr("voyageai.Client", _FakeClient, raising=False)

    enc = VoyageEncoder("k", model="voyage-code-3", dim=8)
    enc.encode("doc text")
    enc.encode_query("query text")
    enc.encode_many(["a", "b"])

    assert calls[0][1] == "document"
    assert calls[1][1] == "query"
    assert calls[2][1] == "document"
    assert all(c[2] == 8 for c in calls)
