"""Write the editor-side files that connect a project to the engine.

Cursor reads a project ``.cursor/mcp.json`` and ``.cursor/rules/*.mdc``; Claude Code reads a project
``.mcp.json``, ``CLAUDE.md`` and hooks in ``.claude/settings.json``. Everything here merges into
existing files (other MCP servers, other hooks and the rest of a ``CLAUDE.md`` are kept) and is
idempotent: running the command twice yields the same files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from bce.config import ENV_FILE_VAR

SERVER_NAME = "bgts-context-engine"
RULE_FILE = "bgts-context-engine.mdc"
CLAUDE_BEGIN = "<!-- bgts-context-engine:begin -->"
CLAUDE_END = "<!-- bgts-context-engine:end -->"
HOOK_MARKER = "bce precontext"  # identifies our hook entry when merging settings


@dataclass
class SetupResult:
    written: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# resolution helpers
# ----------------------------------------------------------------------------------------------


def resolve_bce_command(override: str | None = None) -> str:
    """Absolute path of the ``bce`` executable the editor should spawn.

    Editors do not activate a virtualenv, so a bare ``bce`` only works when it is on the user PATH;
    the path of the interpreter's own console script always works.
    """
    if override:
        return override
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name.split(".")[0] == "bce" and argv0.is_file():
        return str(argv0.resolve())
    found = shutil.which("bce")
    if found:
        return str(Path(found).resolve())
    return "bce"


def resolve_env_file(explicit: Path | None, project: Path) -> Path | None:
    """The ``.env`` the MCP server should load: explicit flag, then ``bce --env-file`` /
    ``BCE_ENV_FILE``, then ``<project>/.env``, then ``./.env``."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    if os.environ.get(ENV_FILE_VAR):
        candidates.append(Path(os.environ[ENV_FILE_VAR]))
    candidates.append(project / ".env")
    candidates.append(Path.cwd() / ".env")
    for c in candidates:
        if c.is_file():
            return c.resolve()
    if explicit is not None:
        raise FileNotFoundError(f"env file not found: {explicit}")
    return None


def mcp_server_entry(bce_cmd: str, env_file: Path | None) -> dict:
    args = ["serve-mcp"]
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    return {"command": bce_cmd, "args": args, "env": {"PYTHONUTF8": "1"}}


def _template(name: str) -> str:
    return (
        resources.files("bce.integrations").joinpath("templates", name).read_text(encoding="utf-8")
    )


def _repo_fields(repo_ids: list[str]) -> dict[str, str]:
    return {
        "repo_ids": ", ".join(f"`{r}`" for r in repo_ids),
        "repo_ids_json": json.dumps(repo_ids),
        "repo_plural": "s" if len(repo_ids) > 1 else "",
        "server_name": SERVER_NAME,
    }


def render_cursor_rule(repo_ids: list[str]) -> str:
    return _template("cursor_rule.mdc").format(**_repo_fields(repo_ids))


def render_claude_section(repo_ids: list[str], *, hook: bool) -> str:
    if hook:
        hook_paragraph = (
            '- Every prompt you receive is accompanied by a "Code context for this task" system\n'
            "  reminder: the result of `get_context_for_task` for the prompt text, computed once by\n"
            "  the `bce precontext` hook before you saw it. Start from those files and lines; read\n"
            "  them before searching the tree."
        )
    else:
        hook_paragraph = (
            "- Before searching the tree for a task, call `get_context_for_task` once with the full\n"
            "  task text. Make it your first action, not something you try after a round of grep."
        )
    return _template("claude_section.md").format(
        hook_paragraph=hook_paragraph, **_repo_fields(repo_ids)
    )


# ----------------------------------------------------------------------------------------------
# file merging
# ----------------------------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or remove it and rerun") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _dump_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_mcp_servers(path: Path, entry: dict, *, key: str = "mcpServers") -> None:
    """Set ``<key>.<SERVER_NAME>`` in a JSON file, keeping every other server."""
    data = _load_json(path)
    servers = data.get(key)
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = entry
    data[key] = servers
    _dump_json(path, data)


def upsert_marked_section(path: Path, section: str) -> None:
    """Replace the text between our markers in ``path`` (append if absent), keeping the rest."""
    block = f"{CLAUDE_BEGIN}\n{section.rstrip()}\n{CLAUDE_END}\n"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if CLAUDE_BEGIN in existing and CLAUDE_END in existing:
        start = existing.index(CLAUDE_BEGIN)
        end = existing.index(CLAUDE_END) + len(CLAUDE_END)
        rest_after = existing[end:]
        if rest_after.startswith("\n"):
            rest_after = rest_after[1:]
        new = existing[:start] + block + rest_after
    else:
        sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        new = existing + sep + block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


def merge_claude_hook(settings_path: Path, command: str, *, timeout: int = 30) -> None:
    """Add (or replace) our ``UserPromptSubmit`` hook in a Claude Code settings file."""
    data = _load_json(settings_path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    entries = hooks.get("UserPromptSubmit")
    if not isinstance(entries, list):
        entries = []
    ours = {"type": "command", "command": command, "timeout": timeout}
    replaced = False
    for group in entries:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            continue
        for i, h in enumerate(inner):
            if isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")):
                inner[i] = ours
                replaced = True
    if not replaced:
        entries.append({"hooks": [ours]})
    hooks["UserPromptSubmit"] = entries
    data["hooks"] = hooks
    _dump_json(settings_path, data)


def _shell_quote(s: str) -> str:
    if not s or any(ch in s for ch in " \t\"'\\$`"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# ----------------------------------------------------------------------------------------------
# public entry points
# ----------------------------------------------------------------------------------------------


def setup_cursor(
    project: Path,
    *,
    repo_ids: list[str],
    bce_cmd: str,
    env_file: Path | None,
) -> SetupResult:
    """``.cursor/mcp.json`` (merged) + ``.cursor/rules/bgts-context-engine.mdc``."""
    res = SetupResult()
    cursor_dir = project / ".cursor"
    mcp_path = cursor_dir / "mcp.json"
    merge_mcp_servers(mcp_path, mcp_server_entry(bce_cmd, env_file))
    res.written.append(mcp_path)
    rule_path = cursor_dir / "rules" / RULE_FILE
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    rule_path.write_text(render_cursor_rule(repo_ids), encoding="utf-8")
    res.written.append(rule_path)
    res.notes.append(
        "Restart Cursor (or 'Developer: Reload Window') so it picks up the MCP server."
    )
    res.notes.append(
        "Cursor's beforeSubmitPrompt hook cannot add context to a prompt, so the rule tells the "
        "agent to call get_context_for_task first; the pre-computed context hook exists for Claude "
        "Code only (bce claude-init)."
    )
    return res


def setup_claude(
    project: Path,
    *,
    repo_ids: list[str],
    bce_cmd: str,
    env_file: Path | None,
    hook: bool,
) -> SetupResult:
    """``.mcp.json`` (merged) + a marked section in ``CLAUDE.md`` + optionally the
    ``UserPromptSubmit`` hook in ``.claude/settings.json``."""
    res = SetupResult()
    mcp_path = project / ".mcp.json"
    merge_mcp_servers(mcp_path, mcp_server_entry(bce_cmd, env_file))
    res.written.append(mcp_path)
    claude_md = project / "CLAUDE.md"
    upsert_marked_section(claude_md, render_claude_section(repo_ids, hook=hook))
    res.written.append(claude_md)
    if hook:
        parts = [_shell_quote(bce_cmd)]
        if env_file is not None:
            parts += ["--env-file", _shell_quote(str(env_file))]
        parts.append("precontext")
        for r in repo_ids:
            parts += ["--repo-id", _shell_quote(r)]
        settings = project / ".claude" / "settings.json"
        merge_claude_hook(settings, " ".join(parts))
        res.written.append(settings)
        res.notes.append(
            "The UserPromptSubmit hook runs `bce precontext` before every prompt (~2 s) and adds the "
            "graph's answer as context; it fails open, so a database outage only means no context."
        )
    res.notes.append(
        "Start a new Claude Code session in the project so it loads the MCP server and CLAUDE.md."
    )
    return res
