"""UI layer (/v1/ui/*) endpoint tests - no database required.

The graph client is faked at the ``get_repository`` boundary (same pattern as
``test_api_rest.py``): a scripted fake returns AGE-shaped vertices/edges so we can assert the
visualisation contract - ``{nodes, edges}`` shape, gid-based ids, heavy properties stripped from
graph views but present in the detail endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bce.api.rest.app import create_app
from bce.api.rest.deps import get_repository

_SYMBOL_VERTEX = {
    "label": "Symbol",
    "properties": {
        "gid": "py::pkg::mod::foo#1",
        "symbol_id": "py::pkg::mod::foo#1",
        "name": "foo",
        "kind": "function",
        "file_id": "demo:src/mod.py",
        "line": 3,
        "body": "def foo():\n    return 1",
        "docstring": "Adds one.",
    },
}

_FILE_VERTEX = {
    "label": "File",
    "properties": {
        "gid": "demo:src/mod.py",
        "file_id": "demo:src/mod.py",
        "path": "src/mod.py",
        "language": "python",
        "repo_id": "demo",
    },
}

_DEFINED_IN_EDGE = {"label": "DEFINED_IN", "properties": {"provenance": "treesitter"}}


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """SQL side: every cursor answers the repos listing."""

    def __init__(self, repo_rows: list[tuple[Any, ...]]) -> None:
        self._repo_rows = repo_rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._repo_rows)


class _FakeClient:
    """Graph side: routes Cypher by shape (node scan / edge scan / detail / neighbors / counts)."""

    def __init__(self) -> None:
        self.conn = _FakeConn([("demo", "demo", "main", None, "c0ffee", None)])

    def cypher(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> list[tuple[Any, ...]]:
        params = params or {}
        if "count(" in query:
            return [(1,)]
        if "RETURN n.gid, m.gid, r, m" in query:  # outgoing neighbors
            if params.get("gid") == _SYMBOL_VERTEX["properties"]["gid"]:
                return [
                    (
                        _SYMBOL_VERTEX["properties"]["gid"],
                        _FILE_VERTEX["properties"]["gid"],
                        _DEFINED_IN_EDGE,
                        _FILE_VERTEX,
                    )
                ]
            return []
        if "RETURN m.gid, n.gid, r, m" in query:  # incoming neighbors
            return []
        if "MATCH (f:File)" in query and "CONTAINS" in query:  # file path search
            return [
                (
                    _FILE_VERTEX["properties"]["gid"],
                    _FILE_VERTEX["properties"]["path"],
                    "python",
                )
            ]
        if query.startswith("MATCH (n {gid:"):
            if params.get("gid") == _SYMBOL_VERTEX["properties"]["gid"]:
                return [(_SYMBOL_VERTEX,)]
            return []
        if "-[r]->" in query and "RETURN a.gid, b.gid, r" in query:
            return [
                (
                    _SYMBOL_VERTEX["properties"]["gid"],
                    _FILE_VERTEX["properties"]["gid"],
                    _DEFINED_IN_EDGE,
                )
            ]
        if "MATCH (n:Symbol)" in query:
            return [(_SYMBOL_VERTEX,)]
        if "MATCH (n:File)" in query:
            return [(_FILE_VERTEX,)]
        if query.startswith("MATCH (n:"):  # other label scans
            return []
        if "f.language" in query:
            return [("python",)]
        return []


class _FakeRepository:
    def __init__(self) -> None:
        self.client = _FakeClient()

    def lexical_search(self, term: str, *, repo_ids: Any = None, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "symbol_id": _SYMBOL_VERTEX["properties"]["gid"],
                "name": "foo",
                "kind": "function",
                "file_id": "demo:src/mod.py",
                "line": 3,
                "indexed_at_commit": "c0ffee",
            }
        ]


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: _FakeRepository()
    return TestClient(app)


def test_ui_endpoints_are_registered() -> None:
    paths = set(create_app().openapi()["paths"])
    for path in (
        "/v1/ui/repos",
        "/v1/ui/graph",
        "/v1/ui/stats",
        "/v1/ui/node",
        "/v1/ui/neighbors",
        "/v1/ui/search",
    ):
        assert path in paths, f"missing endpoint: {path}"


def test_ui_repos_lists_sql_rows() -> None:
    resp = _client().get("/v1/ui/repos")
    assert resp.status_code == 200
    repos = resp.json()["repos"]
    assert repos and repos[0]["repo_id"] == "demo"
    assert repos[0]["last_indexed_commit"] == "c0ffee"


def test_ui_graph_shape_and_heavy_props_stripped() -> None:
    resp = _client().get("/v1/ui/graph", params={"repo_id": "demo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_id"] == "demo"
    ids = {n["id"] for n in body["nodes"]}
    assert {"py::pkg::mod::foo#1", "demo:src/mod.py"} <= ids

    symbol = next(n for n in body["nodes"] if n["label"] == "Symbol")
    assert symbol["display"] == "foo"
    assert "body" not in symbol["properties"]
    assert "docstring" not in symbol["properties"]

    assert body["edges"] == [
        {
            "id": "py::pkg::mod::foo#1|DEFINED_IN|demo:src/mod.py",
            "source": "py::pkg::mod::foo#1",
            "target": "demo:src/mod.py",
            "type": "DEFINED_IN",
            "properties": {"provenance": "treesitter"},
        }
    ]


def test_ui_graph_edge_type_filter() -> None:
    resp = _client().get("/v1/ui/graph", params={"repo_id": "demo", "edge_types": "CALLS"})
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


def test_ui_graph_node_label_filter() -> None:
    resp = _client().get("/v1/ui/graph", params={"repo_id": "demo", "node_labels": "File"})
    assert resp.status_code == 200
    body = resp.json()
    assert [n["label"] for n in body["nodes"]] == ["File"]
    # The DEFINED_IN edge is dropped because its Symbol endpoint is filtered out.
    assert body["edges"] == []


def test_ui_node_detail_includes_heavy_props() -> None:
    resp = _client().get("/v1/ui/node", params={"gid": "py::pkg::mod::foo#1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "py::pkg::mod::foo#1"
    assert body["properties"]["body"].startswith("def foo")
    assert body["properties"]["docstring"] == "Adds one."


def test_ui_node_detail_404() -> None:
    resp = _client().get("/v1/ui/node", params={"gid": "missing-gid"})
    assert resp.status_code == 404


def test_ui_neighbors_expand() -> None:
    resp = _client().get("/v1/ui/neighbors", params={"gid": "py::pkg::mod::foo#1"})
    assert resp.status_code == 200
    body = resp.json()
    assert [n["id"] for n in body["nodes"]] == ["demo:src/mod.py"]
    assert body["edges"][0]["type"] == "DEFINED_IN"


def test_ui_stats_counts() -> None:
    resp = _client().get("/v1/ui/stats", params={"repo_id": "demo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_counts"]["Symbol"] == 1
    assert body["edge_counts"]["CALLS"] == 1
    assert body["languages"] == {"python": 1}


def test_ui_search_returns_symbols_and_files() -> None:
    resp = _client().get("/v1/ui/search", params={"q": "foo", "repo_id": "demo"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    labels = {r["label"] for r in results}
    assert labels == {"Symbol", "File"}
    assert all(r["id"] for r in results)


# --- /v1/ui/context-trace (pipeline trace for the animated visualisation) ---

_ANCHOR = "py::app::auth::login_handler#a1"
_CALLER = "py::app::api::api_login#b2"
_CALLEE = "py::app::auth::validate_token#c3"

_TRACE_SYMBOLS: dict[str, dict[str, Any]] = {
    _ANCHOR: {
        "symbol_id": _ANCHOR, "name": "login_handler", "kind": "function",
        "signature": "(req)", "file_id": "demo:app/auth.py", "line": 10,
        "indexed_at_commit": "c0ffee",
    },
    _CALLER: {
        "symbol_id": _CALLER, "name": "api_login", "kind": "function",
        "signature": "()", "file_id": "demo:app/api.py", "line": 5,
        "indexed_at_commit": "c0ffee",
    },
    _CALLEE: {
        "symbol_id": _CALLEE, "name": "validate_token", "kind": "function",
        "signature": "(tok)", "file_id": "demo:app/auth.py", "line": 30,
        "indexed_at_commit": "c0ffee",
    },
}


class _TraceFakeRepository:
    """Tiny call graph: api_login -CALLS-> login_handler -CALLS-> validate_token."""

    def resolve_symbol(self, name: str, repo_id: Any = None) -> list[dict[str, Any]]:
        if name == "login_handler":
            return [_TRACE_SYMBOLS[_ANCHOR]]
        return []

    def lexical_search(self, term: str, *, repo_ids: Any = None, limit: int = 20) -> list[dict[str, Any]]:
        if term == "login_handler":
            return [_TRACE_SYMBOLS[_ANCHOR]]
        return []

    def get_callers(self, symbol_id: str) -> list[dict[str, Any]]:
        if symbol_id == _ANCHOR:
            return [{"symbol_id": _CALLER, "provenance": "treesitter"}]
        return []

    def get_callees(self, symbol_id: str) -> list[dict[str, Any]]:
        if symbol_id == _ANCHOR:
            return [{"symbol_id": _CALLEE, "provenance": "treesitter"}]
        return []

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        return _TRACE_SYMBOLS.get(symbol_id)

    def symbols_in_file(self, file_id: str) -> list[dict[str, Any]]:
        return [s for s in _TRACE_SYMBOLS.values() if s["file_id"] == file_id]

    def repo_of_symbol(self, symbol_id: str) -> str | None:
        return "demo"

    def symbol_degree(self, symbol_id: str) -> int:
        return 2 if symbol_id == _ANCHOR else 1

    # Empty surfaces the pipeline touches but this scenario does not exercise.
    def find_routes(self, path: Any) -> list[dict[str, Any]]: return []
    def get_referrers(self, symbol_id: str) -> list[dict[str, Any]]: return []
    def get_supertypes(self, symbol_id: str) -> list[dict[str, Any]]: return []
    def get_subtypes(self, symbol_id: str) -> list[dict[str, Any]]: return []
    def find_implementers(self, symbol_id: str) -> list[dict[str, Any]]: return []
    def get_design_notes(self, symbol_id: str) -> list[dict[str, Any]]: return []


class _TraceFakeVectorStore:
    def search(self, vector: Any, *, limit: int = 20, repo_ids: Any = None, kind: Any = None):
        return [{"ref_id": _CALLEE}]


def _trace_client() -> TestClient:
    from bce.api.rest.deps import get_vector_store

    app = create_app()
    app.dependency_overrides[get_repository] = lambda: _TraceFakeRepository()
    app.dependency_overrides[get_vector_store] = lambda: _TraceFakeVectorStore()
    return TestClient(app)


def test_ui_context_trace_stage_order_and_contents() -> None:
    resp = _trace_client().post(
        "/v1/ui/context-trace",
        json={"task_text": "fix login_handler timeout", "max_candidates": 2},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert [s["stage"] for s in body["stages"]] == [
        "semantic", "anchors", "expand", "score", "narrow", "assemble",
    ]
    by_name = {s["stage"]: s for s in body["stages"]}

    # Semantic candidates come from the (fake) vector store and land as anchors.
    assert by_name["semantic"]["candidates"] == [_CALLEE]
    anchors = by_name["anchors"]["anchors"]
    assert set(anchors[_ANCHOR]) == {"explicit", "lexical"}
    assert anchors[_CALLEE] == ["semantic"]

    # Expansion reaches the caller at distance 1 and keeps anchors at distance 0.
    expand_nodes = {n["symbol_id"]: n for n in by_name["expand"]["nodes"]}
    assert expand_nodes[_ANCHOR]["distance"] == 0 and expand_nodes[_ANCHOR]["is_anchor"]
    assert expand_nodes[_CALLER]["distance"] == 1 and not expand_nodes[_CALLER]["is_anchor"]

    # login_handler appears in the task text -> task signal 1.0; ranked list carries scores.
    score_stage = by_name["score"]
    assert score_stage["task_signals"].get(_ANCHOR) == 1.0
    assert all("score" in c and "features" in c for c in score_stage["ranked"])

    # Narrowing keeps max_candidates entries with 1-based ranks; anchors float to the top.
    selected = by_name["narrow"]["selected"]
    assert len(selected) == 2
    assert [c["rank"] for c in selected] == [1, 2]
    assert selected[0]["symbol_id"] in (_ANCHOR, _CALLEE)

    # Assembly + coverage summary present.
    assert by_name["assemble"]["context"]["included"] == 2
    assert by_name["assemble"]["coverage"]["confidence"] in ("high", "medium", "low")

    # Symbol metadata covers every expansion node (for frontend labels/ghost nodes).
    assert set(body["symbols"]) == {_ANCHOR, _CALLER, _CALLEE}
    assert body["symbols"][_ANCHOR]["name"] == "login_handler"


def test_ui_context_trace_without_semantic_stage() -> None:
    resp = _trace_client().post(
        "/v1/ui/context-trace",
        json={"task_text": "fix login_handler timeout", "auto_semantic": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    by_name = {s["stage"]: s for s in body["stages"]}
    assert by_name["semantic"]["candidates"] == []
    assert _CALLEE not in by_name["anchors"]["anchors"] or (
        "semantic" not in by_name["anchors"]["anchors"].get(_CALLEE, [])
    )


def test_ui_context_trace_registered_in_openapi() -> None:
    paths = set(create_app().openapi()["paths"])
    assert "/v1/ui/context-trace" in paths
