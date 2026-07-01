"""Indexing orchestrator: git-sync -> extract -> upsert.

Phase 0 entry point for full-indexing a repository - either a local path
(:meth:`Indexer.index_local_repo`) or a remote Bitbucket Cloud URL
(:meth:`Indexer.index_remote_repo`, which clones/fetches first and then runs the same local walk).
Wires the language registry (multi-language), the extractor, and the graph upserter together, and
stamps every node with the commit the index was built against (freshness guarantee).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
from cce.indexing.parser.symbol_id import make_file_id, make_repo_id
from cce.indexing.upserter import Upserter
from cce.storage.graph.repository import GraphRepository
from cce.storage.vector.store import VectorStore


@dataclass(slots=True)
class IndexSummary:
    repo_id: str
    files: int
    nodes: int
    edges: int
    commit: str
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

        self.upserter.ensure_repo_node(repo_id, name, default_branch, commit, remote_url)
        self._record_repo_row(repo_id, name, commit, remote_url, default_branch)

        self._apply_scip_resolution(root)

        files = 0
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts):
            source = abs_path.read_bytes()
            fragment = self.extractor.extract_file(
                repo_id=repo_id, path=rel, source=source, indexed_at_commit=commit
            )
            if fragment is None:
                continue
            self.upserter.upsert_file_fragment(fragment)
            if self.embedder is not None:
                self.embedder.embed_fragment(
                    fragment, repo_id=repo_id, indexed_at_commit=commit
                )
            files += 1

        nodes, edges = self.repository.counts()
        return IndexSummary(
            repo_id=repo_id,
            files=files,
            nodes=nodes,
            edges=edges,
            commit=commit,
            remote_url=remote_url,
            branch=branch,
        )

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
        """
        root = Path(path).resolve()
        repo_id = make_repo_id(name)
        base = since_commit or self._last_indexed_commit(repo_id)
        if not base:
            raise ValueError(
                f"No baseline commit for repo '{name}'; run a full index first or pass since_commit."
            )
        target = current_commit(root) if to_commit == "HEAD" else to_commit

        changes = changed_files(root, base, to_commit)
        exts = set(self.extractor.registry.supported_extensions())

        added = modified = deleted = 0
        for change in changes:
            if not any(change.path.lower().endswith(ext) for ext in exts):
                continue
            file_id = make_file_id(repo_id, change.path)
            self._drop_file(file_id)
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
            self.upserter.upsert_file_fragment(fragment)
            if self.embedder is not None:
                self.embedder.embed_fragment(fragment, repo_id=repo_id, indexed_at_commit=target)
            if change.status == "added":
                added += 1
            else:
                modified += 1

        self._update_indexed_commit(repo_id, target)
        nodes, edges = self.repository.counts()
        return IncrementalSummary(
            repo_id=repo_id,
            from_commit=base,
            to_commit=target,
            added=added,
            modified=modified,
            deleted=deleted,
            nodes=nodes,
            edges=edges,
        )

    def _apply_scip_resolution(self, root: Path) -> None:
        """Build repo-level SCIP resolution (if any indexer binary exists) for the extractor.

        Tries the known ecosystems; the first resolver that produces a resolution wins (deterministic
        by the fixed order). No-op when disabled or when no binary/reader is available, leaving the
        pure tree-sitter behaviour intact.
        """
        if not self.use_scip:
            return
        from cce.indexing.parser.scip import ScipResolution, build_scip_resolver

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
            merged_edges |= set(resolution.edges)
            merged_defs |= set(resolution.definitions)
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
