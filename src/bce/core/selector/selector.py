"""Context selector: tier the K ranked candidates into what the agent should actually read.

Problem. A K=50 answer finds 94 % of the files a change touches, but spreads them over ~20 distinct
files of which ~18 are noise (measured on 597 real changes across 12 repositories, 6 languages),
so the agent either reads everything (no token saving) or guesses. The engine's own order is not
enough to cut: the first file is right in 75 % of tasks, but the 2nd-3rd files of a multi-file
change sit anywhere in the top 50.

Approach. After ranking, ask a decision model (Jev, or an open-weight decider-2b / decider-4b
served on-prem; see :data:`bce.core.selector.PRESETS`) two batches of typed questions in parallel:

- per distinct file, a ``noul``: *is this one of the files the task edits?* -> ``p(file)``
- per symbol, a ``score`` on a 4-level scale (unrelated / background / must read / must change)

and apply a fixed policy on the probabilities (fitted for Jev on the same 597 changes,
``local_bench/multi_repo_bench/select_bench.py``, policy ``tier 0.5/0.05|12 +sym>=1.0``; the
deciders run under the same thresholds):

- ``full``  files: the engine's first file, plus every file with ``p >= full_threshold``. Their
  symbols are kept at the assembler's normal detail, except symbols scored below
  ``symbol_min_score`` which are dropped (a file always keeps at least one symbol).
- ``stub``  files: ``p >= stub_threshold`` (at most ``max_files`` files listed in total) become one
  reference line each (path + symbol names) - the agent sees they exist and can open them.
- everything else is dropped.

Effect on the benchmark above (600 tasks, end to end): recall of the files a change touches
94.4 -> 93.4 (within the first 20 symbols 91.0 -> 92.5) while the tokens handed to the agent fall
from 8 310 to 1 714 (-79 %) and the listed files from 20.5 to 10.9 (2.3 at full detail, 8.5 as
stubs). The model is not bit-deterministic (probabilities jitter
by ~0.01 between identical calls); the flips this causes sit at the stub boundary, on files the
change never touched, and the recall moved by <= 0.6 points across three repeated runs.

The selector is fail-open: any error or timeout returns the ranked candidates untouched, and the
``coverage.selector`` block says so. It never adds a candidate the engine did not rank.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bce.core.selector.jev import JevClient, JevError

logger = logging.getLogger("bce.selector")

#: Ordered scale of the per-symbol ``score`` question (index = score).
SYMBOL_LEVELS = (
    "Unrelated to the task",
    "Background context only; would not be opened",
    "Must be read to implement the task correctly",
    "Must be modified by the task",
)
_EXCERPT_CHARS = 240
_SYMBOLS_PER_FILE = 12
_STUB_NAMES = 6
_MISMATCH_LOGGED: set[tuple[str, str]] = set()
#: Added to the client's per-request timeout to form the selector's wall-clock budget.
_BUDGET_GRACE_S = 0.5


@dataclass(frozen=True)
class SelectorConfig:
    """Tiering policy. The cut is by probability: every file at ``p >= full_threshold`` is shown
    in full, every file at ``p >= stub_threshold`` is at least listed. ``max_files`` is only a
    hard cap on the *listing* (the stubs stop when it is reached); it never removes a full file -
    a fixed cap of 12 was the binding constraint in 46 % of the answers of a 600-change
    benchmark and cost 2.4x the misses of the uncapped answers. ``full_content_bytes`` bounds the
    text handed over at full detail: when the full files' bodies exceed it, the least probable
    full files are demoted to stubs (the engine's first file and pinned files never are).
    ``pinned_full_limit`` bounds how many files the caller pins into the full tier against the
    model's vote (the rest of the pinned files are still listed as stubs)."""

    full_threshold: float = 0.5
    stub_threshold: float = 0.02
    max_files: int = 20
    symbol_min_score: float = 1.0
    full_content_bytes: int = 12_000
    pinned_full_limit: int = 6


@dataclass
class FileGroup:
    path: str  # file id without the ``repo:`` prefix (what the model reads)
    file_id: str
    pos: int  # first-seen position in the ranked answer (0 = engine's first file)
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def max_score(self) -> float:
        return max((float(it.get("score") or 0.0) for it in self.items), default=0.0)


def _path_of(item: dict[str, Any]) -> str:
    fid = str(item.get("file_id") or "")
    repo = str(item.get("repo_id") or "")
    if repo and fid.startswith(repo + ":"):
        return fid[len(repo) + 1 :]
    return fid.split(":", 1)[1] if ":" in fid else fid


def group_files(items: list[dict[str, Any]]) -> list[FileGroup]:
    """Distinct files of the ranked answer in first-seen order, each with its symbols."""
    groups: dict[str, FileGroup] = {}
    for it in items:
        fid = str(it.get("file_id") or "")
        if fid not in groups:
            groups[fid] = FileGroup(path=_path_of(it), file_id=fid, pos=len(groups))
        groups[fid].items.append(it)
    return list(groups.values())


def _excerpt(item: dict[str, Any], n: int = _EXCERPT_CHARS) -> str:
    body = str(item.get("body") or "")
    if not body:
        sig = str(item.get("signature") or "")
        doc = str(item.get("docstring") or "")
        body = " ".join(p for p in (f"{item.get('name') or ''}{sig}", doc) if p)
    return " ".join(body.split())[:n]


def _label(item: dict[str, Any]) -> str:
    return f"{item.get('kind') or 'symbol'} {item.get('name') or '?'}"


def _file_doc(g: FileGroup) -> dict[str, Any]:
    by_score = sorted(g.items, key=lambda it: -float(it.get("score") or 0.0))
    names: list[str] = []
    for it in by_score:
        lab = _label(it)
        if lab not in names:
            names.append(lab)
        if len(names) >= _SYMBOLS_PER_FILE:
            break
    excerpt = next((e for e in (_excerpt(it) for it in by_score) if len(e) > 20), "")
    return {
        "path": g.path,
        "symbols": names,
        "excerpt": excerpt,
        "engine_rank": g.pos + 1,
        "engine_score": round(g.max_score, 2),
        "matched_symbols": len(g.items),
    }


def file_state(task_text: str, repo_label: str, groups: list[FileGroup]) -> dict[str, Any]:
    return {
        "task": task_text,
        "repository": repo_label,
        "note": (
            "candidate_files come from a code search for the task, ordered by engine_rank (1 = "
            "engine's best guess). A real change usually touches 1-5 files: the file that owns the "
            "feature, plus sibling implementations, the type/interface it extends, and the config "
            "or registry that wires it. Most other candidates are unrelated."
        ),
        "candidate_files": {f"f{i}": _file_doc(g) for i, g in enumerate(groups)},
    }


def file_questions(groups: list[FileGroup]) -> dict[str, Any]:
    return {
        f"f{i}": {
            "type": "noul",
            "instructions": (
                f"Is candidate_files.f{i} ({g.path}) one of the files the change described in "
                "`task` edits?"
            ),
            "criteria": {
                "true": (
                    "Implementing the task adds, modifies or removes code in this file (owner of "
                    "the feature, a sibling implementation that must change the same way, the type "
                    "or interface being extended, or the wiring/config/registry that must register "
                    "the change)."
                ),
                "false": (
                    "The file merely shares vocabulary with the task, calls or is called by the "
                    "changed code without needing to change, is a test, or is an unrelated copy in "
                    "another module."
                ),
            },
        }
        for i, g in enumerate(groups)
    }


def symbol_state(task_text: str, repo_label: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task": task_text,
        "repository": repo_label,
        "note": (
            "candidate_symbols are code symbols a search returned for the task; most are NOT "
            "relevant."
        ),
        "candidate_symbols": {
            f"s{i}": {
                "file": _path_of(it),
                "kind": it.get("kind"),
                "name": it.get("name"),
                "line": it.get("line"),
                "excerpt": _excerpt(it),
            }
            for i, it in enumerate(items)
        },
    }


def symbol_questions(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"s{i}": {
            "type": "score",
            "instructions": (
                f"How relevant is candidate_symbols.s{i} ({it.get('name')} in {_path_of(it)}) to "
                "the task?"
            ),
            "criteria": list(SYMBOL_LEVELS),
        }
        for i, it in enumerate(items)
    }


def parse_file_answers(answers: dict[str, Any], groups: list[FileGroup]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i, g in enumerate(groups):
        ans = answers.get(f"f{i}") or {}
        try:
            out[g.file_id] = max(0.0, min(1.0, float(ans.get("noul", 0.0))))
        except (TypeError, ValueError):
            out[g.file_id] = 0.0
    return out


def parse_symbol_answers(answers: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i, it in enumerate(items):
        ans = answers.get(f"s{i}") or {}
        try:
            out[str(it.get("symbol_id"))] = float(ans.get("score", 0.0))
        except (TypeError, ValueError):
            out[str(it.get("symbol_id"))] = 0.0
    return out


def _stub_item(g: FileGroup, p: float) -> dict[str, Any]:
    best = max(g.items, key=lambda it: float(it.get("score") or 0.0))
    names = []
    for it in sorted(g.items, key=lambda it: -float(it.get("score") or 0.0)):
        n = str(it.get("name") or "")
        if n and n not in names:
            names.append(n)
    shown = ", ".join(names[:_STUB_NAMES]) + (
        f" (+{len(names) - _STUB_NAMES})" if len(names) > _STUB_NAMES else ""
    )
    stub = dict(best)
    stub.update(
        {
            "detail_level": "reference",
            "summary": f"{g.path} - {len(g.items)} candidate symbol(s): {shown}",
            "tier": "stub",
            "file_relevance": round(p, 3),
        }
    )
    return stub


def _content_bytes(item: dict[str, Any]) -> int:
    body = item.get("body")
    if body:
        return len(str(body).encode("utf-8", "ignore"))
    return len(f"{item.get('signature') or ''}{item.get('docstring') or ''}".encode())


def apply_policy(
    items: list[dict[str, Any]],
    groups: list[FileGroup],
    file_p: dict[str, float],
    symbol_score: dict[str, float] | None,
    cfg: SelectorConfig,
    *,
    pinned: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pure function: ranked items + probabilities -> tiered items (full first, then stubs).

    ``pinned`` names files the caller will not let the model drop - typically the files defining
    an identifier or path the task text spells out (:func:`bce.core.selector.pins.pinned_files`).
    They enter the full tier ahead of the model's vote (at most ``cfg.pinned_full_limit`` of
    them, best first; the rest are listed as stubs) and are exempt from the byte budget.
    """
    pinned = {str(f) for f in (pinned or ()) if f}
    ranked = sorted(groups, key=lambda g: (-file_p.get(g.file_id, 0.0), g.pos))

    def p_of(g: FileGroup) -> float:
        return file_p.get(g.file_id, 0.0)

    # Full tier: the engine's first file, every file above the full threshold, then the pinned
    # files (bounded). No cap applies here - a probability cut, not a count cut.
    full: list[FileGroup] = [g for g in groups if g.pos == 0]
    for g in ranked:
        if p_of(g) >= cfg.full_threshold and g not in full:
            full.append(g)
    pinned_groups = [g for g in ranked if g.file_id in pinned]
    pinned_promoted = 0
    for g in pinned_groups:
        if g in full:
            continue
        if pinned_promoted >= cfg.pinned_full_limit:
            break
        full.append(g)
        pinned_promoted += 1

    # Stub tier: everything above the stub threshold, until the listing cap; pinned files that
    # did not make the full tier are listed regardless of the cap.
    stubs: list[FileGroup] = []
    capped = False
    for g in ranked:
        if g in full:
            continue
        if p_of(g) < cfg.stub_threshold:
            break
        if len(full) + len(stubs) >= cfg.max_files:
            capped = True
            break
        stubs.append(g)
    for g in pinned_groups:
        if g not in full and g not in stubs:
            stubs.append(g)

    # Symbol pruning inside the full files, then the byte budget: demote the least probable
    # full files (never the first file, never a pinned one) while the bodies exceed it.
    def kept_symbols(g: FileGroup) -> list[dict[str, Any]]:
        kept = list(g.items)
        if symbol_score is not None:
            kept = [
                it
                for it in g.items
                if symbol_score.get(str(it.get("symbol_id")), 0.0) >= cfg.symbol_min_score
            ]
            if not kept:  # never lose a file the policy decided to show
                kept = [max(g.items, key=lambda it: float(it.get("score") or 0.0))]
            kept.sort(key=lambda it: -symbol_score.get(str(it.get("symbol_id")), 0.0))
        return kept

    kept_by_file = {g.file_id: kept_symbols(g) for g in full}
    size_by_file = {
        fid: sum(_content_bytes(it) for it in kept) for fid, kept in kept_by_file.items()
    }
    demoted = 0
    if cfg.full_content_bytes > 0:
        demotable = sorted(
            (g for g in full if g.pos != 0 and g.file_id not in pinned),
            key=lambda g: (p_of(g), -g.pos),  # least probable first
        )
        for g in demotable:
            if sum(size_by_file.values()) <= cfg.full_content_bytes:
                break
            full.remove(g)
            size_by_file.pop(g.file_id, None)
            stubs.append(g)
            demoted += 1

    full.sort(key=lambda g: (-p_of(g), g.pos))
    stubs.sort(key=lambda g: (-p_of(g), g.pos))

    out: list[dict[str, Any]] = []
    symbols_dropped = 0
    per_file: list[list[dict[str, Any]]] = []
    for g in full:
        p = p_of(g)
        kept = kept_by_file[g.file_id]
        symbols_dropped += len(g.items) - len(kept)
        entries = []
        for it in kept:
            entry = dict(it)
            entry["tier"] = "full"
            entry["file_relevance"] = round(p, 3)
            if symbol_score is not None:
                entry["symbol_relevance"] = round(
                    symbol_score.get(str(it.get("symbol_id")), 0.0), 2
                )
            if g.file_id in pinned:
                entry["pinned"] = True
            entries.append(entry)
        per_file.append(entries)
    # Round-robin over the full files: the assembler cuts from the bottom when the budget runs out,
    # so each full file's best symbol comes before any file's second one.
    for depth in range(max((len(e) for e in per_file), default=0)):
        out.extend(e[depth] for e in per_file if depth < len(e))
    for g in stubs:
        stub = _stub_item(g, p_of(g))
        if g.file_id in pinned:
            stub["pinned"] = True
        out.append(stub)

    # The files the policy dropped, best first. They leave the context but not the answer: the
    # caller lists them as ``candidates`` so an agent has a second ring to check before it searches
    # the tree itself (the engine ranked them; only the decision model voted them down).
    dropped = sorted(
        (g for g in groups if g not in full and g not in stubs),
        key=lambda g: (-file_p.get(g.file_id, 0.0), g.pos),
    )
    stats = {
        "files_in": len(groups),
        "files_full": len(full),
        "files_stub": len(stubs),
        "files_dropped": len(groups) - len(full) - len(stubs),
        "dropped_files": [
            {
                "file_id": g.file_id,
                "path": g.path,
                "file_relevance": round(file_p.get(g.file_id, 0.0), 3),
            }
            for g in dropped
        ],
        "symbols_in": len(items),
        "symbols_out": len(out),
        "symbols_dropped_in_full_files": symbols_dropped,
        "symbol_pruning": symbol_score is not None,
        "files_pinned": len(pinned_groups),
        "files_pinned_to_full": pinned_promoted,
        "files_demoted_by_budget": demoted,
        "full_bytes": int(sum(size_by_file.values())),
        "listing_capped": capped,
    }
    return out, stats


class Selector:
    def __init__(
        self,
        client: JevClient,
        cfg: SelectorConfig | None = None,
        *,
        method: str = "jev",
        expect_served: str | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg or SelectorConfig()
        self.method = method
        # A decider server answers with whatever weights it loaded and ignores the requested
        # model, so a server started with decider-4b silently serves a "decider-2b" config.
        self.expect_served = expect_served

    def _check_served(self, served: str) -> None:
        if not self.expect_served or self.expect_served.lower() in served.lower():
            return
        key = (self.method, served)
        if key not in _MISMATCH_LOGGED:  # selectors are built per request: warn once per process
            _MISMATCH_LOGGED.add(key)
            logger.warning(
                "selector: BCE_SELECTOR=%s but the server answered with %r; check which model "
                "the decider server loaded (DECIDER_MODEL)",
                self.method,
                served,
            )

    def select(
        self,
        items: list[dict[str, Any]],
        *,
        task_text: str,
        pinned_file_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Tiered items + a ``coverage.selector`` block. Fail-open: on any error or budget overrun
        the input items are returned unchanged with ``status: "error"`` / ``"timeout"``.
        ``pinned_file_ids`` are never dropped (see :func:`apply_policy`)."""
        t0 = time.perf_counter()
        info: dict[str, Any] = {"method": self.method, "model": self.client.model, "status": "ok"}
        if not items:
            info.update(status="skipped", reason="no candidates")
            return items, info
        groups = group_files(items)
        repo_label = ", ".join(
            sorted({str(it.get("repo_id") or "") for it in items if it.get("repo_id")})
        )

        # Both requests run in parallel under one wall-clock budget (the client's timeout plus a
        # small grace). The budget is enforced here, not only inside the transport: a server that
        # accepts the connection and then stalls would otherwise hold the answer until the socket
        # gives up, and an editor kills the whole MCP call at ~60 s. On budget overrun the ranked
        # candidates are returned untouched (fail-open) and the pool is shut down without waiting
        # for the straggler, so the caller never pays for a request it no longer needs.
        budget = max(1.0, float(self.client.timeout)) + _BUDGET_GRACE_S
        ex = cf.ThreadPoolExecutor(max_workers=2)
        try:
            f_files = ex.submit(
                self.client.decide,
                file_state(task_text, repo_label, groups),
                file_questions(groups),
            )
            f_syms = ex.submit(
                self.client.decide,
                symbol_state(task_text, repo_label, items),
                symbol_questions(items),
            )
            try:
                file_answers, file_usage, served = f_files.result(timeout=budget)
            except cf.TimeoutError:
                logger.warning(
                    "selector: file decision exceeded the time budget, answer left unselected",
                    extra={"budget_s": budget},
                )
                info.update(status="timeout", reason=f"no answer within {budget:g} s", ms=_ms(t0))
                return items, info
            except JevError as exc:
                logger.warning(
                    "selector: file decision failed, answer left unselected",
                    extra={"detail": str(exc)},
                )
                info.update(status="error", reason=str(exc)[:200], ms=_ms(t0))
                return items, info
            symbol_score: dict[str, float] | None
            remaining = max(0.05, budget - (time.perf_counter() - t0))
            try:
                sym_answers, sym_usage, _ = f_syms.result(timeout=remaining)
                symbol_score = parse_symbol_answers(sym_answers, items)
            except cf.TimeoutError:
                logger.warning(
                    "selector: symbol decision exceeded the time budget, files tiered without "
                    "symbol pruning",
                    extra={"budget_s": budget},
                )
                symbol_score, sym_usage = None, {}
                info.update(status="partial", reason="symbol decision timed out")
            except JevError as exc:
                logger.warning(
                    "selector: symbol decision failed, files tiered without symbol pruning",
                    extra={"detail": str(exc)},
                )
                symbol_score, sym_usage = None, {}
                info.update(status="partial", reason=str(exc)[:200])
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        if served:
            info["served_model"] = served
            self._check_served(served)
        file_p = parse_file_answers(file_answers, groups)
        out, stats = apply_policy(
            items, groups, file_p, symbol_score, self.cfg, pinned=pinned_file_ids
        )
        info.update(stats)
        info["ms"] = _ms(t0)
        tokens = (file_usage.get("input_tokens") or 0) + (sym_usage.get("input_tokens") or 0)
        cost = (file_usage.get("cost") or 0.0) + (sym_usage.get("cost") or 0.0)
        if tokens:
            info["input_tokens"] = int(tokens)
        if cost:
            info["cost_usd"] = round(float(cost), 6)
        info["policy"] = {
            "full_threshold": self.cfg.full_threshold,
            "stub_threshold": self.cfg.stub_threshold,
            "max_files": self.cfg.max_files,
            "symbol_min_score": self.cfg.symbol_min_score,
            "full_content_bytes": self.cfg.full_content_bytes,
        }
        return out, info


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
