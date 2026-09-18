"""Embedding encoders (anchor finding only, P2).

An encoder maps text to a fixed-dimension unit vector. Per P2 the embedding is a *seed* (a way into
the graph), never the answer, and per the determinism rules the model identity is pinned so results
are reproducible within a snapshot.

Three encoders ship:

- :class:`HashingEncoder` - dependency-free, fully deterministic (a hashed bag-of-tokens projected
  to the unit sphere). It is the default "lexical safety-net" fallback the plan calls for: no heavy
  model is required to run semantic search, and its output is byte-stable across machines.
- :class:`VoyageEncoder` - Voyage AI code embeddings (``voyage-code-3``, 1024 dims). It distinguishes
  ``document`` (indexing) from ``query`` (search) input types. Its outputs are written into pgvector
  at index time and pinned there; since embeddings only *find anchors* (P2), the deterministic
  payload is unaffected by any minor API float variation.
- :class:`Encoder` - the abstract contract. Any other on-prem encoder can implement this without
  touching the rest of the pipeline; its ``model_id`` must be pinned.

The encoder used by indexing and search MUST match (same ``model_id`` + dimension), otherwise a
version bump is a determinism-regression boundary and requires a reindex.
"""

from __future__ import annotations

import abc
import hashlib
import math
import re

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


def build_default_encoder(dim: int | None = None, model_id: str | None = None) -> Encoder:
    """Encoder from settings: Voyage when it is selected, else the deterministic hashing encoder.

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

    if provider != "hashing":
        raise EncoderConfigError(
            f"Unknown BCE_EMBEDDING_PROVIDER {settings.embedding_provider!r}. "
            "Supported values are 'hashing' and 'voyage'."
        )

    fallback_dim = dim if dim is not None else settings.embedding_dim
    if model_id and model_id != "unset":
        return HashingEncoder(dim=fallback_dim, model_id=model_id)
    return HashingEncoder(dim=fallback_dim)
