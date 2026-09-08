# Architecture

## The problem

An AI coding agent working on an unfamiliar repository has to decide what to read before
it can decide what to change. The common answer is embedding search over chunked files:
cheap to build, and wrong in a specific way. It returns text that *reads* like the query
rather than code that *participates* in the behaviour. Ask for "the login timeout" and you
get the five places that mention timeouts, not the one function that sets it and the three
callers that will break when you do.

The information that answers the question is structural. `handleLogin` calls
`refreshSession`, which reads `SESSION_TTL`, which is written in one place. That is a graph
walk, and it has an exact answer.

BGTS Context Engine indexes a repository into that graph and answers questions by walking
it. Embeddings appear in exactly one place — finding entry points when the task text names
nothing recognisable — and never in ranking.

## The deterministic line

The engine is split at one hard boundary, and most of its design follows from it.

Everything that produces the payload is deterministic: parsing, graph construction, graph
expansion, scoring, selection, assembly. Same commit and same task in, same bytes out. No
model, no clock, no randomness, no unordered iteration.

Everything probabilistic sits outside: the agent that consumes the pack, and the optional
embedding provider that can nominate anchors. Both influence *which* subgraph is examined.
Neither influences how it is ranked or rendered.

This is worth the constraint because it makes the engine debuggable. When an agent makes a
bad change, you can replay the exact context it was given, see which stage introduced the
wrong symbol, and fix that stage. With a probabilistic retriever, the same investigation
ends in a shrug.

## Layout

```
task text ──► Layer 3  orchestration   context packs, blast radius, repo selection
                 │
                 ├──► Layer 2  search   semantic / hybrid / similar-code
                 │
                 └──► Layer 1  primitives   resolve, references, call graph, hierarchy
                                │
                       ┌────────┴────────┐
                       │   code graph    │  PostgreSQL
                       │  AGE + pgvector │  + relational metadata + FTS
                       └────────┬────────┘
                                │
                          indexing pipeline
                                │
                       git repository (local or remote)
```

The three layers are a deliberate progression from precise to interpretive.

**Layer 1** answers questions with exactly one correct answer. Where is this symbol
defined? Who calls it? What does it inherit from? These are graph lookups; they either
find the answer or report that they did not.

**Layer 2** answers questions of similarity. Find symbols like this description, or like
this code fragment. Hybrid search blends lexical and semantic hits at a fixed 0.55 / 0.45
with a small structural tiebreak, so a name that matches exactly is never beaten by
something that merely reads similarly.

**Layer 3** answers the question an agent actually has: *given this task, what should I
read?* This is where the retrieval pipeline runs, and it is the only layer that applies
the per-user repository scope. [docs/retrieval.md](retrieval.md) covers it in full.

Every layer is one set of Python functions exposed through two transports. `POST
/v1/get-context-for-task` over REST and the `get_context_for_task` MCP tool call the same
function with the same arguments and get the same payload. There is no second
implementation to drift.

## Indexing

Indexing is two passes over the repository, because cross-file references cannot be
resolved while looking at one file.

**Pass one, per file.** Tree-sitter parses the source; a language provider walks the tree
and emits `File`, `Symbol`, `Module`, `Route` and `DesignNote` nodes together with every
edge it can see without leaving the file. References it cannot resolve locally are recorded
as unresolved, along with the file's imports and exports. The fragment is written to the
graph, and symbol text is embedded if an embedding provider is configured.

**Pass two, repository-wide.** The linker builds a global map of exported names and
resolves the unresolved references into cross-file `CALLS`, `REFERENCES`, `INHERITS` and
`IMPLEMENTS` edges, following each file's actual import bindings rather than guessing by
name. If SCIP indexers are available it merges their output, which upgrades edge
provenance from `treesitter` to `scip`. Finally the bridge extractor connects
cross-language call sites that no single parser can see.

Re-indexing is incremental and driven by `git diff --name-status` between the last indexed
commit and the target. Each changed file's subgraph is detached and deleted, then rebuilt;
unchanged files are re-parsed in memory only, because the linker needs the global name map
to reconnect the edges that pointed into the changed files.

Symbol identity is a digest of language, package, namespace, kind, name and signature —
never of path or line number. Moving a function between files or reformatting around it
keeps its id, so history and embeddings survive. Renaming it does not, which is correct: a
renamed function is a different symbol.

[docs/languages.md](languages.md) covers the parsers and how to add one.

## Storage

One PostgreSQL instance holds everything, which is the main operational decision in the
project.

- **Apache AGE** stores the property graph and answers Cypher.
- **pgvector** stores symbol and file embeddings for anchor discovery.
- **Plain tables** hold repositories, tasks, task history, users, scopes, the audit log and
  the job queue.
- **A generated `tsvector` column** on `symbol_fts` provides lexical search over names,
  signatures and docstrings.

The alternative — a dedicated graph database next to a dedicated vector database next to
PostgreSQL — means three systems to run, three backup stories, and no transactional
boundary between a symbol and its embedding. Here, a single connection answers a Cypher
traversal, a vector search and a SQL join, and one `pg_dump` captures the whole index.

[docs/data-model.md](data-model.md) has the node labels, edge types, tables and migrations.

## Surfaces

| Surface | For | Entry point |
| --- | --- | --- |
| MCP over stdio | AI coding agents | `bce serve-mcp` |
| REST | services and scripts | `bce serve`, OpenAPI at `/docs` |
| Web UI | humans reading the graph | `/ui`, bundled in the wheel |
| CLI | indexing and operations | `bce --help` |

The web interface is not a dashboard. Its second tab replays a `get_context_for_task` call
stage by stage — anchors lighting up, expansion spreading outward, candidates being scored
and cut — against the real pipeline through `POST /v1/ui/context-trace`. When retrieval
returns something surprising, watching where it went wrong is considerably faster than
reading the scores.

[docs/mcp.md](mcp.md) documents the agent-facing surface and the full endpoint inventory.

## Scope and access

Layer-3 responses are filtered against the `scopes` table, resolved from the `X-BCE-User`
header. Layers 1 and 2, indexing, jobs and the UI endpoints are not filtered, and the MCP
stdio server runs as the system principal. The engine has no authentication of its own and
expects to sit behind something that does. The exact boundaries are in
[SECURITY.md](../SECURITY.md); read it before exposing a port.

## What this is not

It does not generate or edit code. It has no opinion on your agent framework. It does not
try to be a language server: it is a whole-repository, cross-repository, cross-language
index that answers "what should I read" rather than "what can I autocomplete".

It is also not a general-purpose graph database. The schema, the expansion template and the
scoring weights are specialised for one question, which is why the answers are good and
why the weights are a versioned constant rather than a configuration file.
