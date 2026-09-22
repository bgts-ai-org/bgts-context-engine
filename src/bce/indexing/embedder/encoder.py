"""Embedding encoders (anchor finding only, P2).

An encoder maps text to a fixed-dimension unit vector. Per P2 the embedding is a *seed* (a way into
the graph), never the answer, and per the determinism rules the model identity is pinned so results
are reproducible within a snapshot.

Four encoders ship:

- :class:`HashingEncoder` - dependency-free, fully deterministic (a hashed bag-of-tokens projected
  to the unit sphere). It is the default "lexical safety-net" fallback the plan calls for: no heavy
  model is required to run semantic search, and its output is byte-stable across machines.
- :class:`VoyageEncoder` - Voyage AI code embeddings (``voyage-code-3``, 1024 dims). It distinguishes
  ``document`` (indexing) from ``query`` (search) input types. Its outputs are written into pgvector
  at index time and pinned there; since embeddings only *find anchors* (P2), the deterministic
  payload is unaffected by any minor API float variation.
- :class:`OpenAICompatEncoder` - any server speaking the OpenAI ``/v1/embeddings`` protocol (vLLM,
  Text Embeddings Inference, Ollama, OpenAI). This is how an open model such as
  ``jinaai/jina-code-embeddings-1.5b`` runs on-prem. Query/document asymmetry is expressed as
  instruction prefixes, which the encoder knows for the jina-code family and otherwise takes from
  settings.
- :class:`Encoder` - the abstract contract. Any other on-prem encoder can implement this without
  touching the rest of the pipeline; its ``model_id`` must be pinned.

The encoder used by indexing and search MUST match (same ``model_id`` + dimension), otherwise a
version bump is a determinism-regression boundary and requires a reindex.
"""

from __future__ import annotations

import abc
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class EncoderConfigError(RuntimeError):
    """The selected embedding provider cannot be built as configured.

    Raised rather than substituting a working encoder. ``VectorStore.search`` compares the query
    vector against stored rows without filtering on ``model``, so an encoder swapped in behind the
    caller's back returns plausible-looking nonsense instead of an error.
    """


class Encoder(abc.ABC):
    """Maps text to a pinned, fixed-dimension embedding vector."""

    model_id: str = "abstract"
    dim: int = 768

    @abc.abstractmethod
    def encode(self, text: str) -> list[float]:
        """Return a ``dim``-length embedding for ``text`` (deterministic for a fixed model)."""

    def encode_query(self, text: str) -> list[float]:
        """Encode a search query. Defaults to :meth:`encode`; overridden when the model has a
        distinct query input type (e.g. Voyage)."""
        return self.encode(text)

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        """Batch-encode documents (indexing side). Default is per-item; overridden for batch APIs."""
        return [self.encode(t) for t in texts]


def _tokenize(text: str) -> list[str]:
    """Case-folded identifier/word tokens, with snake/camel split so ``getUser`` ~ ``get user``."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", raw) or [raw]
        for part in parts:
            tokens.append(part.lower())
    return tokens


class HashingEncoder(Encoder):
    """Deterministic hashed bag-of-tokens embedder (no external model, reproducible everywhere)."""

    def __init__(self, dim: int = 768, model_id: str = "bce-hashing-v1") -> None:
        self.dim = dim
        self.model_id = model_id

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


#: Voyage rejects a request carrying more than 1000 texts, and caps the tokens per request. A
#: single large source file can hold well over 1000 symbols, so requests are split to stay under
#: both ceilings rather than failing the whole index.
_MAX_BATCH_TEXTS = 500
_MAX_BATCH_CHARS = 400_000  # ~100k tokens at the usual ~4 chars/token for code


def _split_batches(texts: list[str]) -> list[list[str]]:
    """Split ``texts`` into request-sized batches, preserving order.

    A text that is on its own larger than the char budget still gets its own batch: Voyage
    truncates over-long inputs per text, which is preferable to dropping the symbol.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        if current and (len(current) >= _MAX_BATCH_TEXTS or size + len(text) > _MAX_BATCH_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append(current)
    return batches


class VoyageEncoder(Encoder):
    """Voyage AI code embeddings (``voyage-code-3`` by default).

    Uses the ``voyageai`` SDK. Indexing uses ``input_type='document'`` and search uses
    ``input_type='query'`` (Voyage recommends this asymmetry for retrieval). The output dimension is
    requested explicitly so it matches the pinned pgvector column width.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voyage-code-3",
        dim: int = 1024,
    ) -> None:
        try:
            import voyageai
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dep
            raise EncoderConfigError(
                "BCE_EMBEDDING_PROVIDER=voyage needs the 'voyageai' package, which the base "
                "install leaves out. Install it with: pip install 'bgts-context-engine[embed]' "
                "- or set BCE_EMBEDDING_PROVIDER=hashing to embed offline."
            ) from exc
        self._client = voyageai.Client(api_key=api_key)
        self.model = model
        self.dim = dim
        # model_id pins model + dimension together (a change requires a reindex, P2 determinism).
        self.model_id = f"{model}-{dim}"

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Embed ``texts`` in order, splitting into as many API requests as the limits require."""
        vectors: list[list[float]] = []
        for batch in _split_batches(texts):
            result = self._client.embed(
                batch,
                model=self.model,
                input_type=input_type,
                output_dimension=self.dim,
            )
            vectors.extend(list(vec) for vec in result.embeddings)
        return vectors

    def encode(self, text: str) -> list[float]:
        return self._embed([text], "document")[0]

    def encode_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed(list(texts), "document")


#: Instruction prompts of the ``jinaai/jina-code-embeddings-*`` family for its ``nl2code`` task
#: (natural-language query -> code passage), which is what anchor finding does: the task text is
#: prose, the indexed content is a symbol. Taken from the model card; the model was trained with
#: these exact strings, so a different prefix costs retrieval quality.
JINA_CODE_QUERY_PREFIX = "Find the most relevant code snippet given the following query:\n"
JINA_CODE_DOCUMENT_PREFIX = "Candidate code snippet:\n"

#: Retries for one embedding request. A local server drops a request now and then (model reload,
#: memory pressure); a transient failure should not cost the whole indexing run.
_HTTP_ATTEMPTS = 3
_HTTP_RETRY_SECONDS = 2.0


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    """POST ``body`` as JSON and return the decoded response; the seam tests monkeypatch."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def default_prefixes(model: str) -> tuple[str, str]:
    """``(query_prefix, document_prefix)`` the encoder uses when settings leave them unset."""
    if "jina-code" in model.lower():
        return JINA_CODE_QUERY_PREFIX, JINA_CODE_DOCUMENT_PREFIX
    return "", ""


def _fit_dimension(vector: list[float], dim: int, model: str) -> list[float]:
    """Cut a wider (Matryoshka) vector to ``dim`` and re-normalise; refuse a narrower one."""
    if len(vector) == dim:
        return vector
    if len(vector) < dim:
        raise EncoderConfigError(
            f"{model} returned {len(vector)}-dimensional vectors but BCE_EMBEDDING_DIM={dim}. "
            f"Set BCE_EMBEDDING_DIM={len(vector)} (and re-run `bce migrate`, then reindex)."
        )
    head = vector[:dim]
    norm = math.sqrt(sum(v * v for v in head))
    return [v / norm for v in head] if norm else head


class OpenAICompatEncoder(Encoder):
    """Embeddings from an OpenAI-compatible ``/v1/embeddings`` endpoint (vLLM, TEI, Ollama, ...).

    The wire format has no ``input_type``; instruction-tuned models express the query/document
    asymmetry as a text prefix instead, so the encoder prepends ``query_prefix`` at search time
    and ``document_prefix`` at index time. ``model_id`` pins model + dimension like the Voyage
    encoder does. Vectors wider than ``dim`` are truncated and re-normalised (Matryoshka
    training makes leading dimensions self-contained); narrower ones are a configuration error.

    Only the standard library is used, so the provider needs no extra install.
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        dim: int,
        api_key: str = "",
        query_prefix: str | None = None,
        document_prefix: str | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 600.0,
        query_timeout: float | None = None,
    ) -> None:
        if not base_url.strip():
            raise EncoderConfigError(
                "BCE_EMBEDDING_PROVIDER=openai needs BCE_EMBEDDING_BASE_URL (for example "
                "http://127.0.0.1:8001/v1 for a local vLLM server)."
            )
        if not model.strip():
            raise EncoderConfigError(
                "BCE_EMBEDDING_PROVIDER=openai needs BCE_EMBEDDING_MODEL: the name the server "
                "serves the model under (vLLM: --served-model-name)."
            )
        self.url = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.dim = dim
        self.model_id = f"{model}-{dim}"
        auto_query, auto_document = default_prefixes(model)
        self.query_prefix = auto_query if query_prefix is None else query_prefix
        self.document_prefix = auto_document if document_prefix is None else document_prefix
        self.extra_body = dict(extra_body or {})
        self.timeout = timeout
        # Query-time embedding is one short text: bound the wait so a search tool call fails (and
        # can degrade to lexical) instead of hanging on a stalled connection. None = same as timeout.
        self.query_timeout = min(query_timeout, timeout) if query_timeout else timeout
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _request(
        self, texts: list[str], *, timeout: float | None = None, attempts: int = _HTTP_ATTEMPTS
    ) -> list[list[float]]:
        body = {**self.extra_body, "model": self.model, "input": texts}
        wait = self.timeout if timeout is None else timeout
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                payload = _post_json(self.url, body, self._headers, wait)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code < 500:
                    # The request itself is wrong (unknown model, too long, bad field): retrying
                    # cannot help, and the server's message says what to fix.
                    raise RuntimeError(
                        f"embedding request rejected ({exc.code}): {detail}"
                    ) from exc
                last = RuntimeError(f"embedding server error ({exc.code}): {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = RuntimeError(f"embedding server unreachable at {self.url}: {exc}")
            if attempt < attempts:
                time.sleep(_HTTP_RETRY_SECONDS * attempt)
        else:
            assert last is not None
            raise last
        rows = sorted(payload["data"], key=lambda item: item["index"])
        if len(rows) != len(texts):
            raise RuntimeError(
                f"embedding server returned {len(rows)} vectors for {len(texts)} texts"
            )
        return [_fit_dimension(list(row["embedding"]), self.dim, self.model) for row in rows]

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _split_batches([prefix + text for text in texts]):
            vectors.extend(self._request(batch))
        return vectors

    def encode(self, text: str) -> list[float]:
        return self._embed([text], self.document_prefix)[0]

    def encode_query(self, text: str) -> list[float]:
        # two bounded attempts: a stalled connection costs at most ~2 x query_timeout + 2 s
        return self._request([self.query_prefix + text], timeout=self.query_timeout, attempts=2)[0]

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed(list(texts), self.document_prefix)


def build_default_encoder(dim: int | None = None, model_id: str | None = None) -> Encoder:
    """Encoder from settings: the selected remote provider, else the deterministic hashing encoder.

    Reads :class:`bce.config.Settings` for the provider/model/dim/key. Explicit ``dim``/``model_id``
    args override settings (used by tests); otherwise the hashing encoder adopts the settings dim so
    its vectors match the pinned pgvector column width.

    A provider that is selected but unusable raises :class:`EncoderConfigError` at every call site
    rather than only on the indexing side, so a half-configured Voyage setup cannot index with one
    encoder and search with another.
    """
    from bce.config import get_settings

    settings = get_settings()
    provider = settings.embedding_provider.strip().lower()

    if provider == "voyage":
        if not settings.voyage_api_key:
            raise EncoderConfigError(
                "BCE_EMBEDDING_PROVIDER=voyage needs BCE_VOYAGE_API_KEY, which is empty. "
                "Set the key - or set BCE_EMBEDDING_PROVIDER=hashing to embed offline."
            )
        return VoyageEncoder(
            settings.voyage_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )

    if provider == "openai":
        return OpenAICompatEncoder(
            settings.embedding_base_url,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            api_key=settings.embedding_api_key,
            query_prefix=settings.embedding_query_prefix,
            document_prefix=settings.embedding_document_prefix,
            extra_body=settings.embedding_extra_body,
            timeout=settings.embedding_timeout,
            query_timeout=settings.embedding_query_timeout,
        )

    if provider != "hashing":
        raise EncoderConfigError(
            f"Unknown BCE_EMBEDDING_PROVIDER {settings.embedding_provider!r}. "
            "Supported values are 'hashing', 'voyage' and 'openai'."
        )

    fallback_dim = dim if dim is not None else settings.embedding_dim
    if model_id and model_id != "unset":
        return HashingEncoder(dim=fallback_dim, model_id=model_id)
    return HashingEncoder(dim=fallback_dim)
