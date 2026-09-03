"""Indexing orchestrator: git-sync -> extract (pass 1) -> link (pass 2) -> upsert.

Phase 0 entry point for full-indexing a repository - either a local path
(:meth:`Indexer.index_local_repo`) or a remote Bitbucket Cloud URL
(:meth:`Indexer.index_remote_repo`, which clones/fetches first and then runs the same local walk).
Wires the language registry (multi-language), the extractor, the repo-wide cross-file linker, and
the graph upserter together, and stamps every node with the commit the index was built against
(freshness guarantee).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from cce.domain.models import GraphFragment
from cce.indexing.embedder import Embedder
from cce.indexing.extractor import Extractor
from cce.indexing.gitsync import (
    GitCredentials,
    changed_files,
    current_commit,
    iter_source_files,
    parse_bitbucket_url,
    sync_repo,
)
from cce.indexing.linker import link_fragments, synthesize_scip_edges
from cce.indexing.parser.symbol_id import make_file_id, make_repo_id
from cce.indexing.upserter import Upserter
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore

#: Extensions scanned for cross-language bridge detection (native mobile + JS sides).
_BRIDGE_EXTS = (".m", ".mm", ".swift", ".kt", ".js", ".jsx", ".ts", ".tsx")

logger = logging.getLogger("cce.indexing")


@dataclass(slots=True)
class IndexSummary:
    repo_id: str
    files: int
    nodes: int
    edges: int
    commit: str
    nodes_added: int = 0
    edges_added: int = 0
    remote_url: str | None = None
    branch: str | None = None


@dataclass(slots=True)
class IncrementalSummary:
    repo_id: str
    from_commit: str
    to_commit: str
    added: int
    modified: int
    deleted: int
    nodes: int
    edges: int
    nodes_added: int = 0
    edges_added: int = 0


class Indexer:
    def __init__(
        self,
        repository: GraphRepository,
        extractor: Extractor | None = None,
        embedder: Embedder | None = None,
        embed: bool = True,
        use_scip: bool = True,
    ) -> None:
        self.repository = repository
        self.extractor = extractor or Extractor()
        # When enabled, per-repo SCIP resolution (if an indexer binary exists) elevates edge
        # provenance to 'scip'; absent a binary this is a no-op (feature 3).
        self.use_scip = use_scip
        self.upserter = Upserter(repository)
        # Embeddings are written into the same connection (P5). Disabled if ``embed=False`` or when
        # no connection is available (e.g. unit tests with a fake repository).
        self.embedder = embedder
        if self.embedder is None and embed:
            conn = getattr(getattr(repository, "client", None), "conn", None)
            if conn is not None:
                self.embedder = Embedder(VectorStore(conn))

    def index_local_repo(
        self, *, path: str | Path, name: str, commit: str | None = None
    ) -> IndexSummary:
        root = Path(path).resolve()
        commit = commit or current_commit(root)
        return self._index_root(root=root, name=name, commit=commit)

    def index_remote_repo(
        self,
        *,
        url: str,
        name: str | None = None,
        branch: str | None = None,
        credentials: GitCredentials | None = None,
        ssl_verify: bool | None = None,
        cache_dir: str | Path | None = None,
    ) -> IndexSummary:
        """Clone/fetch a Bitbucket Cloud repo, then full-index the working tree.

        ``name`` defaults to ``workspace/repo_slug`` (stable identity). Credentials and TLS settings
        fall back to :class:`cce.config.Settings` when not provided.
        """
        from cce.config import get_settings

        settings = get_settings()
        ref = parse_bitbucket_url(url)
        name = name or ref.full_name
        creds = credentials or GitCredentials(
            username=settings.bitbucket_username, token=settings.bitbucket_token
        )
        verify = settings.git_ssl_verify if ssl_verify is None else ssl_verify
        cache_root = Path(cache_dir or settings.repo_cache_dir)
        dest = cache_root / ref.host / ref.workspace / ref.repo_slug

        sync_repo(
            https_url=ref.https_url, dest=dest, creds=creds, branch=branch, ssl_verify=verify
        )
        commit = current_commit(dest)
        return self._index_root(
            root=dest.resolve(),
            name=name,
            commit=commit,
            remote_url=ref.https_url,
            branch=branch,
        )

    def _index_root(
        self,
        *,
        root: Path,
        name: str,
        commit: str,
        remote_url: str | None = None,
        branch: str | None = None,
    ) -> IndexSummary:
        repo_id = make_repo_id(name)
        default_branch = branch or "main"
        started = time.perf_counter()
        logger.info(
            "full index started",
            extra={"repo_id": repo_id, "repo_name": name, "commit": commit, "root": str(root)},
        )
        nodes_before, edges_before = self.repository.counts()

        self.upserter.ensure_repo_node(repo_id, name, default_branch, commit, remote_url)
        self._record_repo_row(repo_id, name, commit, remote_url, default_branch)

        self._apply_scip_resolution(root)

        # Pass 1: per-file extraction + upsert. Fragments are kept for the repo-wide link pass.
        files = 0
        fragments: list[GraphFragment] = []
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts):
            source = abs_path.read_bytes()
            fragment = self.extractor.extract_file(
                repo_id=repo_id, path=rel, source=source, indexed_at_commit=commit
            )
            if fragment is None:
                continue
            fragments.append(fragment)
            self.upserter.upsert_file_fragment(fragment)
            if self.embedder is not None:
                self.embedder.embed_fragment(
                    fragment, repo_id=repo_id, indexed_at_commit=commit
                )
            files += 1

        logger.info(
            "extraction pass finished",
            extra={"repo_id": repo_id, "files": files, "commit": commit},
        )

        # Pass 2: cross-file linking, optional SCIP edge synthesis, heuristic bridges.
        self._link_and_upsert(root, fragments)

        nodes, edges = self.repository.counts()
        logger.info(
            "full index finished",
            extra={
                "repo_id": repo_id,
                "files": files,
                "nodes": nodes,
                "edges": edges,
                "nodes_added": nodes - nodes_before,
                "edges_added": edges - edges_before,
                "commit": commit,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return IndexSummary(
            repo_id=repo_id,
            files=files,
            nodes=nodes,
            edges=edges,
            commit=commit,
            nodes_added=nodes - nodes_before,
            edges_added=edges - edges_before,
            remote_url=remote_url,
            branch=branch,
        )

    def _link_and_upsert(self, root: Path, fragments: list[GraphFragment]) -> None:
        """Repo-wide pass 2: resolve cross-file refs, synthesize SCIP + bridge edges, upsert."""
        if not fragments:
            return
        linked = link_fragments(fragments, scip_resolution=self.extractor.scip_resolution)
        if len(linked):
            self.upserter.upsert_file_fragment(linked)
        if self.extractor.scip_resolution is not None:
            synthesized = synthesize_scip_edges(self.extractor.scip_resolution, fragments)
            if len(synthesized):
                self.upserter.upsert_file_fragment(synthesized)
        bridges = self._extract_bridges(root, fragments)
        if len(bridges):
            self.upserter.upsert_file_fragment(bridges)

    def _extract_bridges(self, root: Path, fragments: list[GraphFragment]) -> GraphFragment:
        """Cross-language heuristic bridges (RN/Expo/Swift-ObjC), resolved over this run's symbols."""
        from cce.domain.enums import NodeLabel
        from cce.indexing.extractor.bridges import BridgeFile, SymbolRef, extract_bridges

        bridge_files: list[BridgeFile] = []
        for rel, abs_path in iter_source_files(root, _BRIDGE_EXTS):
            try:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            bridge_files.append(BridgeFile(path=rel, text=text))
        if not bridge_files:
            return GraphFragment()

        by_name: dict[str, list[SymbolRef]] = {}
        for fragment in fragments:
            language = None
            for node in fragment.nodes:
                if node.label is NodeLabel.FILE:
                    language = node.properties.get("language")
                    break
            if not isinstance(language, str):
                continue
            for node in fragment.nodes:
                if node.label is not NodeLabel.SYMBOL:
                    continue
                name = node.properties.get("name")
                if not isinstance(name, str) or not name:
                    continue
                ref = SymbolRef(symbol_id=node.node_id, language=language)
                refs = by_name.setdefault(name, [])
                if ref not in refs:
                    refs.append(ref)

        return extract_bridges(bridge_files, lambda name: by_name.get(name, []))

    def index_incremental(
        self,
        *,
        path: str | Path,
        name: str,
        since_commit: str | None = None,
        to_commit: str = "HEAD",
    ) -> IncrementalSummary:
        """Re-index only the files changed since ``since_commit`` (git-diff driven, Phase 1).

        For each added/modified file: drop its old subgraph + embeddings, then re-extract and upsert.
        For each deleted file: drop its subgraph + embeddings. Falls back to the repo's recorded
        ``last_indexed_commit`` when ``since_commit`` is omitted. All work happens in the repository's
        single connection so the graph + vector writes commit together (P5).

        After the per-file work a repo-wide **relink** runs: dropping a changed file's subgraph
        (DETACH DELETE) also removes cross-file edges into its symbols, and a newly added file may
        satisfy imports that previously resolved to nothing. Re-linking re-derives every cross-file
        edge deterministically (correctness first; unchanged edges MERGE idempotently).
        """
        root = Path(path).resolve()
        repo_id = make_repo_id(name)
        base = since_commit or self._last_indexed_commit(repo_id)
        if not base:
            raise ValueError(
                f"No baseline commit for repo '{name}'; run a full index first or pass since_commit."
            )
        target = current_commit(root) if to_commit == "HEAD" else to_commit
        started = time.perf_counter()
        logger.info(
            "incremental reindex started",
            extra={"repo_id": repo_id, "repo_name": name, "from_commit": base, "to_commit": target},
        )
        nodes_before, edges_before = self.repository.counts()

        self._apply_scip_resolution(root)

        changes = changed_files(root, base, to_commit)
        exts = set(self.extractor.registry.supported_extensions())

        added = modified = deleted = 0
        changed_paths: set[str] = set()
        changed_fragments: dict[str, GraphFragment] = {}
        for change in changes:
            if not any(change.path.lower().endswith(ext) for ext in exts):
                continue
            file_id = make_file_id(repo_id, change.path)
            self._drop_file(file_id)
            changed_paths.add(change.path)
            if change.status == "deleted":
                deleted += 1
                continue

            abs_path = root / change.path
            if not abs_path.is_file():
                # Present in diff as add/modify but missing on disk: treat as deletion.
                deleted += 1
                continue
            source = abs_path.read_bytes()
            fragment = self.extractor.extract_file(
                repo_id=repo_id, path=change.path, source=source, indexed_at_commit=target
            )
            if fragment is None:
                continue
            changed_fragments[change.path] = fragment
            self.upserter.upsert_file_fragment(fragment)
            if self.embedder is not None:
                self.embedder.embed_fragment(fragment, repo_id=repo_id, indexed_at_commit=target)
            if change.status == "added":
                added += 1
            else:
                modified += 1

        if changed_paths:
            fragments = self._collect_fragments_for_relink(
                root, repo_id, target, changed_fragments
            )
            self._link_and_upsert(root, fragments)

        self._update_indexed_commit(repo_id, target)
        nodes, edges = self.repository.counts()
        logger.info(
            "incremental reindex finished",
            extra={
                "repo_id": repo_id,
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "nodes": nodes,
                "edges": edges,
                "to_commit": target,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return IncrementalSummary(
            repo_id=repo_id,
            from_commit=base,
            to_commit=target,
            added=added,
            modified=modified,
            deleted=deleted,
            nodes=nodes,
            edges=edges,
            nodes_added=nodes - nodes_before,
            edges_added=edges - edges_before,
        )

    def _collect_fragments_for_relink(
        self,
        root: Path,
        repo_id: str,
        commit: str,
        changed_fragments: dict[str, GraphFragment],
    ) -> list[GraphFragment]:
        """Fragments for every source file (in-memory only; unchanged files are not re-upserted).

        Changed files reuse their freshly extracted fragments; unchanged files are re-parsed just
        to rebuild the deterministic link inputs (exports/imports/unresolved refs).
        """
        fragments: list[GraphFragment] = []
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts):
            if rel in changed_fragments:
                fragments.append(changed_fragments[rel])
                continue
            source = abs_path.read_bytes()
            fragment = self.extractor.extract_file(
                repo_id=repo_id, path=rel, source=source, indexed_at_commit=commit
            )
            if fragment is not None:
                fragments.append(fragment)
        return fragments

    def _apply_scip_resolution(self, root: Path) -> None:
        """Build repo-level SCIP resolution (if any indexer binary exists) for the extractor.

        Tries the known ecosystems; the first resolver that produces a resolution wins (deterministic
        by the fixed order). No-op when disabled or when no binary/reader is available, leaving the
        pure tree-sitter behaviour intact. Monikers are normalized to short symbol names so both
        provenance elevation and cross-file edge synthesis can match extractor symbols by name.
        """
        if not self.use_scip:
            return
        from cce.indexing.parser.scip import (
            ScipResolution,
            build_scip_resolver,
            moniker_display_name,
        )

        merged_edges: set[tuple[str, str]] = set()
        merged_defs: set[str] = set()
        found_any = False
        for language in ("python", "typescript"):
            resolver = build_scip_resolver(language)
            if not resolver.available():
                continue
            resolution = resolver.resolve(root)
            if resolution is None:
                continue
            merged_edges |= {
                (moniker_display_name(a), moniker_display_name(b)) for a, b in resolution.edges
            }
            merged_defs |= {moniker_display_name(d) for d in resolution.definitions}
            found_any = True
        if found_any:
            self.extractor.scip_resolution = ScipResolution(
                edges=frozenset(merged_edges), definitions=frozenset(merged_defs)
            )

    def _drop_file(self, file_id: str) -> None:
        """Remove a file's symbols/subgraph and their embeddings before re-extraction."""
        symbol_ids = [row["symbol_id"] for row in self.repository.symbols_in_file(file_id)]
        self.repository.delete_file_subgraph(file_id)
        if self.embedder is not None:
            self.embedder.store.delete_for_file(file_id, symbol_ids)

    def _last_indexed_commit(self, repo_id: str) -> str | None:
        conn = getattr(getattr(self.repository, "client", None), "conn", None)
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_indexed_commit FROM repos WHERE repo_id = %s", (repo_id,)
            )
            row = cur.fetchone()
        return row[0] if row and row[0] else None

    def _update_indexed_commit(self, repo_id: str, commit: str) -> None:
        conn = getattr(getattr(self.repository, "client", None), "conn", None)
        if conn is None:
            return
        conn.execute(
            "UPDATE repos SET last_indexed_commit = %s WHERE repo_id = %s", (commit, repo_id)
        )
        conn.commit()

    def _record_repo_row(
        self,
        repo_id: str,
        name: str,
        commit: str,
        remote_url: str | None = None,
        default_branch: str = "main",
    ) -> None:
        conn = self.repository.client.conn
        conn.execute(
            "INSERT INTO repos (repo_id, name, default_branch, remote_url, last_indexed_commit) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (repo_id) DO UPDATE SET "
            "last_indexed_commit = EXCLUDED.last_indexed_commit, name = EXCLUDED.name, "
            "default_branch = EXCLUDED.default_branch, "
            "remote_url = COALESCE(EXCLUDED.remote_url, repos.remote_url)",
            (repo_id, name, default_branch, remote_url, commit),
        )
        conn.commit()
