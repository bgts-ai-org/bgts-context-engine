"""PR-replay ablation benchmark: Voyage embeddings alone vs Voyage + BCE.

The question this answers is "what does the deterministic engine add on top of the embedding
model?". Every merged PR is replayed as a task:

- **snapshot**: a database that indexes the repository at the PR's *base* commit (the code a
  developer had in front of them before doing the work) with the pinned Voyage model;
- **task text**: the PR title + description (``full`` variant) or the title alone (``title``);
- **ground truth**: the symbols the PR actually modified, obtained by mapping the diff hunks
  (old-side line ranges) onto the symbols of the indexed snapshot. Symbols that only exist after
  the PR cannot be retrieved from the snapshot and are therefore not part of the truth set; test
  files are excluded (a task description always resembles the tests that assert it).

Two systems answer the same task against the same snapshot and the *same* embedding rows:

- ``voyage``: pure vector search - encode the task, take the K nearest non-test symbols (the
  same filter BCE's own semantic anchor source applies, so the comparison is about ranking, not
  about a trivial test filter). ``voyage_raw`` (unfiltered nearest neighbours) is reported too.
- ``bce``: the full pipeline - those Voyage neighbours as the semantic anchor source plus
  explicit / lexical anchors, deterministic graph expansion, scoring and diversity-aware
  narrowing (:func:`bce.tools.layer3.suggest_change_sites`).

Per case and system we compute Recall@K, Precision@K, F1, MRR, Hit@K at symbol level and
Recall/Precision at file level; the report aggregates scored cases and expresses BCE's gain in
absolute percentage points and relative percent.
"""

from __future__ import annotations

import base64
import json
import re
import statistics
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bce.core.orchestrator.text import is_test_symbol

#: Hunks with ``old_len == 0`` are insertions *after* the given line; the touched symbol is the
#: one enclosing that line or the next one, so the range is widened by this many lines.
_INSERTION_SPAN = 1
#: Over-fetch factor when filtering test symbols out of the nearest neighbours (mirrors
#: ``bce.tools.layer3.orchestration._AUTO_SEMANTIC_OVERFETCH``).
_NEIGHBOUR_OVERFETCH = 4

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>])")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

SYSTEMS: tuple[str, ...] = ("voyage_raw", "voyage", "bce")
VARIANTS: tuple[str, ...] = ("full", "title")
#: Splits a report aggregates separately. Constants tuned against ``tune`` are validated once on
#: ``holdout``, so a number quoted from the holdout block was never used to pick a weight.
SPLITS: tuple[str, ...] = ("tune", "holdout")


# --------------------------------------------------------------------------- specs / cases


@dataclass(slots=True)
class PRSpec:
    """One pull request to replay."""

    number: int
    title: str
    description: str = ""
    source_commit: str = ""
    destination_commit: str = ""
    #: Commit the per-PR database indexes (normally ``merge-base(source, destination)``).
    indexed_commit: str = ""
    db_name: str = ""
    branch: str = ""
    #: ``False`` keeps the PR in the report but out of the aggregate (user-marked "skor dışı").
    scored: bool = True
    #: "tune" (constants may be fitted on it) or "holdout" (measured once, never fitted).
    split: str = "holdout"


@dataclass(slots=True)
class GroundTruth:
    #: file_ids of changed, non-test files that exist in the snapshot.
    files: list[str] = field(default_factory=list)
    #: symbol_ids of the innermost snapshot symbols overlapping a changed hunk.
    symbols: list[str] = field(default_factory=list)
    #: symbol_id -> {name, kind, file_id, line, end_line} for the report.
    symbol_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Changed paths dropped from the truth set and why (docs, tests, new files ...).
    skipped: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BenchPRCase:
    spec: PRSpec
    truth: GroundTruth
    #: variant -> task text.
    tasks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"spec": asdict(self.spec), "truth": asdict(self.truth), "tasks": dict(self.tasks)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BenchPRCase:
        return cls(
            spec=PRSpec(**raw["spec"]),
            truth=GroundTruth(**raw["truth"]),
            tasks=dict(raw.get("tasks") or {}),
        )


def load_cases(path: str | Path) -> list[BenchPRCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [BenchPRCase.from_dict(item) for item in raw]


def save_cases(cases: list[BenchPRCase], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps([c.to_dict() for c in cases], ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- task text


def clean_pr_text(text: str) -> str:
    """Strip Bitbucket markdown escapes / zero-width characters; keep the wording untouched."""
    out = _ZERO_WIDTH_RE.sub("", text or "")
    out = _MARKDOWN_ESCAPE_RE.sub(r"\1", out)
    return out.strip()


def task_variants(spec: PRSpec) -> dict[str, str]:
    title = clean_pr_text(spec.title)
    description = clean_pr_text(spec.description)
    full = f"{title}\n\n{description}".strip() if description else title
    return {"full": full, "title": title}


# --------------------------------------------------------------------------- bitbucket


def fetch_pr_specs(
    numbers: list[int],
    *,
    workspace: str,
    repo_slug: str,
    username: str,
    token: str,
    unscored: set[int] | None = None,
    tune: set[int] | None = None,
) -> list[PRSpec]:
    """Pull title/description/commits for each PR from the Bitbucket Cloud REST API."""
    unscored = unscored or set()
    tune = tune or set()
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    fields = (
        "id,title,description,state,source.commit.hash,source.branch.name,"
        "destination.commit.hash,destination.branch.name"
    )
    specs: list[PRSpec] = []
    for number in numbers:
        url = (
            f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/pullrequests/"
            f"{number}?fields={fields}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
            data = json.load(resp)
        specs.append(
            PRSpec(
                number=number,
                title=data.get("title") or "",
                description=data.get("description") or "",
                source_commit=data["source"]["commit"]["hash"],
                destination_commit=data["destination"]["commit"]["hash"],
                branch=(data.get("source") or {}).get("branch", {}).get("name", ""),
                scored=number not in unscored,
                split="tune" if number in tune else "holdout",
            )
        )
    return specs


# --------------------------------------------------------------------------- git / diff


class GitDiffError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603,S607 - fixed executable, arguments are shas/paths
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise GitDiffError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def merge_base(repo: str | Path, a: str, b: str) -> str:
    return _git(repo, "merge-base", a, b).strip()


def changed_old_paths(repo: str | Path, base: str, head: str) -> dict[str, str]:
    """Pre-change paths of files the range touched -> git status letter (M/D/R/A...).

    New files (``A``) are returned too so the report can explain why they are not in the truth
    set; renames map the *old* path.
    """
    out: dict[str, str] = {}
    for line in _git(repo, "diff", "-M", "--name-status", base, head).splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        old_path = parts[1]
        out[old_path] = status
    return out


def parse_unified_diff(text: str) -> dict[str, list[tuple[int, int]]]:
    """Old-side changed line ranges per pre-change path from ``git diff --unified=0`` output.

    Deletions/modifications map to ``[start, start + len - 1]``; pure insertions (``len == 0``)
    to ``[start, start + _INSERTION_SPAN]`` (the enclosing symbol of the insertion point).
    New files (``--- /dev/null``) are skipped.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            if path.startswith("a/"):
                path = path[2:]
            current = path
            ranges.setdefault(current, [])
            continue
        if current is None:
            continue
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        length = int(m.group(2)) if m.group(2) is not None else 1
        if length == 0:
            if start == 0:
                ranges[current].append((1, 1 + _INSERTION_SPAN))
            else:
                ranges[current].append((start, start + _INSERTION_SPAN))
        else:
            ranges[current].append((start, start + length - 1))
    return ranges


def innermost_overlapping(
    symbols: list[dict[str, Any]], ranges: list[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Symbols whose ``[line, end_line]`` overlaps any range, minus those that strictly contain
    another overlapping symbol (so a method hit does not also credit its class)."""
    hits: list[dict[str, Any]] = []
    for sym in symbols:
        line = sym.get("line")
        if line is None:
            continue
        end = sym.get("end_line") or line
        if any(line <= b and end >= a for a, b in ranges):
            hits.append(sym)

    def contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
        if outer is inner:
            return False
        o_start, o_end = outer["line"], outer.get("end_line") or outer["line"]
        i_start, i_end = inner["line"], inner.get("end_line") or inner["line"]
        return o_start <= i_start and o_end >= i_end and (o_start, o_end) != (i_start, i_end)

    leaves = [s for s in hits if not any(contains(s, other) for other in hits)]
    leaves.sort(key=lambda s: (s["line"], s["symbol_id"]))
    return leaves


def _symbols_with_span(repository: Any, file_id: str) -> list[dict[str, Any]]:
    columns = ["symbol_id", "name", "kind", "line", "end_line"]
    rows = repository.client.cypher(
        "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: $fid}) "
        "RETURN s.symbol_id, s.name, s.kind, s.line, s.end_line ORDER BY s.line, s.symbol_id",
        {"fid": file_id},
        columns,
    )
    return [dict(zip(columns, row, strict=False)) for row in rows]


def _file_exists(repository: Any, file_id: str) -> bool:
    rows = repository.client.cypher(
        "MATCH (f:File {file_id: $fid}) RETURN f.file_id LIMIT 1", {"fid": file_id}, ["fid"]
    )
    return bool(rows)


def build_ground_truth(
    repo: str | Path, spec: PRSpec, repository: Any, repo_id: str
) -> GroundTruth:
    """Diff hunks of the PR mapped onto the indexed snapshot (see module docstring).

    The PR's own file set comes from ``merge-base(source, destination)..source``; the line
    ranges are then taken from ``indexed_commit..source`` restricted to those files so they are
    valid for the snapshot even when the snapshot is a later commit of the target branch.
    """
    base = merge_base(repo, spec.source_commit, spec.destination_commit)
    statuses = changed_old_paths(repo, base, spec.source_commit)
    truth = GroundTruth()
    pr_paths = sorted(statuses)
    if not pr_paths:
        return truth

    snapshot = spec.indexed_commit or base
    diff_text = _git(
        repo,
        "diff",
        "--unified=0",
        "-M",
        "--no-color",
        snapshot,
        spec.source_commit,
        "--",
        *pr_paths,
    )
    ranges_by_path = parse_unified_diff(diff_text)

    for path in pr_paths:
        status = statuses[path]
        if status == "A":
            truth.skipped[path] = "added by the PR (not in snapshot)"
            continue
        file_id = f"{repo_id}:{path}"
        if is_test_symbol(None, None, file_id):
            truth.skipped[path] = "test file"
            continue
        if not _file_exists(repository, file_id):
            truth.skipped[path] = "not indexed (non-code file)"
            continue
        ranges = ranges_by_path.get(path) or []
        symbols = _symbols_with_span(repository, file_id)
        if status == "D":
            leaves = innermost_overlapping(symbols, [(1, 10**9)])
        else:
            leaves = innermost_overlapping(symbols, ranges)
        truth.files.append(file_id)
        for sym in leaves:
            sid = sym["symbol_id"]
            if sid in truth.symbol_meta:
                continue
            truth.symbols.append(sid)
            truth.symbol_meta[sid] = {
                "name": sym.get("name"),
                "kind": sym.get("kind"),
                "file_id": file_id,
                "line": sym.get("line"),
                "end_line": sym.get("end_line"),
            }
        if not leaves:
            truth.skipped[path] = (
                "file-level truth only: changed lines map to no symbol (imports/usings/top-level)"
            )
    return truth


# --------------------------------------------------------------------------- retrieval


def _nearest_symbols(
    store: Any, repository: Any, vector: list[float], *, limit: int, skip_tests: bool
) -> list[str]:
    fetch = limit * (_NEIGHBOUR_OVERFETCH if skip_tests else 1)
    hits = store.search(vector, limit=fetch, kind="symbol")
    out: list[str] = []
    for hit in hits:
        sid = hit["ref_id"]
        if skip_tests:
            sym = repository.get_symbol(sid) or {}
            if is_test_symbol(sid, sym.get("name"), sym.get("file_id")):
                continue
        out.append(sid)
        if len(out) >= limit:
            break
    return out


@dataclass(slots=True)
class SystemRun:
    system: str
    returned: list[str]
    latency_ms: float
    #: Stage breakdown in milliseconds (BCE only: anchors / expand / candidates / score / narrow).
    timings: dict[str, float] = field(default_factory=dict)
    #: Anchors found and candidates scored before narrowing (BCE only).
    pool: dict[str, int] = field(default_factory=dict)
    #: ``voyage`` only: the whole non-test neighbour list handed to BCE as its semantic source
    #: (longer than K). Stored so BCE can be re-run from a report without touching the embedding
    #: API, and so the report can tell whether a truth symbol was ever in the model's pool.
    semantic_pool: list[str] = field(default_factory=list)
    #: ``bce`` only, trace data for the drop-stage diagnostics: anchor ids and the ranked pool
    #: before narrowing (score order). Not part of the metrics.
    anchor_ids: list[str] = field(default_factory=list)
    ranked_ids: list[str] = field(default_factory=list)


def semantic_pool_limit(k: int) -> int:
    """How many non-test neighbours the ``voyage`` baseline fetches (and BCE gets as anchors)."""
    from bce.tools.layer3.orchestration import AUTO_SEMANTIC_LIMIT

    return max(k, AUTO_SEMANTIC_LIMIT)


def run_baselines(
    repository: Any, store: Any, encoder: Any, task_text: str, *, k: int
) -> dict[str, SystemRun]:
    """``voyage_raw`` and ``voyage`` for one task; the query is encoded once."""
    t0 = time.perf_counter()
    vector = encoder.encode_query(task_text)
    encode_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    raw = _nearest_symbols(store, repository, vector, limit=k, skip_tests=False)
    raw_ms = (time.perf_counter() - t1) * 1000.0

    t2 = time.perf_counter()
    filtered = _nearest_symbols(
        store, repository, vector, limit=semantic_pool_limit(k), skip_tests=True
    )
    filtered_ms = (time.perf_counter() - t2) * 1000.0

    return {
        "voyage_raw": SystemRun("voyage_raw", raw, encode_ms + raw_ms),
        "voyage": SystemRun(
            "voyage", filtered[:k], encode_ms + filtered_ms, semantic_pool=list(filtered)
        ),
    }


def run_bce(
    repository: Any, task_text: str, semantic_pool: list[str], *, k: int, baseline_ms: float
) -> SystemRun:
    """The full engine with ``semantic_pool`` (the ``voyage`` neighbours) as its semantic source.

    BCE receives the same neighbours the ``voyage`` baseline is scored on, widened to the engine's
    own semantic pool (``AUTO_SEMANTIC_LIMIT``) so its narrowing has something to re-rank; only
    the first K are what ``voyage`` is credited with. ``baseline_ms`` (encode + search) is added
    to the latency so the number stays comparable with a live run.
    """
    from bce.tools.layer3.orchestration import AUTO_SEMANTIC_LIMIT, suggest_change_sites

    t0 = time.perf_counter()
    result = suggest_change_sites(
        repository,
        task_text=task_text,
        max_candidates=k,
        semantic_candidates=list(semantic_pool[:AUTO_SEMANTIC_LIMIT]),
        auto_semantic=False,
        trace=True,
    )
    payload = result["payload"]
    bce_ms = (time.perf_counter() - t0) * 1000.0
    return SystemRun(
        "bce",
        [site["symbol_id"] for site in payload["sites"]],
        baseline_ms + bce_ms,
        timings=dict(payload.get("timings") or {}),
        pool={
            "anchors": len(payload.get("anchors") or {}),
            "scored": int((payload.get("coverage") or {}).get("pool_size") or 0),
        },
        anchor_ids=sorted(payload.get("anchors") or {}),
        ranked_ids=list(payload.get("ranked_ids") or []),
    )


def run_systems(
    repository: Any,
    store: Any,
    encoder: Any,
    task_text: str,
    *,
    k: int,
    baselines: dict[str, SystemRun] | None = None,
) -> dict[str, SystemRun]:
    """Run ``voyage_raw``, ``voyage`` and ``bce`` for one task.

    With ``baselines`` (replayed from a previous report, see :func:`replay_from_report`) the
    embedding API is not called at all: the stored semantic pool feeds BCE and the ``voyage``
    list is re-cut to K from it.
    """
    if baselines is None or not baselines.get("voyage") or not baselines["voyage"].semantic_pool:
        baselines = run_baselines(repository, store, encoder, task_text, k=k)
    else:
        voyage = baselines["voyage"]
        baselines = {
            "voyage_raw": SystemRun(
                "voyage_raw",
                baselines["voyage_raw"].returned[:k],
                baselines["voyage_raw"].latency_ms,
            ),
            "voyage": SystemRun(
                "voyage",
                voyage.semantic_pool[:k],
                voyage.latency_ms,
                semantic_pool=voyage.semantic_pool,
            ),
        }
    bce = run_bce(
        repository,
        task_text,
        baselines["voyage"].semantic_pool,
        k=k,
        baseline_ms=baselines["voyage"].latency_ms,
    )
    return {**baselines, "bce": bce}


# --------------------------------------------------------------------------- metrics


@dataclass(slots=True)
class Metrics:
    recall: float
    precision: float
    f1: float
    mrr: float
    hit: float
    #: Recall when a returned symbol that *encloses* a truth symbol (its class, for a method)
    #: also counts. Shows how much of the strict miss is "right neighbourhood, wrong granularity".
    recall_lenient: float
    file_recall: float
    file_precision: float
    returned: int
    relevant: int

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


METRIC_KEYS: tuple[str, ...] = (
    "recall",
    "precision",
    "f1",
    "mrr",
    "hit",
    "recall_lenient",
    "file_recall",
    "file_precision",
)

#: Symbol kinds that are containers rather than change sites (for the kind breakdown).
CONTAINER_KINDS: frozenset[str] = frozenset({"class", "interface", "constructor", "type", "enum"})


def _encloses(outer: dict[str, Any] | None, inner: dict[str, Any]) -> bool:
    if not outer or outer.get("file_id") != inner.get("file_id"):
        return False
    o_start, i_start = outer.get("line"), inner.get("line")
    if o_start is None or i_start is None:
        return False
    o_end = outer.get("end_line") or o_start
    i_end = inner.get("end_line") or i_start
    return o_start <= i_start and o_end >= i_end


def compute_metrics(
    returned: list[str],
    relevant: list[str],
    *,
    returned_files: list[str],
    relevant_files: list[str],
    returned_spans: dict[str, dict[str, Any]] | None = None,
    relevant_spans: dict[str, dict[str, Any]] | None = None,
) -> Metrics:
    rel = set(relevant)
    ret = list(dict.fromkeys(returned))
    hits = [sid for sid in ret if sid in rel]
    recall = len(set(hits)) / len(rel) if rel else 0.0
    precision = len(hits) / len(ret) if ret else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    mrr = 0.0
    for rank, sid in enumerate(ret, start=1):
        if sid in rel:
            mrr = 1.0 / rank
            break

    lenient_found = set(hits)
    if returned_spans and relevant_spans:
        for rsid in rel - lenient_found:
            inner = relevant_spans.get(rsid) or {}
            if any(_encloses(returned_spans.get(sid), inner) for sid in ret):
                lenient_found.add(rsid)
    recall_lenient = len(lenient_found) / len(rel) if rel else 0.0

    rel_files = set(relevant_files)
    ret_files = list(dict.fromkeys(f for f in returned_files if f))
    file_hits = [f for f in ret_files if f in rel_files]
    return Metrics(
        recall=recall,
        precision=precision,
        f1=f1,
        mrr=mrr,
        hit=1.0 if hits else 0.0,
        recall_lenient=recall_lenient,
        file_recall=len(set(file_hits)) / len(rel_files) if rel_files else 0.0,
        file_precision=len(file_hits) / len(ret_files) if ret_files else 0.0,
        returned=len(ret),
        relevant=len(rel),
    )


# --------------------------------------------------------------------------- runner


@dataclass(slots=True)
class CaseOutcome:
    number: int
    title: str
    scored: bool
    split: str
    variant: str
    db_name: str
    relevant_symbols: int
    relevant_files: int
    systems: dict[str, dict[str, Any]]
    #: Task-text quality axis (see :func:`task_text_stats`).
    task_stats: dict[str, int] = field(default_factory=dict)


#: Metrics reported per extra K column (item 12); the rest stay on the headline K.
K_COLUMN_METRICS: tuple[str, ...] = ("recall", "precision", "mrr", "hit", "file_recall")

#: Task-text buckets for the quality axis (item 13): does the text name any code identifier?
TEXT_BUCKETS: tuple[str, ...] = ("names_code", "prose_only")


def text_bucket(stats: dict[str, int]) -> str:
    return "names_code" if int(stats.get("identifiers", 0) or 0) > 0 else "prose_only"


def _win_tie_loss(rows: list[CaseOutcome], metric: str, *, at: str | None = None) -> dict[str, int]:
    def value(case: CaseOutcome, system: str) -> float:
        entry = case.systems[system]
        if at is not None:
            return float(entry.get("metrics_at", {}).get(at, entry["metrics"])[metric])
        return float(entry["metrics"][metric])

    wins = sum(1 for c in rows if value(c, "bce") > value(c, "voyage"))
    losses = sum(1 for c in rows if value(c, "bce") < value(c, "voyage"))
    return {"wins": wins, "ties": len(rows) - wins - losses, "losses": losses}


@dataclass(slots=True)
class AblationReport:
    k: int
    encoder_model: str
    variants: list[str]
    cases: list[CaseOutcome]
    #: Extra K columns derived from the K-long lists (see :func:`score_runs`).
    ks: list[int] = field(default_factory=list)
    #: The retrieval profile BCE ran with (name + the model-specific constants), so two runs of
    #: the same snapshots can be told apart by their engine tuning.
    profile: dict[str, Any] = field(default_factory=dict)

    def _rows(
        self, variant: str, *, scored_only: bool, split: str | None, level: str = "symbol"
    ) -> list[CaseOutcome]:
        """Cases entering an aggregate. ``level="file"`` admits PRs whose diff touched no
        snapshot symbol but did touch indexed files (item 11: they are scored at file level)."""
        return [
            c
            for c in self.cases
            if c.variant == variant
            and (c.scored or not scored_only)
            and (split is None or c.split == split)
            and (c.relevant_symbols > 0 if level == "symbol" else c.relevant_files > 0)
        ]

    def aggregate(
        self, variant: str, *, scored_only: bool = True, split: str | None = None
    ) -> dict[str, Any]:
        rows = self._rows(variant, scored_only=scored_only, split=split)
        out: dict[str, Any] = {
            "cases": len(rows),
            "prs": sorted(c.number for c in rows),
            "systems": {},
            "gain_bce_vs_voyage": {},
            "returned_kind_share": {},
        }
        for system in SYSTEMS:
            agg: dict[str, float] = {}
            for key in METRIC_KEYS:
                values = [c.systems[system]["metrics"][key] for c in rows]
                agg[key] = statistics.mean(values) if values else 0.0
            lat = [c.systems[system]["latency_ms"] for c in rows]
            agg["latency_median_ms"] = statistics.median(lat) if lat else 0.0
            retention = [
                c.systems[system]["semantic_retention"]
                for c in rows
                if c.systems[system].get("semantic_retention") is not None
            ]
            agg["semantic_retention"] = statistics.mean(retention) if retention else 0.0
            out["systems"][system] = {k: round(v, 4) for k, v in agg.items()}
            kinds: dict[str, int] = {}
            for c in rows:
                for kind, n in (c.systems[system].get("kinds") or {}).items():
                    kinds[kind] = kinds.get(kind, 0) + n
            total = sum(kinds.values()) or 1
            share = {k: round(v / total, 4) for k, v in sorted(kinds.items())}
            share["_containers"] = round(
                sum(v for k, v in kinds.items() if k in CONTAINER_KINDS) / total, 4
            )
            out["returned_kind_share"][system] = share
        out["stage_ms_median"] = _median_timings(rows)
        pools = [c.systems["bce"].get("pool") or {} for c in rows]
        out["pool_median"] = {
            key: int(statistics.median([p.get(key, 0) for p in pools]))
            for key in ("anchors", "scored")
            if any(p for p in pools)
        }
        for key in METRIC_KEYS:
            base = out["systems"]["voyage"][key]
            ours = out["systems"]["bce"][key]
            out["gain_bce_vs_voyage"][key] = {
                "abs_pp": round((ours - base) * 100.0, 2),
                "rel_pct": round(((ours - base) / base) * 100.0, 1) if base else None,
                **_win_tie_loss(rows, key),
            }
        out["at_k"] = self._aggregate_at_k(rows)
        out["truth_stages"] = self._aggregate_truth_stages(rows)
        out["file_level"] = self._aggregate_file_level(
            self._rows(variant, scored_only=scored_only, split=split, level="file")
        )
        out["by_text_bucket"] = self._aggregate_text_buckets(rows)
        return out

    def _aggregate_at_k(self, rows: list[CaseOutcome]) -> dict[str, dict[str, Any]]:
        """``str(k) -> {systems: {system: {metric: mean}}, wtl: {metric: W-T-L}}`` (item 12)."""
        out: dict[str, dict[str, Any]] = {}
        for k in self.ks:
            key = str(k)
            if not rows or any(key not in (c.systems["bce"].get("metrics_at") or {}) for c in rows):
                continue
            systems: dict[str, dict[str, float]] = {}
            for system in SYSTEMS:
                systems[system] = {
                    metric: round(
                        statistics.mean(c.systems[system]["metrics_at"][key][metric] for c in rows),
                        4,
                    )
                    for metric in K_COLUMN_METRICS
                }
            out[key] = {
                "systems": systems,
                "wtl": {m: _win_tie_loss(rows, m, at=key) for m in K_COLUMN_METRICS},
            }
        return out

    @staticmethod
    def _aggregate_truth_stages(rows: list[CaseOutcome]) -> dict[str, Any]:
        """Where BCE lost the truth symbols, over all truths of the rows (item 5).

        ``by_channel`` crosses the entry channel with the outcome, so "reached through the graph
        and then narrowed out" is distinguishable from "never in the pool".
        """
        stage_counts = dict.fromkeys(TRUTH_STAGES, 0)
        channel_counts = dict.fromkeys(TRUTH_CHANNELS, 0)
        cross: dict[str, dict[str, int]] = {
            ch: dict.fromkeys(TRUTH_STAGES, 0) for ch in TRUTH_CHANNELS
        }
        narrowed_pool_ranks: list[int] = []
        total = 0
        for c in rows:
            for item in c.systems["bce"].get("truth_trace") or []:
                total += 1
                stage_counts[item["stage"]] += 1
                channel_counts[item["channel"]] += 1
                cross[item["channel"]][item["stage"]] += 1
                if item["stage"] == "narrowed_out" and item.get("pool_rank"):
                    narrowed_pool_ranks.append(int(item["pool_rank"]))
        return {
            "truths": total,
            "stages": stage_counts,
            "channels": channel_counts,
            "by_channel": cross,
            "narrowed_out_pool_rank_median": (
                int(statistics.median(narrowed_pool_ranks)) if narrowed_pool_ranks else None
            ),
        }

    @staticmethod
    def _aggregate_file_level(rows: list[CaseOutcome]) -> dict[str, Any]:
        """File recall / precision over every PR with at least one truth file (item 11)."""
        out: dict[str, Any] = {
            "cases": len(rows),
            "prs": sorted(c.number for c in rows),
            "systems": {},
        }
        for system in SYSTEMS:
            out["systems"][system] = {
                key: round(
                    statistics.mean(c.systems[system]["metrics"][key] for c in rows)
                    if rows
                    else 0.0,
                    4,
                )
                for key in ("file_recall", "file_precision")
            }
        out["wtl"] = {key: _win_tie_loss(rows, key) for key in ("file_recall", "file_precision")}
        return out

    @staticmethod
    def _aggregate_text_buckets(rows: list[CaseOutcome]) -> dict[str, Any]:
        """Recall / hit per system split by whether the task text names any identifier (item 13)."""
        out: dict[str, Any] = {}
        for bucket in TEXT_BUCKETS:
            sub = [c for c in rows if text_bucket(c.task_stats) == bucket]
            entry: dict[str, Any] = {
                "cases": len(sub),
                "prs": sorted(c.number for c in sub),
                "median_content_tokens": (
                    int(statistics.median(c.task_stats.get("content_tokens", 0) for c in sub))
                    if sub
                    else 0
                ),
                "systems": {},
            }
            for system in SYSTEMS:
                entry["systems"][system] = {
                    key: round(
                        statistics.mean(c.systems[system]["metrics"][key] for c in sub)
                        if sub
                        else 0.0,
                        4,
                    )
                    for key in ("recall", "mrr", "hit", "file_recall")
                }
            out[bucket] = entry
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "ks": list(self.ks),
            "encoder_model": self.encoder_model,
            "profile": dict(self.profile),
            "variants": list(self.variants),
            "summary": {v: self.aggregate(v) for v in self.variants},
            "summary_by_split": {
                split: {v: self.aggregate(v, split=split) for v in self.variants}
                for split in SPLITS
            },
            "summary_all_prs": {v: self.aggregate(v, scored_only=False) for v in self.variants},
            "cases": [asdict(c) for c in self.cases],
        }


def _median_timings(rows: list[CaseOutcome]) -> dict[str, float]:
    """Median of each BCE stage timing over the cases (empty when nothing was recorded)."""
    stages: dict[str, list[float]] = {}
    for case in rows:
        for stage, value in (case.systems["bce"].get("timings") or {}).items():
            stages.setdefault(stage, []).append(float(value))
    return {stage: round(statistics.median(v), 1) for stage, v in sorted(stages.items())}


def _snapshot_model(conn: Any) -> str:
    """The single embedding model id pinned in the snapshot's symbol rows."""
    from bce.indexing.embedder.encoder import EncoderConfigError

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT model FROM embeddings WHERE kind = 'symbol'")
        models = sorted(str(r[0]) for r in cur.fetchall())
    if len(models) != 1:
        raise EncoderConfigError(f"snapshot has {len(models)} embedding models: {models}")
    return models[0]


def _encoder_for_snapshot(conn: Any) -> Any:
    """Encoder matching the model pinned in the snapshot's ``embeddings.model`` rows.

    Refuses to run when the configured provider cannot produce that model: a mismatched query
    encoder would return plausible-looking nonsense (see ``EncoderConfigError``).
    """
    from bce.config import get_settings
    from bce.indexing.embedder.encoder import (
        EncoderConfigError,
        VoyageEncoder,
        build_default_encoder,
    )

    model_id = _snapshot_model(conn)
    settings = get_settings()
    if model_id.startswith("voyage") and settings.embedding_provider.strip().lower() == "voyage":
        name, _, dim = model_id.rpartition("-")
        encoder = VoyageEncoder(settings.voyage_api_key, model=name, dim=int(dim))
    else:
        encoder = build_default_encoder()
    if encoder.model_id != model_id:
        raise EncoderConfigError(
            f"query encoder {encoder.model_id!r} does not match snapshot model {model_id!r}"
        )
    return encoder


def _symbol_span(repository: Any, symbol_id: str) -> dict[str, Any]:
    columns = ["symbol_id", "name", "kind", "file_id", "line", "end_line"]
    rows = repository.client.cypher(
        "MATCH (s:Symbol {symbol_id: $sid}) "
        "RETURN s.symbol_id, s.name, s.kind, s.file_id, s.line, s.end_line LIMIT 1",
        {"sid": symbol_id},
        columns,
    )
    return dict(zip(columns, rows[0], strict=False)) if rows else {}


def semantic_retention(baseline: list[str], ours: list[str], relevant: set[str]) -> float | None:
    """Share of the embedding baseline's *correct* hits that the engine still returns.

    The headline metrics say whether BCE is better on average; this says whether it is better
    *for the right reason*. A rerank that finds new truths while throwing away the ones the model
    already had is a wash on recall and a regression in trust.
    """
    baseline_hits = {sid for sid in baseline if sid in relevant}
    if not baseline_hits:
        return None
    return len(baseline_hits & set(ours)) / len(baseline_hits)


#: Stages a truth symbol can be lost at (in pipeline order). ``returned`` is not a loss.
TRUTH_STAGES: tuple[str, ...] = ("not_reached", "narrowed_out", "returned")
#: How a truth symbol entered the candidate pool (``none`` when it never did).
TRUTH_CHANNELS: tuple[str, ...] = ("semantic", "anchor", "expansion", "none")


def trace_truth(
    truth_symbols: list[str], run: SystemRun, semantic_pool: list[str]
) -> list[dict[str, Any]]:
    """Per truth symbol: how it entered BCE's pool and where it was lost (item 5 diagnostics).

    ``channel``: ``semantic`` (in the model's pool handed to BCE), ``anchor`` (found by an
    explicit / lexical / other anchor source but not by the model), ``expansion`` (reached only
    through the graph walk), ``none`` (never in the scored pool). ``stage``: ``returned``,
    ``narrowed_out`` (in the ranked pool, cut by narrowing) or ``not_reached``. ``pool_rank`` is
    the 1-based position in the ranked pool before narrowing, ``semantic_rank`` the 1-based
    position in the model's pool.
    """
    pool_pos = {sid: i + 1 for i, sid in enumerate(run.ranked_ids)}
    returned_pos = {sid: i + 1 for i, sid in enumerate(run.returned)}
    semantic_pos = {sid: i + 1 for i, sid in enumerate(semantic_pool)}
    anchors = set(run.anchor_ids)
    out: list[dict[str, Any]] = []
    for sid in truth_symbols:
        if sid in semantic_pos:
            channel = "semantic"
        elif sid in anchors:
            channel = "anchor"
        elif sid in pool_pos:
            channel = "expansion"
        else:
            channel = "none"
        # Anchors (semantic pool included) always enter the pool at distance 0, so without a
        # recorded ranked pool they still count as reached.
        if sid in returned_pos:
            stage = "returned"
        elif sid in pool_pos or channel in ("semantic", "anchor"):
            stage = "narrowed_out"
        else:
            stage = "not_reached"
        out.append(
            {
                "symbol_id": sid,
                "channel": channel,
                "stage": stage,
                "semantic_rank": semantic_pos.get(sid),
                "pool_rank": pool_pos.get(sid),
                "returned_rank": returned_pos.get(sid),
            }
        )
    return out


def task_text_stats(task_text: str) -> dict[str, int]:
    """Deterministic "how much does the task text carry" axis (item 13).

    ``chars`` / ``content_tokens`` measure length; ``identifiers`` counts identifier-looking
    tokens (``camelCase``, ``snake_case``, ``Type.member``, backticks) - the ones the explicit
    anchor channel can resolve - so results can be split into "the text names code" versus "the
    text is prose only".
    """
    from bce.core.orchestrator.text import content_tokens, explicit_candidates

    return {
        "chars": len(task_text or ""),
        "content_tokens": len(content_tokens(task_text or "")),
        "identifiers": len(explicit_candidates(task_text or "")),
    }


def score_runs(
    repository: Any,
    case: BenchPRCase,
    runs: dict[str, SystemRun],
    *,
    ks: tuple[int, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Metrics + diagnostics (kinds, hits, retention, stage timings) for one case.

    ``ks`` adds ``metrics_at[str(k)]`` for each extra K by truncating the returned list (item
    12: one run, several K columns). Exact for ``voyage`` (a nearest-neighbour list is a prefix
    chain) and for BCE since its narrowing is K-monotonic (the K=n list is the first n of the
    K=n+1 list).
    """
    relevant = set(case.truth.symbols)
    relevant_spans = {
        sid: {**meta, "symbol_id": sid} for sid, meta in case.truth.symbol_meta.items()
    }
    baseline = runs["voyage"].returned if "voyage" in runs else []
    semantic_pool = runs["voyage"].semantic_pool if "voyage" in runs else []
    systems: dict[str, dict[str, Any]] = {}
    for name, run in runs.items():
        spans = {sid: _symbol_span(repository, sid) for sid in run.returned}

        def _metrics(returned: list[str], spans: dict[str, dict[str, Any]] = spans) -> Metrics:
            files = [spans[sid].get("file_id") or "" for sid in returned]
            return compute_metrics(
                returned,
                case.truth.symbols,
                returned_files=files,
                relevant_files=case.truth.files,
                returned_spans=spans,
                relevant_spans=relevant_spans,
            )

        metrics = _metrics(run.returned)
        kinds: dict[str, int] = {}
        for sid in run.returned:
            kind = str(spans[sid].get("kind") or "unknown")
            kinds[kind] = kinds.get(kind, 0) + 1
        entry: dict[str, Any] = {
            "metrics": metrics.to_dict(),
            "metrics_at": {str(k): _metrics(run.returned[:k]).to_dict() for k in ks},
            "latency_ms": round(run.latency_ms, 1),
            "returned": run.returned,
            "hits": [sid for sid in run.returned if sid in relevant],
            "kinds": dict(sorted(kinds.items())),
            "semantic_retention": semantic_retention(baseline, run.returned, relevant),
            "timings": {k: round(v, 1) for k, v in sorted(run.timings.items())},
            "pool": dict(sorted(run.pool.items())),
        }
        if name == "voyage" and run.semantic_pool:
            entry["semantic_pool"] = list(run.semantic_pool)
        if name == "bce" and (run.ranked_ids or run.anchor_ids):
            entry["truth_trace"] = trace_truth(case.truth.symbols, run, semantic_pool)
        systems[name] = entry
    return systems


ReplayKey = tuple[int, str, str]


def replay_from_report(path: str | Path) -> dict[ReplayKey, SystemRun]:
    """Returned lists of a previous report, keyed by ``(pr, variant, system)``.

    Lets metric changes be re-scored without re-running retrieval (BCE's graph stages take tens
    of seconds per task on a 15k-symbol snapshot; the Voyage calls cost API quota).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[ReplayKey, SystemRun] = {}
    for case in data.get("cases", []):
        for system, entry in case.get("systems", {}).items():
            out[(int(case["number"]), str(case["variant"]), str(system))] = SystemRun(
                system=str(system),
                returned=list(entry.get("returned") or []),
                latency_ms=float(entry.get("latency_ms") or 0.0),
                timings=dict(entry.get("timings") or {}),
                pool=dict(entry.get("pool") or {}),
                semantic_pool=list(entry.get("semantic_pool") or []),
            )
    return out


def run_ablation(
    cases: list[BenchPRCase],
    *,
    k: int = 10,
    ks: tuple[int, ...] = (),
    variants: tuple[str, ...] = VARIANTS,
    progress: Any | None = None,
    replay: dict[ReplayKey, SystemRun] | None = None,
    rerun_bce: bool = False,
) -> AblationReport:
    """Replay every case against its snapshot database and score all systems.

    With ``replay`` (see :func:`replay_from_report`) a case/variant whose three systems are all
    present is re-scored from the stored lists instead of being retrieved again. With
    ``rerun_bce`` the stored ``voyage`` lists (and their semantic pool) are kept but BCE runs
    again - the loop for engine changes: no embedding API call, only the deterministic stages.
    ``ks`` adds per-K metric columns cut from the K-long lists; every value must be ``<= k``.
    """
    from bce.config import get_settings
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.storage.vector.store import VectorStore

    ks = tuple(sorted({int(x) for x in ks if 0 < int(x) <= k}))
    outcomes: list[CaseOutcome] = []
    encoder: Any | None = None
    encoder_model = "n/a"
    for case in cases:
        settings = get_settings().model_copy(update={"db_name": case.spec.db_name})
        with connection(settings) as conn:
            repository = GraphRepository(GraphClient(conn))
            store = VectorStore(conn)
            for variant in variants:
                task = case.tasks.get(variant) or task_variants(case.spec)[variant]
                replayed = {
                    system: replay[(case.spec.number, variant, system)]
                    for system in SYSTEMS
                    if replay is not None and (case.spec.number, variant, system) in replay
                }
                # The stored pool is complete when it is at least as long as the list that was
                # scored from it: a task whose neighbours are mostly tests legitimately has a
                # pool shorter than K (the live run had nothing more either).
                baselines_ok = (
                    "voyage" in replayed
                    and "voyage_raw" in replayed
                    and bool(replayed["voyage"].semantic_pool)
                    and len(replayed["voyage"].semantic_pool) >= len(replayed["voyage"].returned)
                )
                if len(replayed) == len(SYSTEMS) and not rerun_bce:
                    runs = replayed
                    if encoder_model == "n/a":
                        encoder_model = _snapshot_model(conn)
                elif baselines_ok:
                    runs = run_systems(repository, store, None, task, k=k, baselines=replayed)
                    if encoder_model == "n/a":
                        encoder_model = _snapshot_model(conn)
                else:
                    if encoder is None:
                        encoder = _encoder_for_snapshot(conn)
                        encoder_model = encoder.model_id
                    runs = run_systems(repository, store, encoder, task, k=k)
                outcome = CaseOutcome(
                    number=case.spec.number,
                    title=clean_pr_text(case.spec.title),
                    scored=case.spec.scored,
                    split=case.spec.split,
                    variant=variant,
                    db_name=case.spec.db_name,
                    relevant_symbols=len(case.truth.symbols),
                    relevant_files=len(case.truth.files),
                    systems=score_runs(repository, case, runs, ks=ks),
                    task_stats=task_text_stats(task),
                )
                outcomes.append(outcome)
                if progress is not None:
                    progress(outcome)
    from bce.core.orchestrator.profile import active_profile

    return AblationReport(
        k=k,
        encoder_model=encoder_model,
        variants=list(variants),
        cases=outcomes,
        ks=list(ks),
        profile=asdict(active_profile()),
    )


# --------------------------------------------------------------------------- markdown


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


_METRIC_LABELS: dict[str, str] = {
    "recall": "Recall@K",
    "precision": "Precision@K",
    "f1": "F1",
    "mrr": "MRR",
    "hit": "Hit@K",
    "recall_lenient": "Recall@K (lenient)",
    "file_recall": "File recall",
    "file_precision": "File precision",
}


def _render_block(lines: list[str], agg: dict[str, Any], title: str) -> None:
    lines.append("")
    lines.append(title)
    lines.append("")
    if not agg["cases"]:
        lines.append("_no cases_")
        return
    lines.append(
        "| System | "
        + " | ".join(_METRIC_LABELS[k] for k in METRIC_KEYS)
        + " | Retention | Latency (median) |"
    )
    lines.append("|" + "---|" * (len(METRIC_KEYS) + 3))
    for system in SYSTEMS:
        row = agg["systems"][system]
        lines.append(
            f"| `{system}` | "
            + " | ".join(_pct(row[k]) for k in METRIC_KEYS)
            + f" | {_pct(row['semantic_retention'])} | {row['latency_median_ms']:.0f} ms |"
        )
    lines.append("")
    lines.append(
        "Kinds of the returned symbols (share of all results; `_containers` = class / "
        "interface / constructor / type):"
    )
    lines.append("")
    kinds = sorted(
        {k for share in agg["returned_kind_share"].values() for k in share if k[0] != "_"}
    )
    lines.append("| System | containers | " + " | ".join(kinds) + " |")
    lines.append("|---|---|" + "---|" * len(kinds))
    for system in SYSTEMS:
        share = agg["returned_kind_share"][system]
        lines.append(
            f"| `{system}` | {_pct(share.get('_containers', 0.0))} | "
            + " | ".join(_pct(share.get(k, 0.0)) for k in kinds)
            + " |"
        )
    if agg["stage_ms_median"]:
        lines.append("")
        lines.append(
            "BCE stage latency (median ms): "
            + ", ".join(f"`{k}` {v:.0f}" for k, v in agg["stage_ms_median"].items())
        )
    lines.append("")
    lines.append(
        "BCE gain over `voyage` (absolute percentage points / relative %; per-PR wins-ties-losses):"
    )
    lines.append("")
    lines.append("| Metric | voyage | bce | Δ pp | Δ rel | W-T-L |")
    lines.append("|---|---|---|---|---|---|")
    for key in METRIC_KEYS:
        g = agg["gain_bce_vs_voyage"][key]
        rel = "-" if g["rel_pct"] is None else f"{g['rel_pct']:+.1f}%"
        lines.append(
            f"| {key} | {_pct(agg['systems']['voyage'][key])} | "
            f"{_pct(agg['systems']['bce'][key])} | {g['abs_pp']:+.1f} | {rel} | "
            f"{g['wins']}-{g['ties']}-{g['losses']} |"
        )
    _render_at_k(lines, agg.get("at_k") or {})
    _render_truth_stages(lines, agg.get("truth_stages") or {})
    _render_file_level(lines, agg.get("file_level") or {}, agg["cases"])
    _render_text_buckets(lines, agg.get("by_text_bucket") or {})


def _render_at_k(lines: list[str], at_k: dict[str, dict[str, Any]]) -> None:
    """One table per metric column: the same lists cut at every K (item 12)."""
    if not at_k:
        return
    lines.append("")
    lines.append(
        "Same lists cut at several K (voyage → bce, W-T-L per PR). BCE's narrowing is K-monotonic, "
        "so each K column is exactly what a run at that K would return:"
    )
    lines.append("")
    lines.append("| Metric | " + " | ".join(f"K={k}" for k in at_k) + " |")
    lines.append("|---|" + "---|" * len(at_k))
    for metric in K_COLUMN_METRICS:
        cells = []
        for entry in at_k.values():
            v = entry["systems"]["voyage"][metric]
            b = entry["systems"]["bce"][metric]
            w = entry["wtl"][metric]
            cells.append(f"{_pct(v)} → {_pct(b)} ({w['wins']}-{w['ties']}-{w['losses']})")
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")


def _render_truth_stages(lines: list[str], stages: dict[str, Any]) -> None:
    """Where the truth symbols were lost inside BCE (item 5)."""
    if not stages or not stages.get("truths"):
        return
    total = stages["truths"]
    lines.append("")
    lines.append(
        f"Where BCE lost the {total} truth symbols (rows = how the symbol entered the pool: "
        "`semantic` = in the model's pool handed to BCE, `anchor` = explicit/lexical anchor only, "
        "`expansion` = reached through the graph walk, `none` = never in the pool):"
    )
    lines.append("")
    lines.append("| Channel | truths | returned | narrowed out | not reached |")
    lines.append("|---|---|---|---|---|")
    for channel in TRUTH_CHANNELS:
        row = stages["by_channel"][channel]
        n = stages["channels"][channel]
        lines.append(
            f"| `{channel}` | {n} ({n / total * 100:.0f}%) | {row['returned']} | "
            f"{row['narrowed_out']} | {row['not_reached']} |"
        )
    st = stages["stages"]
    median = stages.get("narrowed_out_pool_rank_median")
    lines.append(
        f"| **total** | {total} | {st['returned']} ({st['returned'] / total * 100:.0f}%) | "
        f"{st['narrowed_out']} ({st['narrowed_out'] / total * 100:.0f}%) | "
        f"{st['not_reached']} ({st['not_reached'] / total * 100:.0f}%) |"
    )
    if median is not None:
        lines.append("")
        lines.append(
            f"Median pool rank of a narrowed-out truth: {median} (where in the scored pool the "
            "engine had it before cutting the list)."
        )


def _render_file_level(lines: list[str], fl: dict[str, Any], symbol_cases: int) -> None:
    """File-level block over every PR with a truth file, including zero-symbol PRs (item 11)."""
    if not fl or not fl.get("cases") or fl["cases"] == symbol_cases:
        return
    lines.append("")
    lines.append(
        f"File level over all {fl['cases']} PRs with at least one truth file "
        f"({fl['cases'] - symbol_cases} of them touched no snapshot symbol and are scored here only):"
    )
    lines.append("")
    lines.append("| Metric | voyage | bce | Δ pp | W-T-L |")
    lines.append("|---|---|---|---|---|")
    for key in ("file_recall", "file_precision"):
        v = fl["systems"]["voyage"][key]
        b = fl["systems"]["bce"][key]
        w = fl["wtl"][key]
        lines.append(
            f"| {key} | {_pct(v)} | {_pct(b)} | {(b - v) * 100:+.1f} | "
            f"{w['wins']}-{w['ties']}-{w['losses']} |"
        )


def _render_text_buckets(lines: list[str], buckets: dict[str, Any]) -> None:
    """Results split by whether the task text names any code identifier (item 13)."""
    if not buckets or not any(b.get("cases") for b in buckets.values()):
        return
    lines.append("")
    lines.append(
        "By task-text quality (`names_code` = the text contains at least one identifier-looking "
        "token such as `camelCase`, `snake_case` or `Type.member`; `prose_only` = none):"
    )
    lines.append("")
    lines.append(
        "| Bucket | PRs | median tokens | voyage recall / MRR / hit | bce recall / MRR / hit |"
    )
    lines.append("|---|---|---|---|---|")
    for name in TEXT_BUCKETS:
        b = buckets.get(name) or {}
        if not b.get("cases"):
            continue
        v = b["systems"]["voyage"]
        o = b["systems"]["bce"]
        lines.append(
            f"| `{name}` | {b['cases']} | {b['median_content_tokens']} | "
            f"{_pct(v['recall'])} / {v['mrr']:.2f} / {_pct(v['hit'])} | "
            f"{_pct(o['recall'])} / {o['mrr']:.2f} / {_pct(o['hit'])} |"
        )


def render_markdown(report: AblationReport) -> str:
    lines: list[str] = []
    # The pure-embedding baseline is keyed ``voyage`` in the JSON for historical reasons; the
    # actual model is whatever encoded the snapshots, so the title says that instead.
    model_name = report.encoder_model.rpartition("-")[0] or report.encoder_model
    lines.append(f"# {model_name} vs {model_name} + BCE - PR replay benchmark")
    lines.append("")
    lines.append(f"- Encoder: `{report.encoder_model}` (same embedding rows for both systems)")
    if report.profile:
        knobs = ", ".join(f"{k}={v}" for k, v in report.profile.items() if k != "name")
        lines.append(f"- Retrieval profile: `{report.profile.get('name', '?')}` ({knobs})")
    lines.append(f"- K (results per system): {report.k}")
    scored = sorted({c.number for c in report.cases if c.scored})
    unscored = sorted({c.number for c in report.cases if not c.scored})
    lines.append(f"- Scored PRs: {len(scored)} -> {', '.join(f'#{n}' for n in scored)}")
    if unscored:
        lines.append(f"- Reported but not scored: {', '.join(f'#{n}' for n in unscored)}")
    for split in SPLITS:
        numbers = sorted({c.number for c in report.cases if c.scored and c.split == split})
        lines.append(f"- `{split}`: {', '.join(f'#{n}' for n in numbers) or 'none'}")
    lines.append("")
    lines.append(
        "Ground truth = symbols of the pre-PR snapshot that the PR's diff touched (tests and "
        f"files created by the PR excluded). `voyage` = pure `{model_name}` search, nearest "
        f"non-test symbol embeddings (the key is historical, not the model); `voyage_raw` = "
        "unfiltered nearest neighbours; `bce` = full engine with the same "
        "neighbours as semantic anchors. Retention = share of `voyage`'s correct hits the system "
        "still returns. Engine constants were fitted on `tune` only, so `holdout` is the honest "
        "number."
    )

    for variant in report.variants:
        lines.append("")
        lines.append(f"## Variant `{variant}`")
        for split in SPLITS:
            agg = report.aggregate(variant, split=split)
            _render_block(lines, agg, f"### `{split}` ({agg['cases']} PRs)")
        agg = report.aggregate(variant)
        _render_block(lines, agg, f"### all scored ({agg['cases']} PRs)")

        lines.append("")
        lines.append("Per PR (symbol recall / MRR / file recall):")
        lines.append("")
        lines.append("| PR | Split | GT sym / files | voyage | bce | Δ recall pp |")
        lines.append("|---|---|---|---|---|---|")
        for c in report.cases:
            if c.variant != variant:
                continue
            v = c.systems["voyage"]["metrics"]
            b = c.systems["bce"]["metrics"]
            flag = "" if c.scored else " (not scored)"
            if c.relevant_symbols == 0:
                lines.append(
                    f"| #{c.number}{flag} | {c.split} | 0 / {c.relevant_files} | - | - | "
                    "no snapshot symbols in diff |"
                )
                continue
            lines.append(
                f"| #{c.number}{flag} | {c.split} | {c.relevant_symbols} / {c.relevant_files} | "
                f"{_pct(v['recall'])} / {v['mrr']:.2f} / {_pct(v['file_recall'])} | "
                f"{_pct(b['recall'])} / {b['mrr']:.2f} / {_pct(b['file_recall'])} | "
                f"{(b['recall'] - v['recall']) * 100:+.1f} |"
            )
    lines.append("")
    return "\n".join(lines)
