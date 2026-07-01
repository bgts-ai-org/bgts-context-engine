"""Embedding encoders (anchor finding only, P2).

An encoder maps text to a fixed-dimension unit vector. Per P2 the embedding is a *seed* (a way into
the graph), never the answer, and per the determinism rules the model identity is pinned so results
are reproducible within a snapshot.

Two encoders ship:

- :class:`HashingEncoder` - dependency-free, fully deterministic (a hashed bag-of-tokens projected
  to the unit sphere). It is the default "lexical safety-net" fallback the plan calls for: no heavy
  model is required to run semantic search, and its output is byte-stable across machines.
- :class:`Encoder` - the abstract contract. A real transformer encoder (self-hosted, on-prem) can
  implement this later without touching the rest of the pipeline; its ``model_id`` must be pinned.

The encoder used by indexing and search MUST match (same ``model_id`` + dimension), otherwise a
version bump is a determinism-regression boundary and requires a reindex.
"""

from __future__ import annotations

import abc
import hashlib
import math
import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class Encoder(abc.ABC):
    """Maps text to a pinned, fixed-dimension embedding vector."""

    model_id: str = "abstract"
    dim: int = 768

    @abc.abstractmethod
    def encode(self, text: str) -> list[float]:
        """Return a ``dim``-length embedding for ``text`` (deterministic for a fixed model)."""

    def encode_many(self, texts: list[str]) -> list[list[float]]:
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

    def __init__(self, dim: int = 768, model_id: str = "cce-hashing-v1") -> None:
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


def build_default_encoder(dim: int = 768, model_id: str | None = None) -> Encoder:
    """Default encoder: the deterministic hashing encoder (lexical fallback per the plan)."""
    if model_id and model_id != "unset":
        return HashingEncoder(dim=dim, model_id=model_id)
    return HashingEncoder(dim=dim)
