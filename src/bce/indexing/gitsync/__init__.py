"""git-sync (write path).

Provides a local full-index walk plus remote clone/fetch for Bitbucket Cloud repositories
(``remote.py`` + ``bitbucket.py``), and git-diff based incremental change detection
(:func:`changed_files`) driving :meth:`Indexer.index_incremental`. The freshness stamp
(indexed_at_commit) is threaded through the pipeline for reproducibility.
"""

from bce.indexing.gitsync.bitbucket import BitbucketRepoRef, parse_bitbucket_url
from bce.indexing.gitsync.exclude import (
    DEFAULT_EXCLUDES,
    DEFAULT_MINIFIED_LINE_LENGTH,
    IGNORE_FILE,
    ExcludeRules,
    build_rules,
    load_rules,
    looks_minified,
)
from bce.indexing.gitsync.local import (
    FileChange,
    changed_files,
    current_commit,
    file_at_commit,
    iter_source_files,
)
from bce.indexing.gitsync.remote import (
    GitCredentials,
    GitError,
    build_auth_url,
    mask_secrets,
    sync_repo,
)

__all__ = [
    "iter_source_files",
    "current_commit",
    "changed_files",
    "FileChange",
    "file_at_commit",
    "ExcludeRules",
    "build_rules",
    "load_rules",
    "looks_minified",
    "DEFAULT_EXCLUDES",
    "DEFAULT_MINIFIED_LINE_LENGTH",
    "IGNORE_FILE",
    "parse_bitbucket_url",
    "BitbucketRepoRef",
    "sync_repo",
    "GitCredentials",
    "GitError",
    "build_auth_url",
    "mask_secrets",
]
