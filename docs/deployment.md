# Deployment

## Requirements

- **PostgreSQL 16** with Apache AGE and pgvector in the *same* database.
- **Python 3.11+**.
- Optionally `scip-python` / `scip-typescript` on `PATH` for exact cross-file edges, and
  `git` for remote indexing.

Both extensions must live in one database, since a single query joins graph traversal,
vector search and SQL. There is no supported split-database configuration.

## Database

The Compose file builds a PostgreSQL 16 image from `pgvector/pgvector:pg16` with Apache AGE
compiled in, because no public image ships both.

```bash
docker compose -f deploy/docker-compose.yml up -d
```

That gives you `bce-db` on port 5432 with database, role and password all set to `bce`, a
persistent `bce_pgdata` volume, and both extensions created by the init script.

**The credentials are for local development.** Change all three before running anywhere
else, and do not expose 5432.

The build clones Apache AGE from GitHub over HTTPS and the image installs `ca-certificates`
for it, so verification works against the public trust store. Behind a TLS-inspecting proxy
that store is not enough, and the clone fails with `server certificate verification failed`.
Drop the proxy's root CA into `deploy/certs/` as a PEM file with a `.crt` extension, and the
build trusts it:

```bash
cp corporate-root-ca.crt deploy/certs/
docker compose -f deploy/docker-compose.yml up -d --build
```

Only if that CA cannot be obtained, skip verification for the clone alone:

```bash
GIT_SSL_VERIFY=false docker compose -f deploy/docker-compose.yml up -d --build
```

For a managed PostgreSQL, install both extensions and point the engine at it:

```sql
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
```

AGE is not available on most managed offerings, including RDS and Cloud SQL, which in
practice means self-hosting the database or running the provided image.

## Install and migrate

```bash
pip install bgts-context-engine
cp .env.example .env          # then edit
bce migrate
```

`bce migrate` creates the graph, the tables, the vector column, the full-text side table
and the job queue. It is idempotent and tracks what it has applied in `schema_migrations`.
After the SQL files it also fits `embeddings.embedding` to `BCE_EMBEDDING_DIM` (see
[Embeddings](#embeddings) below); if the column already holds vectors of another width the
command refuses unless run with `--reset-embeddings`.

One upgrade caveat: migration `0005` widens the embedding column and **truncates**
`embeddings`. Re-index after crossing it.

## Index a repository

```bash
bce index --repo /path/to/repo --name my-service
```

Subsequent runs only need to process what changed:

```bash
bce reindex --repo /path/to/repo --name my-service
```

With no `--since`, `reindex` diffs against the `last_indexed_commit` recorded for that
repository. This is what belongs in a post-merge hook or a CI step on the default branch;
a stale index shows up in responses as `commit_mismatch`.

Private repositories over HTTPS:

```bash
export BCE_BITBUCKET_TOKEN=...
bce index-remote --url https://bitbucket.org/workspace/repo.git --name repo
```

The token is injected into the remote URL for the duration of a single git invocation and
`origin` is reset afterwards, so it never lands in `.git/config`. It is masked out of error
messages. Clones are cached under `BCE_REPO_CACHE_DIR` and refreshed with `git fetch`.

## Run

```bash
bce serve                      # REST + web UI
bce serve --host 0.0.0.0 --port 8000
bce serve --no-ui              # API only
bce serve-mcp                  # MCP over stdio (agents; the IDE spawns this)
```

- API documentation: `http://127.0.0.1:8000/docs`
- Web interface: `http://127.0.0.1:8000/ui/`
- Liveness: `GET /healthz`, which returns 503 when the database is unreachable

`bce serve` is the HTTP process you start yourself. `bce serve-mcp` is stdio: Cursor and
VS Code must be able to exec `bce` on their PATH (a project venv is not activated), and
you restart the editor after changing MCP config. See [mcp.md](mcp.md#connecting-an-agent).

The UI is served from the wheel and needs no separate deployment. In a source checkout it
is absent until built; see [web/README.md](../web/README.md).

`/healthz` is the right readiness probe. The engine starts fine without a reachable
database and only fails when a request needs one, so process liveness alone tells you
nothing.

## Indexing large repositories

Synchronous `POST /v1/index` blocks for as long as indexing takes, which is unsuitable for
anything large. Use the job queue instead:

```bash
curl -X POST localhost:8000/v1/jobs/index \
  -H 'content-type: application/json' \
  -d '{"repo_path": "/srv/repos/my-service", "name": "my-service"}'
# 202 Accepted, {"payload": {"job": {"job_id": "...", "status": "pending"}}}

curl localhost:8000/v1/jobs/<job_id>
```

Workers run as threads inside the API process, `BCE_JOB_WORKERS` of them, polling every
`BCE_JOB_POLL_INTERVAL` seconds and claiming rows with `FOR UPDATE SKIP LOCKED`. Keeping
the default of 1 makes indexing strictly sequential, which is usually what you want: two
concurrent indexes of the same repository contend on the same subgraphs.

## Configuration

Every setting is an environment variable prefixed `BCE_`, also read from `.env`.

### Database

| Variable | Default | |
| --- | --- | --- |
| `BCE_DB_HOST` | `localhost` | |
| `BCE_DB_PORT` | `5432` | |
| `BCE_DB_NAME` | `bce` | |
| `BCE_DB_USER` | `bce` | |
| `BCE_DB_PASSWORD` | `bce` | change outside local development |
| `BCE_GRAPH_NAME` | `code_graph` | Apache AGE graph name |

### Indexing and git

| Variable | Default | |
| --- | --- | --- |
| `BCE_REPO_CACHE_DIR` | `.bce_data/repos` | where remote clones are cached |
| `BCE_BITBUCKET_USERNAME` | empty | only for app-password auth |
| `BCE_BITBUCKET_TOKEN` | empty | access token or app password |
| `BCE_GIT_SSL_VERIFY` | `true` | disables TLS verification when false; corporate proxies only |

### Embeddings

| Variable | Default | |
| --- | --- | --- |
| `BCE_EMBEDDING_PROVIDER` | `hashing` | `hashing`, `voyage` or `openai` |
| `BCE_EMBEDDING_MODEL` | `voyage-code-3` | pinned model id |
| `BCE_EMBEDDING_DIM` | `1024` | width of the pgvector column; `bce migrate` re-types it |
| `BCE_VOYAGE_API_KEY` | empty | required when provider is `voyage` |
| `BCE_EMBEDDING_BASE_URL` | `http://127.0.0.1:8001/v1` | `openai` only: server base URL |
| `BCE_EMBEDDING_API_KEY` | empty | `openai` only: bearer token, if the server checks one |
| `BCE_EMBEDDING_QUERY_PREFIX` / `_DOCUMENT_PREFIX` | unset | `openai` only: instruction prefixes; unset picks a default from the model name |
| `BCE_EMBEDDING_EXTRA_BODY` | `{"truncate_prompt_tokens": -1}` | `openai` only: JSON merged into every request (vLLM truncation; use `{}` for OpenAI) |
| `BCE_EMBEDDING_TIMEOUT` | `600` | `openai` only: seconds per request |

The default `hashing` provider needs no API key and no network. It is deterministic
arithmetic over token digests, so it reproduces exactly — worse at semantic recall than a
real model, but it means the engine works out of the box and benchmarks are repeatable.
Switching providers invalidates existing embeddings; re-index after changing either the
provider or the dimension. `bce migrate` fits the `embeddings.embedding` column to
`BCE_EMBEDDING_DIM`, but refuses to drop vectors of another width unless run with
`--reset-embeddings`.

`openai` talks to any server that implements the OpenAI `/v1/embeddings` protocol — vLLM,
Text Embeddings Inference, Ollama, or OpenAI itself — and needs nothing beyond the standard
library. It is how an open model runs on-prem; for `jinaai/jina-code-embeddings-1.5b`:

```bash
vllm serve jinaai/jina-code-embeddings-1.5b --runner pooling --port 8001 \
  --served-model-name jina-code-embeddings-1.5b
```

```dotenv
BCE_EMBEDDING_PROVIDER=openai
BCE_EMBEDDING_MODEL=jina-code-embeddings-1.5b
BCE_EMBEDDING_DIM=1536
```

The model id stored with every vector is `<model>-<dim>` (`jina-code-embeddings-1.5b-1536`),
so `BCE_EMBEDDING_MODEL` has to be the name the server serves under. The jina-code family is
instruction-tuned: its `nl2code` prompts are prepended automatically (query at search time,
passage at index time). Other models take theirs from the two prefix variables. A model that
returns wider vectors than `BCE_EMBEDDING_DIM` is truncated and re-normalised (Matryoshka);
a narrower one is a configuration error.

`voyage` needs two things the base install does not give you: the key above and the
`voyageai` package, which ships in the `embed` extra (`pip install
"bgts-context-engine[embed]"`). Selecting it without either one is a configuration error —
the CLI exits with a single message and the API answers `503`. It is deliberately not a
fall back to `hashing`, because nearest-neighbour search does not filter on the stored
`model`: an encoder substituted at query time would be compared against Voyage vectors and
return confident nonsense rather than an error.

#### Retrieval profile

Three engine constants encode an assumption about *where in the model's ranked list the
right answers sit*: the semantic anchor's rank decay, the share of the answer reserved for
the model's ranks, and a guard on its top ranks in narrowing. They are grouped in a
*retrieval profile* (`bce.core.orchestrator.profile`) and selected automatically from
`BCE_EMBEDDING_MODEL`:

| Variable | Default | |
| --- | --- | --- |
| `BCE_RETRIEVAL_PROFILE` | `auto` | `auto` (from the model name), `voyage` or `jina` |
| `BCE_RETRIEVAL_SEMANTIC_GUARD_RANKS` | unset | override: model's first *n* ranks fill slots 1..*n* |
| `BCE_RETRIEVAL_SEMANTIC_RESERVE_SHARE` | unset | override: floor on the model's share of the list |
| `BCE_RETRIEVAL_SEMANTIC_RANK_SCALE` | unset | override: rank at which semantic anchor strength halves |

`voyage` is the historical set every constant was fitted with (no guard, share 0.5, scale
30) and is also what an unknown model or the `hashing` fallback gets. `jina` differs in one
value, a guard of 10: jina-code finds more correct symbols than voyage-code-4 but spreads them
over ranks 1–15, and under the voyage constants the engine ranked those mid hits out of the
answer (PR replay, 30 PRs: recall@20 35.7 % → 38.2 %, holdout 27.8 % → 34.9 %, retention of
the model's own hits 83 % → 92 % with the guard). The overrides exist for fitting runs
(`local_bench/bench40_jina.py rerun <tag> BCE_RETRIEVAL_SEMANTIC_GUARD_RANKS=7`); they do
not change stored data, so a profile change never needs a re-index — only a re-run of the
query side. The active profile is written into every `bce bench-prs` report.

### API

| Variable | Default | |
| --- | --- | --- |
| `BCE_CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` | JSON array of browser origins |
| `BCE_DEFAULT_LOCALE` | `en` | locale for human-readable messages |
| `BCE_SUPPORTED_LOCALES` | `["en","tr"]` | accepted locales |

The bundled UI is same-origin and needs no CORS entry. The default covers the Vite dev
server. Locale affects only the `message` field, never the payload.

### Logging and jobs

| Variable | Default | |
| --- | --- | --- |
| `BCE_LOG_LEVEL` | `INFO` | `DEBUG` also logs request and response bodies |
| `BCE_LOG_FILE_ENABLED` | `false` | rotating file sink in addition to stdout |
| `BCE_LOG_FILE_PATH` | `.bce_data/logs/bce.log` | |
| `BCE_LOG_FILE_MAX_BYTES` | `10485760` | |
| `BCE_LOG_FILE_BACKUP_COUNT` | `5` | |
| `BCE_JOB_WORKERS` | `1` | worker threads inside the API process |
| `BCE_JOB_POLL_INTERVAL` | `2.0` | seconds between polls when idle |

Logs are JSON lines. Bodies are masked on `token`, `password`, `secret`, `api_key`,
`username` and `authorization`, but `DEBUG` still writes request and response payloads —
which for this engine means source code. Keep it at `INFO` in production.

## Putting it behind a proxy

The engine has no authentication. Before exposing it:

- Terminate authentication in front of it and set `X-BCE-User` yourself, overwriting
  anything the client sent. The header is trusted as given.
- Remember that only Layer 3 applies the scope filter. Layers 1 and 2, the indexing and
  job endpoints, and everything under `/v1/ui/*` will serve any indexed repository to
  anyone who can reach the port.
- Restrict the indexing and job endpoints to trusted callers. They clone repositories and
  write to the graph.

[SECURITY.md](../SECURITY.md) has the full list of boundaries.

## Backup

One database holds everything, so one dump captures the whole index:

```bash
docker exec bce-db pg_dump -U bce -d bce -Fc > bce.dump
```

The index is fully derivable from the repositories it was built from, so re-indexing is
always a valid recovery path. Restoring a dump is simply faster.

## Benchmarking

```bash
bce bench --cases cases.json --out report.json
```

Each case names a task and its known-relevant symbols. The harness reports median and p95
latency, recall, precision, precision@1 and MRR, plus `determinism_ok` from running each
case several times and comparing the ordered result, and `rls_ok` from checking that a
scoped principal cannot see forbidden repositories.

Run it before and after any change to scoring, expansion or anchor discovery. It is the
only thing that will tell you whether a retrieval change was actually an improvement.

### The case file

`--cases` takes a JSON array. Only `name`, `task_text` and `relevant_symbol_ids` are
needed; everything else narrows the run or turns on a check.

```json
[
  {
    "name": "session-ttl-change",
    "task_text": "the login timeout fires too early on the meeting webhook",
    "relevant_symbol_ids": [
      "python::auth::session::refresh_session#88c2",
      "python::auth::config::SESSION_TTL#4b0d",
      "python::api::webhooks::handle_meeting_webhook#a3f1"
    ],
    "explicit_symbols": ["refresh_session"],
    "route_paths": ["/webhooks/meeting"],
    "repo_ids": ["my-service"],
    "commit": "9f21ac4",
    "max_candidates": 8,
    "allowed_repo_ids": ["my-service"],
    "forbidden_repo_ids": ["billing-internal"]
  }
]
```

| Field | Effect |
| --- | --- |
| `relevant_symbol_ids` | ground truth; recall, precision, precision@1 and MRR are all measured against it |
| `explicit_symbols`, `route_paths`, `history_file_ids`, `component_repo_ids` | seed the anchor stage the way a real caller would, instead of relying on task text alone |
| `repo_ids` | restrict the search, as a caller working in one service would |
| `commit` | pin the case so ground truth stays valid as the index moves on |
| `allowed_repo_ids`, `forbidden_repo_ids` | enable the `rls_ok` check: the case is re-run as a scoped principal and must not surface a forbidden repository |

Get the `symbol_id` values with `bce resolve-symbol --name refresh_session`, or by clicking
the symbol in the web interface — both print the id in full.

The ground truth has to be written by hand, which is the expensive part and also the point:
a case file is a recorded judgement about what the right answer is, so a scoring change that
looks clever can be checked against it rather than argued about.
