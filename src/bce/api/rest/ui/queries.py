"""Read-only graph/SQL queries for the UI layer.

Everything the ``/v1/ui/*`` endpoints need lives here so the UI layer stays self-contained:
no method is added to ``GraphRepository`` and nothing outside ``bce.api.rest.ui`` imports this
module. All queries are read-only (never commit).

Repo membership: ``File``/``Module``/``Repo`` nodes carry ``repo_id`` directly, while
``Symbol``/``Route``/``DesignNote`` nodes carry a ``file_id`` of the form ``{repo_id}:{path}``,
so a node belongs to a repo iff ``n.repo_id = $repo OR n.file_id STARTS WITH '{repo}:'``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import psycopg

from bce.domain.enums import EdgeLabel, NodeLabel
from bce.storage.graph.client import GraphClient

#: Properties stripped from graph-view nodes (shipped only by the node-detail endpoint).
_HEAVY_PROPS = frozenset({"body", "docstring"})

#: DesignNote text is truncated to this many characters in graph-view payloads.
_NOTE_TEXT_LIMIT = 120

_MEMBER = "({a}.repo_id = $repo OR {a}.file_id STARTS WITH $prefix)"


def _member(alias: str) -> str:
    return _MEMBER.format(a=alias)


def _display_name(label: str | None, props: dict[str, Any]) -> str:
    """Human-friendly node caption for any frontend (kept server-side for reuse)."""
    if label == NodeLabel.FILE:
        path = str(props.get("path") or "")
        return path.rsplit("/", 1)[-1] or path
    if label == NodeLabel.MODULE:
        return str(props.get("namespace") or props.get("gid") or "")
    if label == NodeLabel.ROUTE:
        return f"{props.get('http_method', '')} {props.get('path_pattern', '')}".strip()
    if label == NodeLabel.DESIGN_NOTE:
        return str(props.get("kind") or "note").upper()
    return str(props.get("name") or props.get("gid") or "")


def _light_node(vertex: dict[str, Any]) -> dict[str, Any]:
    """Viz-friendly node: gid + label + display caption + non-heavy properties."""
    props = dict(vertex.get("properties") or {})
    label = vertex.get("label")
    light = {k: v for k, v in props.items() if k not in _HEAVY_PROPS}
    text = light.get("text")
    if isinstance(text, str) and len(text) > _NOTE_TEXT_LIMIT:
        light["text"] = text[:_NOTE_TEXT_LIMIT] + "..."
    return {
        "id": props.get("gid"),
        "label": label,
        "display": _display_name(label, props),
        "properties": light,
    }


def _edge_dict(src_gid: str, dst_gid: str, edge: dict[str, Any]) -> dict[str, Any]:
    props = dict(edge.get("properties") or {})
    props.pop("gid", None)
    edge_type = edge.get("label")
    return {
        "id": f"{src_gid}|{edge_type}|{dst_gid}",
        "source": src_gid,
        "target": dst_gid,
        "type": edge_type,
        "properties": props,
    }


def list_repos(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Indexed repos from the SQL side table, deterministic order."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo_id, name, default_branch, remote_url, last_indexed_commit, created_at "
            "FROM repos ORDER BY repo_id"
        )
        rows = cur.fetchall()
    return [
        {
            "repo_id": r[0],
            "name": r[1],
            "default_branch": r[2],
            "remote_url": r[3],
            "last_indexed_commit": r[4],
            "created_at": r[5].isoformat() if r[5] is not None else None,
        }
        for r in rows
    ]


def repo_graph(
    client: GraphClient,
    repo_id: str,
    *,
    node_labels: list[str] | None = None,
    edge_types: list[str] | None = None,
    node_limit: int = 5000,
    edge_limit: int = 20000,
) -> dict[str, Any]:
    """Full repo subgraph in ``{nodes, edges}`` shape, bounded by limits.

    Edges are kept only when both endpoints made it into the (possibly truncated) node set, so
    the payload is always renderable as-is. Deterministic ordering by gid (P1).
    """
    params: dict[str, Any] = {"repo": repo_id, "prefix": f"{repo_id}:", "limit": node_limit}

    # Label filters are applied per-label in Cypher (not post-limit) so a filtered request
    # cannot come back short just because unfiltered labels consumed the limit.
    wanted_labels = (
        [lb for lb in NodeLabel if str(lb) in set(node_labels)] if node_labels else list(NodeLabel)
    )
    nodes: list[dict[str, Any]] = []
    truncated_nodes = False
    for label in wanted_labels:
        rows = client.cypher(
            f"MATCH (n:{label}) WHERE {_member('n')} RETURN n ORDER BY n.gid LIMIT $limit",
            params,
            ["n"],
        )
        truncated_nodes = truncated_nodes or len(rows) >= node_limit
        nodes.extend(_light_node(row[0]) for row in rows if isinstance(row[0], dict))
    nodes.sort(key=lambda n: str(n["id"]))
    node_ids = {n["id"] for n in nodes}

    edge_params: dict[str, Any] = {
        "repo": repo_id,
        "prefix": f"{repo_id}:",
        "limit": edge_limit,
    }
    edge_query = (
        f"MATCH (a)-[r]->(b) WHERE {_member('a')} AND {_member('b')} "
        "RETURN a.gid, b.gid, r ORDER BY a.gid, b.gid LIMIT $limit"
    )
    edge_rows = client.cypher(edge_query, edge_params, ["src", "dst", "r"])

    edges: list[dict[str, Any]] = []
    for src, dst, edge in edge_rows:
        if not isinstance(edge, dict) or src not in node_ids or dst not in node_ids:
            continue
        item = _edge_dict(src, dst, edge)
        if edge_types and item["type"] not in edge_types:
            continue
        edges.append(item)

    return {
        "repo_id": repo_id,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated_nodes or len(edge_rows) >= edge_limit,
    }


def node_detail(client: GraphClient, gid: str) -> dict[str, Any] | None:
    """Full properties of a single node (including body/docstring for the detail panel)."""
    rows = client.cypher("MATCH (n {gid: $gid}) RETURN n LIMIT 1", {"gid": gid}, ["n"])
    if not rows or not isinstance(rows[0][0], dict):
        return None
    vertex = rows[0][0]
    props = dict(vertex.get("properties") or {})
    label = vertex.get("label")
    return {
        "id": props.get("gid"),
        "label": label,
        "display": _display_name(label, props),
        "properties": props,
    }


def node_neighbors(
    client: GraphClient,
    gid: str,
    *,
    edge_types: list[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Direct neighbors of a node with their connecting edges (click-to-expand)."""
    params = {"gid": gid, "limit": limit}
    out_rows = client.cypher(
        "MATCH (n {gid: $gid})-[r]->(m) RETURN n.gid, m.gid, r, m ORDER BY m.gid LIMIT $limit",
        params,
        ["src", "dst", "r", "m"],
    )
    in_rows = client.cypher(
        "MATCH (m)-[r]->(n {gid: $gid}) RETURN m.gid, n.gid, r, m ORDER BY m.gid LIMIT $limit",
        params,
        ["src", "dst", "r", "m"],
    )

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    for src, dst, edge, vertex in [*out_rows, *in_rows]:
        if not isinstance(edge, dict) or not isinstance(vertex, dict):
            continue
        item = _edge_dict(src, dst, edge)
        if edge_types and item["type"] not in edge_types:
            continue
        edges[item["id"]] = item
        neighbor = _light_node(vertex)
        if neighbor["id"]:
            nodes[neighbor["id"]] = neighbor

    return {
        "gid": gid,
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(edges.values(), key=lambda e: e["id"]),
    }


def repo_stats(client: GraphClient, repo_id: str) -> dict[str, Any]:
    """Per-label node counts, per-type edge counts and the file language distribution.

    One count query per label/type: AGE rejects mixing a grouping key with an aggregate
    (see ``GraphRepository.symbol_degree``), so a single grouped query is not an option.
    """
    params = {"repo": repo_id, "prefix": f"{repo_id}:"}

    def _count(query: str) -> int:
        rows = client.cypher(query, params, ["c"])
        try:
            return int(rows[0][0]) if rows and rows[0][0] is not None else 0
        except (TypeError, ValueError):
            return 0

    node_counts: dict[str, int] = {}
    for label in NodeLabel:
        node_counts[str(label)] = _count(f"MATCH (n:{label}) WHERE {_member('n')} RETURN count(n)")

    edge_counts: dict[str, int] = {}
    for edge_label in EdgeLabel:
        edge_counts[str(edge_label)] = _count(
            f"MATCH (a)-[r:{edge_label}]->(b) WHERE {_member('a')} AND {_member('b')} "
            "RETURN count(r)"
        )

    lang_rows = client.cypher(
        "MATCH (f:File) WHERE f.repo_id = $repo RETURN f.language", params, ["lang"]
    )
    languages = Counter(str(r[0]) for r in lang_rows if r[0])

    return {
        "repo_id": repo_id,
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "total_nodes": sum(node_counts.values()),
        "total_edges": sum(edge_counts.values()),
        "languages": dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def search_nodes(
    repository: Any,
    term: str,
    *,
    repo_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Symbol + file search for the UI search box; results carry a gid to focus in the graph.

    Symbol matching reuses the engine's lexical search (FTS with CONTAINS fallback) via the
    injected ``GraphRepository``; file matching is a case-insensitive path CONTAINS.
    """
    client: GraphClient = repository.client
    repo_ids = [repo_id] if repo_id else None
    results: list[dict[str, Any]] = []

    for hit in repository.lexical_search(term, repo_ids=repo_ids, limit=limit):
        results.append(
            {
                "id": hit.get("symbol_id"),
                "label": str(NodeLabel.SYMBOL),
                "display": hit.get("name") or hit.get("symbol_id"),
                "kind": hit.get("kind"),
                "file_id": hit.get("file_id"),
                "line": hit.get("line"),
            }
        )

    term_l = (term or "").lower()
    if term_l:
        ret = "RETURN f.gid, f.path, f.language ORDER BY f.path LIMIT $limit"
        if repo_id:
            query = (
                "MATCH (f:File) WHERE f.repo_id = $repo AND toLower(f.path) CONTAINS $term " + ret
            )
            params: dict[str, Any] = {"repo": repo_id, "term": term_l, "limit": limit}
        else:
            query = "MATCH (f:File) WHERE toLower(f.path) CONTAINS $term " + ret
            params = {"term": term_l, "limit": limit}
        for gid, path, language in client.cypher(query, params, ["gid", "path", "lang"]):
            results.append(
                {
                    "id": gid,
                    "label": str(NodeLabel.FILE),
                    "display": str(path or "").rsplit("/", 1)[-1],
                    "path": path,
                    "language": language,
                }
            )

    return results[: limit * 2]
