"""Layer-2 hybrid retrieval tools (spec section 7).

These are the only place the engine touches a model (the embedding encoder), and even then strictly
to *find anchors* (P2), never to produce context. Ranking is deterministic:

- ``semantic_search``: cosine nearest neighbours from pgvector (order: distance, then ref_id).
- ``hybrid_search``: a fixed-weight blend of lexical (keyword) + semantic + a light structural
  (degree) signal, combined into one score; deterministic tie-breaks by symbol_id.
- ``find_similar_code``: embed a code fragment, return nearest symbols.

Every payload is language-neutral and carries indexed_at_commit where available; only ``message`` is
localized (P1 i18n boundary).
"""

from __future__ import annotations

from typing import Any

from cce.core.i18n import get_translator
from cce.indexing.embedder.encoder import Encoder, build_default_encoder
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore

# Fixed, versioned blend weights for hybrid_search (determinism: never data-dependent).
_W_LEXICAL = 0.5
_W_SEMANTIC = 0.4
_W_STRUCTURAL = 0.1
_HYBRID_WEIGHTS_VERSION = "hybrid-v1"


def _default_encoder() -> Encoder:
    """The same encoder indexing uses (settings-driven), so query + index vectors are comparable."""
    return build_default_encoder()


def semantic_search(
    store: VectorStore,
    query: str,
    *,
    repo_ids: list[str] | None = None,
    limit: int = 10,
    encoder: Encoder | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Cosine nearest symbols for a natural-language query (anchor finding, P2)."""
    tr = get_translator()
    loc = tr.resolve(locale)
    enc = encoder or _default_encoder()
    vector = enc.encode_query(query)
    hits = store.search(vector, limit=limit, repo_ids=repo_ids, kind="symbol")
    candidates = [
        {
            "symbol_id": h["ref_id"],
            "repo_id": h["repo_id"],
            "distance": round(h["distance"], 6),
            "indexed_at_commit": h["indexed_at_commit"],
        }
        for h in hits
    ]
    message = tr.translate("tool.semantic_search.count", loc, count=len(candidates), query=query)
    return {
        "tool": "semantic_search",
        "payload": {"query": query, "model": enc.model_id, "candidates": candidates},
        "message": message,
        "locale": loc,
    }


def hybrid_search(
    repository: GraphRepository,
    store: VectorStore,
    query: str,
    *,
    repo_ids: list[str] | None = None,
    limit: int = 10,
    encoder: Encoder | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Keyword + semantic + structural blend into one deterministic score."""
    tr = get_translator()
    loc = tr.resolve(locale)
    enc = encoder or _default_encoder()

    pool = max(limit * 3, 30)
    lexical = repository.lexical_search(query, repo_ids=repo_ids, limit=pool)
    semantic = store.search(enc.encode_query(query), limit=pool, repo_ids=repo_ids, kind="symbol")

    scores: dict[str, dict[str, Any]] = {}

    # Lexical: rank-normalized (best keyword hit = 1.0), deterministic by list order.
    n_lex = len(lexical)
    for rank, row in enumerate(lexical):
        sid = row["symbol_id"]
        lex_score = (n_lex - rank) / n_lex if n_lex else 0.0
        scores.setdefault(sid, _blank(sid, row))
        scores[sid]["lexical"] = lex_score

    # Semantic: convert cosine distance (0=identical) to similarity in [0,1].
    for hit in semantic:
        sid = hit["ref_id"]
        sim = max(0.0, 1.0 - hit["distance"])
        entry = scores.setdefault(sid, _blank(sid, {"symbol_id": sid, "repo_id": hit["repo_id"]}))
        entry["semantic"] = sim
        entry.setdefault("indexed_at_commit", hit["indexed_at_commit"])

    # Structural: light degree signal, capped so it only breaks near-ties.
    for sid, entry in scores.items():
        degree = repository.symbol_degree(sid)
        entry["structural"] = min(degree / 20.0, 1.0)
        entry["score"] = round(
            _W_LEXICAL * entry["lexical"]
            + _W_SEMANTIC * entry["semantic"]
            + _W_STRUCTURAL * entry["structural"],
            6,
        )

    ranked = sorted(scores.values(), key=lambda e: (-e["score"], e["symbol_id"]))[:limit]
    message = tr.translate("tool.hybrid_search.count", loc, count=len(ranked), query=query)
    return {
        "tool": "hybrid_search",
        "payload": {
            "query": query,
            "model": enc.model_id,
            "weights_version": _HYBRID_WEIGHTS_VERSION,
            "candidates": ranked,
        },
        "message": message,
        "locale": loc,
    }


def _blank(sid: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "repo_id": row.get("repo_id"),
        "lexical": 0.0,
        "semantic": 0.0,
        "structural": 0.0,
        "score": 0.0,
        "indexed_at_commit": row.get("indexed_at_commit"),
    }


def find_similar_code(
    store: VectorStore,
    code: str,
    *,
    repo_ids: list[str] | None = None,
    limit: int = 10,
    encoder: Encoder | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Embed a code fragment and return the nearest symbols (implementation lookalikes)."""
    tr = get_translator()
    loc = tr.resolve(locale)
    enc = encoder or _default_encoder()
    hits = store.search(enc.encode(code), limit=limit, repo_ids=repo_ids, kind="symbol")
    candidates = [
        {
            "symbol_id": h["ref_id"],
            "repo_id": h["repo_id"],
            "distance": round(h["distance"], 6),
            "indexed_at_commit": h["indexed_at_commit"],
        }
        for h in hits
    ]
    message = tr.translate("tool.find_similar_code.count", loc, count=len(candidates))
    return {
        "tool": "find_similar_code",
        "payload": {"model": enc.model_id, "candidates": candidates},
        "message": message,
        "locale": loc,
    }
