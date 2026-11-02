"""Index quality report: how much of a repository's code the index covers (``bce doctor``).

The indexer counts, for every code file of the repository (any extension in
:data:`~bce.domain.code_files.CODE_LANGUAGES`, parsed or not), its language and whether a parser
handled it; at the end of the run it adds the symbol count, the cross-file references the linker
resolved, whether SCIP ran and the embedding provider, and stores the report in
``repo_index_stats`` (migration 0014).

:func:`index_coverage` folds the reports of the repositories in scope into what an answer tells
the agent: the share of code files the index parsed and the languages it did not, with the
directories holding most of their files. Below :data:`UNSUPPORTED_RATIO` the index cannot speak
for the repository at all (``confidence: unsupported``).

Deterministic given the tree and the installed parsers.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from bce.domain.code_files import CODE_LANGUAGES, extension_of

logger = logging.getLogger("bce.indexing.stats")

#: Below this share of parsed code files the index cannot speak for the repository.
UNSUPPORTED_RATIO = 0.2
#: Below this share the answer says how much of the repository the index covers.
REPORT_RATIO = 0.95
#: A language with fewer files than this share of the code is not listed as unparsed.
MIN_LANGUAGE_SHARE = 0.02
#: Directories listed per unparsed language (most files first).
TOP_DIRS = 3


class IndexStatsBuilder:
    """Collects the per-file census during an index run; :meth:`report` returns the JSON body."""

    def __init__(self) -> None:
        self._files: Counter[str] = Counter()
        self._indexed: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._dirs: dict[str, Counter[str]] = {}
        self.symbols = 0

    def add(
        self,
        path: str,
        *,
        language: str | None,
        indexed: bool,
        parse_error: bool = False,
        symbols: int = 0,
    ) -> None:
        """Count one file. ``language`` is the parser's; ``None`` for a file no parser handles."""
        lang = language or CODE_LANGUAGES.get(extension_of(path))
        if not lang:
            return
        self._files[lang] += 1
        if indexed:
            self._indexed[lang] += 1
            self.symbols += symbols
        else:
            directory = path.replace("\\", "/").rsplit("/", 1)[0] if "/" in path else "."
            self._dirs.setdefault(lang, Counter())[directory] += 1
        if parse_error:
            self._errors[lang] += 1

    def report(
        self,
        *,
        link: dict[str, int] | None = None,
        scip: bool = False,
        embedding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        languages = {
            lang: {
                "files": self._files[lang],
                "indexed": self._indexed[lang],
                "parse_errors": self._errors[lang],
            }
            for lang in sorted(self._files)
        }
        unparsed_dirs = {
            lang: [d for d, _ in sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DIRS]]
            for lang, dirs in sorted(self._dirs.items())
        }
        refs = int((link or {}).get("refs", 0))
        resolved = int((link or {}).get("resolved", 0))
        return {
            "files": sum(self._files.values()),
            "indexed": sum(self._indexed.values()),
            "parse_errors": sum(self._errors.values()),
            "languages": languages,
            "unparsed_dirs": unparsed_dirs,
            "symbols": self.symbols,
            "references": {"cross_file": refs, "resolved": resolved},
            "scip": bool(scip),
            "embedding": dict(embedding or {}),
        }


def stats_available(conn: Any) -> bool:
    """True when migration 0014 has been applied to this database."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.repo_index_stats') IS NOT NULL")
        row = cur.fetchone()
    return bool(row and row[0])


def write_index_stats(conn: Any, *, repo_id: str, commit: str, stats: dict[str, Any]) -> bool:
    """Upsert the repository's report in the caller's transaction; False before migration 0014."""
    if not stats_available(conn):
        return False
    # A savepoint: a failed report must not abort the index run's transaction.
    with conn.transaction():
        conn.execute(
            "INSERT INTO repo_index_stats (repo_id, indexed_at_commit, stats) VALUES (%s, %s, %s) "
            "ON CONFLICT (repo_id) DO UPDATE SET indexed_at_commit = EXCLUDED.indexed_at_commit, "
            "computed_at = now(), stats = EXCLUDED.stats",
            (repo_id, commit, json.dumps(stats, sort_keys=True)),
        )
    return True


def read_index_stats(conn: Any, repo_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """``repo_id -> {"commit", "computed_at", "stats"}`` for the repositories (all when None)."""
    if not stats_available(conn):
        return {}
    sql = "SELECT repo_id, indexed_at_commit, computed_at, stats FROM repo_index_stats"
    params: list[Any] = []
    if repo_ids:
        sql += " WHERE repo_id = ANY(%s)"
        params.append(list(repo_ids))
    with conn.cursor() as cur:
        cur.execute(sql + " ORDER BY repo_id", params)
        rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for repo_id, commit, computed_at, stats in rows:
        body = stats if isinstance(stats, dict) else json.loads(stats or "{}")
        out[str(repo_id)] = {"commit": commit, "computed_at": computed_at, "stats": body}
    return out


def index_coverage(stats_by_repo: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The share of code files the index parsed, over the repositories' reports (None: no report).

    ``unparsed`` lists the languages with a meaningful share of the code
    (:data:`MIN_LANGUAGE_SHARE`) of which less than half is parsed, most files first, each with
    the directories holding most of its unparsed files.
    """
    files = indexed = 0
    by_lang: dict[str, dict[str, Any]] = {}
    for repo_id in sorted(stats_by_repo):
        stats = stats_by_repo[repo_id].get("stats") or {}
        files += int(stats.get("files") or 0)
        indexed += int(stats.get("indexed") or 0)
        dirs = stats.get("unparsed_dirs") or {}
        for lang, row in sorted((stats.get("languages") or {}).items()):
            entry = by_lang.setdefault(lang, {"files": 0, "indexed": 0, "dirs": []})
            entry["files"] += int(row.get("files") or 0)
            entry["indexed"] += int(row.get("indexed") or 0)
            for d in dirs.get(lang) or []:
                if len(entry["dirs"]) < TOP_DIRS and d not in entry["dirs"]:
                    entry["dirs"].append(d)
    if files <= 0:
        return None
    unparsed = [
        {"language": lang, **row}
        for lang, row in by_lang.items()
        if row["files"] >= MIN_LANGUAGE_SHARE * files and row["indexed"] * 2 < row["files"]
    ]
    unparsed.sort(key=lambda r: (-(r["files"] - r["indexed"]), r["language"]))
    return {
        "files": files,
        "indexed": indexed,
        "ratio": round(indexed / files, 4),
        "unparsed": unparsed,
    }
