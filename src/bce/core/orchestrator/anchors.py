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
5. Usage - code fragments the task wrote out (``localStorage``, ``'token'``, a backticked
   expression) found verbatim *inside* symbol bodies (deterministic, no model). Names find the
   thing; usage finds the places that use the thing, which is what "every place that reads the
   raw key" asks for.

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
    body_literals,
    explicit_references,
    file_mentions,
    is_test_symbol,
    mention_terms,
    phrase_terms,
    query_terms,
)
from bce.core.scoring.engine import DECLARING_KINDS
from bce.storage.graph.repository import GraphRepository

_PATH_RE = re.compile(r"/[A-Za-z0-9_\-{}/:]+")

#: Rows fetched per lexical term. A term that fills the pool matches "everything" and is
#: down-weighted (saturation); a term with few hits is a precise, high-value hint. Callers asking
#: for a long answer (``max_candidates``) widen the pool with it (``lexical_pool`` argument).
LEXICAL_POOL = 25
#: Max lexical anchors kept (by coverage strength, then symbol_id); widened with the answer size.
LEXICAL_ANCHOR_LIMIT = 15
#: Minimum lexical coverage strength for a symbol to become an anchor at all.
LEXICAL_MIN_STRENGTH = 0.15
#: How much of a term's weight a lexical match earns by *where* it matched: the exact name (a),
#: the name's identifier parts (b), container / file path words (c), or only the signature,
#: docstring or body (d). Since the FTS document carries whole bodies (migration 0011), a
#: 400-line component matches half the words of any task somewhere in its body; that is real
#: but weak evidence next to a 5-line helper *named* after the word, and without the tiers the
#: big components outranked the named helpers on every task (first-hit rank 2 -> 8 on the
#: K=50 evaluation). Rows from repositories without tiers (pre-0011, fakes) count as names.
MATCH_TIER_WEIGHT: dict[str, float] = {"a": 1.0, "b": 0.9, "c": 0.6, "d": 0.35}
#: A matched *word pair* ("access token") adds this much of a precise term's weight on top of
#: the two words' own coverage. Pairs are looked up as conjunctive queries, so a hit means the
#: symbol carries both words - much rarer, and much more often the thing the task means, than
#: either word alone.
PHRASE_BONUS_WEIGHT = 1.0

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
    "usage": 0.8,
    "impact": 0.7,
    "path": 0.85,
}
#: A file mention resolving to more indexed files than this (``index.ts``) is ambiguous and
#: names nothing; one resolving to a few nominates all of them, split evenly.
PATH_FILE_LIMIT = 4
#: Impact source: a task that names a symbol unambiguously (combined strength at or above this)
#: is, more often than not, also about the places that use it - the callers a signature change
#: breaks, the "every component that calls the admin check" of an impact analysis. Those users
#: are nominated as anchors of their own, with the usage channel's fan-out rule: a helper called
#: from 5 places makes each a strong candidate, one called from 60 (a hub) names nothing.
IMPACT_MIN_STRENGTH = 0.9
#: At most this many dominant anchors nominate their users (strongest first; a long PR
#: description names dozens of symbols and their neighbourhoods would drown the pool).
IMPACT_ROOT_LIMIT = 5
#: Key-like string literals per dominant anchor (query keys, event / storage / config keys, route
#: paths) looked up in other bodies, first in text order; and the total over all roots. A body
#: lookup costs ~2 ms on the trigram index, so the whole step stays under ~0.1 s.
IMPACT_LITERAL_LIMIT = 12
IMPACT_LITERAL_TOTAL = 40
#: A literal quoted by more than this share of the usage pool is a convention (``'en-US'`` in
#: every date formatter, a MIME type), not a coupling, and nominates nothing. The task's own
#: fragments tolerate a wider match set (the author chose them); a literal lifted from a root's
#: body carries no such intent. Measured: with the full pool, two locale tags quoted by 27 and 40
#: symbols added 67 anchors of strength ~0.5 to one task's pool; at a quarter they are dropped and
#: the query keys quoted by 2-4 symbols keep their ~0.65.
IMPACT_LITERAL_POOL_SHARE = 0.25
#: Usage source: a code fragment of the task (``localStorage``, ``'token'``, a backticked
#: expression) found *inside* symbol bodies. A fragment matching at least this many symbols is
#: generic (``useState``) and names nothing; below it, strength falls with the match count -
#: ``cap * sqrt(1 - matches / pool)`` - so a fragment in 3 symbols is near-certain evidence for
#: each and one in 30 of a 60-pool is worth ~0.57. Callers widen the pool with the answer size.
USAGE_POOL = 60
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
    lexical_pool: int = LEXICAL_POOL,
    lexical_limit: int = LEXICAL_ANCHOR_LIMIT,
    usage_pool: int = USAGE_POOL,
) -> AnchorResult:
    """Combine the anchor sources deterministically. Callers supply pre-fetched metadata.

    ``semantic_candidates`` must be in the model's rank order (best first); rank decides the
    semantic evidence strength. ``include_tests`` admits test symbols from lexical/semantic/usage.
    ``profile`` supplies the model-specific rank scale (default: the voyage constants).
    ``lexical_pool`` / ``lexical_limit`` widen the lexical channel for long answers: rows fetched
    per term and anchors kept (defaults: :data:`LEXICAL_POOL`, :data:`LEXICAL_ANCHOR_LIMIT`);
    ``usage_pool`` is the match count at which a body mention counts as generic.
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

    # Source 1c: file paths the task names (``src/utils/auth.ts``, a traceback frame) -> the
    # symbols defined in them. The author pointed at the file; which of its symbols is meant is
    # for the scorer to sort out, so every one is nominated with the same strength.
    for sid, strength in _file_anchors(repository, task_text, repo_ids):
        result.add(sid, "path", strength)

    # Source 3: task_history files -> their symbols.
    for file_id in sorted(set(history_file_ids or [])):
        for sym in repository.symbols_in_file(file_id):
            sid = sym.get("symbol_id")
            if sid:
                result.add(sid, "history")

    # Source 4a: lexical keyword coverage (deterministic, no model). Word pairs of the task text
    # are looked up alongside the words and reward symbols carrying both (phrase bonus).
    terms = lexical_terms if lexical_terms is not None else query_terms(task_text)
    phrases = phrase_terms(task_text) if lexical_terms is None else []
    for sid, strength in _lexical_anchors(
        repository,
        terms,
        repo_ids,
        include_tests,
        phrases=phrases,
        pool=lexical_pool,
        limit=lexical_limit,
    ):
        result.add(sid, "lexical", strength)

    # Source 5: usage - code fragments of the task found inside symbol bodies (deterministic).
    for sid, strength in _usage_anchors(
        repository, task_text, repo_ids, include_tests, pool=usage_pool
    ):
        result.add(sid, "usage", strength)

    # Source 4b: semantic candidates (lowest priority; caller supplies the model's ranked list).
    scale = profile.semantic_rank_scale if profile is not None else SEMANTIC_RANK_SCALE
    for sid, rank, strength in _semantic_anchors(
        repository, semantic_candidates, include_tests, scale=scale
    ):
        result.add(sid, "semantic", strength)
        result.semantic_rank[sid] = rank

    # Source 6: impact - the symbols that call, reference or share a key literal with the task's
    # dominant anchors. Runs last because it reads the combined strengths: only names the other
    # sources agree on unambiguously (>= IMPACT_MIN_STRENGTH) nominate their users.
    for sid, strength in _impact_anchors(
        repository, result, include_tests, pool=usage_pool, repo_ids=repo_ids
    ):
        result.add(sid, "impact", strength)

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
    *,
    phrases: list[str] | None = None,
    pool: int = LEXICAL_POOL,
    limit: int = LEXICAL_ANCHOR_LIMIT,
) -> list[tuple[str, float]]:
    """Per-symbol lexical coverage: which query terms a symbol matches, weighted by term rarity.

    A symbol's strength is ``cap * (sum(weight of matched terms) + phrase bonus) / sum(weight of
    all terms)``. Saturated terms (the pool is full -> the term is common) weigh less than
    precise ones, so a symbol matched only by "error" cannot outrank one matched by "period" +
    "comparison". ``phrases`` (word pairs) are queried conjunctively; a matched pair adds
    :data:`PHRASE_BONUS_WEIGHT` to the numerator only - it is extra evidence for the symbols
    that carry it, not a term every symbol is measured against.
    """
    unique_terms = sorted(t for t in set(terms) if t)
    if not unique_terms:
        return []
    unique_phrases = sorted(p for p in set(phrases or []) if p and p not in unique_terms)
    pool = max(1, int(pool))

    hits_by_term = bulk.lexical_hits(
        repository, unique_terms + unique_phrases, repo_ids=repo_ids, limit=pool
    )
    term_weight: dict[str, float] = {}
    matched: dict[str, dict[str, float]] = {}
    phrase_hits: dict[str, int] = {}
    rows: dict[str, dict] = {}
    for term in unique_terms:
        hits = hits_by_term.get(term) or []
        term_weight[term] = _SATURATED_TERM_WEIGHT if len(hits) >= pool else 1.0
        for row in hits:
            sid = row.get("symbol_id")
            if not sid:
                continue
            rows.setdefault(sid, row)
            tier = MATCH_TIER_WEIGHT.get(str(row.get("match_tier") or "a"), 1.0)
            per_term = matched.setdefault(sid, {})
            per_term[term] = max(per_term.get(term, 0.0), tier)
    for phrase in unique_phrases:
        hits = hits_by_term.get(phrase) or []
        if len(hits) >= pool:
            continue  # both words are everywhere together: the pair names nothing either
        for row in hits:
            sid = row.get("symbol_id")
            if not sid:
                continue
            rows.setdefault(sid, row)
            matched.setdefault(sid, {})
            phrase_hits[sid] = phrase_hits.get(sid, 0) + 1

    denom = sum(term_weight.values()) or 1.0
    cap = SOURCE_STRENGTH["lexical"]
    scored: list[tuple[str, float]] = []
    for sid in sorted(matched):
        row = rows[sid]
        if not include_tests and is_test_symbol(sid, row.get("name"), row.get("file_id")):
            continue
        weight = sum(term_weight[t] * tier for t, tier in matched[sid].items())
        weight += PHRASE_BONUS_WEIGHT * phrase_hits.get(sid, 0)
        coverage = weight / denom
        # Coverage is scaled so a symbol matching every term reaches the cap; a lone saturated
        # term stays well below LEXICAL_MIN_STRENGTH unless the task has very few terms.
        strength = round(cap * min(1.0, coverage * _coverage_gain(len(unique_terms))), 6)
        if strength >= LEXICAL_MIN_STRENGTH:
            scored.append((sid, strength))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[: max(1, int(limit))]


def _usage_anchors(
    repository: GraphRepository,
    task_text: str,
    repo_ids: list[str] | None,
    include_tests: bool,
    *,
    pool: int = USAGE_POOL,
) -> list[tuple[str, float]]:
    """Symbols whose text contains a code fragment the task wrote out, with usage strength.

    Every fragment (:func:`~bce.core.orchestrator.text.mention_terms`) is looked up in the
    symbol bodies; a fragment matching ``pool`` or more symbols is generic and ignored. A
    symbol's strength over several fragments combines with a noisy-OR, so the one symbol that
    carries both ``localStorage`` and ``'token'`` outranks the many that carry one of them.
    Repositories without body search (pre-0011 snapshots, unit-test fakes) yield nothing.
    """
    mentions = mention_terms(task_text)
    lookup = getattr(repository, "body_mentions", None)
    if not mentions or not callable(lookup):
        return []
    pool = max(2, int(pool))
    try:
        found = lookup(mentions, repo_ids=repo_ids, limit=pool)
    except NotImplementedError:
        return []
    cap = SOURCE_STRENGTH["usage"]
    # One piece of evidence per (symbol, fragment text): the ``'ACTIVE'`` literal and the
    # ``ACTIVE`` identifier of the same task are the same mention seen twice, so a symbol keeps
    # the stronger of the two rather than compounding them.
    per_text: dict[str, dict[str, float]] = {}
    rows: dict[str, dict] = {}
    for key in sorted(found):
        total, hits = found[key]
        if total <= 0 or total >= pool:
            continue
        strength = cap * math.sqrt(1.0 - total / pool)
        text = key[1]
        for row in hits:
            sid = row.get("symbol_id")
            if not sid:
                continue
            rows.setdefault(sid, row)
            bucket = per_text.setdefault(sid, {})
            bucket[text] = max(bucket.get(text, 0.0), strength)
    out: list[tuple[str, float]] = []
    for sid in sorted(per_text):
        row = rows[sid]
        if not include_tests and is_test_symbol(sid, row.get("name"), row.get("file_id")):
            continue
        miss = 1.0
        for strength in per_text[sid].values():
            miss *= 1.0 - strength
        out.append((sid, round(min(1.0 - miss, cap), 6)))
    return out


def _file_anchors(
    repository: GraphRepository, task_text: str, repo_ids: list[str] | None
) -> list[tuple[str, float]]:
    """Symbols of the files a task names, at the path source's strength.

    A mention resolving to one indexed file gives its symbols the full cap; one resolving to
    ``n <= PATH_FILE_LIMIT`` files (``utils/auth.ts`` in two packages) gives each ``cap / n``;
    a bare ``index.ts`` resolving to more is ambiguous and dropped by the lookup.
    Repositories without file lookups (unit-test fakes) yield nothing.
    """
    mentions = file_mentions(task_text)
    lookup = getattr(repository, "files_by_suffix", None)
    if not mentions or not callable(lookup):
        return []
    # An absolute traceback path (``opt/app/flask/app.py``) is matched by its longest tail that
    # the index knows: the full path first, then one leading directory fewer each try.
    tails: dict[str, list[str]] = {}
    for mention in mentions:
        parts = mention.split("/")
        tails[mention] = ["/".join(parts[i:]) for i in range(len(parts))]
    try:
        resolved = lookup(
            sorted({t for ts in tails.values() for t in ts}),
            repo_ids=repo_ids,
            limit=PATH_FILE_LIMIT,
        )
    except NotImplementedError:
        return []
    cap = SOURCE_STRENGTH["path"]
    file_strength: dict[str, float] = {}
    for mention in mentions:
        files = next((resolved.get(t) for t in tails[mention] if resolved.get(t)), None)
        if not files:
            continue
        strength = cap / len(files)
        for fid in files:
            file_strength[fid] = max(file_strength.get(fid, 0.0), strength)
    if not file_strength:
        return []
    symbols = bulk.symbols_in_files(repository, sorted(file_strength))
    out: list[tuple[str, float]] = []
    for fid in sorted(file_strength):
        for sym in symbols.get(fid, []):
            sid = sym.get("symbol_id")
            if sid:
                out.append((sid, round(file_strength[fid], 6)))
    return out


def _users_of(
    repository: GraphRepository, ids: list[str], bulk_name: str, single_name: str
) -> dict[str, list[dict]]:
    """One edge channel into ``ids``; empty for repositories without graph reads (test fakes)."""
    if not callable(getattr(repository, bulk_name, None)) and not callable(
        getattr(repository, single_name, None)
    ):
        return {}
    try:
        return bulk.neighbours(repository, ids, bulk_name, single_name)
    except NotImplementedError:
        return {}


def _shared_literals(
    repository: GraphRepository,
    roots: list[str],
    root_strength: dict[str, float],
    repo_ids: list[str] | None,
    *,
    pool: int,
) -> dict[str, tuple[float, list[dict]]]:
    """``literal -> (strength, symbols whose text quotes it)`` for the roots' namespaced literals.

    Each root contributes its first :data:`IMPACT_LITERAL_LIMIT` literals
    (:func:`~bce.core.orchestrator.text.body_literals`); a literal quoted by the root alone
    couples nothing, one quoted by :data:`IMPACT_LITERAL_POOL_SHARE` of ``pool`` or more symbols
    is a convention and dropped. Strength is ``root_strength * sqrt(1 - quoting / literal_pool)``
    with the strongest root that carries the literal. Empty for repositories without body search.
    """
    bodies_of = getattr(repository, "symbol_bodies", None)
    lookup = getattr(repository, "body_mentions", None)
    if not callable(bodies_of) or not callable(lookup):
        return {}
    try:
        bodies = bodies_of(roots)
    except NotImplementedError:
        return {}
    weight: dict[str, float] = {}
    for root in roots:  # strongest root first: its literals win ties on the total cap
        for literal in body_literals(bodies.get(root) or "", limit=IMPACT_LITERAL_LIMIT):
            if literal not in weight and len(weight) < IMPACT_LITERAL_TOTAL:
                weight[literal] = root_strength[root]
    if not weight:
        return {}
    literal_pool = max(2, math.ceil(pool * IMPACT_LITERAL_POOL_SHARE))
    try:
        found = lookup([("literal", lit) for lit in weight], repo_ids=repo_ids, limit=literal_pool)
    except NotImplementedError:
        return {}
    out: dict[str, tuple[float, list[dict]]] = {}
    for literal, root_weight in weight.items():
        total, rows = found.get(("literal", literal), (0, []))
        if total <= 1 or total >= literal_pool:
            continue
        out[literal] = (root_weight * math.sqrt(1.0 - total / literal_pool), rows)
    return out


def _impact_anchors(
    repository: GraphRepository,
    result: AnchorResult,
    include_tests: bool,
    *,
    pool: int = USAGE_POOL,
    repo_ids: list[str] | None = None,
) -> list[tuple[str, float]]:
    """Callers, referrers and literal-coupled symbols of the task's dominant anchors, with
    fan-out-aware strength.

    Dominant = combined strength >= :data:`IMPACT_MIN_STRENGTH`, strongest
    :data:`IMPACT_ROOT_LIMIT` of them. A root's users (CALLS + REFERENCES sources, one hop) each
    get ``cap * root_strength * sqrt(1 - n / pool)`` where ``n`` is the root's non-test fan-out; a
    root with ``pool`` or more users is a hub and nominates nothing. The symbols that quote one of
    a root's namespaced string literals - the pages registering the query key a helper
    invalidates, the reader of a storage key its writer names - are its users through data rather
    than through a call, and get the same strength with ``n`` the number of symbols quoting the
    literal against the smaller literal pool (:func:`_shared_literals`). A symbol reached over
    several roots or literals combines the evidence with a noisy-OR; one root's users and one
    literal's quoters are each counted once.
    """
    pool = max(2, int(pool))
    roots = [sid for sid, s in result.strength.items() if s >= IMPACT_MIN_STRENGTH]
    roots.sort(key=lambda sid: (-result.strength[sid], sid))
    roots = roots[:IMPACT_ROOT_LIMIT]
    if not roots:
        return []
    callers = _users_of(repository, roots, "callers_of", "get_callers")
    referrers = _users_of(repository, roots, "referrers_of", "get_referrers")
    cap = SOURCE_STRENGTH["impact"]
    root_set = set(roots)

    def admissible(row: dict) -> str | None:
        sid = row.get("symbol_id")
        if not sid or sid in root_set:
            return None
        if not include_tests and is_test_symbol(sid, row.get("name"), row.get("file_id")):
            return None
        return sid

    #: ``symbol -> {evidence key -> strength}``; a key is one root's user set or one literal.
    evidence: dict[str, dict[str, float]] = {}
    for root in roots:
        users = {
            sid
            for row in list(callers.get(root, [])) + list(referrers.get(root, []))
            if (sid := admissible(row))
        }
        n = len(users)
        if n == 0 or n >= pool:
            continue
        strength = cap * result.strength[root] * math.sqrt(1.0 - n / pool)
        for sid in users:
            evidence.setdefault(sid, {})[f"user:{root}"] = strength
    shared = _shared_literals(
        repository, roots, {r: result.strength[r] for r in roots}, repo_ids, pool=pool
    )
    for literal in sorted(shared):
        weight, rows = shared[literal]
        quoters = {sid for row in rows if (sid := admissible(row))}
        if not quoters:
            continue
        for sid in quoters:
            evidence.setdefault(sid, {})[f"literal:{literal}"] = cap * weight
    out: list[tuple[str, float]] = []
    for sid in sorted(evidence):
        miss = 1.0
        for strength in evidence[sid].values():
            miss *= 1.0 - strength
        out.append((sid, round(min(1.0 - miss, cap), 6)))
    return out


def _coverage_gain(term_count: int) -> float:
    """Long task texts rarely have one symbol covering every term; scale so ~1/3 coverage is
    already strong evidence. Short texts (<= 3 terms) use raw coverage."""
    if term_count <= 3:
        return 1.0
    return 3.0


def _extract_paths(text: str) -> list[str]:
    return _PATH_RE.findall(text or "")
