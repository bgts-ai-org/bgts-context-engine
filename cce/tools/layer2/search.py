"""Layer-2 hybrid retrieval tools (spec section 7).

These are the only place the engine touches a model (the embedding encoder), and even then strictly
to *find anchors* (P2), never to produce context. Ranking is deterministic:

- ``semantic_search``: cosine nearest neighbours from pgvector (order: distance, then ref_id).
- ``hybrid_search`` (hybrid-v2): token-based lexical coverage + semantic similarity blended with
  fixed weights (re-normalized when a channel is empty), an exact-name boost, and the structural
  degree signal reduced to a tie-breaker; deterministic tie-breaks by symbol_id.
- ``find_similar_code``: embed a code fragment, return nearest symbols.

Every payload is language-neutral and carries indexed_at_commit where available; only ``message`` is
localized (P1 i18n boundary).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from cce.core.i18n import get_translator
from cce.indexing.embedder.encoder import Encoder, build_default_encoder
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore

# Fixed, versioned blend weights for hybrid_search (determinism: never data-dependent).
# v2: lexical is token-coverage based; structural is only a tie-breaker (epsilon-scaled) so
# high-degree but irrelevant symbols can no longer outrank real matches.
_W_LEXICAL = 0.55
_W_SEMANTIC = 0.45
_STRUCTURAL_EPSILON = 0.001
_EXACT_NAME_BOOST = 0.15
_HYBRID_WEIGHTS_VERSION = "hybrid-v2"

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

logger = logging.getLogger("cce.tools.layer2")


def _query_tokens(query: str) -> list[str]:
    """Identifier-like tokens (length>=3) in first-seen order, lowercased, de-duplicated."""
    seen: list[str] = []
    for token in _TOKEN_RE.findall(query or ""):
        lowered = token.lower()
        if len(lowered) >= 3 and lowered not in seen:
            seen.append(lowered)
    return seen


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
    t0 = time.perf_counter()
    vector = enc.encode_query(query)
    encode_ms = round((time.perf_counter() - t0) * 1000, 2)
    t1 = time.perf_counter()
    hits = store.search(vector, limit=limit, repo_ids=repo_ids, kind="symbol")
    logger.debug(
        "semantic_search",
        extra={
            "query": query,
            "model": enc.model_id,
            "hits": len(hits),
            "encode_ms": encode_ms,
            "search_ms": round((time.perf_counter() - t1) * 1000, 2),
        },
    )
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
    """Token-based lexical coverage + semantic similarity, deterministically blended (hybrid-v2)."""
    tr = get_translator()
    loc = tr.resolve(locale)
    enc = encoder or _default_encoder()
    t0 = time.perf_counter()

    pool = max(limit * 3, 30)
    tokens = _query_tokens(query)
    query_l = (query or "").strip().lower()

    # Lexical: search per token; a symbol's score is the fraction of query tokens its name covers.
    # This makes natural-language queries contribute (full-phrase substring matching scored 0.0).
    matched_tokens: dict[str, set[str]] = {}
    lexical_rows: dict[str, dict[str, Any]] = {}
    for token in tokens or ([query_l] if query_l else []):
        for row in repository.lexical_search(token, repo_ids=repo_ids, limit=pool):
            sid = row["symbol_id"]
            lexical_rows.setdefault(sid, row)
            matched_tokens.setdefault(sid, set()).add(token)
    lexical_ms = round((time.perf_counter() - t0) * 1000, 2)

    t1 = time.perf_counter()
    semantic = store.search(enc.encode_query(query), limit=pool, repo_ids=repo_ids, kind="symbol")
    logger.debug(
        "hybrid_search: channels",
        extra={
            "query": query,
            "tokens": tokens,
            "lexical_rows": len(lexical_rows),
            "semantic_hits": len(semantic),
            "lexical_ms": lexical_ms,
            "semantic_ms": round((time.perf_counter() - t1) * 1000, 2),
        },
    )

    scores: dict[str, dict[str, Any]] = {}

    denom = max(len(tokens), 1)
    for sid in sorted(lexical_rows):
        row = lexical_rows[sid]
        entry = scores.setdefault(sid, _blank(sid, row))
        entry["lexical"] = round(len(matched_tokens[sid]) / denom, 6)
        entry["name"] = row.get("name")

    # Semantic: convert cosine distance (0=identical) to similarity in [0,1].
    for hit in semantic:
        sid = hit["ref_id"]
        sim = max(0.0, 1.0 - hit["distance"])
        entry = scores.setdefault(sid, _blank(sid, {"symbol_id": sid, "repo_id": hit["repo_id"]}))
        entry["semantic"] = sim
        entry.setdefault("indexed_at_commit", hit["indexed_at_commit"])

    # Re-normalize weights when a channel contributed nothing (its weight must not stay dead).
    has_lexical = any(e["lexical"] > 0.0 for e in scores.values())
    has_semantic = any(e["semantic"] > 0.0 for e in scores.values())
    w_lex, w_sem = _effective_weights(has_lexical, has_semantic)

    token_set = set(tokens)
    for sid, entry in scores.items():
        degree = repository.symbol_degree(sid)
        entry["structural"] = min(degree / 20.0, 1.0)
        if entry.get("name") is None:
            symbol = repository.get_symbol(sid)
            if symbol:
                entry["name"] = symbol.get("name")
        name_l = str(entry.get("name") or "").lower()
        exact = bool(name_l) and (name_l == query_l or name_l in token_set)
        entry["exact_name"] = exact
        entry["score"] = round(
            w_lex * entry["lexical"]
            + w_sem * entry["semantic"]
            + (_EXACT_NAME_BOOST if exact else 0.0)
            # Structural degree only breaks near-ties; it can no longer promote noisy hubs.
            + _STRUCTURAL_EPSILON * entry["structural"],
            6,
        )

    ranked = sorted(scores.values(), key=lambda e: (-e["score"], e["symbol_id"]))[:limit]
    logger.debug(
        "hybrid_search: ranked",
        extra={
            "query": query,
            "scored": len(scores),
            "returned": len(ranked),
            "total_ms": round((time.perf_counter() - t0) * 1000, 2),
        },
    )
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


def _effective_weights(has_lexical: bool, has_semantic: bool) -> tuple[float, float]:
    """Fixed weights, re-normalized over the channels that actually contributed."""
    if has_lexical and has_semantic:
        return _W_LEXICAL, _W_SEMANTIC
    if has_lexical:
        return 1.0, 0.0
    if has_semantic:
        return 0.0, 1.0
    return 0.0, 0.0


def _blank(sid: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "repo_id": row.get("repo_id"),
        "name": row.get("name"),
        "lexical": 0.0,
        "semantic": 0.0,
        "structural": 0.0,
        "exact_name": False,
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
    t0 = time.perf_counter()
    hits = store.search(enc.encode(code), limit=limit, repo_ids=repo_ids, kind="symbol")
    logger.debug(
        "find_similar_code",
        extra={
            "code_chars": len(code or ""),
            "hits": len(hits),
            "total_ms": round((time.perf_counter() - t0) * 1000, 2),
        },
    )
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
