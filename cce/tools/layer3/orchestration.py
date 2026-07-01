"""Layer-3 task-aware orchestration tools (spec section 7).

These compose the deterministic core (orchestrator + scoring + coverage + assembler + scope filter)
into task-level answers. They never call an LLM (P1); the only probabilistic input is the semantic
anchor, which is optional and pre-computed (P2). Every result carries a coverage object (section 8)
so the consumer can proceed / widen / ask a human.
"""

from __future__ import annotations

import re
from typing import Any

from cce.core.assembler import assemble
from cce.core.auth.scope import ScopeFilter
from cce.core.coverage import compute_coverage, coverage_message
from cce.core.i18n import get_translator
from cce.core.orchestrator import RetrievalOrchestrator
from cce.core.orchestrator.anchors import AnchorResult, find_anchors
from cce.storage.graph.repository import GraphRepository

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _task_signals(task_text: str, symbol_ids: list[str], repository: GraphRepository) -> dict[str, float]:
    """Deterministic task-signal weight per symbol: 1.0 if the symbol name appears in the task text.

    This is the section 6.4 ``task_signal_match`` feature (error codes, keywords, "expired"...).
    """
    tokens = {t.lower() for t in _WORD_RE.findall(task_text or "")}
    signals: dict[str, float] = {}
    for sid in symbol_ids:
        sym = repository.get_symbol(sid)
        name = (sym or {}).get("name")
        if name and name.lower() in tokens:
            signals[sid] = 1.0
    return signals


def _build_anchors(
    repository: GraphRepository,
    *,
    task_text: str,
    explicit_symbols: list[str] | None,
    route_paths: list[str] | None,
    history_file_ids: list[str] | None,
    component_repo_ids: list[str] | None,
    repo_ids: list[str] | None,
    semantic_candidates: list[str] | None,
) -> AnchorResult:
    return find_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
    )


def get_context_for_task(
    repository: GraphRepository,
    *,
    task_text: str,
    max_tokens: int = 4000,
    max_candidates: int = 8,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    scope: ScopeFilter | None = None,
    task_id: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """End-to-end: task -> anchors -> expand -> score -> filter -> assemble + coverage (section 6)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
    )

    orchestrator = RetrievalOrchestrator(repository)
    # First pass to know which symbols exist, so task-signals can be computed on the expansion set.
    prelim = orchestrator.retrieve(anchors, commit=commit, max_candidates=max_candidates * 4)
    signals = _task_signals(task_text, [c.symbol_id for c in prelim.candidates], repository)

    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_signals=signals,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
    )

    items = _enrich_items(repository, result.candidates)
    package = assemble(items, max_tokens=max_tokens)
    coverage = compute_coverage(result)

    message = coverage_message(coverage, loc)
    return {
        "tool": "get_context_for_task",
        "payload": {
            "commit": commit,
            "task_id": task_id,
            "anchors": result.anchors.anchors,
            "context": package,
            "coverage": coverage,
        },
        "message": message,
        "locale": loc,
    }


def _enrich_items(repository: GraphRepository, candidates: list) -> list[dict[str, Any]]:
    """Attach symbol metadata + design notes to ranked candidates, preserving rank order."""
    items: list[dict[str, Any]] = []
    for cand in candidates:
        sym = repository.get_symbol(cand.symbol_id) or {}
        items.append(
            {
                "symbol_id": cand.symbol_id,
                "repo_id": cand.repo_id,
                "name": sym.get("name"),
                "kind": sym.get("kind"),
                "signature": sym.get("signature"),
                "file_id": sym.get("file_id"),
                "line": sym.get("line"),
                "graph_distance": cand.graph_distance,
                "score": cand.score,
                "features": cand.features,
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
    """Impacted surface (files/repos/symbols) reachable as callers of the targets, deterministically."""
    tr = get_translator()
    loc = tr.resolve(locale)

    impacted_symbols: set[str] = set()
    impacted_files: set[str] = set()
    impacted_repos: set[str] = set()

    visited = set(target_symbols)
    frontier = sorted(set(target_symbols))
    for _ in range(3):  # fixed depth (spec: 2-hop callers + type impact); bounded for determinism.
        next_frontier: list[str] = []
        for sid in frontier:
            for caller in repository.get_callers(sid) + repository.get_subtypes(sid):
                cid = caller.get("symbol_id")
                if not cid or cid in visited:
                    continue
                repo_id = repository.repo_of_symbol(cid)
                if scope is not None and not scope.allows(repo_id):
                    continue
                visited.add(cid)
                impacted_symbols.add(cid)
                if caller.get("file_id"):
                    impacted_files.add(caller["file_id"])
                if repo_id:
                    impacted_repos.add(repo_id)
                next_frontier.append(cid)
        frontier = sorted(next_frontier)
        if not frontier:
            break

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
        },
        "message": message,
        "locale": loc,
    }


def suggest_change_sites(
    repository: GraphRepository,
    *,
    task_text: str,
    max_candidates: int = 8,
    commit: str | None = None,
    repo_ids: list[str] | None = None,
    explicit_symbols: list[str] | None = None,
    route_paths: list[str] | None = None,
    history_file_ids: list[str] | None = None,
    component_repo_ids: list[str] | None = None,
    semantic_candidates: list[str] | None = None,
    scope: ScopeFilter | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Scored change-site candidates for a task (does NOT decide; narrows the space, P3)."""
    tr = get_translator()
    loc = tr.resolve(locale)

    anchors = _build_anchors(
        repository,
        task_text=task_text,
        explicit_symbols=explicit_symbols,
        route_paths=route_paths,
        history_file_ids=history_file_ids,
        component_repo_ids=component_repo_ids,
        repo_ids=repo_ids,
        semantic_candidates=semantic_candidates,
    )
    orchestrator = RetrievalOrchestrator(repository)
    prelim = orchestrator.retrieve(anchors, commit=commit, max_candidates=max_candidates * 4)
    signals = _task_signals(task_text, [c.symbol_id for c in prelim.candidates], repository)
    result = orchestrator.retrieve(
        anchors,
        commit=commit,
        task_signals=signals,
        max_candidates=max_candidates,
        scope_filter=scope.allows if scope is not None else None,
    )

    sites = _enrich_items(repository, result.candidates)
    coverage = compute_coverage(result)
    message = tr.translate("tool.change_sites.count", loc, count=len(sites))
    return {
        "tool": "suggest_change_sites",
        "payload": {
            "commit": commit,
            "anchors": result.anchors.anchors,
            "sites": sites,
            "coverage": coverage,
        },
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
    max_tokens: int = 4000,
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
