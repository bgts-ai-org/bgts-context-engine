"""Tests for stable id generation (determinism is the core guarantee, P1)."""

from bce.indexing.parser.symbol_id import (
    make_file_id,
    make_repo_id,
    make_symbol_id,
)


def test_symbol_id_is_deterministic():
    a = make_symbol_id(language="python", package="app.svc", namespace="", name="run", signature="(self)")
    b = make_symbol_id(language="python", package="app.svc", namespace="", name="run", signature="(self)")
    assert a == b


def test_symbol_id_distinguishes_overloads_by_signature():
    a = make_symbol_id(language="python", package="p", namespace="", name="f", signature="(x)")
    b = make_symbol_id(language="python", package="p", namespace="", name="f", signature="(x, y)")
    assert a != b


def test_symbol_id_distinguishes_namespace():
    base = make_symbol_id(language="python", package="p", namespace="Base", name="greet", signature="(self)")
    svc = make_symbol_id(language="python", package="p", namespace="Service", name="greet", signature="(self)")
    assert base != svc


def test_symbol_id_has_readable_moniker_and_hash():
    sid = make_symbol_id(language="python", package="p", namespace="N", name="run", signature="()")
    assert sid.startswith("python::p::N::run#")
    moniker, _, digest = sid.partition("#")
    assert len(digest) == 16


def test_file_id_normalizes_separators():
    assert make_file_id("repo", "a\\b\\c.py") == "repo:a/b/c.py"


def test_repo_id_is_slug():
    assert make_repo_id("My Repo") == "my-repo"
