# Data model

Everything lives in one PostgreSQL database: the property graph in Apache AGE, embeddings
in pgvector, and metadata in ordinary tables.

## Graph nodes

| Label | Id property | Id shape |
| --- | --- | --- |
| `Repo` | `repo_id` | slug of the logical name |
| `File` | `file_id` | `{repo_id}:{path}` |
| `Symbol` | `symbol_id` | see [symbol identity](#symbol-identity) |
| `Module` | `module_id` | `{repo_id}:{namespace}` |
| `Route` | `route_id` | `route:{repo_id}:{digest}` |
| `DesignNote` | `note_id` | `note:{digest}` |

Every node also carries a `gid` property equal to its own id, which is what edge MERGE
statements match on. It exists so an edge can be attached without knowing the label of
either endpoint.

**Properties.** `Repo` has `name`, `default_branch`, `last_indexed_commit` and optionally
`remote_url`. `File` has `path`, `language`, `repo_id` and `indexed_at_commit`. `Symbol`
has `name`, `kind`, `signature`, `visibility`, `docstring`, `body`, `namespace`, `file_id`,
`line` and `indexed_at_commit`. `Route` has `http_method`, `path_pattern`, `framework`,
`file_id` and `line`. `DesignNote` has `kind`, `text`, `file_id` and `line`.

`Symbol.kind` is one of `function`, `method`, `class`, `interface`, `variable`, `constant`,
`enum`, `type`, `field`, `property`, `constructor`.

## Graph edges

| Type | From → To | Meaning |
| --- | --- | --- |
| `DEFINED_IN` | Symbol → File | the symbol is declared in this file |
| `BELONGS_TO` | File → Repo | file membership |
| `IMPORTS` | File → Module, Module → File | import graph, used for transitive dependency walks |
| `CALLS` | Symbol → Symbol | a call site, with `line` where known |
| `REFERENCES` | Symbol → Symbol | a non-call use, with `ref_kind` |
| `INHERITS` | Symbol → Symbol | base class |
| `IMPLEMENTS` | Symbol → Symbol | interface implementation |
| `ROUTES_TO` | Route → Symbol | HTTP route to its handler |
| `EXPLAINS` | DesignNote → Symbol | a `WHY:` comment about this symbol |

Two properties on edges do most of the work in retrieval:

**`ref_kind`** — `define`, `write`, `read` or `pass`. What the reference actually did, not
just that it happened. Scoring weights `define` five times higher than `pass`.

**`provenance`** — `scip`, `treesitter` or `heuristic`, in descending order of trust. A
`scip` edge came from a real compiler index and is exact. A `treesitter` edge came from
syntax and is right almost always. A `heuristic` edge came from a pattern match, such as a
cross-language bridge, and is a good guess. Retrieval scores them accordingly, and the
coverage block reports the mix, so a caller can see how much of an answer rests on
guesswork.

Synthesised edges also carry `synthesized_by`, naming the extractor: `route-binding`,
`rn-bridge`, `swift-objc-bridge`, `expo-module-extract` or `rn-event-channel`.

`ROUTES_TO` is what lets `POST /api/v1/meetings/{id}/webhook` in a bug report resolve
directly to the handler function, and `EXPLAINS` is what lets a `WHY:` comment travel with
the symbol it explains into the context pack. Comments that record intent are the one kind
of prose in a repository that a graph should carry, because they answer questions the code
cannot.

## Symbol identity

```
{language}::{package}::{namespace}::{name}#{blake2b-16}
```

The digest covers language, package, namespace, kind, name and signature. It deliberately
does **not** cover the file path or the line number.

The consequence is that a symbol keeps its identity when it moves between files, when code
above it shifts its line number, and when it is reformatted. Task history and embeddings
keyed on the id stay valid. Overloads stay distinct because the signature is part of the
digest.

Renaming a symbol produces a new id. That is the intended behaviour: after a rename it is
a different symbol, and treating it as the same one would silently carry over history that
no longer applies.

## Relational tables

Applied in order by `bce migrate`, tracked in `schema_migrations`.

| Migration | Adds |
| --- | --- |
| `0001_extensions_graph` | `age` and `vector` extensions, the `code_graph` graph |
| `0002_relational` | `repos`, `tasks`, `task_history`, `users`, `scopes`, `audit_log` |
| `0003_vector` | `embeddings` with a `vector(768)` column and an HNSW index |
| `0004_rls` | row-level security policies on `repos` and `embeddings` |
| `0005_embeddings_dim` | widens embeddings to `vector(1024)`, truncates, rebuilds the index |
| `0006_fts` | `symbol_fts` with a generated `tsvector` column |
| `0007_jobs` | the `jobs` queue |
| `0008_graph_label_indexes` | GIN indexes on the properties of every vertex label |
| `0009_fts_identifier_parts` | the weighted FTS document (name / identifier parts / container + file stem / signature + docstring) |
| `0010_file_churn` | `file_churn` — commits per file inside the churn window |
| `0011_search_text` | `symbol_fts.body` (the symbol's full text) folded into the FTS document, every word of the file *path* at weight C, `embeddings.chunk` and the `(kind, ref_id, chunk)` uniqueness that lets a long symbol be several vectors |
| `0012_body_trgm` | `pg_trgm` GIN indexes on `symbol_fts.body` and `symbol_fts.file_id`, for the usage and path anchor sources (skipped with a notice where the extension is unavailable) |

Note that `0005` **truncates** `embeddings`. Widening a vector column cannot preserve
existing rows, so upgrading across that migration means re-indexing to repopulate them.
Later width changes do not get another numbered SQL file: after the SQL migrations,
`bce migrate` runs `align_embedding_dim` to re-type the column to `BCE_EMBEDDING_DIM`.
Stored vectors of another width are discarded only with `--reset-embeddings`.

`0011` does not truncate anything, but rows indexed before it have no `body` and a single
`chunk 0` vector, so the usage source and body search only see what has been re-indexed
since. Run `bce index` (or `bce reindex` over the whole history) once after upgrading.

### repos

`repo_id` primary key, `name`, `default_branch` (default `main`), `remote_url`,
`last_indexed_commit`, `created_at`. `last_indexed_commit` is the baseline that
`bce reindex` diffs against when no `--since` is given.

### tasks and task_history

`tasks` holds `id`, `title`, `description`, `epic`, `labels` (JSONB), `component`,
`linked_commit`, `linked_pr`, `updated_at`. `task_history` maps a task to the files it
touched: `task_id`, `touched_file_id`, `commit`, `touched_at`, indexed on `task_id`.

`task_history` is what feeds the `history` anchor source. When a task has been worked on
before, the files it touched are strong evidence about where the next change goes.

### users and scopes

`users` holds `id`, `name`, `email`. `scopes` grants a user access to a repository:
`user_id` and `repo_id` foreign keys with `ON DELETE CASCADE`, an `access` column
defaulting to `read`, and a uniqueness constraint on the pair.

This table is the whole authorisation model. A user id arrives in the `X-BCE-User` header,
its rows here decide which repositories a Layer-3 answer may include, and a user with no
rows sees nothing.

### audit_log

`ts`, `user_id`, `task_id`, `commit`, `tool`, `locale` and `returned_symbols` (JSONB). The
point is being able to answer, after an agent made a bad change, exactly what it had been
told — which is only meaningful because retrieval is reproducible.

### embeddings

`kind` (`symbol` or `file`), `ref_id`, `repo_id`, `chunk`, `content`, `model`, `embedding
vector(N)`, `indexed_at_commit`, unique on `(kind, ref_id, chunk)`. `N` is `BCE_EMBEDDING_DIM`
(1024 for Voyage, 1536 for jina-code-embeddings-1.5b, …). Indexed with HNSW using
`vector_cosine_ops`; searches use the `<=>` cosine distance operator and order by
`(distance, ref_id, chunk)` so ties break stably.

A symbol longer than one embedding window (2 000 characters, 200 overlap, at most 12
windows) is stored as several rows, `chunk 0, 1, 2 …`, each carrying the same header (name,
kind, path, signature, docstring) and one window of the body, so a toast string sixty lines
into a page component is as findable as its first line. Nearest-neighbour search collapses
the rows back to one symbol at its best chunk's rank; re-indexing a symbol that got shorter
trims the chunks it no longer needs.

The `model` column records which encoder produced each row (`<model>-<dim>`). Embeddings
from different models are not comparable, so this is what makes it possible to detect a
stale index after switching providers. Changing `N` is a re-index boundary: `bce migrate`
refuses to drop the stored vectors unless run with `--reset-embeddings`.

### symbol_fts

`symbol_id` primary key, plus `repo_id`, `name`, `kind`, `signature`, `docstring`, `body`
(the symbol's full declaration text, capped at 24 000 characters by the extractor),
`file_id`, `line`, `indexed_at_commit`, and a stored generated, weighted `tsvector`:

| Weight | Contents |
| --- | --- |
| A | the exact name |
| B | its camelCase / snake_case parts (`getUserById` → `get user by id`) |
| C | the container's name parts and every word of the file path (`src components settings sections Repos Section`) |
| D | signature, docstring and the identifier words of the whole body |

With a GIN index on `document`. The `simple` configuration is used rather than `english`
on purpose: identifiers are not English words, and stemming `getUserById` helps nobody.
Queries use prefix matching, ranked by `ts_rank` with length normalisation (so a 300-line
component does not outrank a 3-line helper by sheer word count) and `symbol_id` as tiebreak.

`body` is also indexed with `pg_trgm` (migration `0012`), which is what makes the usage
anchor source — "which symbols contain `localStorage.getItem('token')`" — an index lookup
rather than a scan; the same index on `file_id` serves the path source's suffix matches.

This table mirrors the `Symbol` nodes. AGE cannot do full-text search, so lexical anchor
discovery reads from here.

### jobs

`job_id` UUID, `job_type` (`index`, `index_remote` or `reindex`), `payload` JSONB, `status`
(`pending`, `running`, `succeeded`, `failed`, `cancelled`), `result`, `error`, and
`created_at` / `started_at` / `finished_at`. Indexed on `(status, created_at)`.

Workers claim rows with `FOR UPDATE SKIP LOCKED`, so several workers can share the queue
without coordinating. Remote-indexing credentials are **not** written to `payload`; the
worker reads them from the environment.

## Row-level security

Migration `0004` defines policies on `repos` and `embeddings`. A row is visible when the
`bce.user_id` session variable is unset, or when the user has a matching row in `scopes`.

**The application does not set that variable.** Scoping is enforced in application code by
`ScopeFilter`, and only on Layer 3. The policies are a second line of defence for direct
database access, not the live enforcement path — if you query the database yourself, set
`bce.user_id` to get them. The AGE graph is not covered by RLS at all; graph filtering is
application-side by design.

## Apache AGE

Graph name `code_graph`, configurable with `BCE_GRAPH_NAME`. Each session runs `LOAD 'age'`
and sets `search_path` to include `ag_catalog`. Cypher is issued through
`ag_catalog.cypher(...)` with parameters passed as a JSON string cast to `agtype`, and
results are JSON-parsed per cell after stripping the `::vertex` / `::edge` / `::path`
suffixes.

Writes are `MERGE (n:Label {id_prop: $id}) SET ...`, so indexing is idempotent: re-running
it over an unchanged file produces no new nodes.

Every traversal query ends with `ORDER BY symbol_id`, or `ts_rank DESC, symbol_id ASC` for
full-text search. This is not decoration — it is what makes retrieval reproducible, since
PostgreSQL gives no ordering guarantee otherwise.
