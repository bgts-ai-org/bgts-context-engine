"""UI layer (/v1/ui/*) endpoint tests - no database required.

The graph client is faked at the ``get_repository`` boundary (same pattern as
``test_api_rest.py``): a scripted fake returns AGE-shaped vertices/edges so we can assert the
visualisation contract - ``{nodes, edges}`` shape, gid-based ids, heavy properties stripped from
graph views but present in the detail endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from cce.api.rest.app import create_app
from cce.api.rest.deps import get_repository

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

    def __enter__(self) -> "_FakeCursor":
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
