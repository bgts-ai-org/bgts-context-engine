"""Context selector: tier the K ranked candidates into what the agent should actually read.

Problem. A K=50 answer finds 94 % of the files a change touches, but spreads them over ~20 distinct
files of which ~18 are noise (measured on 597 real changes across 12 repositories, 6 languages),
so the agent either reads everything (no token saving) or guesses. The engine's own order is not
enough to cut: the first file is right in 75 % of tasks, but the 2nd-3rd files of a multi-file
change sit anywhere in the top 50.

Approach. After ranking, ask a decision model (Jev) two batches of typed questions in parallel:

- per distinct file, a ``noul``: *is this one of the files the task edits?* -> ``p(file)``
- per symbol, a ``score`` on a 4-level scale (unrelated / background / must read / must change)

and apply a fixed policy on the probabilities (fitted on the same 597 changes, ``local_bench/
multi_repo_bench/select_bench.py``, policy ``tier 0.5/0.05|12 +sym>=1.0``):

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


@dataclass(frozen=True)
class SelectorConfig:
    full_threshold: float = 0.5
    stub_threshold: float = 0.05
    max_files: int = 12
    symbol_min_score: float = 1.0


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


def apply_policy(
    items: list[dict[str, Any]],
    groups: list[FileGroup],
    file_p: dict[str, float],
    symbol_score: dict[str, float] | None,
    cfg: SelectorConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pure function: ranked items + probabilities -> tiered items (full first, then stubs)."""
    ranked = sorted(groups, key=lambda g: (-file_p.get(g.file_id, 0.0), g.pos))

    def pick(threshold: float, keep_first: bool) -> list[FileGroup]:
        chosen: list[FileGroup] = [g for g in groups if g.pos == 0] if keep_first else []
        for g in ranked:
            if len(chosen) >= cfg.max_files:
                break
            if file_p.get(g.file_id, 0.0) >= threshold and g not in chosen:
                chosen.append(g)
        return chosen

    full = pick(cfg.full_threshold, keep_first=True)
    stubs = [g for g in pick(cfg.stub_threshold, keep_first=False) if g not in full]
    full.sort(key=lambda g: (-file_p.get(g.file_id, 0.0), g.pos))
    stubs.sort(key=lambda g: (-file_p.get(g.file_id, 0.0), g.pos))

    out: list[dict[str, Any]] = []
    symbols_dropped = 0
    per_file: list[list[dict[str, Any]]] = []
    for g in full:
        p = file_p.get(g.file_id, 0.0)
        kept = list(g.items)
        if symbol_score is not None:
            kept = [
                it
                for it in g.items
                if symbol_score.get(str(it.get("symbol_id")), 0.0) >= cfg.symbol_min_score
            ]
            if not kept:  # never lose a file the policy decided to show
                kept = [max(g.items, key=lambda it: float(it.get("score") or 0.0))]
            symbols_dropped += len(g.items) - len(kept)
            kept.sort(key=lambda it: -symbol_score.get(str(it.get("symbol_id")), 0.0))
        entries = []
        for it in kept:
            entry = dict(it)
            entry["tier"] = "full"
            entry["file_relevance"] = round(p, 3)
            if symbol_score is not None:
                entry["symbol_relevance"] = round(
                    symbol_score.get(str(it.get("symbol_id")), 0.0), 2
                )
            entries.append(entry)
        per_file.append(entries)
    # Round-robin over the full files: the assembler cuts from the bottom when the budget runs out,
    # so each full file's best symbol comes before any file's second one.
    for depth in range(max((len(e) for e in per_file), default=0)):
        out.extend(e[depth] for e in per_file if depth < len(e))
    for g in stubs:
        out.append(_stub_item(g, file_p.get(g.file_id, 0.0)))

    stats = {
        "files_in": len(groups),
        "files_full": len(full),
        "files_stub": len(stubs),
        "files_dropped": len(groups) - len(full) - len(stubs),
        "symbols_in": len(items),
        "symbols_out": len(out),
        "symbols_dropped_in_full_files": symbols_dropped,
        "symbol_pruning": symbol_score is not None,
    }
    return out, stats


class Selector:
    def __init__(self, client: JevClient, cfg: SelectorConfig | None = None) -> None:
        self.client = client
        self.cfg = cfg or SelectorConfig()

    def select(
        self, items: list[dict[str, Any]], *, task_text: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Tiered items + a ``coverage.selector`` block. Fail-open: on any error the input items are
        returned unchanged with ``status: "error"``."""
        t0 = time.perf_counter()
        info: dict[str, Any] = {"method": "jev", "model": self.client.model, "status": "ok"}
        if not items:
            info.update(status="skipped", reason="no candidates")
            return items, info
        groups = group_files(items)
        repo_label = ", ".join(
            sorted({str(it.get("repo_id") or "") for it in items if it.get("repo_id")})
        )

        with cf.ThreadPoolExecutor(max_workers=2) as ex:
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
                file_answers, file_usage = f_files.result()
            except JevError as exc:
                logger.warning(
                    "selector: file decision failed, answer left unselected",
                    extra={"detail": str(exc)},
                )
                info.update(status="error", reason=str(exc)[:200], ms=_ms(t0))
                return items, info
            symbol_score: dict[str, float] | None
            try:
                sym_answers, sym_usage = f_syms.result()
                symbol_score = parse_symbol_answers(sym_answers, items)
            except JevError as exc:
                logger.warning(
                    "selector: symbol decision failed, files tiered without symbol pruning",
                    extra={"detail": str(exc)},
                )
                symbol_score, sym_usage = None, {}
                info.update(status="partial", reason=str(exc)[:200])

        file_p = parse_file_answers(file_answers, groups)
        out, stats = apply_policy(items, groups, file_p, symbol_score, self.cfg)
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
        }
        return out, info


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
