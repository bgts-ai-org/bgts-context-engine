"""URL parsing + credential-injection tests for the Bitbucket remote sync (no network, no DB)."""

from __future__ import annotations

import pytest

from cce.indexing.gitsync import (
    GitCredentials,
    build_auth_url,
    mask_secrets,
    parse_bitbucket_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://bitbucket.org/acme/widgets",
        "https://bitbucket.org/acme/widgets.git",
        "https://bitbucket.org/acme/widgets/src/main/app.py",
        "git@bitbucket.org:acme/widgets.git",
        "bitbucket.org/acme/widgets",
        "https://user@bitbucket.org/acme/widgets.git",
    ],
)
def test_parse_resolves_workspace_and_repo(url: str) -> None:
    ref = parse_bitbucket_url(url)
    assert ref.workspace == "acme"
    assert ref.repo_slug == "widgets"
    assert ref.full_name == "acme/widgets"
    assert ref.https_url == "https://bitbucket.org/acme/widgets.git"


@pytest.mark.parametrize("bad", ["", "   ", "https://bitbucket.org/onlyworkspace", "justtext"])
def test_parse_rejects_incomplete_urls(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_bitbucket_url(bad)


def test_build_auth_url_app_password() -> None:
    url = build_auth_url(
        "https://bitbucket.org/acme/widgets.git",
        GitCredentials(username="alice", token="s3cret"),
    )
    assert url == "https://alice:s3cret@bitbucket.org/acme/widgets.git"


def test_build_auth_url_access_token_uses_x_token_auth() -> None:
    url = build_auth_url(
        "https://bitbucket.org/acme/widgets.git", GitCredentials(token="ATBB-xyz")
    )
    assert url == "https://x-token-auth:ATBB-xyz@bitbucket.org/acme/widgets.git"


def test_build_auth_url_quotes_special_characters() -> None:
    url = build_auth_url(
        "https://bitbucket.org/acme/widgets.git", GitCredentials(token="a/b@c:d")
    )
    # The secret must be percent-encoded so it cannot break the URL structure.
    assert "a/b@c:d" not in url
    assert "a%2Fb%40c%3Ad" in url


def test_build_auth_url_without_credentials_is_unchanged() -> None:
    clean = "https://bitbucket.org/acme/widgets.git"
    assert build_auth_url(clean, GitCredentials()) == clean


def test_mask_secrets_hides_token_and_encoded_token() -> None:
    masked = mask_secrets("clone https://x-token-auth:tok%2F1@host failed (tok/1)", "tok/1")
    assert "tok/1" not in masked
    assert "tok%2F1" not in masked
    assert "***" in masked
