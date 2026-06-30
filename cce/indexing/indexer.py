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

from cce.indexing.extractor import Extractor
from cce.indexing.gitsync import (
    GitCredentials,
    current_commit,
    iter_source_files,
    parse_bitbucket_url,
    sync_repo,
)
from cce.indexing.parser.symbol_id import make_repo_id
from cce.indexing.upserter import Upserter
from cce.storage.graph.repository import GraphRepository


@dataclass(slots=True)
class IndexSummary:
    repo_id: str
    files: int
    nodes: int
    edges: int
    commit: str
    remote_url: str | None = None
    branch: str | None = None


class Indexer:
    def __init__(self, repository: GraphRepository, extractor: Extractor | None = None) -> None:
        self.repository = repository
        self.extractor = extractor or Extractor()
        self.upserter = Upserter(repository)

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
