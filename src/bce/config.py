"""Central configuration for BCE.

Settings are read from environment variables (prefix ``BCE_``) or an optional ``.env`` file.
Nothing here affects the deterministic payload; it only configures infrastructure (database
connection, default locale, graph name, embedding model identifier).

The ``.env`` path is resolved relative to the process working directory, which is not the project
directory when an editor spawns ``bce serve-mcp``. ``BCE_ENV_FILE`` (or ``bce --env-file``) points at
an explicit file so those processes read the same configuration as the shell.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Environment variable holding an explicit ``.env`` path; set by ``bce --env-file``.
ENV_FILE_VAR = "BCE_ENV_FILE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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

    # --- Retrieval profile (engine constants that depend on the embedding model) ---
    # "auto" (default) picks the profile fitted for ``embedding_model`` - "voyage" for voyage-*,
    # "jina" for jina-*, voyage's constants for anything else; or name one explicitly. The three
    # optional knobs override single values of the chosen profile (fitting runs; leave unset).
    # See ``bce.core.orchestrator.profile``.
    retrieval_profile: str = "auto"
    retrieval_semantic_guard_ranks: int | None = None
    retrieval_semantic_reserve_share: float | None = None
    retrieval_semantic_rank_scale: float | None = None

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
