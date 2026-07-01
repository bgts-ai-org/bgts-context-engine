"""Layer 2 - hybrid retrieval tools (spec section 7).

``semantic_search`` (pgvector), ``hybrid_search`` (keyword + semantic + graph signal), and
``find_similar_code`` (embed a code fragment, find nearest symbols). Per P2 these only find anchors
into the graph; they never produce the final context themselves.
"""

from cce.tools.layer2.search import find_similar_code, hybrid_search, semantic_search

__all__ = ["semantic_search", "hybrid_search", "find_similar_code"]
