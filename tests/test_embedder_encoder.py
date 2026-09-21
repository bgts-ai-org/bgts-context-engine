"""Determinism + shape tests for the embedding encoder (P2)."""

from __future__ import annotations

import math

import pytest

from bce.indexing.embedder.encoder import (
    _MAX_BATCH_CHARS,
    _MAX_BATCH_TEXTS,
    JINA_CODE_DOCUMENT_PREFIX,
    JINA_CODE_QUERY_PREFIX,
    EncoderConfigError,
    HashingEncoder,
    OpenAICompatEncoder,
    VoyageEncoder,
    _split_batches,
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
    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="hashing", embedding_dim=1024),
    )
    yield


def test_default_encoder_pins_model_id(hashing_settings):
    enc = build_default_encoder(dim=64, model_id="unset")
    assert enc.model_id == "bce-hashing-v1"
    enc2 = build_default_encoder(dim=64, model_id="my-pinned-model")
    assert enc2.model_id == "my-pinned-model"


def test_default_encoder_rejects_voyage_without_key(monkeypatch):
    """Selecting Voyage with no key must fail, not quietly index with hashing vectors."""
    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage", voyage_api_key=""),
    )
    with pytest.raises(EncoderConfigError, match="BCE_VOYAGE_API_KEY"):
        build_default_encoder()


def test_default_encoder_rejects_unknown_provider(monkeypatch):
    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage-code-3"),
    )
    with pytest.raises(EncoderConfigError, match="Unknown BCE_EMBEDDING_PROVIDER"):
        build_default_encoder()


def test_default_encoder_reports_the_missing_voyage_package(monkeypatch):
    """The base install has no 'voyageai'; the error must name the extra that provides it."""
    import builtins

    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="voyage", voyage_api_key="k"),
    )

    real_import = builtins.__import__

    def _no_voyageai(name, *args, **kwargs):
        if name == "voyageai":
            raise ModuleNotFoundError("No module named 'voyageai'", name="voyageai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_voyageai)

    with pytest.raises(EncoderConfigError, match=r"bgts-context-engine\[embed\]"):
        build_default_encoder()


def test_default_encoder_selects_voyage_when_ready(monkeypatch):
    pytest.importorskip("voyageai")
    from bce import config

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


def test_split_batches_respects_both_ceilings():
    """Voyage rejects >1000 texts or too many tokens per request; splitting must keep order."""
    texts = [f"t{i}" for i in range(_MAX_BATCH_TEXTS + 3)]
    batches = _split_batches(texts)
    assert [len(b) for b in batches] == [_MAX_BATCH_TEXTS, 3]
    assert [t for b in batches for t in b] == texts

    oversized = "x" * (_MAX_BATCH_CHARS + 1)
    # An input larger than the char budget gets a request of its own rather than being dropped.
    assert _split_batches(["a", oversized, "b"]) == [["a"], [oversized], ["b"]]
    assert _split_batches([]) == []


def test_voyage_encode_many_splits_oversized_input(monkeypatch):
    """A single file can hold more symbols than one request allows; all of them must come back."""
    pytest.importorskip("voyageai")

    sizes = []

    class _FakeClient:
        def __init__(self, api_key):
            pass

        def embed(self, texts, model, input_type, output_dimension):
            sizes.append(len(texts))
            if len(texts) > 1000:
                raise AssertionError("batch size limit is 1000")

            class _Result:
                embeddings = [[float(i)] * 4 for i in range(len(texts))]

            return _Result()

    monkeypatch.setattr("voyageai.Client", _FakeClient, raising=False)

    enc = VoyageEncoder("k", model="voyage-code-4", dim=4)
    vectors = enc.encode_many([f"symbol {i}" for i in range(1400)])

    assert len(vectors) == 1400
    assert sum(sizes) == 1400
    assert max(sizes) <= _MAX_BATCH_TEXTS


# --------------------------------------------------------------------------- OpenAI-compatible


@pytest.fixture
def fake_embeddings_server(monkeypatch):
    """Stand-in for a vLLM ``/v1/embeddings`` endpoint: records requests, answers 6-dim vectors
    whose first component encodes the input position, in shuffled ``index`` order."""
    from bce.indexing.embedder import encoder as module

    calls: list[dict] = []

    def _post(url, body, headers, timeout):
        calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        rows = [
            {"index": i, "embedding": [float(i + 1), 1.0, 0.0, 0.0, 0.0, 0.0]}
            for i in range(len(body["input"]))
        ]
        return {"object": "list", "model": body["model"], "data": list(reversed(rows))}

    monkeypatch.setattr(module, "_post_json", _post)
    return calls


def test_openai_encoder_applies_jina_prompts_and_pins_model_id(fake_embeddings_server):
    enc = OpenAICompatEncoder("http://127.0.0.1:8001/v1/", model="jina-code-embeddings-1.5b", dim=6)
    assert enc.model_id == "jina-code-embeddings-1.5b-6"
    assert enc.url == "http://127.0.0.1:8001/v1/embeddings"

    enc.encode_query("login timeout")
    enc.encode_many(["def a(): ...", "def b(): ..."])

    query, docs = fake_embeddings_server
    assert query["body"]["input"] == [JINA_CODE_QUERY_PREFIX + "login timeout"]
    assert docs["body"]["input"] == [
        JINA_CODE_DOCUMENT_PREFIX + "def a(): ...",
        JINA_CODE_DOCUMENT_PREFIX + "def b(): ...",
    ]
    assert docs["body"]["model"] == "jina-code-embeddings-1.5b"
    # No key -> no Authorization header; a local server does not check one.
    assert "Authorization" not in docs["headers"]


def test_openai_encoder_orders_by_index_and_uses_extra_body(fake_embeddings_server):
    enc = OpenAICompatEncoder(
        "http://h/v1",
        model="some-model",
        dim=6,
        api_key="secret",
        extra_body={"truncate_prompt_tokens": -1},
    )
    vectors = enc.encode_many(["a", "b", "c"])

    # The server answered in reverse order; the encoder must put them back by ``index``.
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
    (call,) = fake_embeddings_server
    assert call["body"]["truncate_prompt_tokens"] == -1
    assert call["headers"]["Authorization"] == "Bearer secret"
    # No jina in the name -> no prompt prefixes unless configured.
    assert call["body"]["input"] == ["a", "b", "c"]


def test_openai_encoder_explicit_prefixes_override_the_defaults(fake_embeddings_server):
    enc = OpenAICompatEncoder(
        "http://h/v1",
        model="jina-code-embeddings-1.5b",
        dim=6,
        query_prefix="Q: ",
        document_prefix="",
    )
    enc.encode_query("x")
    enc.encode("y")
    assert fake_embeddings_server[0]["body"]["input"] == ["Q: x"]
    assert fake_embeddings_server[1]["body"]["input"] == ["y"]


def test_openai_encoder_truncates_matryoshka_vectors_and_renormalises(fake_embeddings_server):
    enc = OpenAICompatEncoder("http://h/v1", model="m", dim=2)
    (vec,) = enc.encode_many(["a"])
    # Server gave [1, 1, 0, 0, 0, 0]; the first two components re-normalised.
    assert len(vec) == 2
    assert abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-9
    assert abs(vec[0] - vec[1]) < 1e-9


def test_openai_encoder_rejects_narrower_vectors(fake_embeddings_server):
    enc = OpenAICompatEncoder("http://h/v1", model="m", dim=1536)
    with pytest.raises(EncoderConfigError, match="BCE_EMBEDDING_DIM=6"):
        enc.encode("a")


def test_openai_encoder_does_not_retry_client_errors(monkeypatch):
    import io
    import urllib.error

    from bce.indexing.embedder import encoder as module

    attempts = []

    def _post(url, body, headers, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"maximum context length"}')
        )

    monkeypatch.setattr(module, "_post_json", _post)
    enc = OpenAICompatEncoder("http://h/v1", model="m", dim=6)
    with pytest.raises(RuntimeError, match="maximum context length"):
        enc.encode("a")
    assert len(attempts) == 1


def test_openai_encoder_retries_when_the_server_is_down(monkeypatch):
    import urllib.error

    from bce.indexing.embedder import encoder as module

    attempts = []

    def _post(url, body, headers, timeout):
        attempts.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(module, "_post_json", _post)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    enc = OpenAICompatEncoder("http://h/v1", model="m", dim=6)
    with pytest.raises(RuntimeError, match="unreachable"):
        enc.encode("a")
    assert len(attempts) == module._HTTP_ATTEMPTS


def test_default_encoder_selects_openai_from_settings(monkeypatch, fake_embeddings_server):
    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(
            embedding_provider="openai",
            embedding_model="jina-code-embeddings-1.5b",
            embedding_dim=6,
            embedding_base_url="http://127.0.0.1:8001/v1",
        ),
    )
    enc = build_default_encoder()
    assert isinstance(enc, OpenAICompatEncoder)
    assert enc.model_id == "jina-code-embeddings-1.5b-6"
    assert enc.query_prefix == JINA_CODE_QUERY_PREFIX
    assert enc.extra_body == {"truncate_prompt_tokens": -1}


def test_default_encoder_rejects_openai_without_base_url(monkeypatch):
    from bce import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: config.Settings(embedding_provider="openai", embedding_base_url=" "),
    )
    with pytest.raises(EncoderConfigError, match="BCE_EMBEDDING_BASE_URL"):
        build_default_encoder()


def test_settings_parse_extra_body_from_env(monkeypatch):
    from bce import config

    monkeypatch.setenv("BCE_EMBEDDING_EXTRA_BODY", "{}")
    monkeypatch.setenv("BCE_EMBEDDING_DOCUMENT_PREFIX", "")
    s = config.Settings(_env_file=None)
    assert s.embedding_extra_body == {}
    # An empty string is a deliberate "no prefix", distinct from unset (None -> model default).
    assert s.embedding_document_prefix == ""
    assert s.embedding_query_prefix is None
