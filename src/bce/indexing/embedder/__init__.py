"""Embedder (write path).

Phase 2 component. Computes symbol/file embeddings (signature + docstring + optional body summary)
and writes them to pgvector in the same transaction as the graph upsert, so graph and vectors stay
consistent. The model identifier and dimension are pinned for reproducibility (P2).
"""

from bce.indexing.embedder.embedder import Embedder
from bce.indexing.embedder.encoder import (
    Encoder,
    EncoderConfigError,
    HashingEncoder,
    OpenAICompatEncoder,
    VoyageEncoder,
    build_default_encoder,
)

__all__ = [
    "Embedder",
    "Encoder",
    "EncoderConfigError",
    "HashingEncoder",
    "OpenAICompatEncoder",
    "VoyageEncoder",
    "build_default_encoder",
]
