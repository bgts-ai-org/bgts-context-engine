# Retrieval

This is the part of the engine that turns a sentence like *"fix the login timeout in the
meeting webhook"* into a small, ordered set of symbols with a reason attached to each one.

The whole pipeline is arithmetic over a graph. There is no language model anywhere in it,
and every step that could depend on iteration order sorts first. That is what makes the
output reproducible: the same task text, against the same commit, with the same weights
and the same retrieval profile, produces the same context pack byte for byte.

## The pipeline

```
task text
   │
   ├─ 1. anchors        four independent sources vote on entry points, with evidence strength
   ├─ 2. expansion      fixed-shape graph walk outward from the best-evidenced anchors
   ├─ 3. features       degree, distance, reference kind, provenance, kind, task signal
   ├─ 4. scoring        weighted sum, deterministic sort
   ├─ 5. scope filter   drop repositories the caller may not see
   ├─ 6. narrowing      K-monotonic interleave of the model's ranks and the engine's scores, file and container caps
   ├─ 7. assembly       fit into a token budget, cheaper detail further out
   └─ 8. coverage       report how much of the answer is trustworthy
```

Stages 1 to 6 live in `src/bce/core/orchestrator/` and `src/bce/core/scoring/`; assembly is
`src/bce/core/assembler/`; coverage is `src/bce/core/coverage/`. `get_context_for_task` in
`src/bce/tools/layer3/orchestration.py` wires them together.

## 1. Anchors

An anchor is a symbol the engine is confident the task is *about*. Four sources nominate
anchors independently, and each anchor remembers which sources nominated it **and how much
evidence** each one brought, as a strength in `[0, 1]`.

| Source | What it looks at | Evidence | Limit |
| --- | --- | --- | --- |
| `explicit` | Identifier-looking names, backticked spans, `Class.method` tails and route paths in the task text, or names passed by the caller | `1.0 / √matches`, halved for a member echoing its container's name | names matching more than 8 symbols are dropped |
| `history` | Every symbol in files that previous work on this task touched | 0.7 | none |
| `lexical` | Full-text search over a weighted document - exact name (A), its camelCase / snake_case parts (B), container and file-stem words (C), signature and docstring (D) - scored by how many of the task's content terms a symbol covers | up to 0.8 | 15 anchors (pool of 25 rows per term) |
| `semantic` | Vector nearest neighbours for the task text, in the model's rank order | `0.9 / (1 + rank/scale)`: 0.9 at #1, 0.45 at rank `scale` — decays with the absolute rank, so a wider pool does not get a stronger tail. `scale` is the active [retrieval profile](#tuning)'s `semantic_rank_scale` (30 for both voyage and jina) | 30 non-test hits |

**Which words count.** Tokens are pulled out with a Unicode-aware word pattern (so "toplantı"
is one word, not `toplant` + a stray letter; `İ` folds to `i`) and path-like fragments with
`/[A-Za-z0-9_\-{}/:]+`, then filtered through a stop-word list of English and Turkish function
words *and* bug-report filler ("fix", "missing", "check", "proper", "the", "and", ...). Those
words match thousands of docstrings and say nothing about the code. A small Turkish → English
vocabulary of generic product / software words ("toplantı" → `meeting`, "takvim" → `calendar`,
"bildirim" → `notification`, ...) adds the English equivalents as extra query terms - suffixed
forms are recognised through a Turkish suffix check, so `serial` or `listener` are left alone -
because a Turkish-speaking team writes the task in Turkish and the identifiers in English.
Compound identifiers are kept whole and also split, so `diff_command` contributes `diff` and
`command`.

Only tokens that *look like* code become explicit names: snake_case, camelCase, digits,
ALL_CAPS constants, anything in backticks, or the tail of a dotted reference. A plain word
such as "check" or "Period" goes through the lexical channel only, so an English verb never
resolves to `Principal.check` as if the author had named it.

**An explicit name is only as good as it is specific.** A body that says `MongoDbService`
names one thing. A body that says `Content` or `ExternalId` names a dozen unrelated DTO
properties, and treating each as a certainty used to seed a dozen full-strength anchors,
each expanding its own callers, referrers and file. Explicit evidence is therefore divided
by `√matches`, and a name matching more than eight symbols is dropped entirely — it names
nothing in particular, and the lexical channel still picks the word up if it carries signal.
Three further rules sharpen the same idea:

- **Qualified first.** `MongoDbService.CancelAsync` is resolved *inside* that type, and the
  bare tail is then not offered separately: the author did not mention the three other
  `CancelAsync` methods in the repository. If the qualifier is not in the graph at all, the
  bare tail is used only while it stays unambiguous (at most three matches).
- **The type, not its constructor.** When a name resolves to a class and its same-named
  members — a C# constructor, a partial-class helper — the container counts fully and the
  members at half. The author named the type; expansion reaches what is inside it.
- **Containers are locations.** This continues through scoring and narrowing (sections 4
  and 5): a class tells you *where* a change goes, one of its methods *is* the change.

**Lexical coverage, not per-term hits.** Each content term is searched once. A symbol's
lexical strength is the share of the task's terms it matches, weighted by term rarity: a
term that fills its pool of 25 rows is common and counts for 0.35 of a precise one. A symbol
matched by "period" and "comparison" therefore outranks one matched only by "authorization".
Coverage is scaled so that matching about a third of a long task's terms is already strong
evidence; symbols below strength 0.15 are dropped.

**Strength combines with a noisy-OR.** `strength = 1 − ∏(1 − sᵢ)` over the sources that
nominated the symbol. A lexical 0.5 plus a semantic 0.6 gives 0.8; an explicit name gives 1.0
regardless of the others; a lone common term stays near 0.2. Corroboration is what pushes a
symbol to the top, and a single weak vote is beatable downstream.

**Tests are not anchors.** A task description almost always resembles the tests that assert
it, so the raw nearest neighbours of "authorization check and proper error responses" are
ten test functions. Symbols in `tests/`, `test_*.py`, `*_test.go`, `*.test.ts`, `*Test.java`
and the like are skipped by the lexical and semantic sources (the semantic channel
over-fetches four times and keeps the first thirty non-test hits). `explicit` and `history`
still admit them — the caller named them on purpose — and `include_tests=True` turns the
filter off.

Anchors are deduplicated by symbol id and handed to expansion in sorted order. Their
nomination order carries no weight; their strength does.

## 2. Expansion

From every anchor the engine walks a fixed template. The shape does not vary with the
task, which is what keeps expansion reproducible.

| Direction | Edge | Depth |
| --- | --- | --- |
| Who calls this | `CALLS` inbound | 2 hops |
| What this calls | `CALLS` outbound | 1 hop |
| Who references this | `REFERENCES` inbound | 1 hop |
| Types above and below | `INHERITS`, `IMPLEMENTS` | 1 hop each way |
| Siblings | other symbols in the same file, nearest by line first | 1 hop, anchors with strength ≥ 0.7 only, 12 per anchor, never from a container |

Callers reach two hops and callees only one on purpose. When you are about to change a
function, the code that will break is upstream of it, and it is usually one level further
away than you expect. What the function calls is mostly detail you can read from the body
you already have.

Siblings are gated on anchor strength because they are the one rule that multiplies: a
weak, single-term anchor would otherwise drag its whole file into the pool, and thirty such
anchors produce hundreds of candidates that dilute the real change site. They are skipped
entirely for containers: a class's "siblings" are the other top-level types of its file,
while its *members* already arrive through the graph, so expanding it floods twice over.

**Only well-evidenced anchors are walked.** Expansion runs from anchors with strength ≥ 0.3,
and from at most the 20 strongest of those. Weaker anchors stay in the pool at distance 0
without dragging their neighbourhoods in. This is a budget, not a filter: a single-term
lexical hit is still a candidate and can still be selected on its own merits, it just no
longer costs a caller/referrer/sibling fan-out and no longer returns its whole neighbourhood
as if the name had been meant.

**Evidence travels with the walk.** Every node records `source_strength`: the strength of
the best anchor that reached it, multiplied by 0.6 once per hop. A neighbour of the model's
#1 hit carries 0.54; a neighbour of a throwaway 0.2 lexical hit carries 0.12. Scoring reads
this (section 4), which is what stops a `define` edge off a generic name from scoring like a
real lead.

The graph walks themselves have no fan-out cap. A breadth-first walk visits each node once,
keeps the shortest distance it was reached at, and processes each frontier in sorted order.
Anchors sit at distance 0.

Every frontier is one query, not one query per node: `callers_of`, `callees_of`,
`referrers_of`, `supertypes_of`, `subtypes_of`, `symbols_in_files`, `symbols_meta` and
`repos_of_symbols` all take an id list. `src/bce/core/orchestrator/bulk.py` prefers the
batched method and falls back to the per-symbol one when a repository does not have it, so
both paths return the same shapes.

`IMPORTS` is deliberately not traversed here: it is a file-level edge, and following it
would pull in whole modules rather than the symbols that matter.

## 3. Features

Each node found during expansion becomes a candidate carrying these features. Metadata,
degree and call presence are fetched for the whole pool in a handful of chunked queries
(`GraphRepository.symbol_features`) rather than four round-trips per symbol. Degree and the
call flags are read from the edge label tables in SQL (`_ag_label_edge`, `CALLS`; both indexed
on `start_id` / `end_id`) - the untyped Cypher `MATCH (s)-[r]->()` re-planned over every
edge × vertex label pair per id and took 15-30 s for a 400-symbol pool; the SQL form takes
20-40 ms.

- **`graph_distance`** — hops from the nearest anchor.
- **`degree`** — total edges on the symbol, a proxy for how central it is.
- **`ref_kind`** — what the reference that reached it actually did: `define`, `write`,
  `read` or `pass`.
- **`provenance`** — how the edge was established: `scip` (a real compiler index),
  `treesitter` (syntax), or `heuristic` (a pattern match, such as a cross-language bridge).
- **`is_leaf`** — the symbol is called by others but calls nothing itself.
- **`task_signal`** — 1.0 if the symbol's name, or every meaningful part of it, appears
  among the task's content tokens; a partial credit of `0.6 · matched / parts` when only
  some parts do (`diff_view` for a task about "diff"); 0.0 otherwise. Computed for *every*
  candidate in the pool, not only for the anchors.
- **`anchor_strength`** — the evidence behind the anchor (section 1), 0 for non-anchors.
- **`source_strength`** — the anchor evidence that reached this node, decayed per hop
  (section 2). `evidence` is the larger of the two, and an unrecorded 0 counts as fully
  trusted, so hand-built candidates never score below real weak evidence.
- **`kind`** — `method`, `class`, `property`, `constructor` and so on, from the index.
- **`is_test`** — the symbol lives in a test file or is a test function.
- **`semantic_rank`** — the symbol's position in the embedding model's list, if it was there
  (0-based); `None` otherwise. Rank is the only thing the model contributes - never a
  similarity number.
- **`churn`** — commits that touched the symbol's file in the last 500 at the indexed commit
  (`file_churn`, written by the indexer from `git log`; ancestors only, so a snapshot at a PR's
  base never sees the PR's own commits).

## 4. Scoring

```
score = 3.0 · ref_kind_weight · (0.5 + 0.5 · evidence)
      + 2.5 · task_signal
      + 0.6 · min(log(1 + degree) / log(21), 1)
      + 2.0 · (1 / (1 + graph_distance)) · evidence
      - 0.5 · is_leaf
      + 0.5 · provenance_weight
      + 3.5 · anchor_strength
      - 2.0 · is_test
      + 2.0 · kind_prior
      + 1.0 · (1 / (1 + semantic_rank))          # 0 when the model did not rank it
      + 1.0 · min(log(1 + churn) / log(21), 1)   # saturates at 20 commits
```

The weights are the `ScoreWeights` dataclass in `src/bce/core/scoring/engine.py` and are
stamped into responses as `scoring-v4`. The sub-weights:

| `ref_kind` | Weight | | `provenance` | Weight | | `kind_prior` | Weight |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `define` | 1.0 | | `scip` | 1.0 | | `method`, `function` | 1.0 |
| `write` | 0.8 | | `treesitter` | 0.8 | | `class`, `struct`, `enum`, `type` | 0.5 |
| `read` | 0.3 | | `heuristic` | 0.4 | | `property`, `field`, `variable` | 0.5 |
| `pass` | 0.2 | | unknown | 0.4 | | `interface`, `trait`, `constructor` | 0.4 |
| unknown | 0.3 | | | | | unknown | 0.5 |

A few of these choices are worth explaining, because they are where most of the retrieval
quality comes from.

**Reference kind carries the largest weight.** Somewhere that *defines* or *writes* a value
is where a bug usually lives; somewhere that merely *reads* or *passes* it is usually just
downstream. Weighting `define` five times higher than `pass` is what stops a widely-read
constant from crowding out the one function that sets it.

**Unknown never beats known.** A candidate reached through a `CALLS` or sibling edge has no
reference kind and no edge provenance. It scores like a `read` over a `heuristic` edge — the
weakest known values — so the absence of evidence is never rewarded.

**The anchor bonus scales with evidence.** Version 1 gave every anchor a flat `+5.0`. With a
natural-language task that produced dozens of single-word anchors, that floor split the pool
into two disjoint bands — every anchor above 7.3, every neighbour below 4.9 — and the result
was simply the highest-degree anchors, whatever the evidence. Now a fully corroborated anchor
still leads by `3.5 + 2.0` (bonus plus distance-0 proximity), but a lone common-term hit at
strength 0.2 is worth `0.7` and loses to a one-hop neighbour with a `define` edge or a name
that appears in the task.

**Proximity distinguishes the anchor from its first hop.** `1 / (1 + d)` gives 1.0, 0.5 and
0.33 for distances 0, 1 and 2. The v1 form `1 / max(d, 1)` scored an anchor and its direct
neighbour identically.

**Proximity and reference kind scale with evidence.** v2 paid a one-hop neighbour of a
throwaway anchor exactly what it paid a one-hop neighbour of the best-evidenced one, so a
`define` edge off a generic name like `Content` scored like a real lead. Both features are
now multiplied by the `evidence` that reached the node (section 3): being adjacent to
something the engine is sure about is worth much more than being adjacent to a guess. The
reference kind keeps half its weight unconditionally — a `define` edge is informative even
when the anchor behind it is weak — while proximity scales fully, because "one hop from
nothing in particular" is worth nothing in particular.

**A kind prior points at change sites rather than their surroundings.** The change a task
describes almost always lands in a method or function body; classes, interfaces and
constructors are where that body *lives*. Without this term, a constructor or property
reached through a `define` edge collected `3.0 · 1.0` while the anchor itself collected
`3.5 · 0.9`, and the answer filled up with declarations. Containers keep a positive prior —
they are legitimate answers when nothing inside them is known — but no longer beat the
members that carry the change.

**A container whose members already rank is demoted.** After scoring,
`demote_redundant_containers` subtracts 1.5 from any container that has a member in the top
`2N`, detected by symbol-id prefix. A class in the answer is only useful while nothing
inside it is known; once one of its methods ranks, the class repeats that information and
costs a slot. A container with no member in the pool is untouched, because it is then the
best available pointer at the change.

**Centrality is log-scaled and saturates at degree 20.** Beyond that a symbol is a hub, and
hubs are not informative: a logger or a base class touches everything and explains nothing.
The weight is deliberately small (0.6): when anchors carried no other distinguishing
feature, degree alone used to decide the top eight, which is how a widely-used helper such
as `acting_user` ended up ranked above the handler that needed the change. The same
threshold marks a candidate as a "god node" in the coverage report.

**Leaf consumers are nudged, not buried.** A symbol that is called but calls nothing is often
a formatter or a getter — but it is also what a small validation handler looks like, and
those are frequent change sites. The penalty is 0.5.

**Tests are penalised.** A test symbol that survived anchor filtering (an explicit mention,
or a graph neighbour) loses 2.0, so it appears only when it is genuinely close to the
answer.

Candidates are sorted by `(-score, symbol_id)`. Scores are rounded to six decimals before
sorting, so the symbol id tiebreak is what actually resolves equal scores, and it resolves
them the same way every time.

## 5. Scope and narrowing

The scope filter runs *after* scoring and *before* narrowing. That ordering matters: it
means a caller with access to two repositories out of five gets their own best eight
results, not whatever survives from a global top eight.

Narrowing to `max_candidates` (default 8) builds the answer one slot at a time from two
streams, in `narrow()` in `src/bce/core/orchestrator/orchestrator.py`: the embedding model's
ranks (in rank order) and the engine's ranking (in score order). The list is returned **in
selection order**, which makes it K-monotonic - `narrow(pool, n)` is exactly the first `n` of
`narrow(pool, n + 1)`, so asking for a longer list never loses a symbol the shorter one had,
and a K=20 run can be cut to K=10. (The earlier proportional budgets, `ceil(N · share)` per
rule, did not have this property: in the 40-PR replay one PR went from 100 % recall at K=10 to
50 % at K=20.)

The two numbers that decide *how much* of the list the model keeps — the reserve share and
an optional guard on its top ranks — come from the active [retrieval profile](#tuning).
Voyage (and any unfitted model) is share 0.5 and no guard, which is what every constant
below was first fitted with. Slot `i` (1-based) is filled as follows:

- **While `i` is inside the profile's `semantic_guard_ranks`, the model's next rank is
  taken.** Those ranks *are* slots 1..n; only the container cap can skip one. A model whose
  correct hits sit at ranks 7–15 (jina-code) needs this: without it the engine's agreement
  with the model's *deeper* ranks already satisfies the share below, and its own picks push
  those mid ranks to positions 11–19 or out of the answer. Voyage's guard is 0, so this
  rule is inert there.
- **Position 1 is always the model's #1.** The model is the only channel that reads *meaning*
  rather than names, and MRR is decided here.
- **The model's next rank is taken while fewer than `i · semantic_reserve_share` of the picks
  so far count as the model's.** The voyage value is 0.5. An engine pick that is itself one
  of the model's ranks counts too - both channels agree on it, and the reserve exists to keep
  the model's ranks in the answer, which an agreed pick does. In practice the streams mostly
  agree and the floor only bites when the engine's scores disagree with the model; without
  the agreement rule the streams alternated even when they agreed, and the engine's own hits
  (explicit / lexical anchors the model never returned) sat at positions 10-14 instead of 5.
  Model picks ignore the diversity caps - a task that reworks one big service class is
  exactly where the model's ranks and the file cap collide, and there the cap is the thing
  that is wrong.
- **Otherwise the engine's best remaining candidate**, subject to at most `ceil(i · 0.6)`
  symbols per file (a strong anchor's file would otherwise fill the pack with its siblings),
  `ceil(i · 0.3)` members of the same container, and `ceil(i · 0.2)` containers overall (a
  class is a location, not a change site). A candidate blocked at slot `i` is reconsidered at
  every later slot.

The cost of the interleave is at the very top of the list: the model's rank `r` lands no
lower than position `2r - 1`, so a model hit at rank 4 or 5 sits at 7-9 in the answer. On the
replay corpus BCE is behind the bare model at K=5 and ahead at K=10 and K=20; a caller that
only shows five results is better served by the model's own list.

Two gates were measured on the tune split and dropped: a score margin an engine pick had to
clear over the model's next rank (inert - the engine's heads are overwhelmingly the model's
own deeper ranks, which the gate exempts) and pre-emption of slot 1 by strong explicit anchors
(a long PR description names dozens of identifiers, so "explicit" is cheap there and it
flooded the top).

Up to v2 a third rule reserved `floor(N · 0.25)` slots for non-anchor graph neighbours,
because v1's flat anchor floor put every anchor above every neighbour. Evidence-scaled
scoring removed that inversion — a well-evidenced neighbour now outranks a weak anchor on its
own — so the reserve only ever spent slots on the best-scoring neighbour whether or not it
had earned one, at a measurable cost in recall. It is gone; nothing forces a neighbour into
the answer any more.

Retrieval is a single pass. Task signals are attached to every expanded candidate in
`to_candidates`, so a neighbour whose name appears in the task is boosted just like an
anchor. (Version 1 ran a preliminary pass and only signalled its top `4 · N` — which, because
anchors always filled that slice, meant a neighbour could never receive a task signal.)

## 6. Assembly

Selected candidates are rendered into a token budget, default 4000, estimated at four
characters per token. Detail level falls off with distance:

| Distance | Detail | What you get |
| --- | --- | --- |
| 0–1 | `full` | The stored body snippet, or signature plus docstring if no body was stored |
| 2 | `signature` | `name(args)` plus docstring |
| 3+ | `reference` | `name @ file:line` |

Bodies and docstrings are loaded only for the selected candidates
(`GraphRepository.get_symbol_detail`), never during expansion or scoring. A distance of 0 is a
valid near distance; only a *missing* distance falls back to reference-only rendering.

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
| `anchor_source_count` | How many of the four anchor sources contributed at all |
| `strong_anchor_count` | Anchors with evidence strength ≥ 0.6 |
| `corroborated_anchor_count` | Anchors named by two or more sources, or by `explicit` / `history` |
| `anchor_agreement_ratio` | Share of the *selected* candidates that are strong/corroborated anchors or their direct neighbours |
| `weak_anchor_ratio` | Share of the selection that is weak (< 0.35), single-source anchors — the flooding signal |
| `max_anchor_strength` | Strongest evidence in the selection |
| `test_ratio` | Share of the selection that is test code |
| `pool_size` | Candidates scored before narrowing |
| `orphan_ratio` | Share of candidates not connected to any anchor |
| `connected_component_ratio` | Share within two hops of an anchor |
| `top_candidate_margin` | Score gap between first and second place |
| `cross_repo_edge_ratio` | How much the answer spans repositories |
| `provenance_distribution` | Share of `scip` / `treesitter` / `heuristic` edges |
| `max_centrality_in_context`, `touches_god_node` | Whether a hub leaked in (degree ≥ 20) |
| `commit_mismatch`, `commit_mismatch_count` | Symbols indexed at a different commit than requested |

These roll up into one label, computed in `_confidence_level`:

- **`low`** — no anchor reaches strength 0.35 and none is corroborated; *or* more than half
  the selection is weak single-source anchors or test code; *or* a single source and either
  a margin under 0.5 or more than half the candidates orphaned.
- **`high`** — the first-ranked candidate is itself a trusted anchor (strong or
  corroborated), at least 60% of the selection is trusted anchors or their direct neighbours,
  at most a quarter is weak anchors, and the top leads the second by 0.5 or more.
- **`medium`** — everything else.

The v1 rule counted source *tags* across all anchors. A natural-language task always has
explicit, lexical and semantic tags somewhere, all its candidates are anchors at distance 0,
and two hubs differ by a point of degree — so a pack of eight noise anchors was reported as
`high`, and confidence *fell* as the true targets entered the list. The v2 rule asks the
question the label is supposed to answer: do the selected candidates agree on something the
engine has real evidence for?

`low` is not a failure. It is the engine saying it found something but could not
corroborate it, which is exactly when an agent should ask a follow-up question or widen the
task description rather than start editing.

`commit_mismatch` deserves attention in CI: it means the index is behind the working tree,
so the answer describes code that no longer exists.

## Cost

A retrieval is dominated by round-trips to the graph, not by arithmetic. Three rules keep it
in the low seconds on a repository of a few hundred thousand symbols:

- **One query per frontier, not per node.** Every read in section 2 is batched. The bulk
  queries use `UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})` rather than
  `WHERE s.symbol_id IN $ids`; on Apache AGE the first form hits the index per id while the
  second degenerates into a scan, which measured about ten times slower on the same data.
- **One full-text query for all terms.** `lexical_search_many` searches every content term
  in a single statement, ranked per term with `ROW_NUMBER() OVER (PARTITION BY term ...)`. A
  long PR description has around eighty terms, and eighty sequential FTS queries were the
  single largest cost in the pipeline.
- **A bounded pool.** The expansion budget (section 2) and the explicit-name rules (section
  1) exist as much for cost as for quality: they are what stops one generic word in a task
  description from producing thousands of candidates whose features must then be fetched.

Stage timings are returned in the `timings` block of `suggest_change_sites`
(`semantic_ms`, `anchors_ms`, `expand_ms`, `candidates_ms`, `score_ms`, `narrow_ms`,
`enrich_ms`, `total_ms`), so a slow request can be attributed without guessing.

## Where nondeterminism could enter

Two places, both upstream of the deterministic core:

**Vector search.** Embeddings decide which anchors are *found*, never how candidates are
ranked. The default `hashing` provider is pure arithmetic over token digests and is exactly
reproducible. A remote provider — Voyage, or `openai` talking to vLLM / TEI / Ollama /
OpenAI — may return marginally different floats across calls; results are still ordered by
`(distance, ref_id)`, so ties break stably, but the anchor set itself is only as
reproducible as the provider. Pin the model, or use the hashing provider, when you need
bit-exact reproducibility.

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

Three constants *do* depend on the embedding model, because they encode an assumption about
where in that model's ranked list the right answers sit: `semantic_rank_scale` (anchor
strength), `semantic_reserve_share` and `semantic_guard_ranks` (narrowing). They live in a
*retrieval profile* (`bce.core.orchestrator.profile`), selected automatically from
`BCE_EMBEDDING_MODEL` (`voyage-*` → the historical voyage set, `jina-*` → a guard of 10,
anything else → voyage). `BCE_RETRIEVAL_PROFILE` forces a named profile;
`BCE_RETRIEVAL_SEMANTIC_*` overrides a single knob for a fitting run. None of this changes
stored data, so a profile switch is a query-side re-run, not a re-index. The active profile
is written into every `bce bench-prs` report. See [docs/deployment.md](deployment.md#retrieval-profile).

**How the constants were chosen.** `bce bench-prs` replays merged pull requests: the task is
the PR title and description, the ground truth is the symbols the PR actually changed, and it
reports the engine against the raw embedding ranking on the same query. The pull requests are
split into a tune half and a holdout half; every constant above was tuned against the tune
half only and then measured once on the holdout, which is the number worth quoting. Tuning a
retrieval engine against the cases you report on produces a number that describes those cases
and nothing else. The jina profile's guard of 10 is the only setting that beat jina-alone on
that holdout at every K while keeping the most of the model's own hits.
