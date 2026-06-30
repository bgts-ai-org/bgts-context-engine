"""git-sync (write path).

Phase 0 provides a local full-index walk plus remote clone/fetch for Bitbucket Cloud repositories
(``remote.py`` + ``bitbucket.py``). Webhook/polling-driven incremental diff sync lands in Phase 1;
the freshness stamp (indexed_at_commit) is already threaded through the pipeline.
"""

from cce.indexing.gitsync.bitbucket import BitbucketRepoRef, parse_bitbucket_url
from cce.indexing.gitsync.local import current_commit, iter_source_files
from cce.indexing.gitsync.remote import (
    GitCredentials,
    GitError,
    build_auth_url,
    mask_secrets,
    sync_repo,
)

__all__ = [
    "iter_source_files",
    "current_commit",
    "parse_bitbucket_url",
    "BitbucketRepoRef",
    "sync_repo",
    "GitCredentials",
    "GitError",
    "build_auth_url",
    "mask_secrets",
]
