"""``inspect_symbol_graph`` (tool, Markdown, MCP toolsets) and the Faz 3 hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bce.api.mcp import tools as mcp_tools
from bce.api.mcp.tools import (
    JOB_TOOLS,
    TOOL_SPECS,
    WRITE_TOOLS,
    available_tool_specs,
    normalize_toolset,
    result_text,
)
from bce.core.render.graph import render_inspect
from bce.integrations import precontext as pre_mod
from bce.integrations.agents import setup_claude
from bce.integrations.edit_hook import EDITS_FILE, edit_hook_response
from bce.integrations.precontext import (
    precontext_url,
    remote_hook_command,
    remote_hook_response,
    server_hook_response,
)
from bce.tools.layer3.inspect import inspect_symbol_graph, parse_change


def _sid(module: str, name: str, namespace: str = "") -> str:
    return f"python::{module}::{namespace}::{name}#0"


SESSION = _sid("app.auth", "Session")
REFRESH = _sid("app.auth", "refresh", "Session")
CACHE_REFRESH = _sid("app.cache", "refresh")
LOGIN = _sid("app.views", "login")
HANDLE = _sid("app.api", "handle")
TEST_REFRESH = _sid("tests.test_auth", "test_refresh")

META: dict[str, dict[str, Any]] = {
    SESSION: {"name": "Session", "kind": "class", "file_id": "r:app/auth.py", "line": 10,
              "end_line": 40, "text_line": 10, "col": 0, "signature": None},
    REFRESH: {"name": "refresh", "kind": "method", "file_id": "r:app/auth.py", "line": 20,
              "end_line": 22, "text_line": 20, "col": 4, "signature": "(self)"},
    CACHE_REFRESH: {"name": "refresh", "kind": "function", "file_id": "r:app/cache.py",
                    "line": 1, "end_line": 4, "text_line": 1, "col": 0, "signature": "()"},
    LOGIN: {"name": "login", "kind": "function", "file_id": "r:app/views.py", "line": 5,
            "end_line": 9, "text_line": 5, "col": 0, "signature": "(user)"},
    HANDLE: {"name": "handle", "kind": "function", "file_id": "r:app/api.py", "line": 1,
             "end_line": 6, "text_line": 1, "col": 0, "signature": "(request)"},
    TEST_REFRESH: {"name": "test_refresh", "kind": "function", "file_id": "r:tests/test_auth.py",
                   "line": 3, "end_line": 9, "text_line": 3, "col": 0, "signature": "()"},
}  # fmt: skip
BODIES = {
    REFRESH: "def refresh(self):\n        self.token = new_token()\n        return self.token",
    LOGIN: (
        "def login(user):\n    s = Session(user)\n    token = s.token\n    s.refresh()\n"
        "    return token"
    ),
    TEST_REFRESH: "def test_refresh():\n    s = Session('u')\n    s.refresh()\n    assert s.token",
}


def _row(sid: str) -> dict[str, Any]:
    m = META[sid]
    return {"symbol_id": sid, "name": m["name"], "kind": m["kind"], "file_id": m["file_id"],
            "line": m["line"]}  # fmt: skip


class _Repo:
    def __init__(self) -> None:
        self.callers = {REFRESH: [LOGIN, TEST_REFRESH], LOGIN: [HANDLE]}
        self.callees = {LOGIN: [SESSION, REFRESH]}

    def resolve_symbols(self, names):
        return {n: [_row(s) for s in META if META[s]["name"] == n] for n in names}

    def symbols_meta(self, ids):
        return {i: {"symbol_id": i, **META[i]} for i in ids if i in META}

    def symbols_in_files(self, fids):
        return {f: [_row(s) for s in META if META[s]["file_id"] == f] for f in fids}

    def files_by_suffix(self, paths, *, repo_ids=None, limit=8):
        files = {m["file_id"] for m in META.values()}
        return {p: sorted(f for f in files if f.endswith(p)) for p in paths}

    def find_references(self, sid):
        call_lines = {(REFRESH, LOGIN): 8}
        return [
            {"caller_id": c, "file_id": META[c]["file_id"], "line": META[c]["line"],
             "call_line": call_lines.get((sid, c)), "edge_type": "CALLS"}
            for c in self.callers.get(sid, [])
        ]  # fmt: skip

    def symbol_bodies(self, ids):
        return {i: BODIES[i] for i in ids if i in BODIES}

    def get_symbol_detail(self, sid):
        return {"symbol_id": sid, **META[sid]} if sid in META else None

    def callers_of(self, ids):
        return {i: [_row(c) for c in self.callers.get(i, [])] for i in ids}

    def callees_of(self, ids):
        return {i: [_row(c) for c in self.callees.get(i, [])] for i in ids}

    def referrers_of(self, ids):
        return {i: [] for i in ids}

    def subtypes_of(self, ids):
        return {i: [] for i in ids}

    def files_by_basename_pattern(self, patterns, *, repo_ids=None, limit=8):
        return {}


def _ask(query: str, mode: str = "definition") -> dict[str, Any]:
    return inspect_symbol_graph(_Repo(), query=query, mode=mode)["payload"]


# ----------------------------------------------------------------------------------------------
# resolution
# ----------------------------------------------------------------------------------------------


def test_a_member_query_names_one_symbol_and_shows_its_numbered_code():
    payload = _ask("Session.refresh")
    assert payload["status"] == "ok"
    assert payload["target"]["symbol_id"] == REFRESH
    text = render_inspect(payload)
    assert text.startswith("# BCE graph · definition of `refresh` (`app/auth.py:20-22`)")
    assert "20→    def refresh(self):" in text and "22→        return self.token" in text


def test_a_shared_name_lists_the_candidates_as_queries():
    payload = _ask("refresh")
    assert payload["status"] == "ambiguous"
    assert {c["symbol_id"] for c in payload["candidates"]} == {REFRESH, CACHE_REFRESH}
    text = render_inspect(payload)
    assert "names 2 symbols" in text
    assert "→ query `app/auth.py:20`" in text and "→ query `app/cache.py:1`" in text


def test_a_position_names_the_innermost_declaration():
    payload = _ask("app/auth.py:21")
    assert payload["target"]["symbol_id"] == REFRESH  # not the enclosing class


def test_an_unknown_name_says_so_and_points_at_search():
    payload = _ask("nonexistent_thing")
    assert payload["status"] == "not_found"
    assert "search for it as usual" in render_inspect(payload)


def test_an_unknown_mode_or_empty_query_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        inspect_symbol_graph(_Repo(), query="x", mode="everything")
    with pytest.raises(ValueError, match="query"):
        inspect_symbol_graph(_Repo(), query="  ", mode="callers")


# ----------------------------------------------------------------------------------------------
# callers / callees
# ----------------------------------------------------------------------------------------------


def test_callers_come_with_the_lines_around_the_call_production_first():
    payload = _ask("Session.refresh", "callers")
    sites = payload["sites"]
    assert [s["symbol_id"] for s in sites] == [LOGIN, TEST_REFRESH]
    assert [n for n, _ in sites[0]["lines"]] == [6, 7, 8, 9]  # call_line 8, two lines each side
    assert [n for n, _ in sites[1]["lines"]] == [3, 4, 5, 6]  # found by name: line 5
    text = render_inspect(payload)
    assert "callers of `refresh` (`app/auth.py:20-22`) · 2" in text
    assert "8→    s.refresh()" in text and "5→    s.refresh()" in text
    assert text.index("app/views.py") < text.index("tests/test_auth.py")


def test_callees_are_listed_with_their_ranges():
    payload = _ask("login", "callees")
    text = render_inspect(payload)
    assert "- `app/auth.py:20-22` method `refresh(self)`" in text
    assert "- `app/auth.py:10-40` class `Session`" in text


# ----------------------------------------------------------------------------------------------
# impact
# ----------------------------------------------------------------------------------------------

DIFF = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -21,2 +21,3 @@ class Session:
-        self.token = new_token()
+        self.token = new_token(ttl)
+        self.at = now()
"""


def test_parse_change_reads_diffs_and_range_lists():
    assert parse_change(DIFF) == [("app/auth.py", 21, 22)]
    added_only = "--- a/x.py\n+++ b/x.py\n@@ -30,0 +31,2 @@\n+a\n+b\n"
    assert parse_change(added_only) == [("x.py", 30, 30)]
    new_file = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,3 @@\n+x\n"
    assert parse_change(new_file) == [("new.py", 1, 1)]
    assert parse_change("app/a.py:L10-L20, pkg/b.go:7") == [
        ("app/a.py", 10, 20),
        ("pkg/b.go", 7, 7),
    ]


def test_impact_of_a_diff_names_the_method_its_dependents_and_tests():
    payload = _ask(DIFF, "impact")
    impact = payload["impact"]
    assert [c["symbol_id"] for c in impact["changed"]] == [REFRESH]  # the method, not its class
    assert [(a["symbol_id"], a["depth"]) for a in impact["affected"]] == [(LOGIN, 1), (HANDLE, 2)]
    assert [t["path"] for t in impact["tests"]] == ["tests/test_auth.py"]
    text = render_inspect(payload)
    assert "## Changed symbols" in text and "## Dependents" in text and "## Tests to run" in text
    assert "`app/api.py:1-6` function `handle(request)` · calls `login` (indirect)" in text


def test_impact_of_lines_no_symbol_covers_says_how_to_ask():
    payload = _ask("app/auth.py:L200-L210", "impact")
    assert payload["status"] == "not_found"
    assert "pre-change line numbers" in render_inspect(payload)


def test_the_text_stays_within_the_budget():
    payload = _ask("Session.refresh", "callers")
    for budget in (120, 200, 400):
        text = render_inspect(payload, max_tokens=budget)
        assert len(text) <= budget * 4


# ----------------------------------------------------------------------------------------------
# MCP surface
# ----------------------------------------------------------------------------------------------


def test_the_minimal_toolset_is_two_short_tools():
    specs = available_tool_specs(allow_write=False, toolset="minimal")
    assert set(specs) == {"get_context_for_task", "inspect_symbol_graph"}
    listed = [{"name": n, "description": s["description"], "inputSchema": s["schema"]}
              for n, s in specs.items()]  # fmt: skip
    assert len(json.dumps(listed)) / 4 <= 350
    with_write = available_tool_specs(allow_write=True, toolset="minimal")
    assert set(with_write) == {"get_context_for_task", "inspect_symbol_graph"} | JOB_TOOLS


def test_the_full_toolset_and_an_allowlist_keep_their_meaning():
    full = available_tool_specs(allow_write=False, toolset="full")
    assert set(full) == set(TOOL_SPECS) - WRITE_TOOLS
    only = available_tool_specs(
        allow_write=False, allowlist=frozenset({"get_context_for_task"}), toolset="minimal"
    )
    assert set(only) == {"get_context_for_task"}
    assert normalize_toolset("") == "minimal"
    with pytest.raises(ValueError, match="BCE_MCP_TOOLSET"):
        normalize_toolset("everything")


def test_an_inspect_answer_is_markdown_unless_debug(monkeypatch):
    monkeypatch.setattr(mcp_tools, "GraphRepository", lambda client: _Repo())
    monkeypatch.setattr(mcp_tools, "GraphClient", lambda conn: None)
    monkeypatch.setattr(mcp_tools, "VectorStore", lambda conn: None)
    monkeypatch.setattr(mcp_tools, "resolve_repo_ids", lambda conn, ids: ids)
    out = mcp_tools.dispatch_tool(
        None, "inspect_symbol_graph", {"query": "Session.refresh", "mode": "callers"}
    )
    assert result_text("inspect_symbol_graph", out).startswith("# BCE graph · callers of")
    assert json.loads(result_text("inspect_symbol_graph", out, view="debug"))["payload"][
        "mode"
    ] == ("callers")


# ----------------------------------------------------------------------------------------------
# hooks
# ----------------------------------------------------------------------------------------------

PROMPT = {"prompt": "Fix the session refresh so the token keeps its ttl", "cwd": "/client/w"}


def test_the_server_hook_never_reads_the_clients_cwd(monkeypatch):
    calls = []

    def fake(prompt, **kw):
        calls.append((prompt, kw))
        return {"text": "ctx", "n_items": 1}

    projects = []

    def resolve(mode, project):
        projects.append(project)
        return mode

    monkeypatch.setattr(pre_mod, "precontext_for_task", fake)
    monkeypatch.setattr(pre_mod, "resolve_agent_mode", resolve)
    out = server_hook_response(PROMPT, repo_ids=["r"], mode="trust")
    assert Path("/client/w") not in projects
    assert out["hookSpecificOutput"]["additionalContext"] == "ctx"
    assert calls[0][1]["mode"] == "trust" and calls[0][1]["repo_ids"] == ["r"]
    assert server_hook_response({"prompt": "yes"}, repo_ids=None, mode="hint") == {}
    with pytest.raises(ValueError):
        server_hook_response(PROMPT, repo_ids=None, mode="sometimes")


def test_the_hook_url_and_curl_command(monkeypatch):
    assert precontext_url("http://h:8765/mcp", repo_ids=["a", "b"]) == (
        "http://h:8765/v1/precontext?repo=a&repo=b"
    )
    command = remote_hook_command("http://h:8765", repo_ids=["a"], token_env="BCE_HOOK_TOKEN")
    assert command.startswith("curl -sf ") and command.endswith("|| true")
    assert '"Authorization: Bearer ${BCE_HOOK_TOKEN}"' in command
    assert "'http://h:8765/v1/precontext?repo=a'" in command


def test_the_remote_hook_fails_open(monkeypatch, capsys):
    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(pre_mod.urllib.request, "urlopen", refuse)
    assert remote_hook_response(PROMPT, server="http://h:1") is None
    assert "skipped" in capsys.readouterr().err
    assert remote_hook_response({"prompt": "ok"}, server="http://h:1") is None  # not a task


def test_the_edit_hook_warns_once_per_file_and_session(tmp_path: Path):
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "app" / "a.py")},
            "cwd": str(tmp_path), "session_id": "s1"}  # fmt: skip
    first = edit_hook_response(edit)
    assert (
        "`app/a.py` now differs from the index" in first["hookSpecificOutput"]["additionalContext"]
    )
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert edit_hook_response(edit) is None
    assert edit_hook_response({**edit, "session_id": "s2"}) is not None
    assert edit_hook_response({**edit, "tool_name": "Read"}) is None
    assert (tmp_path / EDITS_FILE).read_text(encoding="utf-8").splitlines() == [
        "s1\tapp/a.py",
        "s2\tapp/a.py",
    ]


def test_claude_init_with_a_hook_server_writes_curl_hooks(tmp_path: Path):
    setup_claude(
        tmp_path,
        repo_ids=["r"],
        bce_cmd="/opt/bce",
        env_file=None,
        hook=True,
        hook_server="http://bce:8765",
        hook_token_env="BCE_HOOK_TOKEN",
    )
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    prompt = [h["command"] for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert len(prompt) == 1 and prompt[0].startswith("curl -sf ")
    edits = settings["hooks"]["PostToolUse"]
    assert edits[0]["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    assert edits[0]["hooks"][0]["command"] == "/opt/bce edit-hook"
    setup_claude(tmp_path, repo_ids=["r"], bce_cmd="/opt/bce", env_file=None, hook=True)
    again = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    prompt = [h["command"] for g in again["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert prompt == ["/opt/bce precontext --repo-id r"]  # the curl hook was replaced
    assert len(again["hooks"]["PostToolUse"]) == 1
