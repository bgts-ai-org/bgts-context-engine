"""Typed graph operations built on the AGE Cypher client.

This is the surface the indexing pipeline and the Layer-1 tools depend on. Labels and id-property
names are interpolated from our own enums (never user input), while values flow through agtype
parameters.
"""

from __future__ import annotations

import re
from typing import Any

from bce.domain.enums import NodeLabel
from bce.domain.models import GraphEdge, GraphFragment, GraphNode
from bce.storage.graph.client import GraphClient

_REFERENCE_EDGE_TYPES = "['CALLS', 'REFERENCES']"

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _sortable(value: Any) -> tuple[int, Any]:
    """Sort key that tolerates NULLs and mixed int/str columns (line numbers may be missing)."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))


def _set_clause(alias: str, props: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build a deterministic ``SET alias.key = $pN`` clause + params for a property map.

    AGE does not accept ``SET n += $mapParam``, so we set each property individually with a scalar
    parameter. Keys come from our schema (safe identifiers); values flow through agtype params.
    """
    parts: list[str] = []
    params: dict[str, Any] = {}
    for i, key in enumerate(sorted(props)):
        pk = f"p{i}"
        parts.append(f"{alias}.{key} = ${pk}")
        params[pk] = props[key]
    return ", ".join(parts), params


class GraphRepository:
    def __init__(self, client: GraphClient) -> None:
        self.client = client
        # Lazily probed: whether the symbol_fts table exists (migration 0006 applied).
        self._fts_ready: bool | None = None
        # Lazily probed: whether the file_churn table exists (migration 0010 applied).
        self._churn_ready: bool | None = None

    # --- write path (incremental upsert) ---

    def upsert_node(self, node: GraphNode) -> None:
        set_sql, params = _set_clause("n", node.merged_properties())
        params["id"] = node.node_id
        query = f"MERGE (n:{node.label} {{{node.id_property}: $id}})"
        if set_sql:
            query += f" SET {set_sql}"
        self.client.execute(query, params)
        if node.label is NodeLabel.SYMBOL:
            self._upsert_symbol_fts(node)

    def _fts_available(self) -> bool:
        if self._fts_ready is None:
            conn = getattr(self.client, "conn", None)
            if conn is None:
                self._fts_ready = False
            else:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT to_regclass('symbol_fts')")
                        row = cur.fetchone()
                    self._fts_ready = bool(row and row[0] is not None)
                except Exception:
                    self._fts_ready = False
        return self._fts_ready

    def _churn_available(self) -> bool:
        if self._churn_ready is None:
            conn = getattr(self.client, "conn", None)
            if conn is None:
                self._churn_ready = False
            else:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT to_regclass('file_churn')")
                        row = cur.fetchone()
                    self._churn_ready = bool(row and row[0] is not None)
                except Exception:
                    self._churn_ready = False
        return self._churn_ready

    def file_churn(self, file_ids: list[str]) -> dict[str, int]:
        """``file_id -> commits touching the file in the churn window`` (missing / unknown omitted).

        Reads the ``file_churn`` side table the indexer fills from ``git log``; returns ``{}`` when
        the migration has not been applied so scoring degrades to a zero churn prior.
        """
        ids = sorted({fid for fid in file_ids if fid})
        if not ids or not self._churn_available():
            return {}
        out: dict[str, int] = {}
        for i in range(0, len(ids), self._FEATURE_CHUNK):
            chunk = ids[i : i + self._FEATURE_CHUNK]
            with self.client.conn.cursor() as cur:
                cur.execute(
                    "SELECT file_id, commits FROM file_churn WHERE file_id = ANY(%s)", (chunk,)
                )
                for file_id, commits in cur.fetchall():
                    out[str(file_id)] = int(commits or 0)
        return out

    def _upsert_symbol_fts(self, node: GraphNode) -> None:
        """Mirror a Symbol node into the FTS side table (same transaction, P5)."""
        if not self._fts_available():
            return
        props = node.properties
        file_id = str(props.get("file_id") or "")
        repo_id = file_id.split(":", 1)[0] if ":" in file_id else ""
        self.client.conn.execute(
            "INSERT INTO symbol_fts (symbol_id, repo_id, name, kind, signature, docstring, "
            "file_id, line, indexed_at_commit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (symbol_id) DO UPDATE SET repo_id = EXCLUDED.repo_id, "
            "name = EXCLUDED.name, kind = EXCLUDED.kind, signature = EXCLUDED.signature, "
            "docstring = EXCLUDED.docstring, file_id = EXCLUDED.file_id, line = EXCLUDED.line, "
            "indexed_at_commit = EXCLUDED.indexed_at_commit",
            (
                node.node_id,
                repo_id,
                str(props.get("name") or ""),
                props.get("kind"),
                props.get("signature"),
                props.get("docstring"),
                file_id or None,
                props.get("line"),
                props.get("indexed_at_commit"),
            ),
        )

    def upsert_edge(self, edge: GraphEdge) -> None:
        set_sql, params = _set_clause("r", edge.merged_properties())
        params["src"] = edge.src_id
        params["dst"] = edge.dst_id
        query = f"MATCH (a {{gid: $src}}), (b {{gid: $dst}}) MERGE (a)-[r:{edge.label}]->(b)"
        if set_sql:
            query += f" SET {set_sql}"
        self.client.execute(query, params)

    def upsert_fragment(self, fragment: GraphFragment) -> None:
        frag = fragment.deduped()
        for node in frag.nodes:
            self.upsert_node(node)
        for edge in frag.edges:
            self.upsert_edge(edge)

    def delete_file_subgraph(self, file_id: str) -> None:
        """Remove a file's symbols and the file node (for incremental re-index, Phase 1)."""
        self.client.execute(
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: $fid}) DETACH DELETE s",
            {"fid": file_id},
        )
        self.client.execute("MATCH (f:File {file_id: $fid}) DETACH DELETE f", {"fid": file_id})
        if self._fts_available():
            self.client.conn.execute("DELETE FROM symbol_fts WHERE file_id = %s", (file_id,))

    # --- read path (Layer-1 primitives) ---

    def resolve_symbol(self, name: str, repo_id: str | None = None) -> list[dict[str, Any]]:
        columns = [
            "symbol_id",
            "kind",
            "signature",
            "docstring",
            "file_id",
            "line",
            "indexed_at_commit",
        ]
        # ORDER BY symbol_id: multi-match order must not depend on storage order (determinism, P1).
        ret = (
            "RETURN s.symbol_id, s.kind, s.signature, s.docstring, s.file_id, s.line, "
            "s.indexed_at_commit ORDER BY s.symbol_id"
        )
        if repo_id:
            query = (
                "MATCH (s:Symbol {name: $name})-[:DEFINED_IN]->(f:File) "
                f"WHERE f.repo_id = $repo {ret}"
            )
            params = {"name": name, "repo": repo_id}
        else:
            query = f"MATCH (s:Symbol {{name: $name}}) {ret}"
            params = {"name": name}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def find_references(self, symbol_id: str) -> list[dict[str, Any]]:
        """Incoming CALLS/REFERENCES from symbols plus ROUTES_TO from routes, merged deterministically.

        ``line`` is the caller's definition line; ``call_line`` is the call/reference site line
        (an edge property written by the linker/extractor) when known.
        """
        columns = [
            "caller_id",
            "file_id",
            "line",
            "call_line",
            "edge_type",
            "ref_kind",
            "provenance",
        ]
        query = (
            "MATCH (caller:Symbol)-[r]->(t:Symbol {symbol_id: $sym}) "
            f"WHERE type(r) IN {_REFERENCE_EDGE_TYPES} "
            "RETURN caller.symbol_id, caller.file_id, caller.line, r.line, type(r), r.ref_kind, "
            "r.provenance ORDER BY caller.symbol_id, caller.line"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        references = [dict(zip(columns, row, strict=False)) for row in rows]

        # Route -> Symbol handlers are references too, but Route is not a Symbol: separate MATCH.
        route_query = (
            "MATCH (rt:Route)-[r:ROUTES_TO]->(t:Symbol {symbol_id: $sym}) "
            "RETURN rt.route_id, rt.file_id, rt.line, rt.line, type(r), r.ref_kind, r.provenance "
            "ORDER BY rt.route_id"
        )
        route_rows = self.client.cypher(route_query, {"sym": symbol_id}, columns)
        references.extend(dict(zip(columns, row, strict=False)) for row in route_rows)

        references.sort(
            key=lambda ref: (
                ref.get("caller_id") or "",
                ref.get("line") or 0,
                ref.get("edge_type") or "",
            )
        )
        return references

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        """Return a single symbol's core properties (or None if it does not exist)."""
        columns = ["symbol_id", "name", "kind", "signature", "file_id", "line", "indexed_at_commit"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym}) "
            "RETURN s.symbol_id, s.name, s.kind, s.signature, s.file_id, s.line, "
            "s.indexed_at_commit LIMIT 1"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        if not rows:
            return None
        return dict(zip(columns, rows[0], strict=False))

    def get_symbol_detail(self, symbol_id: str) -> dict[str, Any] | None:
        """Core properties plus the heavy ``docstring`` / ``body`` snippet (context assembly).

        Kept separate from :meth:`get_symbol` so hot paths (expansion, scoring) never ship bodies.
        """
        columns = [
            "symbol_id",
            "name",
            "kind",
            "signature",
            "docstring",
            "body",
            "file_id",
            "line",
            "indexed_at_commit",
        ]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym}) "
            "RETURN s.symbol_id, s.name, s.kind, s.signature, s.docstring, s.body, s.file_id, "
            "s.line, s.indexed_at_commit LIMIT 1"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        if not rows:
            return None
        return dict(zip(columns, rows[0], strict=False))

    #: Symbol-id chunk size for the bulk feature queries below.
    _FEATURE_CHUNK = 500

    def symbol_features(self, symbol_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Scoring features for many symbols in a handful of chunked queries.

        Returns ``symbol_id -> {name, kind, file_id, line, degree, has_callers, has_callees,
        churn}`` -
        the exact values :meth:`get_symbol` + :meth:`symbol_degree` + :meth:`get_callers` /
        :meth:`get_callees` would produce, but in 5 round-trips per 500 symbols instead of 4 per
        symbol. Symbols not found are omitted.

        The edge queries aggregate server-side (``count``, ``DISTINCT``). Returning one row per
        edge instead made this the slowest stage of retrieval on a hub-heavy graph: a pool of a
        few thousand symbols shipped hundreds of thousands of rows just to be counted here.

        Degree and the caller/callee flags are read straight from the edge label tables in SQL
        (``<graph>._ag_label_edge`` is the parent of every edge table; ``start_id`` / ``end_id``
        are btree-indexed). The Cypher form ``MATCH (s)-[r]->()`` expands the untyped pattern
        over every edge x vertex label pair and re-plans it per UNWIND row: ~1 s of fixed cost
        even for an empty id list and 15-30 s for a 400-symbol pool, i.e. the whole "candidates"
        stage. The SQL form measured 20-40 ms for the same pool with identical counts. The Cypher
        path is kept for clients without a raw connection (test fakes).
        """
        ids = sorted(set(symbol_ids))
        out: dict[str, dict[str, Any]] = {}
        gids: dict[str, str] = {}
        for i in range(0, len(ids), self._FEATURE_CHUNK):
            chunk = ids[i : i + self._FEATURE_CHUNK]
            params = {"ids": chunk}
            meta_cols = ["gid", "symbol_id", "name", "kind", "file_id", "line"]
            for gid, sid, name, kind, file_id, line in self.client.cypher(
                "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid}) "
                "RETURN id(s), s.symbol_id, s.name, s.kind, s.file_id, s.line",
                params,
                meta_cols,
            ):
                out[sid] = {
                    "name": name,
                    "kind": kind,
                    "file_id": file_id,
                    "line": line,
                    "degree": 0,
                    "has_callers": False,
                    "has_callees": False,
                }
                if gid is not None:
                    gids[str(gid)] = sid
            if getattr(self.client, "conn", None) is not None and gids:
                continue
            self._edge_features_cypher(params, out)
        if getattr(self.client, "conn", None) is not None and gids:
            self._edge_features_sql(gids, out)
        churn = self.file_churn([str(f["file_id"]) for f in out.values() if f.get("file_id")])
        for feat in out.values():
            feat["churn"] = churn.get(str(feat.get("file_id") or ""), 0)
        return out

    def _edge_features_sql(self, gids: dict[str, str], out: dict[str, dict[str, Any]]) -> None:
        """Degree + CALLS flags from the edge tables for ``graphid(text) -> symbol_id``."""
        graph = self.client.graph_name
        edge_table = f'"{graph}"."_ag_label_edge"'
        calls_table = f'"{graph}"."CALLS"'
        ids_expr = "ANY(ARRAY(SELECT x::graphid FROM unnest(%s::text[]) AS x))"
        keys = sorted(gids)
        with self.client.conn.cursor() as cur:
            for i in range(0, len(keys), self._FEATURE_CHUNK):
                chunk = keys[i : i + self._FEATURE_CHUNK]
                for col in ("start_id", "end_id"):
                    cur.execute(
                        f"SELECT {col}::text, count(*) FROM {edge_table} "  # noqa: S608
                        f"WHERE {col} = {ids_expr} GROUP BY {col}",
                        (chunk,),
                    )
                    for gid, degree in cur.fetchall():
                        sid = gids.get(str(gid))
                        if sid in out:
                            out[sid]["degree"] += int(degree or 0)
                for flag, col in (("has_callees", "start_id"), ("has_callers", "end_id")):
                    cur.execute(
                        f"SELECT DISTINCT {col}::text FROM {calls_table} "  # noqa: S608
                        f"WHERE {col} = {ids_expr}",
                        (chunk,),
                    )
                    for (gid,) in cur.fetchall():
                        sid = gids.get(str(gid))
                        if sid in out:
                            out[sid][flag] = True

    def _edge_features_cypher(self, params: dict[str, Any], out: dict[str, dict[str, Any]]) -> None:
        """Cypher fallback for :meth:`symbol_features` edge features (one id chunk)."""
        for query in (
            "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})-[r]->() "
            "RETURN s.symbol_id, count(r)",
            "UNWIND $ids AS sid MATCH ()-[r]->(s:Symbol {symbol_id: sid}) "
            "RETURN s.symbol_id, count(r)",
        ):
            for sid, degree in self.client.cypher(query, params, ["sid", "degree"]):
                if sid in out:
                    out[sid]["degree"] += int(degree or 0)
        for flag, query in (
            (
                "has_callees",
                "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})-[:CALLS]->(:Symbol) "
                "RETURN DISTINCT s.symbol_id",
            ),
            (
                "has_callers",
                "UNWIND $ids AS sid MATCH (:Symbol)-[:CALLS]->(s:Symbol {symbol_id: sid}) "
                "RETURN DISTINCT s.symbol_id",
            ),
        ):
            for (sid,) in self.client.cypher(query, params, ["sid"]):
                if sid in out:
                    out[sid][flag] = True

    def _chunks(self, values: list[str]) -> list[list[str]]:
        ids = sorted(set(values))
        return [ids[i : i + self._FEATURE_CHUNK] for i in range(0, len(ids), self._FEATURE_CHUNK)]

    def _grouped(
        self,
        query: str,
        values: list[str],
        param: str,
        columns: list[str],
        *,
        sort_key: tuple[str, ...] = ("symbol_id",),
    ) -> dict[str, list[dict[str, Any]]]:
        """Run a chunked ``UNWIND``-ed query and group its rows by their first column.

        ``columns[0]`` is the grouping key (the queried symbol/file) and is dropped from the rows,
        so each group is shaped exactly like the single-id method it replaces. Groups are sorted
        here rather than in Cypher: the sort is the determinism guarantee (P1), and doing it in
        Python keeps it independent of how AGE plans the query.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for chunk in self._chunks(values):
            for row in self.client.cypher(query, {param: chunk}, columns):
                out.setdefault(row[0], []).append(dict(zip(columns[1:], row[1:], strict=False)))
        for rows in out.values():
            rows.sort(key=lambda r: tuple(_sortable(r.get(k)) for k in sort_key))
        return out

    def symbols_meta(self, symbol_ids: list[str]) -> dict[str, dict[str, Any]]:
        """``symbol_id -> {name, kind, file_id, line}`` for many symbols (missing ones omitted)."""
        columns = ["symbol_id", "name", "kind", "file_id", "line"]
        out: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks(symbol_ids):
            for row in self.client.cypher(
                "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid}) "
                "RETURN s.symbol_id, s.name, s.kind, s.file_id, s.line",
                {"ids": chunk},
                columns,
            ):
                out[row[0]] = dict(zip(columns, row, strict=False))
        return out

    def repos_of_symbols(self, symbol_ids: list[str]) -> dict[str, str | None]:
        """``symbol_id -> repo_id`` for many symbols (the scope filter reads it per candidate)."""
        out: dict[str, str | None] = {}
        for chunk in self._chunks(symbol_ids):
            for sid, repo_id in self.client.cypher(
                "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})-[:DEFINED_IN]->(f:File) "
                "RETURN s.symbol_id, f.repo_id",
                {"ids": chunk},
                ["symbol_id", "repo_id"],
            ):
                out.setdefault(sid, repo_id)
        return out

    def resolve_symbols(self, names: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``name -> matching symbols`` for many names at once (see :meth:`resolve_symbol`)."""
        columns = ["name", "symbol_id", "kind", "file_id", "line", "indexed_at_commit"]
        return self._grouped(
            "UNWIND $names AS nm MATCH (s:Symbol {name: nm}) "
            "RETURN s.name, s.symbol_id, s.kind, s.file_id, s.line, s.indexed_at_commit",
            names,
            "names",
            columns,
        )

    def symbols_in_files(self, file_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``file_id -> symbols defined in it`` (see :meth:`symbols_in_file`)."""
        return self._grouped(
            "UNWIND $fids AS fid MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: fid}) "
            "RETURN f.file_id, s.symbol_id, s.name, s.kind, s.line",
            file_ids,
            "fids",
            ["file_id", "symbol_id", "name", "kind", "line"],
            sort_key=("line", "symbol_id"),
        )

    def callers_of(self, symbol_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``symbol_id -> callers`` (see :meth:`get_callers`)."""
        return self._grouped(
            "UNWIND $ids AS sid MATCH (caller:Symbol)-[r:CALLS]->(t:Symbol {symbol_id: sid}) "
            "RETURN t.symbol_id, caller.symbol_id, caller.name, caller.file_id, caller.line, "
            "r.provenance",
            symbol_ids,
            "ids",
            ["target_id", "symbol_id", "name", "file_id", "line", "provenance"],
        )

    def callees_of(self, symbol_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``symbol_id -> callees`` (see :meth:`get_callees`)."""
        return self._grouped(
            "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})-[r:CALLS]->(callee:Symbol) "
            "RETURN s.symbol_id, callee.symbol_id, callee.name, callee.file_id, callee.line, "
            "r.provenance",
            symbol_ids,
            "ids",
            ["source_id", "symbol_id", "name", "file_id", "line", "provenance"],
        )

    def referrers_of(self, symbol_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``symbol_id -> REFERENCES sources`` (see :meth:`get_referrers`)."""
        return self._grouped(
            "UNWIND $ids AS sid MATCH (ref:Symbol)-[r:REFERENCES]->(t:Symbol {symbol_id: sid}) "
            "RETURN t.symbol_id, ref.symbol_id, ref.name, ref.file_id, ref.line, r.ref_kind, "
            "r.provenance",
            symbol_ids,
            "ids",
            ["target_id", "symbol_id", "name", "file_id", "line", "ref_kind", "provenance"],
        )

    def supertypes_of(self, symbol_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``symbol_id -> supertypes`` (see :meth:`get_supertypes`)."""
        return self._grouped(
            "UNWIND $ids AS sid MATCH (s:Symbol {symbol_id: sid})-[r]->(super:Symbol) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN s.symbol_id, super.symbol_id, super.name, super.kind, super.file_id, "
            "super.line, type(r), r.provenance",
            symbol_ids,
            "ids",
            [
                "source_id",
                "symbol_id",
                "name",
                "kind",
                "file_id",
                "line",
                "edge_type",
                "provenance",
            ],
        )

    def subtypes_of(self, symbol_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """``symbol_id -> subtypes`` (see :meth:`get_subtypes`)."""
        return self._grouped(
            "UNWIND $ids AS sid MATCH (sub:Symbol)-[r]->(s:Symbol {symbol_id: sid}) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN s.symbol_id, sub.symbol_id, sub.name, sub.kind, sub.file_id, sub.line, "
            "type(r), r.provenance",
            symbol_ids,
            "ids",
            [
                "target_id",
                "symbol_id",
                "name",
                "kind",
                "file_id",
                "line",
                "edge_type",
                "provenance",
            ],
        )

    def find_implementers(self, symbol_id: str) -> list[dict[str, Any]]:
        """Types that INHERITS/IMPLEMENTS the given type symbol (may be cross-repo)."""
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (impl:Symbol)-[r]->(t:Symbol {symbol_id: $sym}) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN impl.symbol_id, impl.name, impl.kind, impl.file_id, impl.line, "
            "type(r), r.provenance "
            "ORDER BY impl.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_callers(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "file_id", "line", "provenance"]
        query = (
            "MATCH (caller:Symbol)-[r:CALLS]->(t:Symbol {symbol_id: $sym}) "
            "RETURN caller.symbol_id, caller.name, caller.file_id, caller.line, r.provenance "
            "ORDER BY caller.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_callees(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "file_id", "line", "provenance"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[r:CALLS]->(callee:Symbol) "
            "RETURN callee.symbol_id, callee.name, callee.file_id, callee.line, r.provenance "
            "ORDER BY callee.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_referrers(self, symbol_id: str) -> list[dict[str, Any]]:
        """Symbols with a REFERENCES edge into the target, carrying ref_kind (scoring §6.4).

        Distinct from ``get_callers`` (CALLS): this surfaces define/write/read/pass usages so
        expansion can tag candidates with the strongest reference kind that reached them.
        """
        columns = ["symbol_id", "name", "file_id", "line", "ref_kind", "provenance"]
        query = (
            "MATCH (ref:Symbol)-[r:REFERENCES]->(t:Symbol {symbol_id: $sym}) "
            "RETURN ref.symbol_id, ref.name, ref.file_id, ref.line, r.ref_kind, r.provenance "
            "ORDER BY ref.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_supertypes(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[r]->(super:Symbol) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN super.symbol_id, super.name, super.kind, super.file_id, super.line, "
            "type(r), r.provenance "
            "ORDER BY super.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_subtypes(self, symbol_id: str) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "kind", "file_id", "line", "edge_type", "provenance"]
        query = (
            "MATCH (sub:Symbol)-[r]->(s:Symbol {symbol_id: $sym}) "
            "WHERE type(r) IN ['INHERITS', 'IMPLEMENTS'] "
            "RETURN sub.symbol_id, sub.name, sub.kind, sub.file_id, sub.line, "
            "type(r), r.provenance "
            "ORDER BY sub.symbol_id"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_file_imports(self, file_id: str) -> list[dict[str, Any]]:
        """Direct IMPORTS edges out of a file *or* module (matched by gid).

        Matching by ``gid`` lets a transitive dependency walk follow the
        ``File -> Module -> File`` chain: the linker adds a ``Module -[IMPORTS]-> File`` edge
        when a module namespace maps onto a repo file (A5).
        """
        columns = ["target_id", "namespace", "provenance"]
        query = (
            "MATCH (f {gid: $fid})-[r:IMPORTS]->(m) "
            "RETURN m.gid, m.namespace, r.provenance "
            "ORDER BY m.gid"
        )
        rows = self.client.cypher(query, {"fid": file_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def file_importers(self, file_id: str) -> list[dict[str, Any]]:
        """Files that import the given file, via the ``File -> Module -> File`` chain (A5).

        Used by blast-radius (D2): a change to a file potentially impacts every file that imports
        its module. Deterministic order by importer file_id; de-duplicated (a file may reach the
        module through several bindings).
        """
        columns = ["file_id", "repo_id"]
        query = (
            "MATCH (src:File)-[:IMPORTS]->(m:Module)-[:IMPORTS]->(f:File {file_id: $fid}) "
            "RETURN src.file_id, src.repo_id ORDER BY src.file_id"
        )
        rows = self.client.cypher(query, {"fid": file_id}, columns)
        importers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            fid = row[0]
            if not fid or fid in seen:
                continue
            seen.add(fid)
            importers.append(dict(zip(columns, row, strict=False)))
        return importers

    def find_routes(self, path_substring: str | None = None) -> list[dict[str, Any]]:
        """Route nodes and their handler symbol (anchor source #1: explicit route reference)."""
        columns = ["route_id", "http_method", "path_pattern", "framework", "handler_id", "line"]
        if path_substring:
            query = (
                "MATCH (rt:Route) WHERE rt.path_pattern CONTAINS $q "
                "OPTIONAL MATCH (rt)-[:ROUTES_TO]->(s:Symbol) "
                "RETURN rt.route_id, rt.http_method, rt.path_pattern, rt.framework, s.symbol_id, "
                "rt.line ORDER BY rt.route_id"
            )
            params = {"q": path_substring}
        else:
            query = (
                "MATCH (rt:Route) OPTIONAL MATCH (rt)-[:ROUTES_TO]->(s:Symbol) "
                "RETURN rt.route_id, rt.http_method, rt.path_pattern, rt.framework, s.symbol_id, "
                "rt.line ORDER BY rt.route_id"
            )
            params = {}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def get_design_notes(self, symbol_id: str) -> list[dict[str, Any]]:
        """DesignNotes that EXPLAINS a symbol (feature 5: the "why")."""
        columns = ["note_id", "kind", "text", "file_id", "line"]
        query = (
            "MATCH (n:DesignNote)-[:EXPLAINS]->(s:Symbol {symbol_id: $sym}) "
            "RETURN n.note_id, n.kind, n.text, n.file_id, n.line ORDER BY n.line"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def symbols_in_file(self, file_id: str) -> list[dict[str, Any]]:
        """All symbols defined in a file (used to expand file anchors to symbols)."""
        columns = ["symbol_id", "name", "kind", "line"]
        query = (
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {file_id: $fid}) "
            "RETURN s.symbol_id, s.name, s.kind, s.line ORDER BY s.line, s.symbol_id"
        )
        rows = self.client.cypher(query, {"fid": file_id}, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def symbol_degree(self, symbol_id: str) -> int:
        """Total in+out edge degree for a symbol (structural centrality proxy, spec section 6.4).

        Computed as two independent counts (out-degree + in-degree) rather than a single query:
        Apache AGE rejects ``RETURN <grouping_key> + count(...)`` (mixing a key with an aggregate).
        """
        out_rows = self.client.cypher(
            "MATCH (s:Symbol {symbol_id: $sym})-[out]->() RETURN count(out)",
            {"sym": symbol_id},
            ["deg"],
        )
        in_rows = self.client.cypher(
            "MATCH ()-[inc]->(s:Symbol {symbol_id: $sym}) RETURN count(inc)",
            {"sym": symbol_id},
            ["deg"],
        )

        def _as_int(rows: list[Any]) -> int:
            if not rows or rows[0][0] is None:
                return 0
            try:
                return int(rows[0][0])
            except (TypeError, ValueError):
                return 0

        return _as_int(out_rows) + _as_int(in_rows)

    def lexical_search(
        self, term: str, *, repo_ids: list[str] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Deterministic keyword match on symbol name/signature/docstring.

        Prefers the Postgres FTS side table (``ts_rank`` DESC, then symbol_id - reproducible) when
        migration 0006 is applied; otherwise falls back to the AGE Cypher CONTAINS path (name only,
        ordered by name then symbol_id). Feeds hybrid search + anchor source #4 without any model.
        """
        if self._fts_available():
            rows = self._lexical_search_fts(term, repo_ids, limit)
            if rows is not None:
                return rows
        return self._lexical_search_contains(term, repo_ids, limit)

    def _lexical_search_fts(
        self, term: str, repo_ids: list[str] | None, limit: int
    ) -> list[dict[str, Any]] | None:
        """FTS path: per-token prefix matching (``tok:*``) so 'calc' still finds 'calculate_cost'.

        Returns ``None`` when the term has no indexable tokens (caller falls back to CONTAINS).
        """
        tokens = [t.lower() for t in _FTS_TOKEN_RE.findall(term or "") if t]
        if not tokens:
            return None
        tsquery = " & ".join(f"{t}:*" for t in tokens)

        params: list[Any] = [tsquery, tsquery]
        where = "document @@ to_tsquery('simple', %s)"
        if repo_ids:
            where += " AND repo_id = ANY(%s)"
            params.append(repo_ids)
        params.append(limit)
        sql = (
            "SELECT symbol_id, name, kind, file_id, line, indexed_at_commit, "
            "ts_rank(document, to_tsquery('simple', %s)) AS rank "
            f"FROM symbol_fts WHERE {where} "
            "ORDER BY rank DESC, symbol_id ASC LIMIT %s"
        )
        with self.client.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {
                "symbol_id": r[0],
                "name": r[1],
                "kind": r[2],
                "file_id": r[3],
                "line": r[4],
                "indexed_at_commit": r[5],
            }
            for r in rows
        ]

    def lexical_search_many(
        self, terms: list[str], *, repo_ids: list[str] | None = None, limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        """``term -> best ``limit`` keyword matches`` for many terms in a single FTS query.

        Anchor finding asks for one pool per content token, and a PR description yields dozens of
        them; per-term round-trips dominated retrieval latency. Ranking is unchanged - the window
        function reproduces the per-term ``ORDER BY rank DESC, symbol_id ASC LIMIT n`` exactly.
        Falls back to per-term queries when the FTS side table is absent.
        """
        unique = sorted({t for t in terms if t})
        if not unique:
            return {}
        if not self._fts_available():
            return {term: self._lexical_search_contains(term, repo_ids, limit) for term in unique}

        queries: list[tuple[str, str]] = []
        out: dict[str, list[dict[str, Any]]] = {}
        for term in unique:
            tokens = [t.lower() for t in _FTS_TOKEN_RE.findall(term) if t]
            if not tokens:
                # No indexable token: the CONTAINS path is the only thing that can match.
                out[term] = self._lexical_search_contains(term, repo_ids, limit)
                continue
            queries.append((term, " & ".join(f"{t}:*" for t in tokens)))
        if not queries:
            return out

        params: list[Any] = [[q[0] for q in queries], [q[1] for q in queries]]
        where = "f.document @@ to_tsquery('simple', q.tsq)"
        if repo_ids:
            where += " AND f.repo_id = ANY(%s)"
            params.append(repo_ids)
        params.append(limit)
        sql = (
            "WITH q AS (SELECT * FROM unnest(%s::text[], %s::text[]) AS t(term, tsq)) "
            "SELECT term, symbol_id, name, kind, file_id, line, indexed_at_commit FROM ("
            "  SELECT q.term, f.symbol_id, f.name, f.kind, f.file_id, f.line, "
            "         f.indexed_at_commit, "
            "         row_number() OVER (PARTITION BY q.term "
            "             ORDER BY ts_rank(f.document, to_tsquery('simple', q.tsq)) DESC, "
            "                      f.symbol_id ASC) AS rn "
            f"  FROM q JOIN symbol_fts f ON {where}"
            ") ranked WHERE rn <= %s ORDER BY term, rn"
        )
        with self.client.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        for row in rows:
            out.setdefault(row[0], []).append(
                {
                    "symbol_id": row[1],
                    "name": row[2],
                    "kind": row[3],
                    "file_id": row[4],
                    "line": row[5],
                    "indexed_at_commit": row[6],
                }
            )
        return out

    def _lexical_search_contains(
        self, term: str, repo_ids: list[str] | None, limit: int
    ) -> list[dict[str, Any]]:
        columns = ["symbol_id", "name", "kind", "file_id", "line", "indexed_at_commit"]
        term_l = (term or "").lower()
        ret = (
            "RETURN s.symbol_id, s.name, s.kind, s.file_id, s.line, s.indexed_at_commit "
            "ORDER BY s.name, s.symbol_id LIMIT $limit"
        )
        if repo_ids:
            query = (
                "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File) "
                "WHERE toLower(s.name) CONTAINS $term AND f.repo_id IN $repos " + ret
            )
            params: dict[str, Any] = {"term": term_l, "repos": repo_ids, "limit": limit}
        else:
            query = "MATCH (s:Symbol) WHERE toLower(s.name) CONTAINS $term " + ret
            params = {"term": term_l, "limit": limit}
        rows = self.client.cypher(query, params, columns)
        return [dict(zip(columns, row, strict=False)) for row in rows]

    def repo_of_symbol(self, symbol_id: str) -> str | None:
        columns = ["repo_id"]
        query = (
            "MATCH (s:Symbol {symbol_id: $sym})-[:DEFINED_IN]->(f:File) RETURN f.repo_id LIMIT 1"
        )
        rows = self.client.cypher(query, {"sym": symbol_id}, columns)
        return rows[0][0] if rows else None

    def counts(self) -> tuple[int, int]:
        nodes = self.client.cypher("MATCH (n) RETURN count(n)", None, ["c"])
        edges = self.client.cypher("MATCH ()-[r]->() RETURN count(r)", None, ["c"])
        n = int(nodes[0][0]) if nodes else 0
        e = int(edges[0][0]) if edges else 0
        return n, e
