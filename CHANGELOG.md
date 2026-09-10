# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

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

[Unreleased]: https://github.com/bgts-ai-org/bgts-context-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bgts-ai-org/bgts-context-engine/releases/tag/v0.1.0
