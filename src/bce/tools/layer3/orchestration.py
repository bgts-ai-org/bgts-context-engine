"""Layer-3 task-aware orchestration tools (spec section 7).

These compose the deterministic core (orchestrator + scoring + coverage + assembler + scope filter)
into task-level answers. They never call an LLM (P1); the only probabilistic input is the semantic
anchor, which is optional and pre-computed (P2). Every result carries a coverage object (section 8)
so the consumer can proceed / widen / ask a human.

One optional stage sits *after* ranking in :func:`get_context_for_task`: the context selector
(:mod:`bce.core.selector`) asks a decision model which of the K ranked files the task edits and
tiers the answer (full / stub / dropped). It never adds a candidate, is fail-open, reports itself in
``coverage.selector`` and is switched with ``BCE_SELECTOR``.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from bce.config import get_settings
from bce.core.assembler import assemble
from bce.core.assembler.files_summary import files_summary
from bce.core.auth.scope import ScopeFilter
from bce.core.coverage import compute_coverage, coverage_message
from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS, SELECTED_MAX_CANDIDATES
from bce.core.i18n import get_translator
from bce.core.orchestrator import RetrievalOrchestrator, bulk
from bce.core.orchestrator.anchors import (
    LEXICAL_ANCHOR_LIMIT,
    LEXICAL_POOL,
    USAGE_POOL,
    AnchorResult,
    find_anchors,
)
from bce.core.orchestrator.gaps import coverage_gaps, coverage_state
from bce.core.orchestrator.profile import RetrievalProfile, active_profile
from bce.core.orchestrator.related_tests import related_tests
from bce.core.orchestrator.text import is_test_symbol
from bce.core.render.evidence import build_evidence
from bce.core.selector import selector_from_settings
from bce.core.selector.pins import identifier_gaps, pinned_files
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore

logger = logging.getLogger("bce.tools.layer3")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


#: How many nearest non-test symbols the automatic semantic anchor pulls in (D1; opt-out via
#: auto_semantic) for a default-sized answer. Wider than the answer itself: narrowing reserves
#: slots for the model's best ranks, and the rest of the pool is what the graph stages get to
#: re-rank. A caller asking for a long answer widens it (:func:`semantic_limit_for`).
AUTO_SEMANTIC_LIMIT = 30
#: Over-fetch factor: test functions usually dominate the raw nearest neighbours of a task
#: description (they literally spell it out), and a long symbol occupies several chunk rows
#: (migration 0011), so fetch more rows and keep the first distinct non-test symbols.
_AUTO_SEMANTIC_OVERFETCH = 4
#: The channel widths scale with the answer: the model's list and the lexical anchor list must
#: both be at least ``K`` deep, or a K=50 answer is filled from a 30-deep model list plus graph
#: neighbours of 15 lexical hits and never sees a candidate the model ranked 35th.
CHANNEL_WIDTH_PER_CANDIDATE = 2
#: ``coverage.likely_incomplete``: a selector listing cut by its cap counts as a gap only when the
#: best file it left out had at least this probability of being edited.
CAPPED_SIGNAL_RELEVANCE = 0.1
#: Recent query embeddings kept per process: the pre-context hook and the agent's own call, or an
#: agent asking the same task twice, pay the embedding provider once.
QUERY_VECTOR_CACHE_SIZE = 64

_query_vectors: OrderedDict[tuple[Any, ...], list[float]] = OrderedDict()
_query_vectors_lock = threading.Lock()
#: Long-lived threads for the query embedding (see :class:`_SemanticChannel`); a provider client
#: that keeps one HTTP session per thread reuses it from one call to the next.
_query_vector_pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="bce-query-vector")


def semantic_limit_for(max_candidates: int) -> int:
    """Depth of the model's ranked list for an answer of ``max_candidates`` symbols."""
    return max(AUTO_SEMANTIC_LIMIT, CHANNEL_WIDTH_PER_CANDIDATE * max(int(max_candidates), 1))


def lexical_limits_for(max_candidates: int) -> tuple[int, int]:
    """``(rows per term, anchors kept)`` for the lexical channel at this answer size."""
    k = max(int(max_candidates), 1)
    return (
        max(LEXICAL_POOL, CHANNEL_WIDTH_PER_CANDIDATE * k),
        max(LEXICAL_ANCHOR_LIMIT, k),
    )


def usage_pool_for(max_candidates: int) -> int:
    """Match count at which a body mention counts as generic, for this answer size."""
    return max(USAGE_POOL, CHANNEL_WIDTH_PER_CANDIDATE * max(int(max_candidates), 1))


def _query_vector(encoder: Any, task_text: str) -> tuple[list[float], float]:
    """The task's query embedding and the milliseconds the provider took (0 from the cache).

    Keyed by the encoder's class and model as well as the text, so encoders that share a model
    name (test doubles) never read each other's vectors. Failures are not cached."""
    model = getattr(encoder, "model_id", None)
    key = (type(encoder), model, task_text)
    if model is not None:
        with _query_vectors_lock:
            cached = _query_vectors.get(key)
            if cached is not None:
                _query_vectors.move_to_end(key)
                return cached, 0.0
    start = time.perf_counter()
    vector = encoder.encode_query(task_text)
    ms = _elapsed_ms(start)
    if model is not None:
        with _query_vectors_lock:
            _query_vectors[key] = vector
            while len(_query_vectors) > QUERY_VECTOR_CACHE_SIZE:
                _query_vectors.popitem(last=False)
    return vector, ms


def _index_model_mismatch(store: VectorStore, encoder: Any) -> str | None:
    """The model the index was embedded with, when it is not the one ``encoder`` queries with."""
    stored_model = getattr(store, "stored_model", None)
    indexed_with = stored_model("symbol") if callable(stored_model) else None
    model = getattr(encoder, "model_id", None)
    return indexed_with if indexed_with and indexed_with != model else None


class _SemanticChannel:
    """The automatic semantic anchors (D1), resolved when :func:`find_anchors` reaches them.

    Construction requests the query embedding on a worker thread, so the provider's round trip
    (a few hundred ms) overlaps the anchor sources that need none - explicit names, paths, lexical
    and usage lookups - instead of preceding them. Nothing is requested for an index embedded with
    another model. Calling the channel waits for the vector and runs the nearest-symbol search on
    the caller's connection; ``candidates`` / ``error`` / ``timings`` hold the outcome.
    """

    def __init__(
        self,
        tool: str,
        store: VectorStore,
        task_text: str,
        repo_ids: list[str] | None,
        repository: GraphRepository | None,
        *,
        limit: int,
    ) -> None:
        self.tool, self.store, self.task_text = tool, store, task_text
        self.repo_ids, self.repository, self.limit = repo_ids, repository, limit
        self.candidates: list[str] | None = None
        self.error: str | None = None
        self.timings: dict[str, float] = {}
        self._vector: cf.Future | None = None
        from bce.indexing.embedder.encoder import build_default_encoder

        try:
            encoder = build_default_encoder()
            with _savepoint(store):
                mismatch = _index_model_mismatch(store, encoder)
        except Exception:  # noqa: BLE001 - the same failure is reported when the channel is read
            return
        if mismatch is None:
            self._vector = _query_vector_pool.submit(_query_vector, encoder, task_text)

    def __call__(self) -> list[str] | None:
        stage = time.perf_counter()
        self.candidates, self.error = _auto_semantic_or_none(
            self.tool,
            self.store,
            self.task_text,
            self.repo_ids,
            self.repository,
            limit=self.limit,
            query_vector=self._vector,
        )
        if self._vector is not None and self._vector.done() and not self._vector.exception():
            self.timings["encode_ms"] = self._vector.result()[1]
        self.timings["semantic_ms"] = _elapsed_ms(stage)
        logger.debug(
            "%s: auto semantic anchors",
            self.tool,
            extra={"count": len(self.candidates or []), "duration_ms": self.timings["semantic_ms"]},
        )
        return self.candidates


def _auto_semantic_or_none(
    tool: str,
    store: VectorStore,
    task_text: str,
    repo_ids: list[str] | None,
    repository: GraphRepository | None,
    *,
    limit: int = AUTO_SEMANTIC_LIMIT,
    query_vector: cf.Future | None = None,
) -> tuple[list[str] | None, str | None]:
    """:func:`_auto_semantic_candidates`, degraded to ``(None, reason)`` when the embedding server
    cannot be reached or does not match the index. The semantic anchor is one of several signals;
    losing it lowers coverage/confidence (which the caller sees in the report) but the lexical and
    structural anchors still produce an answer. Failing the whole tool call instead would leave the
    agent with nothing, after having waited on a stalled network."""
    try:
        return (
            _auto_semantic_candidates(
                store, task_text, repo_ids, repository, limit=limit, query_vector=query_vector
            ),
            None,
        )
    except Exception as exc:
        logger.warning(
            "%s: semantic anchor skipped, embedding query failed", tool, extra={"detail": str(exc)}
        )
        return None, str(exc)[:300]


def _savepoint(store: VectorStore):
    """A savepoint around the vector queries, so a failing one (a query vector of the wrong width,
    say) leaves the caller's transaction usable for the graph stages that follow."""
    conn = getattr(store, "conn", None)
    begin = getattr(conn, "transaction", None)
    return begin() if callable(begin) else contextlib.nullcontext()


def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sid in ids:
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _auto_semantic_candidates(
    store: VectorStore,
    task_text: str,
    repo_ids: list[str] | None,
    repository: GraphRepository | None = None,
    *,
    include_tests: bool = False,
    limit: int = AUTO_SEMANTIC_LIMIT,
    query_vector: cf.Future | None = None,
) -> list[str]:
    """Nearest non-test symbol_ids for the task text, best first (deterministic given the pinned
    encoder + snapshot). Used only when the caller did not supply semantic_candidates.
    ``query_vector`` is the embedding already requested by :class:`_SemanticChannel` (a future of
    :func:`_query_vector`); without it the text is encoded here.

    One query vector for the whole text. Splitting the task into clauses and fusing the per-clause
    rankings (reciprocal rank, clauses at half weight) was measured on the K=50 evaluation and
    dropped: with chunked embeddings the whole-text query already found 89 of 127 expected files
    in its top 100 against the fusion's 87, and the fusion cost the head dearly (semantic hit@1
    19 -> 14 of 30, MRR 0.75 -> 0.58) because a clause's #1 outranked the whole task's #1.

    Chunk rows of one symbol (migration 0011) collapse to the symbol at its best rank. Test
    symbols are skipped (their file/name is checked via the repository when available, else from
    the symbol id) unless ``include_tests``."""
    from bce.indexing.embedder.encoder import EncoderConfigError, build_default_encoder

    encoder = build_default_encoder()
    rows = max(int(limit), 1) * (1 if include_tests else _AUTO_SEMANTIC_OVERFETCH)
    with _savepoint(store):
        indexed_with = _index_model_mismatch(store, encoder)
        if indexed_with is not None:
            raise EncoderConfigError(
                f"the index was embedded with {indexed_with!r} but this process encodes queries "
                f"with {encoder.model_id!r}; set BCE_EMBEDDING_PROVIDER / BCE_EMBEDDING_MODEL / "
                "BCE_EMBEDDING_DIM to the values the index was built with"
            )
        if query_vector is not None:
            vector = query_vector.result()[0]
        else:
            vector = _query_vector(encoder, task_text)[0]
        hits = store.search(vector, limit=rows, repo_ids=repo_ids, kind="symbol")
    ids = _dedupe([hit["ref_id"] for hit in hits])

    meta = (
        bulk.symbols_meta(repository, sorted(set(ids)))
        if repository is not None and not include_tests
        else {}
    )
    out: list[str] = []
    for sid in ids:
        if not include_tests:
            sym = meta.get(sid) or {}
            if is_test_symbol(sid, sym.get("name"), sym.get("file_id")):
                continue
        out.append(sid)
        if len(out) >= limit:
            break
    return out


def _build_anchors(
    repository: GraphRepository,
    *,
    task_text: str,
    explicit_symbols: list[str] | None,
    route_paths: list[str] | None,
    history_file_ids: list[str] | None,
    component_repo_ids: list[str] | None,
    repo_ids: list[str] | None,
    semantic_candidates: list[str] | Callable[[], list[str] | None] | None,
    profile: RetrievalProfile | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> AnchorResult:
    lexical_pool, lexical_limit = lexical_limits_for(max_candidates)
    return find_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
        profile=profile,
        lexical_pool=lexical_pool,
        lexical_limit=lexical_limit,
        usage_pool=usage_pool_for(max_candidates),
    )


def get_context_for_task(
    repository: GraphRepository,
    *,
    task_text: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_candidates: int | None = None,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    store: VectorStore | None = None,
    auto_semantic: bool = True,
    scope: ScopeFilter | None = None,
    task_id: str | None = None,
    locale: str | None = None,
    select: bool = True,
) -> dict[str, Any]:
    """End-to-end: task -> anchors -> expand -> score -> filter -> [select] -> assemble + coverage
    (section 6). ``select=False`` skips the context selector regardless of configuration.

    ``max_candidates=None`` picks K for the caller: :data:`SELECTED_MAX_CANDIDATES` when the
    selector will run, :data:`DEFAULT_MAX_CANDIDATES` otherwise.

    The envelope's ``timings`` holds the wall time of each stage (``*_ms``, plus ``total_ms``);
    it sits beside ``payload`` because it differs between runs and the payload must not."""
    tr = get_translator()
    loc = tr.resolve(locale)
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    # Engine constants fitted for the configured embedding model (voyage / jina / default).
    profile = active_profile()
    selector = selector_from_settings() if select else None
    if max_candidates is None:
        max_candidates = SELECTED_MAX_CANDIDATES if selector is not None else DEFAULT_MAX_CANDIDATES

    # D1: automatic semantic anchor when the caller did not pre-compute one (opt-out: auto_semantic),
    # its embedding requested now and read once the anchor sources reach it.
    semantic_error: str | None = None
    channel: _SemanticChannel | None = None
    if semantic_candidates is None and auto_semantic and store is not None:
        channel = _SemanticChannel(
            "get_context_for_task",
            store,
            task_text,
            repo_ids,
            repository,
            limit=semantic_limit_for(max_candidates),
        )

    stage = time.perf_counter()
    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=channel if channel is not None else semantic_candidates,
        profile=profile,
        max_candidates=max_candidates,
    )
    anchors_ms = _elapsed_ms(stage)
    if channel is not None:
        semantic_candidates, semantic_error = channel.candidates, channel.error
        timings.update(channel.timings)
    timings["anchors_ms"] = anchors_ms
    logger.debug(
        "get_context_for_task: anchors resolved",
        extra={
            "anchor_count": len(anchors.anchor_ids),
            "strong_anchors": len(anchors.strong_ids(0.6)),
            "duration_ms": timings["anchors_ms"],
        },
    )

    # Single pass: task signals are computed for every expanded candidate inside retrieve().
    orchestrator = RetrievalOrchestrator(repository, profile=profile)
    stage = time.perf_counter()
    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_text=task_text,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
    )
    timings.update(result.timings)
    timings["retrieve_ms"] = _elapsed_ms(stage)
    logger.debug(
        "get_context_for_task: retrieval",
        extra={
            "pool": result.pool_size,
            "candidates": len(result.candidates),
            "task_signals": len(result.task_signals),
            "duration_ms": timings["retrieve_ms"],
        },
    )

    stage = time.perf_counter()
    items = _enrich_items(repository, result.candidates, with_body=True)
    # Files the task text names outright (a path, or an identifier a file defines) are pinned:
    # the selector may tier them but never drop them, and ``payload.files[].why`` says so.
    pins = pinned_files(items, task_text)
    timings["enrich_ms"] = _elapsed_ms(stage)

    # Optional: tier the ranked candidates (full / stub / dropped) with the decision-model selector.
    # ``select`` is False for callers that want the raw ranking (benchmarks of the engine alone).
    selection: dict[str, Any] | None = None
    if selector is not None:
        stage = time.perf_counter()
        items, selection = selector.select(items, task_text=task_text, pinned_file_ids=set(pins))
        timings["selector_ms"] = _elapsed_ms(stage)
        logger.debug(
            "get_context_for_task: selector",
            extra={
                k: selection.get(k)
                for k in ("status", "files_full", "files_stub", "files_dropped", "ms")
            },
        )

    stage = time.perf_counter()
    package = assemble(items, max_tokens=max_tokens)
    coverage = compute_coverage(result, repository)
    # Files the selector voted out stay visible as ``candidates`` (paths only, no symbols): the
    # engine ranked them into the top K, so they are the cheapest place to look for what the
    # context misses. Empty when the selector is off or kept everything.
    candidates: list[dict[str, Any]] = []
    if selection is not None:
        candidates = list(selection.pop("dropped_files", None) or [])
        coverage["selector"] = selection
    if semantic_error is not None:
        coverage["semantic_error"] = semantic_error
    # The two signals an agent needs to decide whether to look further (and where): the task's
    # identifiers the context does not cover - each with the indexed file defining it - and a
    # single flag folding them with the selector's own failure modes.
    timings["assemble_ms"] = _elapsed_ms(stage)
    stage = time.perf_counter()
    identifiers = identifier_gaps(
        repository, package.get("items") or [], task_text, repo_ids=repo_ids
    )
    timings["identifiers_ms"] = _elapsed_ms(stage)
    unresolved = identifiers["unresolved"]
    coverage["unresolved_identifiers"] = unresolved
    # The listing cap only matters when it cut a file the model found plausible: a cap reached
    # with the best dropped file at p=0.03 says the answer is complete, not that it is short.
    capped_plausible = bool(selection and selection.get("listing_capped")) and any(
        float(c.get("file_relevance") or 0.0) >= CAPPED_SIGNAL_RELEVANCE for c in candidates[:1]
    )
    coverage["likely_incomplete"] = (
        bool(unresolved)
        or capped_plausible
        or (selection is not None and selection.get("status") in ("timeout", "error"))
    )
    stage = time.perf_counter()
    files = files_summary(package.get("items") or [], ranked=items, pins=pins)
    # The code the agent-facing view shows verbatim: whole declarations with their file range,
    # windowed around what the task names when long, classes as skeletons.
    evidence = build_evidence(
        package.get("items") or [],
        items,
        task_text=task_text,
        bodies=getattr(repository, "symbol_bodies", None),
    )
    timings["evidence_ms"] = _elapsed_ms(stage)
    stage = time.perf_counter()
    tests = related_tests(repository, _test_targets(evidence, items, files), repo_ids=repo_ids)
    timings["tests_ms"] = _elapsed_ms(stage)
    stage = time.perf_counter()
    coverage["gaps"] = coverage_gaps(
        repository,
        task_text=task_text,
        answer_file_ids=[str(f.get("file_id") or "") for f in files],
        evidence=evidence,
        tests=tests,
        unknown_identifiers=identifiers["unknown"],
        repo_ids=repo_ids,
        semantic_error=semantic_error,
        semantic_ran=semantic_candidates is not None and store is not None,
        embedding_provider=get_settings().embedding_provider,
    )
    coverage["state"] = coverage_state(coverage["gaps"])
    answer_repos = sorted(
        {str(f["file_id"]).split(":", 1)[0] for f in files if ":" in str(f.get("file_id") or "")}
    )
    _apply_index_coverage(coverage, repository, repo_ids or answer_repos or None)
    timings["gaps_ms"] = _elapsed_ms(stage)
    package["estimated_content_tokens"] = package.get("used_tokens")
    timings["total_ms"] = _elapsed_ms(t0)
    logger.debug(
        "get_context_for_task: assembled",
        extra={
            "included": package.get("included"),
            "confidence": (coverage or {}).get("confidence"),
            "total_ms": timings["total_ms"],
        },
    )

    message = coverage_message(coverage, loc)
    return {
        "tool": "get_context_for_task",
        "payload": {
            "commit": commit,
            "task_id": task_id,
            "anchors": result.anchors.anchors,
            "files": files,
            "context": package,
            "evidence": evidence,
            "tests": tests,
            "candidates": candidates,
            "coverage": coverage,
        },
        "message": message,
        "locale": loc,
        "timings": timings,
    }


def _apply_index_coverage(
    coverage: dict[str, Any], repository: GraphRepository, repo_ids: list[str] | None
) -> None:
    """``coverage.index``: the share of the scoped repositories' code files the index parsed.

    ``partial`` when a noticeable share is unparsed (the answer cannot see it: coverage state
    ``unknown``); below :data:`~bce.indexing.index_stats.UNSUPPORTED_RATIO` the index cannot
    speak for the repository and confidence becomes ``unsupported``. Nothing without a report.
    """
    from bce.indexing.index_stats import REPORT_RATIO, UNSUPPORTED_RATIO, index_coverage

    reader = getattr(repository, "index_stats", None)
    if not callable(reader):
        return
    try:
        index = index_coverage(reader(repo_ids) or {})
    except Exception:  # noqa: BLE001 - a missing report must not fail the answer
        logger.debug("get_context_for_task: index report unavailable", exc_info=True)
        return
    if index is None:
        return
    index["partial"] = index["ratio"] < REPORT_RATIO
    coverage["index"] = index
    if index["partial"]:
        coverage["state"] = "unknown"
    if index["ratio"] < UNSUPPORTED_RATIO:
        coverage["confidence"] = "unsupported"


def _test_targets(
    evidence: list[dict[str, Any]], items: list[dict[str, Any]], files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What the related tests are looked up for: the shown spans first, then the other ranked
    symbols of the answer's full files."""
    out = [
        {k: sp.get(k) for k in ("symbol_id", "file_id", "name", "role")}
        for sp in evidence
        if sp.get("symbol_id")
    ]
    full = {f.get("file_id") for f in files if f.get("tier") in ("full", None)}
    seen = {t["symbol_id"] for t in out}
    for it in items:
        sid = it.get("symbol_id")
        if sid and sid not in seen and it.get("file_id") in full and it.get("tier") != "stub":
            seen.add(sid)
            out.append(
                {
                    "symbol_id": sid,
                    "file_id": it.get("file_id"),
                    "name": it.get("name"),
                    "role": "dependency",
                }
            )
    return out


def _enrich_items(
    repository: GraphRepository, candidates: list, *, with_body: bool = False
) -> list[dict[str, Any]]:
    """Attach symbol metadata + design notes to ranked candidates, preserving rank order.

    ``with_body`` also loads ``docstring`` / ``body`` (via ``get_symbol_detail`` when the
    repository offers it) so the assembler can render near items at FULL detail (section 6.5).
    """
    detail_getter = getattr(repository, "get_symbol_detail", None) if with_body else None
    items: list[dict[str, Any]] = []
    for cand in candidates:
        sym = None
        if callable(detail_getter):
            sym = detail_getter(cand.symbol_id)
        if sym is None:
            sym = repository.get_symbol(cand.symbol_id) or {}
        items.append(
            {
                "symbol_id": cand.symbol_id,
                "repo_id": cand.repo_id,
                "name": sym.get("name"),
                "kind": sym.get("kind"),
                "signature": sym.get("signature"),
                "docstring": sym.get("docstring"),
                "body": sym.get("body"),
                "file_id": sym.get("file_id"),
                "line": sym.get("line"),
                "end_line": sym.get("end_line"),
                "col": sym.get("col"),
                "text_line": sym.get("text_line"),
                "indexed_at_commit": sym.get("indexed_at_commit"),
                "graph_distance": cand.graph_distance,
                "is_anchor": cand.anchor,
                "anchor_strength": round(cand.effective_anchor_strength, 6),
                "is_test": cand.is_test,
                "score": cand.score,
                "features": cand.features,
                "sources": list(getattr(cand, "sources", None) or []),
                "provenance": getattr(cand, "provenance", None),
                "design_notes": repository.get_design_notes(cand.symbol_id),
            }
        )
    return items


def expand_blast_radius(
    repository: GraphRepository,
    *,
    target_symbols: list[str],
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Impacted surface (files/repos/symbols) reachable from the targets, deterministically.

    Impact channels (D2): incoming CALLS (callers), incoming REFERENCES (referrers), subtype
    impact (INHERITS/IMPLEMENTS), and file-level IMPORTS (files importing the target's file).
    The payload carries a per-edge-type breakdown.
    """
    tr = get_translator()
    loc = tr.resolve(locale)

    impacted_symbols: set[str] = set()
    impacted_files: set[str] = set()
    impacted_repos: set[str] = set()
    # First edge type that reached each newly impacted symbol/file (deterministic given the
    # fixed channel order below), so the breakdown never double-counts.
    edge_counts = {"CALLS": 0, "REFERENCES": 0, "INHERITS_IMPLEMENTS": 0, "IMPORTS": 0}

    visited = set(target_symbols)
    frontier = sorted(set(target_symbols))
    for _ in range(3):  # fixed depth (spec: 2-hop callers + type impact); bounded for determinism.
        next_frontier: list[str] = []
        for sid in frontier:
            neighbours = (
                [("CALLS", n) for n in repository.get_callers(sid)]
                + [("REFERENCES", n) for n in repository.get_referrers(sid)]
                + [("INHERITS_IMPLEMENTS", n) for n in repository.get_subtypes(sid)]
            )
            for edge_type, neighbour in neighbours:
                cid = neighbour.get("symbol_id")
                if not cid or cid in visited:
                    continue
                repo_id = repository.repo_of_symbol(cid)
                if scope is not None and not scope.allows(repo_id):
                    continue
                visited.add(cid)
                impacted_symbols.add(cid)
                edge_counts[edge_type] += 1
                if neighbour.get("file_id"):
                    impacted_files.add(neighbour["file_id"])
                if repo_id:
                    impacted_repos.add(repo_id)
                next_frontier.append(cid)
        frontier = sorted(next_frontier)
        if not frontier:
            break

    # File-level IMPORTS impact: every file importing a target's file is potentially affected.
    target_files = sorted(
        {
            fid
            for fid in (
                (repository.get_symbol(sid) or {}).get("file_id")
                for sid in sorted(set(target_symbols))
            )
            if fid
        }
    )
    for fid in target_files:
        for importer in repository.file_importers(fid):
            ifid = importer.get("file_id")
            irepo = importer.get("repo_id")
            if not ifid:
                continue
            if scope is not None and not scope.allows(irepo):
                continue
            if ifid not in impacted_files:
                impacted_files.add(ifid)
                edge_counts["IMPORTS"] += 1
            if irepo:
                impacted_repos.add(irepo)

    message = tr.translate(
        "tool.blast_radius.summary",
        loc,
        files=len(impacted_files),
        symbols=len(impacted_symbols),
        repos=len(impacted_repos),
    )
    return {
        "tool": "expand_blast_radius",
        "payload": {
            "target_symbols": sorted(set(target_symbols)),
            "impacted_symbols": sorted(impacted_symbols),
            "impacted_files": sorted(impacted_files),
            "impacted_repos": sorted(impacted_repos),
            "counts": {
                "symbols": len(impacted_symbols),
                "files": len(impacted_files),
                "repos": len(impacted_repos),
            },
            "impact_by_edge": edge_counts,
        },
        "message": message,
        "locale": loc,
    }


def suggest_change_sites(
    repository: GraphRepository,
    *,
    task_text: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    store: VectorStore | None = None,
    auto_semantic: bool = True,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
    trace: bool = False,
) -> dict[str, Any]:
    """Scored change-site candidates for a task (does NOT decide; narrows the space, P3).

    ``trace`` additionally returns the whole ranked pool (``payload["ranked_ids"]``, score order,
    before narrowing) so a benchmark can tell *where* a missed symbol was lost: never reached by
    an anchor or the expansion, or reached and then narrowed out.
    """
    tr = get_translator()
    loc = tr.resolve(locale)
    t0 = time.perf_counter()
    # Engine constants fitted for the configured embedding model (voyage / jina / default).
    profile = active_profile()

    timings: dict[str, float] = {}

    # D1: automatic semantic anchor when the caller did not pre-compute one (opt-out: auto_semantic).
    semantic_error: str | None = None
    if semantic_candidates is None and auto_semantic and store is not None:
        stage = time.perf_counter()
        semantic_candidates, semantic_error = _auto_semantic_or_none(
            "suggest_change_sites",
            store,
            task_text,
            repo_ids,
            repository,
            limit=semantic_limit_for(max_candidates),
        )
        timings["semantic_ms"] = _elapsed_ms(stage)
        logger.debug(
            "suggest_change_sites: auto semantic anchors",
            extra={"count": len(semantic_candidates or []), "duration_ms": timings["semantic_ms"]},
        )

    stage = time.perf_counter()
    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
        profile=profile,
        max_candidates=max_candidates,
    )
    timings["anchors_ms"] = _elapsed_ms(stage)
    logger.debug(
        "suggest_change_sites: anchors resolved",
        extra={"anchor_count": len(anchors.anchor_ids), "duration_ms": timings["anchors_ms"]},
    )
    orchestrator = RetrievalOrchestrator(repository, profile=profile)
    stage = time.perf_counter()
    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_text=task_text,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
        keep_ranked=trace,
    )
    timings.update(result.timings)
    timings["retrieve_ms"] = _elapsed_ms(stage)
    logger.debug(
        "suggest_change_sites: retrieval done",
        extra={
            "pool": result.pool_size,
            "candidates": len(result.candidates),
            "duration_ms": timings["retrieve_ms"],
            "total_ms": _elapsed_ms(t0),
        },
    )

    stage = time.perf_counter()
    sites = _enrich_items(repository, result.candidates)
    coverage = compute_coverage(result, repository)
    if semantic_error is not None:
        coverage["semantic_error"] = semantic_error
    timings["enrich_ms"] = _elapsed_ms(stage)
    timings["total_ms"] = _elapsed_ms(t0)
    message = tr.translate("tool.change_sites.count", loc, count=len(sites))
    payload: dict[str, Any] = {
        "commit": commit,
        "anchors": result.anchors.anchors,
        "sites": sites,
        "coverage": coverage,
        "timings": timings,
    }
    if trace:
        payload["ranked_ids"] = [c.symbol_id for c in result.ranked]
    return {
        "tool": "suggest_change_sites",
        "payload": payload,
        "message": message,
        "locale": loc,
    }


def select_repos(
    repository: GraphRepository,
    *,
    task_text: str,
    component_repo_ids: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Candidate repo set for a task from metadata + anchor spread (deterministic union)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=None,
        route_paths=None,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=None,
        semantic_candidates=None,
    )
    repos: set[str] = set(component_repo_ids or [])
    for sid in anchors.anchor_ids:
        repo_id = repository.repo_of_symbol(sid)
        if repo_id:
            repos.add(repo_id)
    if scope is not None:
        repos = {r for r in repos if scope.allows(r)}

    selected = sorted(repos)
    message = tr.translate("tool.select_repos.count", loc, count=len(selected))
    return {
        "tool": "select_repos",
        "payload": {"repos": selected, "anchor_count": len(anchors.anchor_ids)},
        "message": message,
        "locale": loc,
    }


def assemble_context(
    repository: GraphRepository,
    *,
    symbol_ids: list[str],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Deduplicate a caller-supplied symbol list and fit it into a token budget (section 6.5)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in symbol_ids:
        if sid in seen:
            continue
        seen.add(sid)
        sym = repository.get_symbol(sid)
        if sym is None:
            continue
        repo_id = repository.repo_of_symbol(sid)
        if scope is not None and not scope.allows(repo_id):
            continue
        items.append(
            {
                "symbol_id": sid,
                "repo_id": repo_id,
                "name": sym.get("name"),
                "signature": sym.get("signature"),
                "file_id": sym.get("file_id"),
                "line": sym.get("line"),
                "graph_distance": 1,
            }
        )
    package = assemble(items, max_tokens=max_tokens)
    message = tr.translate(
        "tool.assemble_context.summary", loc, count=package["included"], tokens=max_tokens
    )
    return {
        "tool": "assemble_context",
        "payload": {"context": package},
        "message": message,
        "locale": loc,
    }
