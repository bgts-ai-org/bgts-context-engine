# MCP and API reference

Every capability is one set of Python functions exposed through two transports. The MCP
tool `get_context_for_task` and `POST /v1/get-context-for-task` call the same function with
the same arguments and return the same payload, so nothing can drift between them.

The MCP catalog is deliberately narrower than the REST surface. An agent chooses worse when
handed a dozen overlapping retrieval tools, so only the non-redundant ones are exposed:
`hybrid_search` covers what `semantic_search` and `find_similar_code` do, and
`get_context_for_task` covers `suggest_change_sites`, `select_repos` and `assemble_context`.
Everything left out is still available over REST.

## Connecting an agent

Install the `mcp` extra onto an interpreter the editor can spawn. The extra is pinned to
Python MCP SDK 1.x (`mcp>=1.0,<2`); 2.x removed the `list_tools` / `call_tool` decorators
`bce serve-mcp` uses.

Cursor and VS Code start the server themselves. They do **not** activate a project `.venv`,
so `bce` must be on the user PATH (or you use `uvx` as the command):

```bash
pip install "bgts-context-engine[mcp]"
bce --version   # in a new terminal, venv deactivated
```

```bash
bce serve-mcp
```

That last command is what the editor runs over stdio. It registers as `bgts-context-engine`
and prints nothing: stdout is the protocol. Do not leave a copy running in a terminal for
the IDE.

After you add or change MCP config, **restart Cursor or VS Code** (or Command Palette →
“Developer: Reload Window”). The server should appear enabled with eight tools, or ten once
indexing is enabled (see [Configuration](#configuration)).

### One-command setup: `bce cursor-init`, `bce claude-init`

Run in the project the agent works on, pointing at the engine's `.env`:

```bash
bce --env-file /path/to/engine/.env cursor-init --repo-id my-service
bce --env-file /path/to/engine/.env claude-init --repo-id my-service
```

| | writes | merged into existing? |
| --- | --- | --- |
| `cursor-init` | `.cursor/mcp.json` — server entry with the absolute path of this `bce`, `serve-mcp --env-file …`, `PYTHONUTF8=1` | yes: other servers kept |
| | `.cursor/rules/bgts-context-engine.mdc` — `alwaysApply` rule | overwritten (ours) |
| `claude-init` | `.mcp.json` — same server entry | yes |
| | `CLAUDE.md` — a section between `<!-- bgts-context-engine:begin/end -->` markers | yes: rest of the file kept |
| | `.claude/settings.json` — `UserPromptSubmit` hook running `bce precontext` (skip with `--no-hook`) | yes: other hooks kept |

Options: `--project DIR` (default `.`), `--repo-id ID` (repeatable; default: the directory
name — must match the name the repository was indexed under), `--env-file PATH` (default:
`bce --env-file` / `BCE_ENV_FILE`, then `<project>/.env`, then `./.env`; without one the
server reads `BCE_*` from the editor's environment and the command says so), `--bce-command`
(default: the running `bce`). Both commands are idempotent.

**Why a rule and a hook, not just the server.** In the agent benchmark (a Cursor CLI agent
on 14 tasks against a React/TypeScript codebase, plain vs. with the engine) the server alone made
things worse: the agent asked the graph late, after a round of grep, or asked it eight times
per task. Two changes turned that around — a rule that says *call it first, treat `file:line`
as a verified location, do not edit a file because it was listed*, and handing the agent the
graph's answer before its first turn. With both, on 14 tasks against the same model and
machine: total tokens −20 %, grep/glob output −46 %, model turns −13 %, 73/73 checks passed
against 72/73 for the plain agent, with 23 % fewer lines changed.

The hook is Claude Code only. Its `UserPromptSubmit` event accepts `additionalContext`;
Cursor's `beforeSubmitPrompt` can only allow or block a prompt, so under Cursor the rule
carries the whole instruction and the agent makes the one call itself. `bce precontext`
reads the hook JSON on stdin, runs `get_context_for_task` once for the prompt (skipping
prompts under 20 characters and slash commands), and prints the context block capped at
Claude Code's 10 000-character limit. It fails open: an unreachable database means a
prompt without context, never a blocked prompt. `bce precontext --task "…"` prints the same
block for inspection.

### Manual configuration

**Cursor** — `~/.cursor/mcp.json` for every project, or a local `.cursor/mcp.json` (the
`.cursor/` directory is gitignored):

```json
{
  "mcpServers": {
    "bgts-context-engine": {
      "command": "bce",
      "args": ["serve-mcp", "--env-file", "C:/path/to/project/.env"]
    }
  }
}
```

**VS Code** — user MCP settings, or a project `.vscode/mcp.json` (also gitignored):

```json
{
  "servers": {
    "bgts-context-engine": {
      "type": "stdio",
      "command": "bce",
      "args": ["serve-mcp", "--env-file", "/path/to/project/.env"]
    }
  }
}
```

## Configuration

The editor starts the server from its own working directory, not the project directory, so a
bare `.env` is never found. Point at it explicitly with `--env-file` (above), or the
`BCE_ENV_FILE` environment variable, or list the individual variables in the `env` block:

```json
"env": { "BCE_DB_HOST": "localhost", "BCE_DB_NAME": "bce", "BCE_DB_PASSWORD": "..." }
```

Prefer `--env-file`: `mcp.json` is a config file people share and paste into issues, and the
engine needs real secrets (`BCE_VOYAGE_API_KEY`, `BCE_BITBUCKET_TOKEN`, the database
password). Every `bce` command takes it before the command name (`bce --env-file PATH index
…`), and `serve-mcp` also takes it after, since that is the form an MCP config reads
naturally. So the shell and the editor read one file.

The variables that matter for MCP:

| Variable | |
| --- | --- |
| `BCE_DB_*` | database connection; without it the server starts but every tool fails |
| `BCE_EMBEDDING_PROVIDER` | `hashing` (offline default), `voyage` or `openai` (OpenAI-compatible server, e.g. vLLM) |
| `BCE_EMBEDDING_MODEL` / `BCE_EMBEDDING_DIM` | pinned together as the stored model id; a change is a re-index (`bce migrate --reset-embeddings`) |
| `BCE_VOYAGE_API_KEY` | required when the provider is `voyage` |
| `BCE_EMBEDDING_BASE_URL` | the server URL when the provider is `openai` |
| `BCE_EMBEDDING_API_KEY` | `openai` only; leave empty for a local server that does not check one |
| `BCE_MCP_ALLOW_WRITE` | `false` by default; `true` adds the two indexing tools |
| `BCE_MCP_TOOLS` | comma-separated names to advertise (default: all). Agents fetch a tool's schema before first use and chain small calls when many tools are offered, so a narrow catalog such as `get_context_for_task,find_references` costs fewer model turns. Unknown names fail at startup. |

The encoder is built once at startup rather than on the first search. It pulls in numpy, and
loading that lazily inside a tool call can stall for minutes on Windows; paying it during
startup costs a few seconds there instead. A provider that is misconfigured is reported on
stderr and leaves the graph tools working — only the search tools fail, and they say why.

If the status stays disconnected, the editor cannot resolve `bce`. Confirm `where bce` /
`command -v bce` outside the venv, then restart the IDE. Without a global install:

```json
"command": "uvx",
"args": ["--from", "bgts-context-engine[mcp]", "bce", "serve-mcp"]
```

The database must be reachable and at least one repository indexed. The stdio server runs
as the system principal and can see every indexed repository, so run one process per trust
boundary.

## Tools

Eight read-only tools (narrow the catalog with `BCE_MCP_TOOLS`), plus two indexing tools when `BCE_MCP_ALLOW_WRITE=true`. Every one
accepts an optional `locale` and returns the same envelope.

### Retrieval

| Tool | Required | Optional | Returns |
| --- | --- | --- | --- |
| `get_context_for_task` | `task_text` | see below | `anchors`, `context`, `coverage` |
| `hybrid_search` | `query` | `repo_ids`, `limit` (10, max 100) | `candidates`, `model`, `weights_version` |
| `resolve_symbol` | `name` | `repo_id` | `matches`, `indexed_at_commit` |
| `find_references` | `symbol_id` | | `references` with file, line, edge type, `ref_kind`, provenance |
| `get_call_graph` | `symbol_id` | `hops` (1, max 10), `direction` (`both`) | `callers`, `callees` |
| `expand_blast_radius` | `target_symbols` | | the impact surface of changing them |

`hybrid_search` blends lexical and vector hits at a fixed 0.55 / 0.45 with an exact-name
boost, so a symbol whose name matches the query is never beaten by something that merely
reads similarly — the failure mode pure vector search is prone to.

`resolve_symbol` turns a name from a bug report into the `symbol_id` the graph tools take.
Those tools either find the answer or say they did not.

### Indexing

Only advertised when `BCE_MCP_ALLOW_WRITE=true`; calling one otherwise returns an error
naming the variable. A full index outlives any agent's tool-call timeout, so these enqueue a
job and return it immediately.

| Tool | Required | Optional |
| --- | --- | --- |
| `index_repo` | `repo_path`, `name` | `commit` |
| `reindex_repo` | `repo_path`, `name` | `since_commit`, `to_commit` (`HEAD`) |
| `get_index_job` | `job_id` | |
| `list_index_jobs` | | `status`, `repo`, `limit` (50) |

`get_index_job` and `list_index_jobs` are read-only and always available, so an agent can
watch jobs that REST enqueued.

With writes enabled the MCP process runs its own job workers, so queued indexing progresses
without a separate `bce serve`. The queue claims rows with `FOR UPDATE SKIP LOCKED`, so
running both an API server and an MCP server against one database is safe. Those workers
claim `index` and `reindex` jobs only: a clone that REST enqueued waits for `bce serve`
rather than running inside an editor-spawned process. Poll
`get_index_job` until `status` leaves `pending`/`running`; `result` then holds the same
summary the REST endpoints return, or `error` explains the failure.

These cover local working trees only. Cloning a remote repository needs its own credentials
and is a deployment task rather than something an editor-spawned agent should reach for, so
it stays on `POST /v1/index-remote`, `POST /v1/jobs/index-remote` and `bce index-remote`.

### `get_context_for_task` options

This is the tool an agent should reach for first. Full options:

| Field | Default | |
| --- | --- | --- |
| `task_text` | required | the task in prose |
| `task_id` | `null` | for audit correlation and history lookup |
| `max_candidates` | `20` | how many symbols to return (K). 20 is where recall@K flattened in the retrieval benchmarks for both fitted profiles |
| `max_tokens` | `1500` | budget for the assembled context. Kept short because an agent re-sends it on every turn; raise it for longer snippets |
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
  has `symbol_id`, `repo_id`, `detail_level`, `graph_distance`, `score`, `tokens`,
  `content` and, where the graph knows them, `name`, `kind`, `file_id` and `line` (so an
  agent opens the file directly instead of searching for the symbol). A non-zero `skipped` means relevant symbols were dropped for budget; raise
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
| `bce context --task T [--max-tokens 1500] [--max-candidates 20]` | Layer 3 from the shell |
| `bce precontext --task T [--repo-id R]` | the compact context block an agent prompt carries; without `--task` it is a Claude Code `UserPromptSubmit` hook |
| `bce cursor-init [--project DIR] [--repo-id R] [--env-file PATH]` | write `.cursor/mcp.json` + the agent rule into a project |
| `bce claude-init [--project DIR] [--repo-id R] [--env-file PATH] [--no-hook]` | write `.mcp.json`, a `CLAUDE.md` section and the pre-context hook |
| `bce bench --cases F [--out F] [--determinism-runs 3]` | benchmark report as JSON |
| `bce languages` | list supported languages; needs no database |
| `bce serve [--host] [--port] [--reload] [--no-ui] [--env-file PATH]` | REST API and web UI |
| `bce serve-mcp [--env-file PATH]` | MCP over stdio; needs `[mcp]`, PATH-visible `bce`, IDE restart |
| `bce --env-file PATH <command>` | load configuration from an explicit `.env` (any command) |
| `bce --version` | version, for bug reports |

Every command prints the human-readable message followed by the JSON payload, so output
pipes into `jq` while staying readable.
