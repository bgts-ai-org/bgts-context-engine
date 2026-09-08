"""Remote git sync: clone/fetch a repository so the local indexer can walk it.

Credentials are injected into the HTTPS URL only for the duration of a single git invocation; the
stored ``origin`` is always reset to the clean URL afterwards, so a token is never persisted to
``.git/config``. Any token value is masked out of error messages.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("bce.indexing.gitsync")


class GitError(RuntimeError):
    """A git subprocess failed (message already has secrets masked)."""


@dataclass(frozen=True, slots=True)
class GitCredentials:
    username: str = ""
    token: str = ""

    @property
    def active(self) -> bool:
        return bool(self.token or self.username)


def build_auth_url(https_url: str, creds: GitCredentials) -> str:
    """Return ``https_url`` with credentials injected into the userinfo part.

    Bitbucket Cloud HTTPS git auth depends on the credential type:

    - App password / Atlassian API token (``ATATT...``): ``https://<username>:<token>@host/...``
      (Basic auth). For these a username is required; an email-style username has its
      ``@domain`` suffix stripped (Bitbucket expects the account username, not the email).
    - Workspace/project/repo access token (no username): ``https://x-token-auth:<token>@host/...``.
      ``x-token-auth`` only works for these access tokens, never for ``ATATT`` API tokens.
    - No credentials, or a non-HTTPS URL: returned unchanged.
    """
    if not creds.active or not https_url.startswith("https://"):
        return https_url
    rest = https_url[len("https://") :]

    username = creds.username
    if username and "@" in username:  # email-style → keep only the account username
        username = username.split("@", 1)[0]

    # x-token-auth is valid ONLY for workspace/project/repo access tokens, which are
    # passed without a username. ATATT API tokens and app passwords need username:token.
    user = username or ("x-token-auth" if creds.token else "")
    userinfo = quote(user, safe="")
    if creds.token:
        userinfo += ":" + quote(creds.token, safe="")
    return f"https://{userinfo}@{rest}"


def mask_secrets(text: str, *secrets: str) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***").replace(quote(secret, safe=""), "***")
    return out


def _run_git(args: list[str], *, secrets: tuple[str, ...]) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)  # noqa: S603,S607
    if proc.returncode != 0:
        detail = mask_secrets((proc.stderr or proc.stdout or "").strip(), *secrets)
        shown = mask_secrets(" ".join(args), *secrets)
        raise GitError(f"`git {shown}` failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def _default_branch(dest: Path, *, base: list[str]) -> str:
    try:
        out = _run_git(
            [*base, "-C", str(dest), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            secrets=(),
        )
    except GitError:
        return "main"
    ref = out.strip()
    return ref.split("/", 1)[1] if "/" in ref else (ref or "main")


def sync_repo(
    *,
    https_url: str,
    dest: str | Path,
    creds: GitCredentials,
    branch: str | None = None,
    ssl_verify: bool = True,
) -> Path:
    """Clone ``https_url`` into ``dest`` (or fetch+reset if already cloned). Returns ``dest``.

    On return the working tree is at the tip of ``branch`` (or the remote's default branch) and
    ``origin`` points at the clean (token-free) URL.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    auth_url = build_auth_url(https_url, creds)
    secrets = (creds.token,) if creds.token else ()
    base = ["-c", f"http.sslVerify={'true' if ssl_verify else 'false'}"]

    if (dest / ".git").exists():
        logger.info("git fetch", extra={"url": https_url, "dest": str(dest), "branch": branch})
        try:
            _run_git(
                [*base, "-C", str(dest), "remote", "set-url", "origin", auth_url], secrets=secrets
            )
            _run_git(
                [*base, "-C", str(dest), "fetch", "--prune", "--tags", "origin"], secrets=secrets
            )
            target = branch or _default_branch(dest, base=base)
            _run_git([*base, "-C", str(dest), "checkout", target], secrets=secrets)
            _run_git(
                [*base, "-C", str(dest), "reset", "--hard", f"origin/{target}"], secrets=secrets
            )
        finally:
            _restore_origin(dest, https_url, base=base, secrets=secrets)
    else:
        logger.info("git clone", extra={"url": https_url, "dest": str(dest), "branch": branch})
        clone_args = [*base, "clone"]
        if branch:
            clone_args += ["--branch", branch]
        clone_args += [auth_url, str(dest)]
        try:
            _run_git(clone_args, secrets=secrets)
        finally:
            _restore_origin(dest, https_url, base=base, secrets=secrets)
    return dest


def _restore_origin(
    dest: Path, https_url: str, *, base: list[str], secrets: tuple[str, ...]
) -> None:
    """Reset ``origin`` to the clean URL so no token lingers in ``.git/config``."""
    if (dest / ".git").exists():
        try:
            _run_git(
                [*base, "-C", str(dest), "remote", "set-url", "origin", https_url], secrets=secrets
            )
        except GitError:
            pass
