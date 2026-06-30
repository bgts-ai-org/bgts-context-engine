"""Embedder (write path).

Phase 2 component. Computes symbol/file embeddings (signature + docstring + optional body summary)
and writes them to pgvector in the same transaction as the graph upsert, so graph and vectors stay
consistent. The model identifier and dimension are pinned for reproducibility (P2).
"""
