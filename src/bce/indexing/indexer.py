"""Indexing orchestrator: git-sync -> extract (pass 1) -> link (pass 2) -> upsert.

Phase 0 entry point for full-indexing a repository - either a local path
(:meth:`Indexer.index_local_repo`) or a remote Bitbucket Cloud URL
(:meth:`Indexer.index_remote_repo`, which clones/fetches first and then runs the same local walk).
Wires the language registry (multi-language), the extractor, the repo-wide cross-file linker, and
the graph upserter together, and stamps every node with the commit the index was built against
(freshness guarantee).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bce.domain.models import GraphFragment
from bce.indexing.embedder import Embedder
from bce.indexing.extractor import Extractor
from bce.indexing.gitsync import (
    IGNORE_FILE,
    ExcludeRules,
    FileChange,
    GitCredentials,
    build_rules,
    changed_files,
    current_commit,
    file_at_commit,
    iter_source_files,
    load_rules,
    parse_bitbucket_url,
    sync_repo,
)
from bce.indexing.linker import link_fragments, synthesize_scip_edges
from bce.indexing.parser.symbol_id import make_file_id, make_repo_id
from bce.indexing.upserter import Upserter
from bce.storage.graph.repository import GraphRepository
from bce.storage.vector.store import VectorStore

#: Extensions scanned for cross-language bridge detection (native mobile + JS sides).
_BRIDGE_EXTS = (".m", ".mm", ".swift", ".kt", ".js", ".jsx", ".ts", ".tsx")

logger = logging.getLogger("bce.indexing")


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
    excluded: int = 0


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
    excluded: int = 0


class Indexer:
    def __init__(
        self,
        repository: GraphRepository,
        extractor: Extractor | None = None,
        embedder: Embedder | None = None,
        embed: bool = True,
        use_scip: bool = True,
        exclude: Sequence[str] = (),
        minified_line_length: int | None = None,
    ) -> None:
        from bce.config import get_settings

        self.repository = repository
        self.extractor = extractor or Extractor()
        # When enabled, per-repo SCIP resolution (if an indexer binary exists) elevates edge
        # provenance to 'scip'; absent a binary this is a no-op (feature 3).
        self.use_scip = use_scip
        self.upserter = Upserter(repository)
        # File exclusion: the built-in defaults and each repo's ``.bceignore`` are added per run
        # (see ``bce.indexing.gitsync.exclude``); these are the configured extras, environment
        # (``BCE_INDEX_EXCLUDE``) first so an explicit ``exclude`` can override it.
        settings = get_settings()
        self.exclude_patterns: tuple[str, ...] = (*settings.index_exclude_patterns, *exclude)
        self.minified_line_length = (
            settings.index_minified_line_length
            if minified_line_length is None
            else minified_line_length
        )
        # Embeddings are written into the same connection (P5). Disabled if ``embed=False`` or when
        # no connection is available (e.g. unit tests with a fake repository). Deferred: encoded in
        # cross-file batches and flushed after each pass (see ``Embedder``), still one transaction.
        self.embedder = embedder
        if self.embedder is None and embed:
            conn = getattr(getattr(repository, "client", None), "conn", None)
            if conn is not None:
                self.embedder = Embedder(VectorStore(conn), defer=True)

    @contextmanager
    def _point_lookup_plans(self) -> Iterator[None]:
        """Keep the planner on nested loops for the duration of an index run.

        Every write statement is a point lookup: the endpoints of an edge ``MERGE`` are matched by
        ``gid`` through the GIN index, and the edge itself through the ``start_id`` / ``end_id``
        indexes. But the label tables are filled inside this one transaction and never analyzed,
        and AGE's ``@>`` operator has a constant selectivity, so the planner believes the two
        endpoint lookups return hundreds of rows and prefers a merge join for the existence check:
        a sequential scan *and sort of the whole edge table* per edge (netty: 64 000 edge
        statements, 372 GB of sort spill files, 50-700 ms each). With merge and hash joins
        disabled it takes the index path, which is flat at 1-2 ms whatever the graph size. The
        setting is per session, so the read paths and other connections are unaffected; the
        connection is restored afterwards. No-op without a database connection (fakes).
        """
        conn = getattr(getattr(self.repository, "client", None), "conn", None)
        if conn is None:
            yield
            return
        conn.execute("SET enable_mergejoin = off")
        conn.execute("SET enable_hashjoin = off")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                conn.execute("RESET enable_mergejoin")
                conn.execute("RESET enable_hashjoin")

    def _rules(self, root: Path) -> ExcludeRules:
        """Exclusion rules for a run over the working tree at ``root`` (its ``.bceignore``)."""
        return load_rules(root, self.exclude_patterns, self.minified_line_length)

    def _rules_from(self, ignore_text: str | None) -> ExcludeRules:
        """The same rules with ``ignore_text`` as the ``.bceignore`` contents (another commit)."""
        return build_rules(ignore_text, self.exclude_patterns, self.minified_line_length)

    def _flush_embeddings(self) -> None:
        """Write buffered embeddings (deferred embedders only; fakes without ``flush`` are fine)."""
        flush = getattr(self.embedder, "flush", None)
        if callable(flush):
            written = flush()
            if written:
                logger.info("embeddings flushed", extra={"rows": written})

    def _record_churn(self, root: Path, repo_id: str, commit: str, paths: set[str] | None) -> None:
        """Store per-file git churn at ``commit`` (scoring prior w11); no-op without a connection
        or before migration 0010. ``paths`` limits the rows to this run's files; ``None`` means
        every file the repo has in ``symbol_fts`` (incremental runs shift the whole window)."""
        conn = getattr(getattr(self.repository, "client", None), "conn", None)
        if conn is None:
            return
        from bce.indexing.churn import churn_available, file_churn, indexed_paths, write_file_churn

        if not churn_available(conn):
            return
        counts = file_churn(root, commit=commit)
        if not counts:
            return
        only = paths if paths is not None else indexed_paths(conn, repo_id)
        rows = write_file_churn(
            conn, repo_id=repo_id, counts=counts, commit=commit, only_paths=only
        )
        logger.info("file churn recorded", extra={"repo_id": repo_id, "rows": rows})

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
        fall back to :class:`bce.config.Settings` when not provided.
        """
        from bce.config import get_settings

        settings = get_settings()
        ref = parse_bitbucket_url(url)
        name = name or ref.full_name
        creds = credentials or GitCredentials(
            username=settings.bitbucket_username, token=settings.bitbucket_token
        )
        verify = settings.git_ssl_verify if ssl_verify is None else ssl_verify
        cache_root = Path(cache_dir or settings.repo_cache_dir)
        dest = cache_root / ref.host / ref.workspace / ref.repo_slug

        sync_repo(https_url=ref.https_url, dest=dest, creds=creds, branch=branch, ssl_verify=verify)
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
        with self._point_lookup_plans():
            return self._index_root_locked(
                root=root, name=name, commit=commit, remote_url=remote_url, branch=branch
            )

    def _index_root_locked(
        self,
        *,
        root: Path,
        name: str,
        commit: str,
        remote_url: str | None,
        branch: str | None,
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
        rules = self._rules(root)

        # Pass 1: per-file extraction + upsert. Fragments are kept for the repo-wide link pass.
        files = 0
        fragments: list[GraphFragment] = []
        indexed_paths: list[str] = []
        excluded_paths: list[str] = []
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts):
            source = _read_unless_excluded(rules, rel, abs_path)
            if source is None:
                excluded_paths.append(rel)
                continue
            fragment = self.extractor.extract_file(
                repo_id=repo_id, path=rel, source=source, indexed_at_commit=commit
            )
            if fragment is None:
                continue
            fragments.append(fragment)
            indexed_paths.append(rel)
            self.upserter.upsert_file_fragment(fragment)
            if self.embedder is not None:
                self.embedder.embed_fragment(fragment, repo_id=repo_id, indexed_at_commit=commit)
            files += 1
        dropped = self._drop_excluded_leftovers(repo_id, excluded_paths)
        self._flush_embeddings()
        self._record_churn(root, repo_id, commit, set(indexed_paths))
        excluded = len(excluded_paths)

        logger.info(
            "extraction pass finished",
            extra={
                "repo_id": repo_id,
                "files": files,
                "excluded": excluded,
                "excluded_dropped": dropped,
                "commit": commit,
            },
        )

        # Pass 2: cross-file linking, optional SCIP edge synthesis, heuristic bridges.
        self._link_and_upsert(root, fragments, rules)

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
            excluded=excluded,
        )

    def _drop_excluded_leftovers(self, repo_id: str, excluded_paths: list[str]) -> int:
        """Drop excluded files that an earlier run indexed (a full index upserts, never deletes).

        The candidates are narrowed to the files that own nodes in the graph (one lookup, see
        ``GraphRepository.indexed_file_ids``), so a large excluded tree costs nothing once it is
        gone; a repository without that lookup (fakes) drops every excluded path, a no-op for files
        that were never indexed. Returns the number of files dropped.
        """
        if not excluded_paths:
            return 0
        candidates = [(rel, make_file_id(repo_id, rel)) for rel in excluded_paths]
        lookup = getattr(self.repository, "indexed_file_ids", None)
        if callable(lookup) and getattr(getattr(self.repository, "client", None), "conn", None):
            known = lookup(repo_id)
            candidates = [(rel, fid) for rel, fid in candidates if fid in known]
        for _, file_id in candidates:
            self._drop_file(file_id)
        return len(candidates)

    def _link_and_upsert(
        self, root: Path, fragments: list[GraphFragment], rules: ExcludeRules
    ) -> None:
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
        bridges = self._extract_bridges(root, fragments, rules)
        if len(bridges):
            self.upserter.upsert_file_fragment(bridges)

    def _extract_bridges(
        self, root: Path, fragments: list[GraphFragment], rules: ExcludeRules
    ) -> GraphFragment:
        """Cross-language heuristic bridges (RN/Expo/Swift-ObjC), resolved over this run's symbols."""
        from bce.domain.enums import NodeLabel
        from bce.indexing.extractor.bridges import BridgeFile, SymbolRef, extract_bridges

        bridge_files: list[BridgeFile] = []
        for rel, abs_path in iter_source_files(root, _BRIDGE_EXTS, exclude=rules):
            try:
                source = _read_unless_excluded(rules, rel, abs_path)
            except OSError:
                continue
            if source is None:
                continue
            bridge_files.append(BridgeFile(path=rel, text=source.decode("utf-8", errors="ignore")))
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
        with self._point_lookup_plans():
            return self._index_incremental_locked(
                root=root, name=name, repo_id=repo_id, base=base, to_commit=to_commit
            )

    def _index_incremental_locked(
        self, *, root: Path, name: str, repo_id: str, base: str, to_commit: str
    ) -> IncrementalSummary:
        target = current_commit(root) if to_commit == "HEAD" else to_commit
        started = time.perf_counter()
        logger.info(
            "incremental reindex started",
            extra={"repo_id": repo_id, "repo_name": name, "from_commit": base, "to_commit": target},
        )
        nodes_before, edges_before = self.repository.counts()

        self._apply_scip_resolution(root)
        rules = self._rules(root)

        changes = changed_files(root, base, to_commit)
        if any(change.path == IGNORE_FILE for change in changes):
            changes = self._with_ignore_flips(root, base, rules, changes)
        exts = set(self.extractor.registry.supported_extensions())

        added = modified = deleted = excluded = 0
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
            source = _read_unless_excluded(rules, change.path, abs_path)
            if source is None:
                # Excluded now; its old subgraph (if it was ever indexed) was dropped above.
                excluded += 1
                continue
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
        self._flush_embeddings()

        if changed_paths:
            fragments = self._collect_fragments_for_relink(
                root, repo_id, target, changed_fragments, rules
            )
            self._link_and_upsert(root, fragments, rules)
        self._record_churn(root, repo_id, target, None)

        self._update_indexed_commit(repo_id, target)
        nodes, edges = self.repository.counts()
        logger.info(
            "incremental reindex finished",
            extra={
                "repo_id": repo_id,
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "excluded": excluded,
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
            excluded=excluded,
        )

    def _with_ignore_flips(
        self, root: Path, base: str, rules: ExcludeRules, changes: list[FileChange]
    ) -> list[FileChange]:
        """Add the unchanged files whose exclusion a ``.bceignore`` edit flipped to ``changes``.

        Re-included files come back as ``added``; newly excluded ones as ``modified``, which the
        caller drops from the index and then skips under the new rules. The decision before the
        edit uses ``.bceignore`` as it was at ``base``. Returned sorted (path, status) like
        :func:`changed_files`, so the re-index order stays reproducible.
        """
        before = self._rules_from(file_at_commit(root, base, IGNORE_FILE))
        in_diff = {change.path for change in changes}
        flips: list[FileChange] = []
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts):
            if rel in in_diff or before.match(rel) is rules.match(rel):
                continue
            source = abs_path.read_bytes()
            was, now = before.excluded(rel, source), rules.excluded(rel, source)
            if was != now:
                flips.append(FileChange(status="added" if was else "modified", path=rel))
        if flips:
            logger.info(
                "bceignore changed",
                extra={"reincluded": sum(f.status == "added" for f in flips), "flips": len(flips)},
            )
        return sorted([*changes, *flips], key=lambda c: (c.path, c.status))

    def _collect_fragments_for_relink(
        self,
        root: Path,
        repo_id: str,
        commit: str,
        changed_fragments: dict[str, GraphFragment],
        rules: ExcludeRules,
    ) -> list[GraphFragment]:
        """Fragments for every source file (in-memory only; unchanged files are not re-upserted).

        Changed files reuse their freshly extracted fragments; unchanged files are re-parsed just
        to rebuild the deterministic link inputs (exports/imports/unresolved refs).
        """
        fragments: list[GraphFragment] = []
        exts = self.extractor.registry.supported_extensions()
        for rel, abs_path in iter_source_files(root, exts, exclude=rules):
            if rel in changed_fragments:
                fragments.append(changed_fragments[rel])
                continue
            source = _read_unless_excluded(rules, rel, abs_path)
            if source is None:
                continue
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
        from bce.indexing.parser.scip import (
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
            cur.execute("SELECT last_indexed_commit FROM repos WHERE repo_id = %s", (repo_id,))
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


def _read_unless_excluded(rules: ExcludeRules, rel: str, abs_path: Path) -> bytes | None:
    """The file's bytes, or ``None`` when ``rules`` exclude it (by path, or as minified content).

    Path-excluded files are not read at all, which keeps multi-megabyte bundles off the hot path.
    """
    if rules.path_excluded(rel):
        return None
    source = abs_path.read_bytes()
    return None if rules.excluded(rel, source) else source
