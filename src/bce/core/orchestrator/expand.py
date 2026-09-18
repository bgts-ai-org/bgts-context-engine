"""Deterministic expansion (spec section 6.3).

From each anchor, a fixed, rule-based graph walk produces the candidate set. Three rules guarantee
determinism:

1. Fixed template - always the same edge types, direction, and hop limits:
   CALLS callers 2-hop + callees 1-hop, INHERITS/IMPLEMENTS both directions (full), same-file
   sibling symbols (strong anchors only, nearest-by-line, capped). (IMPORTS is file-level.)
2. Deterministic ordering - candidates carry a graph_distance; final ordering is left to scoring
   (section 6.4), which is itself deterministic.
3. Deterministic de-dup + bound - a symbol reached by multiple paths keeps its smallest distance;
   cycles are cut by a visited-set; hop limits are fixed.

Expansion is *budgeted and evidence-carrying*. Only anchors with real evidence behind them are
walked (:data:`EXPAND_MIN_STRENGTH`, :data:`EXPAND_ROOT_LIMIT`); the rest stay in the pool at
distance 0 without dragging their neighbourhoods in. Every node records the strength of the anchor
that reached it, decayed once per hop (:data:`HOP_DECAY`), so scoring can tell the first hop off
the best-evidenced anchor from the first hop off a generic name.

Output: a mapping symbol_id -> ExpansionInfo(distance, is_anchor, anchor_strength, source_strength,
ref_kind, provenance), ready to become scoring Candidates via :func:`to_candidates`, which also
attaches the graph features (degree, leaf) and the task signal in a single pass.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bce.core.orchestrator import bulk
from bce.core.orchestrator.anchors import AnchorResult
from bce.core.orchestrator.text import (
    MIN_TOKEN_LEN,
    STOPWORDS,
    is_test_symbol,
    query_terms,
    split_identifier,
)
from bce.core.scoring.engine import CONTAINER_KINDS, Candidate
from bce.storage.graph.repository import GraphRepository

_CALLERS_HOPS = 2
_CALLEES_HOPS = 1

#: Evidence is multiplied by this once per hop, so a neighbour of a 0.9 anchor outranks a
#: neighbour of a 0.2 one instead of both being "distance 1".
HOP_DECAY = 0.6

#: Anchors below this strength are kept as candidates but never expanded from: walking a
#: single-term lexical hit costs a caller/referrer/sibling fan-out and returns its whole
#: neighbourhood as if the name had been meant.
EXPAND_MIN_STRENGTH = 0.3
#: Hard cap on expansion roots (strongest first). A task naming a dozen real symbols already
#: produces a pool of thousands; beyond that the extra roots only add noise and latency.
EXPAND_ROOT_LIMIT = 20

#: Same-file siblings are only pulled in for anchors at least this strong: a weak, single-term
#: lexical hit must not drag its whole file into the candidate pool.
SIBLING_MIN_STRENGTH = 0.7
#: Max siblings per anchor (nearest by line, then symbol_id).
SIBLING_LIMIT = 12

#: Partial task-signal credit (fraction of the name's parts that appear in the task text).
_PARTIAL_SIGNAL_SCALE = 0.6


@dataclass(slots=True)
class ExpansionInfo:
    symbol_id: str
    repo_id: str | None
    distance: int
    is_anchor: bool
    anchor_strength: float = 0.0
    #: Anchor evidence that reached this node, decayed per hop (see :data:`HOP_DECAY`).
    source_strength: float = 0.0
    ref_kind: str | None = None
    provenance: str | None = None


def expand_from_anchors(
    repository: GraphRepository,
    anchor_ids: Iterable[str] | AnchorResult,
    *,
    strengths: dict[str, float] | None = None,
) -> dict[str, ExpansionInfo]:
    """Fixed-template, bounded, deterministic expansion. Returns candidates keyed by symbol_id.

    Accepts either an :class:`AnchorResult` (strengths read from it) or a plain id iterable with an
    optional ``strengths`` map (missing -> 1.0, i.e. fully trusted anchors).
    """
    if isinstance(anchor_ids, AnchorResult):
        strengths = dict(anchor_ids.strength)
        roots = sorted(anchor_ids.anchors)
    else:
        roots = sorted(set(anchor_ids))
        strengths = dict(strengths or {})

    found: dict[str, ExpansionInfo] = {}

    def record(
        sid: str,
        distance: int,
        *,
        anchor: bool,
        provenance: str | None,
        ref_kind: str | None = None,
        source_strength: float = 0.0,
    ) -> None:
        """Insert/keep-smallest-distance and keep-strongest-evidence.

        ref_kind, once set from a REFERENCES edge, is retained (stable per node) so scoring gets a
        deterministic define/write/read/pass signal regardless of traversal order.
        """
        existing = found.get(sid)
        if existing is None:
            found[sid] = ExpansionInfo(
                symbol_id=sid,
                repo_id=None,
                distance=distance,
                is_anchor=anchor,
                anchor_strength=strengths.get(sid, 1.0) if anchor else 0.0,
                source_strength=round(source_strength, 6),
                ref_kind=ref_kind,
                provenance=provenance,
            )
            return
        if distance < existing.distance:
            existing.distance = distance
        if anchor:
            existing.is_anchor = True
            existing.anchor_strength = max(existing.anchor_strength, strengths.get(sid, 1.0))
        existing.source_strength = round(max(existing.source_strength, source_strength), 6)
        if existing.ref_kind is None and ref_kind is not None:
            existing.ref_kind = ref_kind

    for sid in roots:
        strength = strengths.get(sid, 1.0)
        record(sid, 0, anchor=True, provenance=None, source_strength=strength)

    walked = expansion_roots(roots, strengths)

    # Callers up to 2 hops (who depends on the anchor -> likely change sites).
    _walk(repository, walked, _CALLERS_HOPS, "callers_of", "get_callers", record)
    # Callees 1 hop (what the anchor uses).
    _walk(repository, walked, _CALLEES_HOPS, "callees_of", "get_callees", record)

    # Referrers (REFERENCES edges) 1 hop: define/write/read/pass usages -> tag ref_kind (§6.4).
    referrers = bulk.neighbours(repository, sorted(walked), "referrers_of", "get_referrers")
    for sid, strength in sorted(walked.items()):
        for ref in referrers.get(sid, []):
            record(
                ref["symbol_id"],
                1,
                anchor=False,
                provenance=ref.get("provenance"),
                ref_kind=ref.get("ref_kind"),
                source_strength=strength * HOP_DECAY,
            )

    # Type hierarchy (full, both directions), 1 hop from anchors. Implementers are exactly the
    # INHERITS/IMPLEMENTS subtypes, so the subtype channel already covers them.
    ids = sorted(walked)
    supertypes = bulk.neighbours(repository, ids, "supertypes_of", "get_supertypes")
    subtypes = bulk.neighbours(repository, ids, "subtypes_of", "get_subtypes")
    for sid, strength in sorted(walked.items()):
        for group in (supertypes, subtypes):
            for rel in group.get(sid, []):
                record(
                    rel["symbol_id"],
                    1,
                    anchor=False,
                    provenance=rel.get("provenance"),
                    source_strength=strength * HOP_DECAY,
                )

    _expand_siblings(repository, walked, record)

    _fill_repo_ids(repository, found)
    return found


def expansion_roots(roots: list[str], strengths: dict[str, float]) -> dict[str, float]:
    """The anchors worth walking: strong enough, and at most :data:`EXPAND_ROOT_LIMIT` of them.

    Returns ``symbol_id -> strength`` (deterministic: strongest first, ties by symbol_id).
    """
    eligible = [sid for sid in roots if strengths.get(sid, 1.0) >= EXPAND_MIN_STRENGTH]
    eligible.sort(key=lambda sid: (-strengths.get(sid, 1.0), sid))
    return {sid: strengths.get(sid, 1.0) for sid in sorted(eligible[:EXPAND_ROOT_LIMIT])}


def _expand_siblings(repository: GraphRepository, walked: dict[str, float], record: Any) -> None:
    """Same-file symbols near a well-evidenced anchor (they tend to change together).

    Skipped for containers: a class's "siblings" are the other top-level types of the file, while
    its *members* already arrive through the graph - expanding it would flood the pool twice over.
    """
    strong = {sid: s for sid, s in walked.items() if s >= SIBLING_MIN_STRENGTH}
    if not strong:
        return
    meta = bulk.symbols_meta(repository, sorted(strong))
    anchors_by_file: dict[str, list[tuple[str, float]]] = {}
    for sid in sorted(strong):
        info = meta.get(sid) or {}
        if (info.get("kind") or "").lower() in CONTAINER_KINDS:
            continue
        file_id = info.get("file_id")
        if file_id:
            anchors_by_file.setdefault(file_id, []).append((sid, strong[sid]))
    if not anchors_by_file:
        return

    in_file = bulk.symbols_in_files(repository, sorted(anchors_by_file))
    for file_id in sorted(anchors_by_file):
        candidates = [s for s in in_file.get(file_id, []) if s.get("symbol_id")]
        for sid, strength in anchors_by_file[file_id]:
            anchor_line = _as_int((meta.get(sid) or {}).get("line"))
            siblings = [s for s in candidates if s["symbol_id"] != sid]
            siblings.sort(key=lambda s: (abs(_as_int(s.get("line")) - anchor_line), s["symbol_id"]))
            for sibling in siblings[:SIBLING_LIMIT]:
                record(
                    sibling["symbol_id"],
                    1,
                    anchor=False,
                    provenance=None,
                    source_strength=strength * HOP_DECAY,
                )


def _walk(
    repository: GraphRepository,
    roots: dict[str, float],
    hops: int,
    bulk_name: str,
    single_name: str,
    record: Any,
) -> None:
    """Breadth-first over one edge channel, carrying decayed anchor evidence."""
    visited = set(roots)
    frontier = dict(roots)
    for depth in range(1, hops + 1):
        if not frontier:
            return
        neighbours = bulk.neighbours(repository, sorted(frontier), bulk_name, single_name)
        next_frontier: dict[str, float] = {}
        for node_id in sorted(frontier):
            strength = frontier[node_id] * HOP_DECAY
            for neigh in neighbours.get(node_id, []):
                nid = neigh.get("symbol_id")
                if not nid or nid in visited:
                    continue
                visited.add(nid)
                record(
                    nid,
                    depth,
                    anchor=False,
                    provenance=neigh.get("provenance"),
                    source_strength=strength,
                )
                next_frontier[nid] = strength
        frontier = next_frontier


def _fill_repo_ids(repository: GraphRepository, found: dict[str, ExpansionInfo]) -> None:
    """Attach repo_ids in one query (the scope filter reads them) instead of one per node."""
    repos = bulk.repos_of_symbols(repository, sorted(found))
    for sid, info in found.items():
        info.repo_id = repos.get(sid)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def task_signal_for(name: str | None, task_tokens: set[str]) -> float:
    """Deterministic task-relevance of a symbol name against the task's content tokens.

    1.0 when the whole name appears in the task; otherwise a partial credit proportional to the
    share of the name's snake/camel parts that appear (stop-words and short parts ignored), so
    ``diff_command`` still lights up for a task mentioning "command".
    """
    if not name or not task_tokens:
        return 0.0
    lowered = name.lower()
    if lowered in task_tokens:
        return 1.0
    parts = [p for p in split_identifier(name) if len(p) >= MIN_TOKEN_LEN and p not in STOPWORDS]
    if not parts:
        return 0.0
    hit = sum(1 for p in parts if p in task_tokens)
    if hit == 0:
        return 0.0
    if hit == len(parts):
        return 1.0  # every meaningful part named in the task ("diff command" ~ diff_command)
    return round(_PARTIAL_SIGNAL_SCALE * hit / len(parts), 6)


def to_candidates(
    repository: GraphRepository,
    expansion: dict[str, ExpansionInfo],
    *,
    anchors: AnchorResult | None = None,
    task_signals: dict[str, float] | None = None,
    task_text: str | None = None,
    task_tokens: set[str] | None = None,
) -> list[Candidate]:
    """Turn expansion info into scoring candidates, filling degree/leaf/test/task-signal features.

    Uses ``repository.symbol_features(ids)`` (one chunked bulk query set) when the repository
    offers it; otherwise falls back to per-symbol lookups (unit-test fakes). Task signals are
    computed here for *every* candidate - not only a preliminary top slice - from ``task_tokens``
    (or ``task_text``); explicit ``task_signals`` override per symbol. ``anchors`` carries the
    per-symbol source tags and the model's ranks through to narrowing.
    """
    task_signals = task_signals or {}
    if task_tokens is None:
        task_tokens = set(query_terms(task_text or ""))
    sources = dict(anchors.anchors) if anchors is not None else {}
    semantic_ranks = dict(anchors.semantic_rank) if anchors is not None else {}

    ids = sorted(expansion)
    features = _fetch_features(repository, ids)

    candidates: list[Candidate] = []
    for sid in ids:
        info = expansion[sid]
        feat = features.get(sid, {})
        name = feat.get("name")
        file_id = feat.get("file_id")
        signal = task_signals.get(sid)
        if signal is None:
            signal = task_signal_for(name, task_tokens)
        candidates.append(
            Candidate(
                symbol_id=sid,
                repo_id=info.repo_id,
                ref_kind=info.ref_kind,
                provenance=info.provenance,
                graph_distance=info.distance,
                degree=int(feat.get("degree", 0) or 0),
                is_leaf=bool(feat.get("has_callers")) and not feat.get("has_callees"),
                task_signal=float(signal),
                anchor=info.is_anchor,
                anchor_strength=info.anchor_strength,
                source_strength=info.source_strength,
                sources=list(sources.get(sid) or []),
                semantic_rank=semantic_ranks.get(sid),
                churn=int(feat.get("churn") or 0),
                is_test=is_test_symbol(sid, name, file_id),
                name=name,
                kind=feat.get("kind"),
                file_id=file_id,
            )
        )
    return candidates


def _fetch_features(repository: GraphRepository, ids: list[str]) -> dict[str, dict[str, Any]]:
    bulk = getattr(repository, "symbol_features", None)
    if callable(bulk):
        try:
            return bulk(ids)
        except NotImplementedError:
            pass
    out: dict[str, dict[str, Any]] = {}
    for sid in ids:
        sym = repository.get_symbol(sid) or {}
        callees = repository.get_callees(sid)
        callers = repository.get_callers(sid)
        out[sid] = {
            "name": sym.get("name"),
            "kind": sym.get("kind"),
            "file_id": sym.get("file_id"),
            "line": sym.get("line"),
            "degree": repository.symbol_degree(sid),
            "has_callees": bool(callees),
            "has_callers": bool(callers),
        }
    return out
