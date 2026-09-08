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
bce serve-mcp                  # MCP over stdio
```

- API documentation: `http://127.0.0.1:8000/docs`
- Web interface: `http://127.0.0.1:8000/ui/`
- Liveness: `GET /healthz`, which returns 503 when the database is unreachable

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
| `BCE_EMBEDDING_PROVIDER` | `hashing` | `hashing` or `voyage` |
| `BCE_EMBEDDING_MODEL` | `voyage-code-3` | pinned model id |
| `BCE_EMBEDDING_DIM` | `1024` | must match the pgvector column |
| `BCE_VOYAGE_API_KEY` | empty | required when provider is `voyage` |

The default `hashing` provider needs no API key and no network. It is deterministic
arithmetic over token digests, so it reproduces exactly — worse at semantic recall than a
real model, but it means the engine works out of the box and benchmarks are repeatable.
Switching providers invalidates existing embeddings; re-index after changing either the
provider or the dimension.

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
