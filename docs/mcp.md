# MCP and API reference

Every capability is one set of Python functions exposed through two transports. The MCP
tool `get_context_for_task` and `POST /v1/get-context-for-task` call the same function with
the same arguments and return the same payload, so nothing can drift between them.

## Connecting an agent

```bash
bce serve-mcp
```

This speaks MCP over stdio and registers as `bgts-context-engine`. Point any MCP client at
it:

```json
{
  "mcpServers": {
    "bgts-context-engine": {
      "command": "bce",
      "args": ["serve-mcp"],
      "env": { "BCE_DB_HOST": "localhost", "BCE_DB_PASSWORD": "..." }
    }
  }
}
```

The database must be reachable and at least one repository indexed. The stdio server runs
as the system principal and can see every indexed repository, so run one process per trust
boundary.

## Tools

Fourteen tools, in three layers. Every one accepts an optional `locale` and returns the
same envelope.

### Layer 1 — exact answers

| Tool | Required | Optional | Returns |
| --- | --- | --- | --- |
| `resolve_symbol` | `name` | `repo_id` | `matches`, `indexed_at_commit` |
| `find_references` | `symbol_id` | | `references` with file, line, edge type, `ref_kind`, provenance |
| `find_implementers` | `symbol_id` | | `implementers` |
| `get_call_graph` | `symbol_id` | `hops` (1, max 10), `direction` (`both`) | `callers`, `callees` |
| `get_dependencies` | `file_id` | `transitive` (false) | `dependencies` |
| `get_type_hierarchy` | `symbol_id` | | `supertypes`, `subtypes` |

These either find the answer or say they did not. `resolve_symbol` is usually the first
call: it turns a name from a bug report into the `symbol_id` the other tools take.

### Layer 2 — similarity

| Tool | Required | Optional | Returns |
| --- | --- | --- | --- |
| `semantic_search` | `query` | `repo_ids`, `limit` (10, max 100) | `candidates`, `model` |
| `hybrid_search` | `query` | `repo_ids`, `limit` | `candidates`, `model`, `weights_version` |
| `find_similar_code` | `code` | `repo_ids`, `limit` | `candidates`, `model` |

Prefer `hybrid_search` over `semantic_search`. It blends lexical and vector hits at a fixed
0.55 / 0.45 with an exact-name boost, so a symbol whose name matches the query is never
beaten by something that merely reads similarly — the failure mode pure vector search is
prone to.

### Layer 3 — task context

| Tool | Required | Returns |
| --- | --- | --- |
| `get_context_for_task` | `task_text` | `anchors`, `context`, `coverage` |
| `suggest_change_sites` | `task_text` | ranked candidate change sites |
| `expand_blast_radius` | `target_symbols` | the impact surface of changing them |
| `select_repos` | `task_text` | which repositories the task probably concerns |
| `assemble_context` | `symbol_ids` | those symbols fitted into a token budget |

`get_context_for_task` is the one an agent should reach for first. Full options:

| Field | Default | |
| --- | --- | --- |
| `task_text` | required | the task in prose |
| `task_id` | `null` | for audit correlation and history lookup |
| `max_candidates` | `8` | how many symbols to return |
| `max_tokens` | `4000` | budget for the assembled context |
| `commit` | `null` | pin the answer to a commit |
| `repo_ids` | `null` | restrict to these repositories |
| `explicit_symbols` | `null` | symbol names you already know are relevant |
| `route_paths` | `null` | HTTP paths from a stack trace or bug report |
| `history_file_ids` | `null` | files previous work on this task touched |
| `component_repo_ids` | `null` | repository hints from issue-tracker metadata |
| `semantic_candidates` | `null` | pre-computed anchor symbol ids |
| `auto_semantic` | `true` | let the engine run its own vector search for anchors |

The hint fields are the highest-leverage thing available to a caller. Passing one known
symbol name in `explicit_symbols`, or the route path from a stack trace in `route_paths`,
converts a guess into a certainty and typically moves the reported confidence a whole
level. If your agent already parsed a traceback, forward it.

`commit` matters for reproducibility: pin it and the response tells you, through
`commit_mismatch`, whether the index actually covers that commit.

Layer-3 tools apply the repository scope filter when a user id is supplied
programmatically. The stdio server does not supply one.

## Response envelope

```json
{
  "tool": "get_context_for_task",
  "payload": { "...": "deterministic, language-neutral" },
  "message": "Assembled 8 symbols (2913 tokens), confidence: high.",
  "locale": "en"
}
```

`payload` is the contract. It never changes with locale and is what an agent should read.
`message` is human-readable prose for logs and UIs, translated according to `locale`, and
should not be parsed.

A `get_context_for_task` payload contains:

- **`anchors`** — `symbol_id` → the sources that nominated it (`explicit`, `history`,
  `lexical`, `semantic`). Reading this tells you *why* the engine looked where it did.
- **`context`** — `items` plus `used_tokens`, `budget`, `included` and `skipped`. Each item
  has `symbol_id`, `repo_id`, `detail_level`, `graph_distance`, `score`, `tokens` and
  `content`. A non-zero `skipped` means relevant symbols were dropped for budget; raise
  `max_tokens` or lower `max_candidates`.
- **`coverage`** — the trust report, ending in `confidence` of `high`, `medium` or `low`.
  See [docs/retrieval.md](retrieval.md#7-coverage-and-confidence). Treat `low` as a signal
  to ask a clarifying question rather than to start editing.

## REST endpoints

`bce serve`, then OpenAPI at `/docs`.

### Layer 1 (GET)

| Path | Query |
| --- | --- |
| `/v1/resolve-symbol` | `name`, `repo` |
| `/v1/find-references` | `symbol_id` |
| `/v1/find-implementers` | `symbol_id` |
| `/v1/get-call-graph` | `symbol_id`, `hops` (1–10), `direction` (`callers`/`callees`/`both`) |
| `/v1/get-dependencies` | `file_id`, `transitive` |
| `/v1/get-type-hierarchy` | `symbol_id` |

### Layer 2 (POST)

`/v1/semantic-search`, `/v1/hybrid-search` with `{query, repo_ids, limit}`;
`/v1/find-similar-code` with `{code, repo_ids, limit}`.

### Layer 3 (POST)

`/v1/get-context-for-task`, `/v1/suggest-change-sites`, `/v1/expand-blast-radius`,
`/v1/select-repos`, `/v1/assemble-context`. These are the only endpoints that apply the
`X-BCE-User` scope filter.

### Indexing

Synchronous `POST /v1/index`, `/v1/index-remote`, `/v1/reindex`, or the queue:
`POST /v1/jobs/index`, `/v1/jobs/index-remote`, `/v1/jobs/reindex` returning 202, with
`GET /v1/jobs`, `GET /v1/jobs/{job_id}` and `POST /v1/jobs/{job_id}/cancel`. Prefer the
queue for anything large; see [docs/deployment.md](deployment.md#indexing-large-repositories).

### Meta and UI

`GET /healthz` reports database reachability and returns 503 when it is unavailable.
`GET /v1/languages` lists supported languages and extensions without touching the
database.

The `/v1/ui/*` endpoints back the web interface: `config`, `repos`, `stats`, `graph`,
`node`, `neighbors`, `search`, and `POST /v1/ui/context-trace`, which replays the retrieval
pipeline stage by stage for the trace player. They return flat JSON rather than the tool
envelope, and **none of them apply scope filtering**.

## Headers

| Header | Effect |
| --- | --- |
| `X-BCE-User` | Names the user whose `scopes` rows filter Layer-3 results. Absent means unrestricted. |
| `Accept-Language` | Locale for `message`; `?locale=` takes precedence. |

There is no built-in authentication. `X-BCE-User` is trusted exactly as received, so
whatever sits in front of the engine must authenticate the user and overwrite the header.

## CLI

| Command | |
| --- | --- |
| `bce migrate` | apply pending migrations |
| `bce index --repo P --name N` | full-index a local repository |
| `bce index-remote --url U [--name N] [--branch B]` | clone and index a remote repository |
| `bce reindex --repo P --name N [--since C] [--to HEAD]` | incremental re-index from git diff |
| `bce resolve-symbol --name N [--repo R]` | Layer 1 from the shell |
| `bce find-references --symbol-id S` | Layer 1 from the shell |
| `bce context --task T [--max-tokens 4000] [--max-candidates 8]` | Layer 3 from the shell |
| `bce bench --cases F [--out F] [--determinism-runs 3]` | benchmark report as JSON |
| `bce languages` | list supported languages; needs no database |
| `bce serve [--host] [--port] [--reload] [--no-ui]` | REST API and web UI |
| `bce serve-mcp` | MCP over stdio |
| `bce --version` | version, for bug reports |

Every command prints the human-readable message followed by the JSON payload, so output
pipes into `jq` while staying readable.
