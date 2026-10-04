"""Central configuration for BCE.

Settings are read from environment variables (prefix ``BCE_``) or an optional ``.env`` file.
Nothing here affects the deterministic payload; it only configures infrastructure (database
connection, default locale, graph name, embedding model identifier). The exception is the
indexing exclusion settings, which decide which files an index is built from.

The ``.env`` path is resolved relative to the process working directory, which is not the project
directory when an editor spawns ``bce serve-mcp``. ``BCE_ENV_FILE`` (or ``bce --env-file``) points at
an explicit file so those processes read the same configuration as the shell.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Environment variable holding an explicit ``.env`` path; set by ``bce --env-file``.
ENV_FILE_VAR = "BCE_ENV_FILE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # ``openrouter_api_key`` has an explicit alias; keep the field name usable in ``Settings(...)``.
        populate_by_name=True,
    )

    # --- PostgreSQL (single DB: AGE + pgvector + SQL + RLS) ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "bce"
    db_user: str = "bce"
    db_password: str = "bce"

    # Apache AGE graph name (single graph in v1).
    graph_name: str = "code_graph"

    # --- Remote source sync (Bitbucket Cloud over HTTPS) ---
    # Clones land under this directory (re-index does a fast ``git fetch`` instead of a full clone).
    repo_cache_dir: str = ".bce_data/repos"
    # Credentials for cloning private repositories. ``bitbucket_token`` is a Bitbucket Cloud access
    # token or app password. With an access token leave the username empty (we use ``x-token-auth``);
    # with an app password set ``bitbucket_username``. Tokens are never persisted to ``.git/config``.
    bitbucket_username: str = ""
    bitbucket_token: str = ""
    # Disable TLS verification for git operations (only for corporate MITM proxies; off by default).
    git_ssl_verify: bool = True

    # --- Indexing file exclusion (see docs/languages.md, "Excluding files") ---
    # Extra gitignore patterns skipped by every index run (comma- or newline-separated), applied
    # after the built-in ``*.min.js`` defaults and the repo's ``.bceignore``. Part of what an index
    # is built from: changing it is a reindex boundary (run a full ``bce index``).
    index_exclude: str = ""
    # Files whose mean line length exceeds this many bytes are treated as minified and skipped,
    # unless a negated pattern re-includes them. 0 disables the heuristic.
    index_minified_line_length: int = 300

    # --- i18n (presentation layer only; never affects payload) ---
    default_locale: str = "en"
    supported_locales: tuple[str, ...] = ("en", "tr")

    # --- CORS ---
    # Browser origins allowed to call the API. The bundled UI is served from the API's own origin
    # and therefore needs no entry here; this list only matters for a separately hosted frontend,
    # such as the Vite dev server. Set to ``["*"]`` to allow any origin (development only).
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

    # --- Embedding (Phase 2; pinned for determinism, P2) ---
    # Provider selects the encoder: "hashing" (default, dependency-free, deterministic fallback),
    # "voyage" (Voyage AI code embeddings) or "openai" (any OpenAI-compatible ``/v1/embeddings``
    # server: vLLM, TEI, Ollama, OpenAI itself - the way to run an open model such as
    # jinaai/jina-code-embeddings-1.5b). Model + dim are pinned; a change is a reindex boundary.
    embedding_provider: str = Field(
        default="hashing",
        description=(
            "Embedding backend: 'hashing' (local fallback), 'voyage' (Voyage AI) or 'openai' "
            "(OpenAI-compatible HTTP endpoint)."
        ),
    )
    embedding_model: str = Field(
        default="voyage-code-3",
        description="Pinned embedding model identifier. Versioned for reproducibility (P2).",
    )
    # Width of the pgvector column. ``bce migrate`` re-types ``embeddings.embedding`` to this width;
    # an encoder returning wider vectors is truncated to it and re-normalised (Matryoshka models).
    embedding_dim: int = 1024
    # Voyage AI API key (used only when embedding_provider == 'voyage').
    voyage_api_key: str = ""
    # --- provider "openai" only ---
    # Base URL of the OpenAI-compatible server; ``/embeddings`` is appended.
    embedding_base_url: str = "http://127.0.0.1:8001/v1"
    # Bearer token; leave empty for a local server that does not check one.
    embedding_api_key: str = ""
    # Instruction prefixes prepended to queries / documents (instruction-tuned models). ``None``
    # picks a built-in default from the model name (jina-code-* -> its nl2code prompts); set a
    # string - even an empty one - to override.
    embedding_query_prefix: str | None = None
    embedding_document_prefix: str | None = None
    # Extra JSON fields merged into every request body. The default asks vLLM to truncate inputs
    # longer than its ``--max-model-len`` instead of rejecting the batch; other servers ignore the
    # field, OpenAI's own API rejects unknown fields, so set it to ``{}`` there.
    embedding_extra_body: dict[str, Any] = Field(
        default_factory=lambda: {"truncate_prompt_tokens": -1}
    )
    # Seconds to wait for one embedding request (a local model on a laptop can need minutes).
    embedding_timeout: float = 600.0
    # Seconds to wait when embedding a single *query* at search time (openai provider). Queries are
    # one short text, so a healthy server answers in well under a second; a longer wait only means
    # the network stalled. Kept short so a tool call returns (possibly without its semantic anchor)
    # before an agent's own MCP timeout (Cursor: 60 s) kills it. Each query gets two attempts.
    embedding_query_timeout: float = 10.0

    # --- Retrieval profile (engine constants that depend on the embedding model) ---
    # "auto" (default) picks the profile fitted for ``embedding_model`` - "voyage" for voyage-*,
    # "jina" for jina-*, voyage's constants for anything else; or name one explicitly. The three
    # optional knobs override single values of the chosen profile (fitting runs; leave unset).
    # See ``bce.core.orchestrator.profile``.
    retrieval_profile: str = "auto"
    retrieval_semantic_guard_ranks: int | None = None
    retrieval_semantic_reserve_share: float | None = None
    retrieval_semantic_rank_scale: float | None = None

    # --- Context selector (post-ranking tiering of the K candidates, see bce.core.selector) ---
    # The decision model that tiers the ranked answer: "off" (default: raw ranking), "jev"
    # (TypeSafe's hosted model, via OpenRouter or TypeSafe's own API), "decider-2b" or "decider-4b"
    # (open-weight models behind a self-hosted ``decider.serve``). The selector never changes
    # *which* candidates the engine ranked - it only tiers them (full / stub / dropped) so an
    # agent reads the right 2-3 files instead of 20.
    selector: str = "off"
    # OpenRouter API key; also read from the un-prefixed ``OPENROUTER_API_KEY``. Jev's key when
    # ``selector_api_key`` is empty.
    openrouter_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("BCE_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    )
    # Bearer token for the selector endpoint: a TypeSafe key when Jev is called on TypeSafe's own
    # API, or the proxy's token in front of a decider server. A plain decider server needs none.
    selector_api_key: str = ""
    # Empty = the chosen selector's default (bce.core.selector.PRESETS): Jev on OpenRouter as
    # ``typesafe/jev-1.13``, a decider server on ``http://127.0.0.1:8000/v1/systemone``.
    selector_model: str = ""
    selector_url: str = ""
    # Seconds for one decision request; on timeout the answer is returned unselected, so this is
    # also the worst-case latency the selector adds. Unset: 3 for Jev (p99 ~1 s), 10 for a decider
    # (p99 ~5 s for decider-4b on one 32 GB GPU).
    selector_timeout: float | None = None
    # File-level policy: p(file is edited) >= full_threshold -> full content; >= stub_threshold ->
    # one reference line; below -> dropped. At most ``max_files`` files are listed; the engine's
    # first file is always kept at full detail.
    selector_full_threshold: float = 0.5
    selector_stub_threshold: float = 0.05
    selector_max_files: int = 12
    # Symbols inside a full file scored below this (0 unrelated .. 3 must change) are dropped.
    selector_symbol_min_score: float = 1.0

    # --- Logging (JSON to stdout; optional rotating file sink) ---
    log_level: str = "INFO"
    log_file_enabled: bool = False
    log_file_path: str = ".bce_data/logs/bce.log"
    log_file_max_bytes: int = 10 * 1024 * 1024
    log_file_backup_count: int = 5

    # --- Background jobs (DB-backed queue; workers run inside the API process) ---
    job_workers: int = 1
    # Seconds a worker sleeps between polls when the queue is empty.
    job_poll_interval: float = 2.0

    # --- MCP (stdio agent surface) ---
    # Indexing tools mutate the graph, so they are opt-in: with this off the MCP server advertises
    # only read-only tools and runs no job workers. Turning it on also starts a worker pool inside
    # the MCP process, so queued indexing runs without a separate ``bce serve``.
    mcp_allow_write: bool = False
    # Which tools the MCP server advertises (comma-separated names; empty = all). Agents fetch a
    # tool's schema before using it and tend to chain small calls when many tools are on offer, so a
    # narrow catalog (e.g. "get_context_for_task,find_references") costs fewer model turns.
    mcp_tools: str = ""

    @property
    def mcp_tool_allowlist(self) -> frozenset[str] | None:
        names = frozenset(n.strip() for n in self.mcp_tools.split(",") if n.strip())
        return names or None

    @property
    def index_exclude_patterns(self) -> tuple[str, ...]:
        from bce.indexing.gitsync.exclude import parse_patterns

        return parse_patterns(self.index_exclude)

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get(ENV_FILE_VAR) or ".env")


def set_env_file(path: str) -> None:
    """Point configuration at an explicit ``.env`` file and drop the cached settings."""
    os.environ[ENV_FILE_VAR] = path
    get_settings.cache_clear()
