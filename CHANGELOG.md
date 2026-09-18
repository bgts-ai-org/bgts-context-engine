# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `bce bench-prs prepare` / `run`: a PR replay ablation that measures what the engine adds on
  top of the embedding model. Each merged PR is replayed against a database indexing the repo
  at the PR's base commit; the title (+ description) is the task and the symbols the diff
  touched in that snapshot are the ground truth. `voyage` (nearest non-test symbol embeddings),
  `voyage_raw` and `bce` (the same neighbours as semantic anchors + explicit/lexical anchors,
  expansion, scoring, narrowing) are scored with Recall/Precision/F1/MRR/Hit@K, a lenient
  recall that credits enclosing symbols, file-level recall/precision, latency and a breakdown
  of returned symbol kinds. `--from-report` re-scores stored result lists without querying.
  `--tune` splits the PRs into a tune and a holdout half and reports each separately, so the
  constants can be fitted on one half and quoted from the other; `--only` narrows a run to a
  few PRs. Each `bce` case also reports `semantic_retention` (the share of the embedding
  model's correct hits the engine still returns) and per-stage timings.
- `suggest_change_sites` returns a `timings` block (`semantic_ms`, `anchors_ms`, `expand_ms`,
  `candidates_ms`, `score_ms`, `narrow_ms`, `enrich_ms`, `total_ms`); `trace=True` also returns
  the ranked pool before narrowing (`ranked_ids`), for diagnostics.
- `bce bench-prs run --k 5,10,20` runs once at the largest K and reports every K as an extra
  column (`metrics_at`) - exact because both lists are prefix chains (see narrowing below).
  Each `bce` case carries a `truth_trace`: for every ground-truth symbol, the channel that
  brought it in (`semantic` / `anchor` / `expansion` / none), the stage where it was lost
  (`not_reached`, `narrowed_out`, `returned`), its rank in the model's list, in the scored pool
  and in the answer. The report aggregates these per split and per K, together with a
  task-text axis (`task_stats`: characters, content tokens, code identifiers in the PR text) so
  a "no identifiers in the description" PR can be told from an engine miss. PRs whose diff
  touches no symbol in the snapshot are scored at file level (`file_recall` / `file_precision`)
  instead of being dropped. `--rerun-bce` replays the stored embedding neighbours and re-runs
  only the engine, which turns a constants sweep from an API-bound run into ~100 s per split.
- `bce churn --repo --name [--commit] [--db-name]` (re)computes per-file git churn for an
  indexed repository; the indexer records it on every run (migration `0010_file_churn`).

### Changed

- Retrieval scoring is now `scoring-v3`. Anchors carry an evidence *strength* in `[0, 1]`
  (noisy-OR over the sources that nominated them) and the flat `+5.0` anchor floor became
  `3.5 · strength`. On natural-language tasks the floor had split the pool into two disjoint
  bands — every anchor above 7.3, every graph neighbour below 4.9 — so the top-N was the
  highest-degree anchors regardless of evidence, and the functions that actually needed the
  change ranked #25–#478. Proximity is `1 / (1 + distance)` (an anchor and its first hop no
  longer score the same), centrality is log-scaled with weight 0.6 instead of 1.0, the leaf
  penalty dropped from 1.5 to 0.5, unknown `ref_kind` / `provenance` now score at or below the
  weakest known value, and test symbols lose 2.0.
- Scoring reads the evidence that *reached* a candidate, not only whether it is an anchor.
  Proximity is multiplied by that evidence and the reference kind keeps half its weight
  unconditionally (`3.0 · ref_kind · (0.5 + 0.5 · evidence)`), so a `define` edge off a generic
  name like `Content` is no longer worth what one off the model's best hit is worth.
- New `w9_kind_prior` (2.0): method/function 1.0, class/struct/enum/property 0.5,
  interface/constructor 0.4. A task describes a change, and the change lands in a body;
  declarations and type shells are where it lives. Without the prior, a constructor reached
  through a `define` edge outscored the anchor itself, and 28% of returned symbols were
  containers against 10% in the diffs being replayed. `demote_redundant_containers` then
  subtracts 1.5 from a container that already has a member in the top `2N`.
- Explicit-name resolution is discounted by ambiguity: evidence is `1.0 / √matches`, and a name
  matching more than 8 symbols is dropped rather than seeding an anchor per match. A qualified
  `Type.member` reference resolves inside its type and no longer *also* admits the bare tail
  (which handed back the ambiguity the qualifier removed); with the type absent from the graph
  the bare tail is used only while it has at most 3 matches. A member echoing its container's
  name — a C# constructor, a partial-class helper — counts at half the container's evidence.
  Generic member names in PR text (`Content`, `ExternalId`, `IsCancelled`) were the largest
  single source of pool flooding: each resolved to every same-named symbol at full strength and
  each expanded its own callers, referrers and file.
- Expansion is budgeted and evidence-carrying. It runs only from anchors with strength ≥ 0.3 and
  at most the 20 strongest; weaker anchors stay in the pool at distance 0 unexpanded. Every node
  records `source_strength`, the reaching anchor's strength decayed by 0.6 per hop.
- Anchor finding filters task text through an English + Turkish stop-word list (function words
  and bug-report filler such as "fix", "missing", "check"), so "the" and "and" no longer produce
  anchors. Only identifier-looking tokens (snake_case, camelCase, digits, ALL_CAPS, backticks,
  `Class.method` tails) resolve as *explicit* symbol names; a plain English word cannot turn
  `Principal.check` into an explicit anchor. The lexical source scores symbols by how many of the
  task's terms they cover, weighted by term rarity, and keeps the best 15 instead of 5 hits per
  term. The semantic source decays evidence hyperbolically with rank, `0.9 / (1 + rank/n)`, so
  the model's #1 is comparable to a moderately ambiguous explicit name and the tail still
  outweighs a lone saturated lexical hit. Rank remains the only model input: a similarity number
  would let the model reorder candidates, which is the one thing the engine does not delegate.
- Test symbols (`tests/`, `test_*.py`, `*_test.go`, `*.test.ts`, `*Test.java`, ...) are excluded
  from the lexical and semantic anchor sources; the automatic semantic anchor over-fetches and
  keeps the first thirty non-test neighbours. `explicit` and `history` still admit them, and
  `find_anchors(include_tests=True)` disables the filter.
- Same-file sibling expansion runs only from anchors with strength ≥ 0.7, nearest by line, at
  most 12 per anchor, and never from a container — whose members already arrive through the
  graph — so a weak single-term hit no longer drags its whole file into the pool.
- Retrieval is a single pass. Task signals are computed for every expanded candidate (with
  partial credit for snake/camel parts, so `diff_command` lights up for "diff command"), rather
  than only for the preliminary top `4 · N` — which, being all anchors, meant no neighbour could
  ever receive one. Candidate features come from `GraphRepository.symbol_features`, five chunked
  queries per 500 symbols instead of four round-trips per symbol.
- Narrowing builds the answer one slot at a time from two streams - the embedding model's ranks
  and the engine's score order - and returns it in selection order, so `narrow(pool, n)` is
  exactly the first `n` of `narrow(pool, n + 1)` (K-monotonic: a K=20 run can be cut to K=10,
  and asking for more can no longer lose a symbol that K=10 had, which the proportional
  `ceil(N · share)` budgets did - #856 in the 40-PR replay went from 100 % at K=10 to 50 % at
  K=20). Position 1 is always the model's #1. Slot `i` goes to the model's next rank while fewer
  than `i · 0.5` picks came from the model; an engine pick that is itself one of the model's
  ranks counts toward that share, because the reserve exists to keep the model's ranks in the
  answer and an agreed pick does exactly that (without this the streams alternated even when
  they agreed and the engine's own hits sat at positions 10-14 instead of 5). Model picks are
  exempt from the diversity caps; engine picks are capped at `ceil(i · 0.6)` per file and
  `ceil(i · 0.3)` per container, containers overall at `ceil(i · 0.2)`. Two gates were tried on
  the tune split and dropped: a score margin an engine pick had to clear over the model's next
  rank (inert) and pre-emption of slot 1 by strong explicit anchors (a long PR description
  names dozens of identifiers, so "explicit" is cheap there). The planned neighbour reserve was
  dropped earlier for the same reason: evidence-scaled scoring already lets a real neighbour
  outrank a weak anchor, and reserving slots for the best-scoring neighbour cost recall.
- Scoring is `scoring-v4`: `w10_semantic_rank` (1.0) adds `1 / (1 + rank)` for the model's
  ranked list as an explicit factor, and `w11_churn` (1.0) adds a log-saturated prior from the
  number of commits that touched the symbol's file in the last 500 (`file_churn`, filled by the
  indexer from `git log` at the indexed commit - ancestors only, so a PR-base snapshot never sees
  the PR's own commits). Diffs land where diffs have landed before; on the replay corpus most
  indexed files have no commit in the window at all and a handful of hub files have 20-90, so
  the prior separates the live part of a codebase from its sediment. The prior saturates at 20
  commits.
- The semantic anchor's strength decays with the model's *absolute* rank,
  `0.9 / (1 + rank / 30)`, instead of `rank / len(list)`, so widening the pool no longer makes
  its tail stronger (rank 29 of a 30-list used to be worth as much as rank 10 of an 11-list),
  and expansion is gated by the model's rank rather than by the list length. Steeper decays
  (8 / 10 / 15) were measured and lost recall@20 and semantic retention.
- Full-text search tokenises identifiers (migration `0009_fts_identifier_parts`): the
  `symbol_fts.document` is a weighted `tsvector` of the exact name (A), its camelCase /
  snake_case parts (B), the container and file-stem words (C) and the signature / docstring
  (D), so "meeting join" matches `MeetingJoinService` and a class name lends weight to its
  members. Task-text tokenisation is Unicode-aware - Turkish words used to be split at the
  first non-ASCII letter - with `İ` folded to `i`, so the Turkish stop-words finally apply, and
  a Turkish -> English domain vocabulary (generic product / software words: "toplantı" ->
  `meeting`, "takvim" -> `calendar`, "bildirim" -> `notification`, ...; suffixed forms are
  recognised by a Turkish suffix-chain check so `serial`, `listener`, `indirect` are left
  alone) feeds the lexical channel and the task-signal feature. No repository-specific names.
- `GraphRepository.symbol_features` reads degree and the caller / callee flags from the edge
  label tables in SQL (`<graph>._ag_label_edge`, `<graph>."CALLS"`, both btree-indexed on
  `start_id` / `end_id`) instead of Cypher. `MATCH (s)-[r]->()` expands the untyped pattern over
  every edge x vertex label pair and re-plans it per `UNWIND` row: about 1 s of fixed cost for
  an empty id list and 15-30 s for a 400-symbol pool, which was the entire `candidates` stage
  (median 1 s, p90 15 s in the 40-PR replay). The SQL form measures 20-40 ms for the same pool
  with identical counts; Cypher remains the fallback for clients without a raw connection.
- The embedder defers `embed_fragment` calls and flushes them in batches of up to 500 texts /
  400k characters across files (`Embedder(defer=True)`, the indexer's default), so a run makes
  a few dozen Voyage requests instead of one per file.
- Graph reads are batched: `callers_of`, `callees_of`, `referrers_of`, `supertypes_of`,
  `subtypes_of`, `symbols_meta`, `symbols_in_files`, `repos_of_symbols`, `resolve_symbols` and
  `lexical_search_many` all take id/term lists, and expansion consumes them frontier by
  frontier (`src/bce/core/orchestrator/bulk.py`, which falls back to the per-symbol methods).
  The bulk queries use `UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})` rather than
  `WHERE s.symbol_id IN $ids`, which on Apache AGE measured about ten times faster. Together
  with the expansion budget this took a long PR description from ~48 s to under 2 s.
- The confidence level is computed from anchor *agreement* — strong / corroborated anchors,
  whether the top candidate is one, the share of weak single-source anchors and of test code in
  the selection — instead of counting source tags across all anchors, which reported a pack of
  eight noise anchors as `high`. Coverage gains `strong_anchor_count`,
  `corroborated_anchor_count`, `anchor_agreement_ratio`, `weak_anchor_ratio`,
  `max_anchor_strength`, `test_ratio` and `pool_size`.
- Context items carry `docstring`, `body`, `is_anchor`, `anchor_strength` and `is_test`;
  bodies are loaded for the selected candidates only (`GraphRepository.get_symbol_detail`).

### Fixed

- The assembler treated `graph_distance == 0` as missing (`0 or 3`), so anchors were always
  rendered reference-only instead of at full detail.
- Indexing slowed down quadratically with graph size. AGE creates a label table on its first
  `MERGE` with only a primary key, so every edge upsert's `MATCH (a {gid: ...})` was a sequential
  scan of the growing vertex tables (~24 ms per edge on a 15k-symbol graph, most of a 40-minute
  index). Migration `0008_graph_label_indexes` now creates the vertex labels up front with a GIN
  index on `properties` (`fastupdate = off`), which brings the lookup to ~2 ms and makes the
  embedding calls the remaining cost.
- `VoyageEncoder` sent a whole file's symbols in one request; files with more than 1000 symbols
  (or over the token ceiling) failed the index with `InvalidRequestError`. Requests are now split
  into batches of at most 500 texts / 400k characters.

## [0.2.1] - 2026-09-11

### Added

- Indexing over MCP: `index_repo` and `reindex_repo`, plus the read-only `get_index_job` and
  `list_index_jobs`. Both write tools take a local working tree and enqueue a job rather than
  indexing inline, because a full index outlives any agent's tool-call timeout. They are gated
  behind `BCE_MCP_ALLOW_WRITE` (default off) and are neither advertised nor dispatchable while
  it is off. With it on, the MCP process also runs job workers, so queued indexing progresses
  without a separate `bce serve`; the pool claims `index` and `reindex` only, so a clone that
  REST enqueued is not run inside an editor-spawned process. Cloning a remote repository is
  deliberately not exposed over MCP: it carries its own credentials and stays on REST and the CLI.
- `JobWorkerPool(job_types=...)` and `claim_next_job(job_types=...)` restrict a pool to a subset
  of the queue.
- `bce --env-file PATH` and `BCE_ENV_FILE` load configuration from an explicit `.env`. Editors
  spawn `bce serve-mcp` from their own working directory, so a bare `.env` was never found and the
  MCP server silently ran on defaults — no Voyage key, no database password. `serve` and
  `serve-mcp` accept the option after the command name as well as before it, because that is how
  an MCP config or a service definition is written.

### Changed

- The MCP catalog is now eight read-only tools instead of fourteen. Removed `semantic_search`,
  `find_similar_code`, `find_implementers`, `get_type_hierarchy`, `get_dependencies`,
  `suggest_change_sites`, `select_repos` and `assemble_context`: an agent picks worse when
  handed a dozen overlapping retrieval tools, and `hybrid_search` and `get_context_for_task`
  already cover what they did. All of them remain available over REST.
- The optional `mcp` extra is now `mcp>=1.0,<2`. SDK 2.x dropped `Server.list_tools` /
  `Server.call_tool`, so `bce serve-mcp` failed on a default `pip install` of 2.x.

### Fixed

- The first search on an MCP server no longer hangs. Building the embedding encoder imports
  numpy, and loading that extension lazily from inside a tool call stalled for over three
  minutes on Windows while the same import costs seconds at process start, so the encoder is
  now built during startup. Tool dispatch also moved off the event loop onto a worker thread,
  since its database and HTTP calls are synchronous.
- The MCP server configures logging, against stderr because stdout carries the protocol. Its job
  worker's failures previously fell through to Python's last-resort handler and surfaced in editor
  logs as bare tracebacks; they are now the same JSON lines the API server emits, and they reach
  the log file too. `setup_logging(stream=...)` makes the console destination overridable.

## [0.2.0] - 2026-09-10

### Changed

- Both READMEs now reference images, videos and sibling documents by absolute URL. PyPI
  renders the long description outside the repository, so relative paths resolved to
  nothing there: the banner was a broken image and every documentation link a dead one.
- Selecting `BCE_EMBEDDING_PROVIDER=voyage` without its API key no longer falls back to the
  hashing encoder. Both that case and a missing `voyageai` package now raise
  `EncoderConfigError`, which the CLI reports as a single line and the REST API as `503`.
  A substituted encoder is not detectable by nearest-neighbour search, so it returned bad
  anchors instead of an error. An unrecognised provider name is rejected the same way.
- Removed `Settings.voyage_ready`, whose "ready" no longer matched how the encoder is
  chosen.

## [0.1.0] - 2026-09-08

First public release.

### Added

- Deterministic retrieval pipeline: anchor discovery, graph expansion, scoring, narrowing
  and context assembly, with coverage and confidence reporting.
- Code graph over PostgreSQL with Apache AGE and pgvector in a single database. Layer-3
  results are filtered per user against the `scopes` table, resolved from the
  `X-BCE-User` header.
- Tree-sitter indexing for Python, JavaScript and TypeScript, with optional grammars for
  Java, C# and Go. Incremental re-indexing driven by `git diff`.
- Layer 1, 2 and 3 tool surfaces exposed over both REST and MCP.
- Web interface at `/ui`: a graph explorer and a player that replays each pipeline stage
  for a given task. Ships inside the wheel; English by default with a Turkish catalog.
- `bce` command line interface: `migrate`, `index`, `index-remote`, `reindex`, `bench`,
  `resolve-symbol`, `find-references`, `languages`, `serve` and `serve-mcp`.
- Benchmark harness reporting latency, recall, precision and determinism.
- Docker Compose deployment with PostgreSQL, Apache AGE and pgvector preconfigured.
- `server.json` manifest describing the stdio server for the official MCP registry.

[Unreleased]: https://github.com/bgts-ai-org/bgts-context-engine/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/bgts-ai-org/bgts-context-engine/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/bgts-ai-org/bgts-context-engine/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bgts-ai-org/bgts-context-engine/releases/tag/v0.1.0
