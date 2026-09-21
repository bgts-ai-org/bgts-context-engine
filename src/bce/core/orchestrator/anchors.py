"""Multi-source anchor finding (spec section 6.2).

Four sources feed the graph entry points, in priority order:

1. Explicit reference - symbol names or route paths mentioned in the task text (fully
   deterministic). Only identifier-looking tokens (snake_case, camelCase, ``Class.method``,
   backticks) are resolved as explicit names; plain words go through the lexical channel so an
   English "check" never resolves to ``Principal.check`` as if the author had named it.
2. Jira metadata - component -> repo mapping, linked commit/PR (deterministic lookup).
3. task_history - files touched by past resolutions of the task (semi-deterministic, past data).
4. Semantic/lexical hybrid - lexical keyword *coverage* first, embedding as the lowest-priority
   "widest net" fallback. This is the only source that may touch a model, and only to *find* an
   anchor; the model's ranked list is supplied by the caller.

Every anchor carries a **strength** in [0, 1]: how much evidence backs it. Strength combines the
per-source evidence with a noisy-OR (``1 - prod(1 - s_i)``), so a symbol found by several sources
outranks one found by a single weak term. Scoring (section 6.4) uses the strength instead of a
flat "is anchor" bonus, and coverage (section 8) reads it to judge anchor agreement.

An explicit name is only as good as it is *specific*. A PR body that mentions ``MongoDbService``
names one thing; a body that mentions ``Content`` or ``Id`` names a dozen properties that have
nothing to do with each other. Explicit evidence is therefore divided by ``sqrt(matches)`` and
dropped past :data:`EXPLICIT_MAX_MATCHES`, a qualified ``Type.member`` reference is resolved
inside its type, and a member echoing its container's name (a C# constructor, a partial-class
helper) counts as secondary: the author named the type.

Test symbols (``tests/``, ``test_*.py``, ``*.test.ts`` ...) are excluded from the lexical and
semantic sources: a task description almost always resembles the tests that assert it, which
would otherwise saturate the entry points. Explicit and history sources still admit them (the
caller named them on purpose).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from bce.core.orchestrator import bulk
from bce.core.orchestrator.profile import RetrievalProfile
from bce.core.orchestrator.text import (
    explicit_references,
    is_test_symbol,
    query_terms,
)
from bce.core.scoring.engine import DECLARING_KINDS
from bce.storage.graph.repository import GraphRepository

_PATH_RE = re.compile(r"/[A-Za-z0-9_\-{}/:]+")

#: Rows fetched per lexical term. A term that fills the pool matches "everything" and is
#: down-weighted (saturation); a term with few hits is a precise, high-value hint.
LEXICAL_POOL = 25
#: Max lexical anchors kept (by coverage strength, then symbol_id).
LEXICAL_ANCHOR_LIMIT = 15
#: Minimum lexical coverage strength for a symbol to become an anchor at all.
LEXICAL_MIN_STRENGTH = 0.15

#: Beyond this many matches a name is generic ("Id", "Content", "Handle"): it names nothing in
#: particular, so it stops counting as an explicit reference at all. Seeding one weak anchor per
#: match instead would put dozens of unrelated symbols in the pool for every such word; the
#: lexical channel still picks the word up if it carries any signal.
EXPLICIT_MAX_MATCHES = 8
#: A ``Type.member`` reference whose type is not in the graph falls back to the bare member name
#: only while that name stays this unambiguous.
EXPLICIT_QUALIFIED_FALLBACK = 3
#: Members that echo their container's name (constructors, same-named helpers) are secondary
#: evidence next to the container itself.
EXPLICIT_CONTAINER_MEMBER_SCALE = 0.5

#: Per-source evidence caps (strength contributed when the source is fully confident).
SOURCE_STRENGTH: dict[str, float] = {
    "explicit": 1.0,
    "jira": 0.9,
    "history": 0.7,
    "lexical": 0.8,
    "semantic": 0.9,
}
#: Saturated terms (pool full) contribute this fraction of a precise term's weight.
_SATURATED_TERM_WEIGHT = 0.35

#: Semantic strength decays with the model's *absolute* rank: ``cap / (1 + rank / scale)``.
#: The top hit is worth 0.9, rank 10 about 0.68, rank 30 exactly half - so the pool can be
#: widened without the tail growing stronger, and expansion
#: (:data:`~bce.core.orchestrator.expand.EXPAND_MIN_STRENGTH`, ``SIBLING_MIN_STRENGTH``) is gated
#: by the model's rank rather than by how long the list happens to be (the earlier
#: ``rank / len(list)`` form made rank 29 of a 30-list as strong as rank 10 of an 11-list).
#: Measured on the PR-replay tune split: 8 / 10 / 15 all lost semantic retention (80 %) and
#: recall@20 against 30, which keeps every model rank in the pool; steeper decay is not paid for.
#: This is the voyage-code-4 value; another embedding model may carry its own in its
#: :class:`~bce.core.orchestrator.profile.RetrievalProfile` (``semantic_rank_scale``).
SEMANTIC_RANK_SCALE = 30


@dataclass(slots=True)
class AnchorResult:
    #: symbol_id -> sorted list of source tags ("explicit", "jira", "history", "lexical", "semantic")
    anchors: dict[str, list[str]] = field(default_factory=dict)
    #: symbol_id -> evidence strength in [0, 1] (noisy-OR over sources).
    strength: dict[str, float] = field(default_factory=dict)
    #: symbol_id -> position in the embedding model's ranked list (semantic source only).
    semantic_rank: dict[str, int] = field(default_factory=dict)
    #: repo_ids implied by the task metadata (component match, history), for repo scoping.
    repo_hints: list[str] = field(default_factory=list)

    def add(self, symbol_id: str, source: str, strength: float | None = None) -> None:
        """Record ``source`` for ``symbol_id`` and fold ``strength`` in with a noisy-OR.

        ``strength`` defaults to the source's cap; it is clamped to [0, cap].
        """
        sources = self.anchors.setdefault(symbol_id, [])
        if source not in sources:
            sources.append(source)
            sources.sort()
        cap = SOURCE_STRENGTH.get(source, 0.5)
        s = cap if strength is None else max(0.0, min(float(strength), cap))
        prev = self.strength.get(symbol_id, 0.0)
        self.strength[symbol_id] = round(1.0 - (1.0 - prev) * (1.0 - s), 6)

    @property
    def anchor_ids(self) -> list[str]:
        return sorted(self.anchors)

    @property
    def source_count(self) -> int:
        seen = set()
        for sources in self.anchors.values():
            seen.update(sources)
        return len(seen)

    def strength_of(self, symbol_id: str) -> float:
        return self.strength.get(symbol_id, 0.0)

    def strong_ids(self, threshold: float) -> list[str]:
        return sorted(sid for sid, s in self.strength.items() if s >= threshold)


def find_anchors(
    repository: GraphRepository,
    *,
    task_text: str,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    repo_ids: list[str] | None = None,
    lexical_terms: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    include_tests: bool = False,
    profile: RetrievalProfile | None = None,
) -> AnchorResult:
    """Combine the four anchor sources deterministically. Callers supply pre-fetched metadata.

    ``semantic_candidates`` must be in the model's rank order (best first); rank decides the
    semantic evidence strength. ``include_tests`` admits test symbols from lexical/semantic.
    ``profile`` supplies the model-specific rank scale (default: the voyage constants).
    """
    result = AnchorResult()
    for rid in sorted(set(component_repo_ids or [])):
        result.repo_hints.append(rid)

    # Source 1a: explicit symbol names (exact name resolution, identifier-looking tokens only).
    references = [(None, name) for name in sorted(set(explicit_symbols or []))]
    references += explicit_references(task_text)
    for sid, strength in _explicit_anchors(repository, references):
        result.add(sid, "explicit", strength)

    # Source 1b: explicit route paths -> handler symbols (feature 1 as anchor source).
    paths = list(route_paths or []) + _extract_paths(task_text)
    for path in sorted(set(paths)):
        for route in repository.find_routes(path):
            handler = route.get("handler_id")
            if handler:
                result.add(handler, "explicit")

    # Source 3: task_history files -> their symbols.
    for file_id in sorted(set(history_file_ids or [])):
        for sym in repository.symbols_in_file(file_id):
            sid = sym.get("symbol_id")
            if sid:
                result.add(sid, "history")

    # Source 4a: lexical keyword coverage (deterministic, no model).
    terms = lexical_terms if lexical_terms is not None else query_terms(task_text)
    for sid, strength in _lexical_anchors(repository, terms, repo_ids, include_tests):
        result.add(sid, "lexical", strength)

    # Source 4b: semantic candidates (lowest priority; caller supplies the model's ranked list).
    scale = profile.semantic_rank_scale if profile is not None else SEMANTIC_RANK_SCALE
    for sid, rank, strength in _semantic_anchors(
        repository, semantic_candidates, include_tests, scale=scale
    ):
        result.add(sid, "semantic", strength)
        result.semantic_rank[sid] = rank

    return result


def _explicit_anchors(
    repository: GraphRepository, references: list[tuple[str | None, str]]
) -> list[tuple[str, float]]:
    """Resolve ``(container, name)`` references, discounting ambiguous and container-echo matches.

    Strength is ``cap / sqrt(matches)``: a unique name is full evidence, a name shared by four
    symbols is half, and a name shared by more than :data:`EXPLICIT_MAX_MATCHES` is dropped
    because the task cannot have meant all of them.
    """
    if not references:
        return []
    resolved = bulk.resolve_names(repository, sorted({name for _, name in references}))
    cap = SOURCE_STRENGTH["explicit"]
    best: dict[str, float] = {}
    for container, name in sorted(references, key=lambda ref: (ref[1], ref[0] or "")):
        matches = [m for m in (resolved.get(name) or []) if m.get("symbol_id")]
        if not matches:
            continue
        if container:
            qualified = [m for m in matches if _in_container(m["symbol_id"], container)]
            if qualified:
                matches = qualified
            elif len(matches) > EXPLICIT_QUALIFIED_FALLBACK:
                # "Foo.bar" with no Foo in the graph: taking every bar() is a guess, not a name.
                continue
        if len(matches) > EXPLICIT_MAX_MATCHES:
            continue
        strength = cap / math.sqrt(len(matches))
        declaring = {
            m["symbol_id"] for m in matches if (m.get("kind") or "").lower() in DECLARING_KINDS
        }
        for match in matches:
            sid = match["symbol_id"]
            evidence = strength
            if declaring and sid not in declaring:
                evidence *= EXPLICIT_CONTAINER_MEMBER_SCALE
            best[sid] = max(best.get(sid, 0.0), round(evidence, 6))
    return sorted(best.items())


def _in_container(symbol_id: str, container: str) -> bool:
    """True when ``symbol_id`` is declared inside a scope named ``container``.

    Symbol ids carry their scope as ``<lang>::<module.path>::<Container>::<name>``, and the module
    path itself may end in the container's name (a namespace), so both are checked case-insensitively.
    """
    target = container.lower()
    for segment in symbol_id.split("#", 1)[0].split("::")[:-1]:
        lowered = segment.lower()
        if lowered == target or lowered.endswith("." + target):
            return True
    return False


def _semantic_anchors(
    repository: GraphRepository,
    semantic_candidates: list[str] | None,
    include_tests: bool,
    *,
    scale: float = SEMANTIC_RANK_SCALE,
) -> list[tuple[str, int, float]]:
    """``(symbol_id, rank, strength)`` for the model's ranked list, strongest first.

    Strength decays hyperbolically with the absolute rank - ``cap / (1 + rank / scale)``
    (:data:`SEMANTIC_RANK_SCALE` unless the profile says otherwise) - so the best hit is worth as much as a moderately ambiguous
    explicit name, mid ranks still expand, and the tail of a wide pool is kept as candidates only.
    Only the rank enters the engine: a similarity number would let the model reorder candidates,
    which is the one thing the deterministic core does not delegate.
    """
    ranked = [sid for sid in (semantic_candidates or []) if sid]
    if not ranked:
        return []
    meta = {} if include_tests else bulk.symbols_meta(repository, sorted(set(ranked)))
    out: list[tuple[str, int, float]] = []
    for rank, sid in enumerate(ranked):
        if not include_tests:
            info = meta.get(sid) or {}
            if is_test_symbol(sid, info.get("name"), info.get("file_id")):
                continue
        out.append((sid, rank, semantic_strength(rank, scale=scale)))
    return out


def semantic_strength(rank: int, *, scale: float = SEMANTIC_RANK_SCALE) -> float:
    """Anchor strength for the model's 0-based ``rank`` (see :data:`SEMANTIC_RANK_SCALE`)."""
    return round(SOURCE_STRENGTH["semantic"] / (1.0 + max(rank, 0) / float(scale)), 6)


def _lexical_anchors(
    repository: GraphRepository,
    terms: list[str],
    repo_ids: list[str] | None,
    include_tests: bool,
) -> list[tuple[str, float]]:
    """Per-symbol lexical coverage: which query terms a symbol matches, weighted by term rarity.

    A symbol's strength is ``cap * sum(weight of matched terms) / sum(weight of all terms)``.
    Saturated terms (the pool is full -> the term is common) weigh less than precise ones, so
    a symbol matched only by "error" cannot outrank one matched by "period" + "comparison".
    """
    unique_terms = sorted(t for t in set(terms) if t)
    if not unique_terms:
        return []

    hits_by_term = bulk.lexical_hits(
        repository, unique_terms, repo_ids=repo_ids, limit=LEXICAL_POOL
    )
    term_weight: dict[str, float] = {}
    matched: dict[str, set[str]] = {}
    rows: dict[str, dict] = {}
    for term in unique_terms:
        hits = hits_by_term.get(term) or []
        term_weight[term] = _SATURATED_TERM_WEIGHT if len(hits) >= LEXICAL_POOL else 1.0
        for row in hits:
            sid = row.get("symbol_id")
            if not sid:
                continue
            rows.setdefault(sid, row)
            matched.setdefault(sid, set()).add(term)

    denom = sum(term_weight.values()) or 1.0
    cap = SOURCE_STRENGTH["lexical"]
    scored: list[tuple[str, float]] = []
    for sid in sorted(matched):
        row = rows[sid]
        if not include_tests and is_test_symbol(sid, row.get("name"), row.get("file_id")):
            continue
        coverage = sum(term_weight[t] for t in matched[sid]) / denom
        # Coverage is scaled so a symbol matching every term reaches the cap; a lone saturated
        # term stays well below LEXICAL_MIN_STRENGTH unless the task has very few terms.
        strength = round(cap * min(1.0, coverage * _coverage_gain(len(unique_terms))), 6)
        if strength >= LEXICAL_MIN_STRENGTH:
            scored.append((sid, strength))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:LEXICAL_ANCHOR_LIMIT]


def _coverage_gain(term_count: int) -> float:
    """Long task texts rarely have one symbol covering every term; scale so ~1/3 coverage is
    already strong evidence. Short texts (<= 3 terms) use raw coverage."""
    if term_count <= 3:
        return 1.0
    return 3.0


def _extract_paths(text: str) -> list[str]:
    return _PATH_RE.findall(text or "")
