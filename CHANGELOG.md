# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
