"""Editor integrations: ``bce cursor-init`` / ``bce claude-init`` files and the pre-context block."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bce.api.mcp.tools import TOOL_SPECS
from bce.api.rest.schemas import AssembleContextRequest, ContextForTaskRequest
from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS
from bce.integrations import agents, precontext
from bce.integrations.agents import (
    CLAUDE_BEGIN,
    CLAUDE_END,
    SERVER_NAME,
    merge_claude_hook,
    render_cursor_rule,
    setup_claude,
    setup_cursor,
    upsert_marked_section,
)
from bce.integrations.precontext import HEADING, claude_hook_response, render_context_markdown

# ----------------------------------------------------------------------------------------------
# defaults are shared by every surface
# ----------------------------------------------------------------------------------------------


def test_defaults_are_the_benchmarked_values():
    assert DEFAULT_MAX_CANDIDATES == 20
    assert DEFAULT_MAX_TOKENS == 1500


def test_rest_and_mcp_read_the_shared_defaults():
    req = ContextForTaskRequest(task_text="x")
    assert (req.max_candidates, req.max_tokens) == (DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS)
    assert AssembleContextRequest(symbol_ids=["a"]).max_tokens == DEFAULT_MAX_TOKENS
    props = TOOL_SPECS["get_context_for_task"]["schema"]["properties"]
    assert props["max_candidates"]["default"] == DEFAULT_MAX_CANDIDATES
    assert props["max_tokens"]["default"] == DEFAULT_MAX_TOKENS


def test_cli_context_defaults_follow_the_constants():
    import inspect

    from bce.cli import context

    params = inspect.signature(context).parameters
    assert params["max_candidates"].default.default == DEFAULT_MAX_CANDIDATES
    assert params["max_tokens"].default.default == DEFAULT_MAX_TOKENS


# ----------------------------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------------------------


def _item(name: str, path: str, line: int, distance: int, content: str, level: str = "full"):
    return {
        "symbol_id": f"typescript::{path}::::{name}#abcd",
        "repo_id": "demo",
        "file_id": f"demo:{path}",
        "line": line,
        "name": name,
        "kind": "function",
        "graph_distance": distance,
        "detail_level": level,
        "content": content,
    }


def _payload(items, confidence="high"):
    return {"context": {"items": items}, "coverage": {"confidence": confidence}}


def test_render_lists_files_in_rank_order_and_strips_repo_prefix():
    items = [
        _item("refresh", "src/http.ts", 10, 0, "{\n  return 1;\n}"),
        _item("store", "src/token.ts", 5, 1, "{\n  return 2;\n}"),
        _item("raw", "src/http.ts", 40, 0, "{\n  return 3;\n}"),
    ]
    text = render_context_markdown(_payload(items))
    assert text.startswith(HEADING)
    assert "Confidence: high." in text
    assert "Files: `src/http.ts` (2), `src/token.ts`" in text
    assert "- `src/http.ts:10` function `refresh` d0" in text
    assert "demo:src" not in text  # repo prefix stripped everywhere


def test_render_drops_placeholder_snippets_and_truncates_long_ones():
    long_body = "\n".join(f"line {i}" for i in range(30))
    items = [
        _item("CONST", "src/a.ts", 1, 0, "CONST"),  # name-only content: no code block
        _item("ref", "src/b.ts", 2, 2, "ref @ demo:src/b.ts:2", level="reference"),
        _item("big", "src/c.ts", 3, 0, long_body),
    ]
    text = render_context_markdown(_payload(items), snippet_lines=5)
    assert text.count("```") == 2  # only `big` gets a block
    assert "line 4" in text and "line 5" not in text
    assert "... (+25 lines)" in text


def test_render_respects_max_distance_and_max_chars():
    items = [_item(f"f{i}", f"src/f{i}.ts", i, i % 3, "{\n  x\n}") for i in range(12)]
    near = render_context_markdown(_payload(items), max_distance=0)
    assert "d1" not in near and "d2" not in near and "d0" in near
    short = render_context_markdown(_payload(items), max_chars=900)
    assert len(short) <= 900 + len("- ... (truncated)\n") + 1
    assert short.rstrip().endswith("- ... (truncated)")


# ----------------------------------------------------------------------------------------------
# Claude Code hook
# ----------------------------------------------------------------------------------------------


def test_hook_skips_short_prompts_and_slash_commands():
    assert claude_hook_response({"prompt": "continue"}, repo_ids=None) is None
    assert claude_hook_response({"prompt": "/compact please do it now ok"}, repo_ids=None) is None


def test_hook_fails_open_when_the_engine_is_unreachable(monkeypatch, capsys):
    def boom(*a, **k):
        raise ConnectionError("db down")

    monkeypatch.setattr(precontext, "precontext_for_task", boom)
    out = claude_hook_response(
        {"prompt": "fix the token refresh in the raw fetch path"}, repo_ids=["r"]
    )
    assert out is None
    assert "skipped" in capsys.readouterr().err


def test_hook_returns_claude_additional_context(monkeypatch):
    monkeypatch.setattr(
        precontext,
        "precontext_for_task",
        lambda *a, **k: {
            "text": "## ctx\n- `a.ts:1`",
            "n_items": 1,
            "ms": 1,
            "chars": 10,
            "confidence": "high",
            "payload": {},
        },
    )
    out = claude_hook_response(
        {"prompt": "fix the token refresh in the raw fetch path"}, repo_ids=["r"]
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "## ctx\n- `a.ts:1`",
        }
    }


# ----------------------------------------------------------------------------------------------
# file writers
# ----------------------------------------------------------------------------------------------


def test_cursor_init_merges_mcp_json_and_writes_rule(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    env = tmp_path / "engine.env"
    env.write_text("BCE_DB_NAME=x\n", encoding="utf-8")

    res = setup_cursor(tmp_path, repo_ids=["cortex-web"], bce_cmd="/opt/bce", env_file=env)

    cfg = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["other"] == {"command": "x"}  # kept
    assert cfg["mcpServers"][SERVER_NAME] == {
        "command": "/opt/bce",
        "args": ["serve-mcp", "--env-file", str(env)],
        "env": {"PYTHONUTF8": "1"},
    }
    rule = (tmp_path / ".cursor" / "rules" / agents.RULE_FILE).read_text(encoding="utf-8")
    assert rule.startswith("---\n") and "alwaysApply: true" in rule
    assert '`repo_ids: ["cortex-web"]`' in rule
    assert "get_context_for_task" in rule
    assert {p.name for p in res.written} == {"mcp.json", agents.RULE_FILE}


def test_cursor_rule_pluralises_for_several_repos():
    rule = render_cursor_rule(["a", "b"])
    assert "repository ids `a`, `b`" in rule
    assert '["a", "b"]' in rule


def test_cursor_init_refuses_invalid_existing_json(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        setup_cursor(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None)


def test_claude_init_upserts_section_and_hook_idempotently(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# Project\n\nKeep me.\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}]
                },
            }
        ),
        encoding="utf-8",
    )

    for _ in range(2):  # second run must not duplicate anything
        setup_claude(
            tmp_path, repo_ids=["cortex-web"], bce_cmd="/opt/bce", env_file=None, hook=True
        )

    md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert md.startswith("# Project\n\nKeep me.\n")
    assert md.count(CLAUDE_BEGIN) == 1 and md.count(CLAUDE_END) == 1
    assert "bce precontext" in md  # hook variant of the section

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    groups = settings["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands == ["echo hi", "/opt/bce precontext --repo-id cortex-web"]

    mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"][SERVER_NAME]["args"] == ["serve-mcp"]  # no env file -> no flag


def test_claude_init_without_hook_tells_the_agent_to_call_the_tool(tmp_path: Path):
    setup_claude(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, hook=False)
    md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "call `get_context_for_task` once" in md
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_upsert_replaces_only_the_marked_block(tmp_path: Path):
    p = tmp_path / "CLAUDE.md"
    p.write_text(f"before\n{CLAUDE_BEGIN}\nold\n{CLAUDE_END}\nafter\n", encoding="utf-8")
    upsert_marked_section(p, "new")
    assert p.read_text(encoding="utf-8") == f"before\n{CLAUDE_BEGIN}\nnew\n{CLAUDE_END}\nafter\n"


def test_merge_claude_hook_replaces_an_earlier_bce_entry(tmp_path: Path):
    p = tmp_path / "settings.json"
    merge_claude_hook(p, "/old/bce precontext --repo-id a")
    merge_claude_hook(p, "/new/bce precontext --repo-id b")
    data = json.loads(p.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert cmds == ["/new/bce precontext --repo-id b"]


def test_hook_command_quotes_paths_with_spaces(tmp_path: Path):
    env = tmp_path / "my env" / ".env"
    env.parent.mkdir()
    env.write_text("", encoding="utf-8")
    setup_claude(tmp_path, repo_ids=["r"], bce_cmd="/opt/b ce/bce", env_file=env, hook=True)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmd = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert cmd.startswith('"/opt/b ce/bce" --env-file "') and cmd.endswith(
        " precontext --repo-id r"
    )
