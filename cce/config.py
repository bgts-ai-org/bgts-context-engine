"""Central configuration for CCE.

Settings are read from environment variables (prefix ``CCE_``) or an optional ``.env`` file.
Nothing here affects the deterministic payload; it only configures infrastructure (database
connection, default locale, graph name, embedding model identifier).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL (single DB: AGE + pgvector + SQL + RLS) ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "cce"
    db_user: str = "cce"
    db_password: str = "cce"

    # Apache AGE graph name (single graph in v1).
    graph_name: str = "code_graph"

    # --- Remote source sync (Bitbucket Cloud over HTTPS) ---
    # Clones land under this directory (re-index does a fast ``git fetch`` instead of a full clone).
    repo_cache_dir: str = ".cce_data/repos"
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

    # --- Embedding (Phase 2; pinned for determinism, P2) ---
    embedding_model: str = Field(
        default="unset",
        description="Pinned embedding model identifier. Versioned for reproducibility (P2).",
    )
    embedding_dim: int = 768

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
