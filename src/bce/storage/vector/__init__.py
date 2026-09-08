"""pgvector access for embeddings.

Phase 0 only provisions the schema (see migration 0003). The embedder and similarity search land in
Phase 2, where embeddings are used strictly for anchor finding (P2) and the model + index versions
are pinned for reproducibility.
"""

from bce.storage.vector.store import VectorStore

__all__ = ["VectorStore"]
