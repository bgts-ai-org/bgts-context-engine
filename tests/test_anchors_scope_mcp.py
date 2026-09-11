"""Anchor finding (section 6.2), scope filter (section 9), and MCP catalog tests (no database)."""

from __future__ import annotations

from typing import Any

from bce.api.mcp.tools import (
    TOOL_SPECS,
    WRITE_TOOLS,
    available_tool_specs,
    dispatch_tool,
)
from bce.core.auth.scope import Principal, ScopeFilter
from bce.core.orchestrator.anchors import find_anchors


class _FakeRepo:
    """Read-only fake exposing just what anchor finding + lexical search touch."""

    def __init__(self) -> None:
        self.resolved: dict[str, list[dict[str, Any]]] = {
            "getUser": [{"symbol_id": "js::getUser#1"}],
        }
        self.routes: dict[str, list[dict[str, Any]]] = {
            "/users": [{"handler_id": "js::createUser#2"}],
        }

    def resolve_symbol(self, name, repo_id=None):
        return self.resolved.get(name, [])

    def find_routes(self, path):
        return self.routes.get(path, [])

    def symbols_in_file(self, file_id):
        return [{"symbol_id": f"{file_id}::sym"}]

    def lexical_search(self, term, repo_ids=None, limit=5):
        if term.lower() == "getuser":
            return [{"symbol_id": "js::getUser#1"}]
        return []


def test_find_anchors_combines_sources():
    repo = _FakeRepo()
    result = find_anchors(
        repo,
        task_text="Fix getUser on /users",
        history_file_ids=["repo:file.py"],
        semantic_candidates=["js::semantic#9"],
    )
    # explicit (name + route handler), history, lexical, semantic all present.
    assert "js::getUser#1" in result.anchors
    assert "js::createUser#2" in result.anchors
    assert "repo:file.py::sym" in result.anchors
    assert "js::semantic#9" in result.anchors
    assert "explicit" in result.anchors["js::getUser#1"]
    assert result.source_count >= 3


def test_scope_filter_allow_all_for_system():
    sf = ScopeFilter(Principal.system())
    assert sf.allows("any-repo") is True


def test_scope_filter_restricts_and_fails_closed():
    sf = ScopeFilter(Principal(user_id="u1", allowed_repo_ids=["repo-a"]))
    assert sf.allows("repo-a") is True
    assert sf.allows("repo-b") is False
    assert sf.allows(None) is False


def test_mcp_catalog_covers_retrieval_and_indexing():
    expected = {
        "get_context_for_task",
        "hybrid_search",
        "resolve_symbol",
        "find_references",
        "get_call_graph",
        "expand_blast_radius",
        "index_repo",
        "reindex_repo",
        "get_index_job",
        "list_index_jobs",
    }
    assert set(TOOL_SPECS) == expected
    for spec in TOOL_SPECS.values():
        assert spec["schema"]["type"] == "object"
        assert "properties" in spec["schema"]
        for field in spec["schema"]["required"]:
            assert field in spec["schema"]["properties"]


def test_mcp_write_tools_hidden_unless_allowed():
    read_only = available_tool_specs(allow_write=False)
    assert not (WRITE_TOOLS & set(read_only))
    assert "get_context_for_task" in read_only
    # Job inspection is read-only, so it stays available either way.
    assert "get_index_job" in read_only
    assert WRITE_TOOLS <= set(available_tool_specs(allow_write=True))


def test_mcp_dispatch_unknown_tool_raises():
    import pytest

    with pytest.raises(KeyError):
        dispatch_tool(conn=None, name="does_not_exist", arguments={})


def test_mcp_dispatch_write_tool_denied_without_allow_write():
    import pytest

    with pytest.raises(PermissionError):
        dispatch_tool(
            conn=None,
            name="index_repo",
            arguments={"repo_path": ".", "name": "demo"},
        )


def test_mcp_exposes_no_remote_indexing():
    """Cloning is a deployment task with its own credentials; the MCP surface is local-only."""
    for name, spec in TOOL_SPECS.items():
        assert "remote" not in name
        assert "url" not in spec["schema"]["properties"], name


def test_mcp_job_payload_carries_only_local_fields():
    from bce.api.mcp.tools import _job_payload

    payload = _job_payload(
        "index_repo", {"repo_path": "/src/app", "name": "app", "token": "secret"}
    )

    assert payload == {"repo_path": "/src/app", "name": "app", "commit": None}
