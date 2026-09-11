"""Configuration from an explicit ``.env``.

``Settings`` resolves ``.env`` against the process working directory, which is the editor's
directory (not the project's) when one spawns ``bce serve-mcp``. That made the MCP server run on
defaults with no database password and no Voyage key, so the path is settable explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bce.config import ENV_FILE_VAR, get_settings, set_env_file


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings are cached process-wide; a stale entry would leak between these tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "BCE_GRAPH_NAME=from_custom_env\nBCE_MCP_ALLOW_WRITE=true\nBCE_DB_NAME=custom_db\n",
        encoding="utf-8",
    )
    return env_file


def test_env_file_var_overrides_default_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_FILE_VAR, str(_write_env(tmp_path)))

    settings = get_settings()

    assert settings.graph_name == "from_custom_env"
    assert settings.db_name == "custom_db"
    assert settings.mcp_allow_write is True


def test_set_env_file_drops_the_cached_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    before = get_settings()

    set_env_file(str(_write_env(tmp_path)))
    after = get_settings()

    assert after is not before
    assert after.graph_name == "from_custom_env"


def test_real_environment_still_wins_over_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence is unchanged: mcp.json's ``env`` block overrides whatever the file says."""
    monkeypatch.setenv(ENV_FILE_VAR, str(_write_env(tmp_path)))
    monkeypatch.setenv("BCE_GRAPH_NAME", "from_process_env")

    assert get_settings().graph_name == "from_process_env"


def test_mcp_writes_are_off_by_default() -> None:
    from bce.config import Settings

    assert Settings(_env_file=None).mcp_allow_write is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--env-file", "does_not_exist.env", "serve-mcp"],
        # An MCP config spawns `bce serve-mcp`, so the option has to work after the command too.
        ["serve-mcp", "--env-file", "does_not_exist.env"],
        ["--env-file", "does_not_exist.env", "serve"],
        # Same for `bce serve`, which a supervisor or service definition launches the same way.
        ["serve", "--env-file", "does_not_exist.env"],
    ],
)
def test_serve_commands_accept_env_file_in_either_position(argv: list[str]) -> None:
    """A bad path is rejected before the server starts, which also proves the option is known."""
    from typer.testing import CliRunner

    from bce.cli import app

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    assert "env file not found" in result.output
    assert "No such option" not in result.output
