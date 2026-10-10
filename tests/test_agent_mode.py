"""Agent mode (``BCE_AGENT_MODE``): the hint / trust workflows on every surface that hands the
``get_context_for_task`` answer to an agent, and the switch in the editor MCP configs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bce.api.mcp import tools as mcp_tools
from bce.api.mcp.tools import TOOL_SPECS, available_tool_specs, dispatch_tool
from bce.core.agent_mode import (
    DEFAULT_AGENT_MODE,
    HINT,
    TRUST,
    normalize_agent_mode,
    workflow,
    workflow_text,
)
from bce.integrations import precontext
from bce.integrations.agents import (
    SERVER_NAME,
    project_agent_mode,
    render_claude_section,
    render_cursor_rule,
    setup_claude,
    setup_codex,
    setup_copilot,
    setup_cursor,
    setup_opencode,
)
from bce.integrations.precontext import claude_hook_response, render_context_markdown

# ----------------------------------------------------------------------------------------------
# the modes
# ----------------------------------------------------------------------------------------------


def test_default_is_the_starting_point_mode():
    assert DEFAULT_AGENT_MODE == HINT
    assert normalize_agent_mode(None) == HINT
    assert normalize_agent_mode("  ") == HINT
    assert normalize_agent_mode(" Trust ") == TRUST


def test_unknown_mode_raises_naming_the_variable():
    with pytest.raises(ValueError, match="BCE_AGENT_MODE='strict'.*hint, trust"):
        normalize_agent_mode("strict")


def test_workflows_differ_where_the_benchmark_arms_did():
    hint = " ".join(workflow(HINT)["steps"])
    trust = " ".join(workflow(TRUST)["steps"])
    # hint fills from the engine's own gap signals and may search when the engine says so
    assert "`coverage.unresolved_identifiers`" in hint and "`payload.candidates`" in hint
    assert "greps" in hint
    # trust takes the files as the answer and forbids searching the tree
    assert "`payload.files`" in trust and "Do not search the repository" in trust
    assert "unresolved_identifiers" not in trust
    assert workflow(TRUST)["label"] == "accept as correct"


def test_prose_workflow_names_the_block_not_json_fields():
    text = workflow_text(HINT, style="prose")
    assert text.startswith("Workflow - BCE_AGENT_MODE=hint (starting point):\n1. ")
    assert "payload." not in text and "coverage." not in text
    assert '"Not covered"' in text and '"Also ranked"' in text


# ----------------------------------------------------------------------------------------------
# MCP surface
# ----------------------------------------------------------------------------------------------


def test_tool_description_carries_the_mode_workflow():
    base = TOOL_SPECS["get_context_for_task"]["description"]
    specs = available_tool_specs(allow_write=False, agent_mode=TRUST)
    desc = specs["get_context_for_task"]["description"]
    assert desc.startswith(base) and desc.endswith(workflow_text(TRUST))
    assert TOOL_SPECS["get_context_for_task"]["description"] == base  # catalog untouched
    assert specs["hybrid_search"] is TOOL_SPECS["hybrid_search"]
    plain = available_tool_specs(allow_write=False)
    assert plain["get_context_for_task"]["description"] == base


def test_answer_carries_payload_workflow(monkeypatch):
    def fake_context(repository, **kwargs):
        return {"tool": "get_context_for_task", "payload": {"anchors": {}, "files": []}}

    monkeypatch.setattr(mcp_tools, "get_context_for_task", fake_context)
    out = dispatch_tool(None, "get_context_for_task", {"task_text": "x"}, agent_mode=HINT)
    assert out["payload"]["workflow"] == workflow(HINT)
    bare = dispatch_tool(None, "get_context_for_task", {"task_text": "x"})
    assert "workflow" not in bare["payload"]


# ----------------------------------------------------------------------------------------------
# editor configs: the switch
# ----------------------------------------------------------------------------------------------


def _mode_in(path: Path, key: str = "mcpServers", env_key: str = "env") -> str:
    return json.loads(path.read_text(encoding="utf-8"))[key][SERVER_NAME][env_key]["BCE_AGENT_MODE"]


def test_init_writes_hint_by_default_and_keeps_a_switched_mode(tmp_path: Path):
    cfg = tmp_path / ".cursor" / "mcp.json"
    res = setup_cursor(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None)
    assert _mode_in(cfg) == HINT
    assert any("Agent mode: hint" in n and "mcp.json" in n for n in res.notes)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["mcpServers"][SERVER_NAME]["env"]["BCE_AGENT_MODE"] = "trust"  # the user's toggle
    cfg.write_text(json.dumps(data), encoding="utf-8")
    setup_cursor(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None)
    assert _mode_in(cfg) == TRUST  # a rerun does not reset it

    setup_cursor(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="hint")
    assert _mode_in(cfg) == HINT  # an explicit --mode does


def test_init_resets_an_invalid_configured_mode(tmp_path: Path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {SERVER_NAME: {"env": {"BCE_AGENT_MODE": "nope"}}}}),
        encoding="utf-8",
    )
    res = setup_claude(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, hook=False)
    assert _mode_in(cfg) == HINT
    assert any("'nope'" in n for n in res.notes)


def test_opencode_init_writes_the_mode_into_environment(tmp_path: Path):
    setup_opencode(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="trust")
    assert _mode_in(tmp_path / "opencode.json", "mcp", "environment") == TRUST


def test_codex_init_writes_the_mode_and_keeps_a_switched_one(tmp_path: Path):
    import tomllib

    cfg = tmp_path / ".codex" / "config.toml"

    def mode() -> str:
        return tomllib.loads(cfg.read_text(encoding="utf-8"))["mcp_servers"][SERVER_NAME]["env"][
            "BCE_AGENT_MODE"
        ]

    res = setup_codex(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None)
    assert mode() == HINT
    assert any("Agent mode: hint" in n and "config.toml" in n for n in res.notes)
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('"hint"', '"trust"'), encoding="utf-8")
    setup_codex(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None)
    assert mode() == TRUST  # a rerun does not reset it
    setup_codex(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="hint")
    assert mode() == HINT  # an explicit --mode does


def test_copilot_init_writes_the_mode_into_mcp_json(tmp_path: Path):
    setup_copilot(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="trust")
    assert _mode_in(tmp_path / ".mcp.json") == TRUST


def test_project_agent_mode_reads_the_editor_configs(tmp_path: Path):
    assert project_agent_mode(tmp_path) is None
    setup_codex(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="hint")
    assert project_agent_mode(tmp_path) == HINT
    setup_opencode(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, mode="trust")
    assert project_agent_mode(tmp_path) == TRUST  # opencode.json before .codex/config.toml
    setup_claude(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, hook=False, mode="hint")
    assert project_agent_mode(tmp_path) == HINT  # .mcp.json first


def test_rules_point_at_the_workflow_instead_of_a_fixed_search_policy():
    rule = render_cursor_rule(["r"])
    assert "`payload.workflow`" in rule and "`BCE_AGENT_MODE` in `.cursor/mcp.json`" in rule
    assert "prefer normal search" not in rule and "Search only for what" not in rule
    hooked = render_claude_section(["r"], hook=True)
    assert '"Workflow" lines of the code context block' in hooked
    assert "`BCE_AGENT_MODE` in `.mcp.json`" in hooked


def test_cli_rejects_an_unknown_mode_before_writing(tmp_path: Path):
    from typer.testing import CliRunner

    from bce.cli import app

    out = CliRunner().invoke(
        app, ["cursor-init", "--project", str(tmp_path), "--bce-command", "bce", "--mode", "x"]
    )
    assert out.exit_code != 0
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_cli_init_mode_flag(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    from bce.cli import app

    monkeypatch.delenv("BCE_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(
        app, ["claude-init", "--project", str(tmp_path), "--bce-command", "bce", "--mode", "trust"]
    )
    assert out.exit_code == 0, out.output
    assert _mode_in(tmp_path / ".mcp.json") == TRUST
    assert "Agent mode: trust" in out.output


# ----------------------------------------------------------------------------------------------
# bce precontext
# ----------------------------------------------------------------------------------------------


def _payload():
    return {
        "context": {
            "items": [
                {
                    "symbol_id": "s1",
                    "repo_id": "demo",
                    "file_id": "demo:src/a.ts",
                    "line": 3,
                    "name": "a",
                    "kind": "function",
                    "graph_distance": 0,
                    "detail_level": "full",
                    "content": "{\n  x\n}",
                }
            ]
        },
        "candidates": [{"file_id": "demo:src/c.ts", "path": "src/c.ts"}],
        "coverage": {
            "confidence": "medium",
            "unresolved_identifiers": [{"name": "Foo", "defined_in": ["demo:src/foo.ts"]}],
            "likely_incomplete": True,
        },
    }


def test_block_shows_the_gap_signals_and_the_mode_workflow():
    text = render_context_markdown(_payload(), mode=HINT)
    assert "Not covered: `Foo` (defined in `src/foo.ts`)" in text
    assert "Also ranked: `src/c.ts`" in text
    assert "likely incomplete" in text
    assert workflow_text(HINT, style="prose") in text
    assert text.index("Workflow -") < text.index("- `src/a.ts:3`")  # survives truncation
    assert "Workflow -" not in render_context_markdown(_payload())


def test_hook_follows_the_mode_in_the_sessions_mcp_json(tmp_path: Path, monkeypatch):
    setup_claude(tmp_path, repo_ids=["r"], bce_cmd="bce", env_file=None, hook=True, mode="trust")
    seen: dict = {}

    def fake(prompt, **kwargs):
        seen.update(kwargs)
        return {"text": "t", "n_items": 1}

    monkeypatch.setattr(precontext, "precontext_for_task", fake)
    prompt = {"prompt": "fix the token refresh in the raw fetch path", "cwd": str(tmp_path)}
    assert claude_hook_response(prompt, repo_ids=["r"]) is not None
    assert seen["mode"] == TRUST
    claude_hook_response(prompt, repo_ids=["r"], mode="hint")
    assert seen["mode"] == HINT
