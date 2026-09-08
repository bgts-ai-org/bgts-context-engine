"""Bitbucket Cloud URL parsing.

Turns any of the URL shapes a user might paste into a canonical ``workspace/repo_slug`` reference
plus a clean HTTPS clone URL:

- ``https://bitbucket.org/acme/widgets``
- ``https://bitbucket.org/acme/widgets.git``
- ``https://bitbucket.org/acme/widgets/src/main/...`` (web "Source" URL - extra path ignored)
- ``git@bitbucket.org:acme/widgets.git`` (SSH form - still parsed; we clone over HTTPS)

The logical repo name defaults to ``workspace/repo_slug`` so a repo keeps a stable identity (and
stable symbol ids) regardless of where it is cloned to on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_DEFAULT_HOST = "bitbucket.org"
_SSH_RE = re.compile(r"^[\w.+-]+@([\w.-]+):(.+)$")


@dataclass(frozen=True, slots=True)
class BitbucketRepoRef:
    host: str
    workspace: str
    repo_slug: str

    @property
    def full_name(self) -> str:
        """Stable logical name: ``workspace/repo_slug``."""
        return f"{self.workspace}/{self.repo_slug}"

    @property
    def https_url(self) -> str:
        """Clean HTTPS clone URL (no credentials)."""
        return f"https://{self.host}/{self.workspace}/{self.repo_slug}.git"


def parse_bitbucket_url(url: str) -> BitbucketRepoRef:
    """Parse a Bitbucket repository URL into a :class:`BitbucketRepoRef`.

    Raises ``ValueError`` if the URL does not contain a ``<workspace>/<repo>`` pair.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Empty Bitbucket URL.")

    ssh = _SSH_RE.match(raw)
    if ssh:
        host, path = ssh.group(1), ssh.group(2)
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        netloc = parsed.netloc
        if "@" in netloc:  # strip any embedded credentials
            netloc = netloc.split("@", 1)[1]
        host = netloc or _DEFAULT_HOST
        path = parsed.path

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Bitbucket URL must contain '<workspace>/<repo>': {url!r}")

    workspace = parts[0]
    repo_slug = parts[1]
    if repo_slug.endswith(".git"):
        repo_slug = repo_slug[:-4]
    if not workspace or not repo_slug:
        raise ValueError(f"Could not parse workspace/repo from URL: {url!r}")

    return BitbucketRepoRef(host=host, workspace=workspace, repo_slug=repo_slug)
