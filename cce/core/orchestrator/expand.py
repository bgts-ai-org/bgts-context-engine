"""Deterministic expansion (spec section 6.3).

From each anchor, a fixed, rule-based graph walk produces the candidate set. Three rules guarantee
determinism:

1. Fixed template - always the same edge types, direction, and hop limits:
   CALLS callers 2-hop + callees 1-hop, INHERITS/IMPLEMENTS both directions (full), same-file
   sibling symbols. (IMPORTS is used at the file level elsewhere.)
2. Deterministic ordering - candidates carry a graph_distance; final ordering is left to scoring
   (section 6.4), which is itself deterministic.
3. Deterministic de-dup + bound - a symbol reached by multiple paths keeps its smallest distance;
   cycles are cut by a visited-set; hop limits are fixed.

Output: a mapping symbol_id -> ExpansionInfo(distance, is_anchor, ref_kind, provenance, degree,
is_leaf), ready to become scoring Candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from cce.core.scoring.engine import Candidate
from cce.storage.graph.repository import GraphRepository

_CALLERS_HOPS = 2
_CALLEES_HOPS = 1


@dataclass(slots=True)
class ExpansionInfo:
    symbol_id: str
    repo_id: str | None
    distance: int
    is_anchor: bool
    ref_kind: str | None = None
    provenance: str | None = None


def expand_from_anchors(
    repository: GraphRepository,
    anchor_ids: list[str],
) -> dict[str, ExpansionInfo]:
    """Fixed-template, bounded, deterministic expansion. Returns candidates keyed by symbol_id."""
    found: dict[str, ExpansionInfo] = {}

    def record(
        sid: str,
        distance: int,
        *,
        anchor: bool,
        provenance: str | None,
        ref_kind: str | None = None,
    ) -> bool:
        """Insert/keep-smallest-distance. Returns True if this is a new node to expand from.

        ref_kind, once set from a REFERENCES edge, is retained (stable per node) so scoring gets a
        deterministic define/write/read/pass signal regardless of traversal order.
        """
        existing = found.get(sid)
        if existing is None:
            found[sid] = ExpansionInfo(
                symbol_id=sid,
                repo_id=repository.repo_of_symbol(sid),
                distance=distance,
                is_anchor=anchor,
                ref_kind=ref_kind,
                provenance=provenance,
            )
            return True
        if distance < existing.distance:
            existing.distance = distance
        if anchor:
            existing.is_anchor = True
        if existing.ref_kind is None and ref_kind is not None:
            existing.ref_kind = ref_kind
        return False

    for sid in sorted(set(anchor_ids)):
        record(sid, 0, anchor=True, provenance=None)

    # Callers up to 2 hops (who depends on the anchor -> likely change sites).
    _bfs(repository, sorted(set(anchor_ids)), _CALLERS_HOPS, repository.get_callers, record)
    # Callees 1 hop (what the anchor uses).
    _bfs(repository, sorted(set(anchor_ids)), _CALLEES_HOPS, repository.get_callees, record)

    # Referrers (REFERENCES edges) 1 hop: define/write/read/pass usages -> tag ref_kind (§6.4).
    for sid in sorted(set(anchor_ids)):
        for ref in repository.get_referrers(sid):
            record(
                ref["symbol_id"],
                1,
                anchor=False,
                provenance=ref.get("provenance"),
                ref_kind=ref.get("ref_kind"),
            )

    # Type hierarchy (full, both directions), 1 hop from anchors.
    for sid in sorted(set(anchor_ids)):
        for sup in repository.get_supertypes(sid):
            record(sup["symbol_id"], 1, anchor=False, provenance=sup.get("provenance"))
        for sub in repository.get_subtypes(sid):
            record(sub["symbol_id"], 1, anchor=False, provenance=sub.get("provenance"))
        for impl in repository.find_implementers(sid):
            record(impl["symbol_id"], 1, anchor=False, provenance=impl.get("provenance"))

    # Same-file siblings, 1 hop: symbols co-located with an anchor often change together.
    # symbols_in_file orders by (line, symbol_id), so traversal stays deterministic.
    for sid in sorted(set(anchor_ids)):
        file_id = (repository.get_symbol(sid) or {}).get("file_id")
        if not file_id:
            continue
        for sibling in repository.symbols_in_file(file_id):
            sib_id = sibling.get("symbol_id")
            if sib_id and sib_id != sid:
                record(sib_id, 1, anchor=False, provenance=None)

    return found


def _bfs(repository, roots, hops, neighbors, record) -> None:
    visited = set(roots)
    frontier = list(roots)
    for depth in range(1, hops + 1):
        next_frontier: list[str] = []
        for node_id in sorted(frontier):
            for neigh in neighbors(node_id):
                nid = neigh.get("symbol_id")
                if not nid or nid in visited:
                    continue
                visited.add(nid)
                is_new = record(nid, depth, anchor=False, provenance=neigh.get("provenance"))
                if is_new:
                    next_frontier.append(nid)
        frontier = next_frontier
        if not frontier:
            break


def to_candidates(
    repository: GraphRepository,
    expansion: dict[str, ExpansionInfo],
    *,
    task_signals: dict[str, float] | None = None,
) -> list[Candidate]:
    """Turn expansion info into scoring candidates, filling degree/leaf/task-signal features."""
    task_signals = task_signals or {}
    candidates: list[Candidate] = []
    for sid in sorted(expansion):
        info = expansion[sid]
        degree = repository.symbol_degree(sid)
        callees = repository.get_callees(sid)
        callers = repository.get_callers(sid)
        is_leaf = not callees and bool(callers)  # consumed but calls nothing = leaf consumer
        candidates.append(
            Candidate(
                symbol_id=sid,
                repo_id=info.repo_id,
                ref_kind=info.ref_kind,
                provenance=info.provenance,
                graph_distance=info.distance,
                degree=degree,
                is_leaf=is_leaf,
                task_signal=task_signals.get(sid, 0.0),
                anchor=info.is_anchor,
            )
        )
    return candidates
