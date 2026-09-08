# Retrieval

This is the part of the engine that turns a sentence like *"fix the login timeout in the
meeting webhook"* into a small, ordered set of symbols with a reason attached to each one.

The whole pipeline is arithmetic over a graph. There is no language model anywhere in it,
and every step that could depend on iteration order sorts first. That is what makes the
output reproducible: the same task text, against the same commit, with the same weights,
produces the same context pack byte for byte.

## The pipeline

```
task text
   │
   ├─ 1. anchors        four independent sources vote on entry points
   ├─ 2. expansion      fixed-shape graph walk outward from every anchor
   ├─ 3. features       degree, distance, reference kind, provenance per candidate
   ├─ 4. scoring        weighted sum, deterministic sort
   ├─ 5. scope filter   drop repositories the caller may not see
   ├─ 6. narrowing      keep the top max_candidates
   ├─ 7. assembly       fit into a token budget, cheaper detail further out
   └─ 8. coverage       report how much of the answer is trustworthy
```

Stages 1 to 6 live in `src/bce/core/orchestrator/` and `src/bce/core/scoring/`; assembly is
`src/bce/core/assembler/`; coverage is `src/bce/core/coverage/`. `get_context_for_task` in
`src/bce/tools/layer3/orchestration.py` wires them together.

## 1. Anchors

An anchor is a symbol the engine is confident the task is *about*. Four sources nominate
anchors independently, and each anchor remembers which sources nominated it.

| Source | What it looks at | Limit |
| --- | --- | --- |
| `explicit` | Symbol names and route paths named in the task text or passed by the caller | none |
| `history` | Every symbol in files that previous work on this task touched | none |
| `lexical` | Full-text search over symbol name, signature and docstring | 5 per term |
| `semantic` | Vector nearest neighbours for the task text | 10 |

Identifier-like tokens of three characters or more are pulled out of the task text with
`[A-Za-z_][A-Za-z0-9_]*`, and path-like fragments with `/[A-Za-z0-9_\-{}/:]+`. So "fix
login timeout in meeting webhook" contributes `fix`, `login`, `timeout`, `meeting`,
`webhook` as lexical terms and as candidate symbol names.

How many *distinct sources* agree is the single strongest signal the engine has about
whether it understood the task, and it feeds directly into the reported confidence. One
source agreeing is a guess; three agreeing is usually right.

Anchors are deduplicated by symbol id and handed to expansion in sorted order. Their
nomination order carries no weight: everything downstream is decided by the score.

## 2. Expansion

From every anchor the engine walks a fixed template. The shape does not vary with the
task, which is what keeps expansion reproducible.

| Direction | Edge | Depth |
| --- | --- | --- |
| Who calls this | `CALLS` inbound | 2 hops |
| What this calls | `CALLS` outbound | 1 hop |
| Who references this | `REFERENCES` inbound | 1 hop |
| Types above and below | `INHERITS`, `IMPLEMENTS` | 1 hop each way |
| Siblings | other symbols in the same file | 1 hop |

Callers reach two hops and callees only one on purpose. When you are about to change a
function, the code that will break is upstream of it, and it is usually one level further
away than you expect. What the function calls is mostly detail you can read from the body
you already have.

There is no fan-out cap. A breadth-first walk visits each node once, keeps the shortest
distance it was reached at, and processes each frontier in sorted order. Anchors sit at
distance 0.

`IMPORTS` is deliberately not traversed here: it is a file-level edge, and following it
would pull in whole modules rather than the symbols that matter.

## 3. Features

Each node found during expansion becomes a candidate carrying five features:

- **`graph_distance`** — hops from the nearest anchor.
- **`degree`** — total edges on the symbol, a proxy for how central it is.
- **`ref_kind`** — what the reference that reached it actually did: `define`, `write`,
  `read` or `pass`.
- **`provenance`** — how the edge was established: `scip` (a real compiler index),
  `treesitter` (syntax), or `heuristic` (a pattern match, such as a cross-language bridge).
- **`is_leaf`** — the symbol is called by others but calls nothing itself.
- **`task_signal`** — 1.0 if the symbol's name appears among the task's identifier tokens,
  otherwise 0.0.

## 4. Scoring

```
score = 3.0 · ref_kind_weight
      + 2.0 · task_signal
      + 1.0 · min(degree / 20, 1)
      + 2.0 · (1 / max(graph_distance, 1))
      - 1.5 · is_leaf
      + 0.5 · provenance_weight
      + 5.0 if the candidate is itself an anchor
```

The weights are the `ScoreWeights` dataclass in `src/bce/core/scoring/engine.py` and are
stamped into responses as `scoring-v1`. The sub-weights:

| `ref_kind` | Weight | | `provenance` | Weight |
| --- | --- | --- | --- | --- |
| `define` | 1.0 | | `scip` | 1.0 |
| `write` | 0.8 | | `treesitter` | 0.8 |
| `read` | 0.3 | | `heuristic` | 0.4 |
| `pass` | 0.2 | | unknown | 0.6 |
| unknown | 0.5 | | | |

A few of these choices are worth explaining, because they are where most of the retrieval
quality comes from.

**Reference kind carries the largest weight.** Somewhere that *defines* or *writes* a value
is where a bug usually lives; somewhere that merely *reads* or *passes* it is usually just
downstream. Weighting `define` five times higher than `pass` is what stops a widely-read
constant from crowding out the one function that sets it.

**Centrality saturates at degree 20.** Beyond that a symbol is a hub, and hubs are not
informative: a logger or a base class touches everything and explains nothing. The same
threshold marks a candidate as a "god node" in the coverage report.

**Leaf consumers are penalised.** A symbol that is called but calls nothing is typically a
formatter or a getter. It is real, it is connected, and it is almost never where you make
the change.

**The anchor bonus is a floor, not a nudge.** At `+5.0` it is larger than any other term
can reach, so a symbol the engine identified as the subject of the task cannot be pushed
out of the results by its own neighbourhood.

Candidates are sorted by `(-score, symbol_id)`. Scores are rounded to six decimals before
sorting, so the symbol id tiebreak is what actually resolves equal scores, and it resolves
them the same way every time.

## 5. Scope and narrowing

The scope filter runs *after* scoring and *before* narrowing. That ordering matters: it
means a caller with access to two repositories out of five gets their own best eight
results, not whatever survives from a global top eight.

`max_candidates` (default 8) is then a plain slice of the ranked list.

One subtlety: `get_context_for_task` runs retrieval twice. The first pass, over
`max_candidates * 4` results, exists only to learn which symbol names are present so the
`task_signal` feature can be computed against them. The second pass re-expands and
re-scores everything with those signals available.

## 6. Assembly

Selected candidates are rendered into a token budget, default 4000, estimated at four
characters per token. Detail level falls off with distance:

| Distance | Detail | What you get |
| --- | --- | --- |
| 0–1 | `full` | The body, or signature plus docstring if no body was stored |
| 2 | `signature` | `name(args)` plus docstring |
| 3+ | `reference` | `name @ file:line` |

Items are walked top-down in score order. If one does not fit, the assembler retries it at
`reference` detail; if it still does not fit, it is skipped and the walk continues, so a
single large function near the top cannot starve everything below it. The response reports
`used_tokens`, `included` and `skipped` so a caller can tell the difference between "there
was nothing else" and "there was more, and it did not fit".

## 7. Coverage and confidence

Every response carries a coverage block. It is there so an agent can decide whether to
trust the pack, and so a human can tell *why* an answer was thin.

| Field | Meaning |
| --- | --- |
| `anchor_source_count` | How many of the four anchor sources agreed |
| `orphan_ratio` | Share of candidates not connected to any anchor |
| `connected_component_ratio` | Share within two hops of an anchor |
| `top_candidate_margin` | Score gap between first and second place |
| `cross_repo_edge_ratio` | How much the answer spans repositories |
| `provenance_distribution` | Share of `scip` / `treesitter` / `heuristic` edges |
| `max_centrality_in_context`, `touches_god_node` | Whether a hub leaked in (degree ≥ 20) |
| `commit_mismatch`, `commit_mismatch_count` | Symbols indexed at a different commit than requested |

These roll up into one label:

- **`high`** — at least three anchor sources agreed, at least 70% of candidates are within
  two hops, and the top candidate leads the second by 1.0 or more.
- **`low`** — one source or fewer agreed, and either the margin is under 0.5 or more than
  half the candidates are orphans.
- **`medium`** — everything else.

`low` is not a failure. It is the engine saying it found something but could not
corroborate it, which is exactly when an agent should ask a follow-up question or widen the
task description rather than start editing.

`commit_mismatch` deserves attention in CI: it means the index is behind the working tree,
so the answer describes code that no longer exists.

## Where nondeterminism could enter

Two places, both upstream of the deterministic core:

**Vector search.** Embeddings decide which anchors are *found*, never how candidates are
ranked. The default `hashing` provider is pure arithmetic over token digests and is exactly
reproducible. A hosted provider such as Voyage may return marginally different floats
across calls; results are still ordered by `(distance, ref_id)`, so ties break stably, but
the anchor set itself is only as reproducible as the provider. Pin the model, or use the
hashing provider, when you need bit-exact reproducibility.

**Task history.** Which files past work touched is data about the past, and it changes as
work happens.

Everything after anchor selection — expansion, features, scoring, narrowing, assembly — is
deterministic by construction. `bce bench` measures this directly: it runs each case
several times and reports `determinism_ok` only if the ordered symbol list is identical
every time.

## Tuning

The knobs a caller controls per request are `max_candidates`, `max_tokens`, `commit`,
`repo_ids` and the four anchor hint lists (`explicit_symbols`, `route_paths`,
`history_file_ids`, `component_repo_ids`). Passing hints is by far the cheapest way to
improve results: an explicit symbol name turns a guess into a certainty.

The scoring weights are not runtime configuration. They are a versioned constant, because
a context pack is only comparable to another pack produced with the same weights. Changing
them means changing `SCORE_WEIGHTS_VERSION`, and a pull request that does so needs to show
its effect on `bce bench`.
