"""Retrieval refinements after the 12-repo benchmark (Java / C# were the weak languages).

* Test symbols are excluded *in the query* (``symbol_fts.is_test``, migration 0013) so the lexical
  and usage pools fill with production symbols instead of test methods that then got filtered.
* URLs contribute no tokens, and contractions (``don``/``doesn``) are stop-words.
"""

from __future__ import annotations

from typing import Any

from bce.core.orchestrator import bulk
from bce.core.orchestrator.anchors import find_anchors
from bce.core.orchestrator.text import (
    explicit_references,
    is_test_symbol,
    mention_terms,
    query_terms,
    tokens,
)
from bce.domain import testness
from bce.domain.enums import NodeLabel
from bce.domain.models import GraphNode
from bce.storage.graph.repository import GraphRepository

# --- text ------------------------------------------------------------------------------------


def test_urls_contribute_no_tokens_or_references():
    text = (
        "See https://github.com/org/repo/pull/4242 and http://example.com:8080/x?y=1 - "
        "the ThemeResolver drops the `resolveTheme` fallback."
    )
    toks = tokens(text)
    assert "ThemeResolver" in toks and "resolveTheme" in toks
    assert not {"https", "github", "com", "org", "repo", "pull", "example", "8080"} & set(toks)
    assert not any("github" in name or "com" == name for _, name in explicit_references(text))
    assert all("github" not in value for _, value in mention_terms(text))


def test_contractions_are_stop_words():
    terms = query_terms("the parser doesn't accept it and the cache isn't refreshed, don't retry")
    assert not {"don", "doesn", "isn"} & set(terms)
    assert {"parser", "accept", "cache", "refreshed", "retry"} <= set(terms)


def test_is_test_symbol_lives_in_the_domain_and_is_re_exported():
    # storage imports it without going through the orchestrator (no import cycle)
    assert is_test_symbol is testness.is_test_symbol
    assert testness.is_test_symbol(
        "java::com.acme::FooTest::x#1", file_id="r:src/test/FooTest.java"
    )
    assert not testness.is_test_symbol("java::com.acme::Foo::x#1", file_id="r:src/main/Foo.java")


# --- bulk: optional keyword arguments ---------------------------------------------------------


def test_call_with_optional_drops_unknown_keywords_only():
    seen: list[dict[str, Any]] = []

    def old_style(term, *, repo_ids=None, limit=20):
        seen.append({"term": term, "limit": limit})
        return ["old"]

    def new_style(term, *, repo_ids=None, limit=20, exclude_tests=False):
        seen.append({"term": term, "exclude_tests": exclude_tests})
        return ["new"]

    def broken(term, *, repo_ids=None, limit=20, exclude_tests=False):
        raise TypeError("unrelated failure")

    extra = {"exclude_tests": True}
    assert bulk._call_with_optional(old_style, extra, "x", repo_ids=None, limit=5) == ["old"]
    assert bulk._call_with_optional(new_style, extra, "x", repo_ids=None, limit=5) == ["new"]
    assert seen == [{"term": "x", "limit": 5}, {"term": "x", "exclude_tests": True}]
    try:
        bulk._call_with_optional(broken, extra, "x", repo_ids=None, limit=5)
    except TypeError as exc:
        assert "unrelated" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unrelated TypeError must propagate")


class _FlagRepo:
    """Records whether the lexical / body lookups were asked to exclude tests."""

    def __init__(self) -> None:
        self.lexical_flags: list[bool] = []
        self.body_flags: list[bool] = []

    def lexical_search_many(self, terms, *, repo_ids=None, limit=20, exclude_tests=False):
        self.lexical_flags.append(exclude_tests)
        return {
            t: [
                {
                    "symbol_id": "s::Theme",
                    "name": "Theme",
                    "file_id": "r:Theme.ts",
                    "match_tier": "a",
                }
            ]
            for t in terms
        }

    def lexical_search(self, term, repo_ids=None, limit=20, exclude_tests=False):
        return self.lexical_search_many([term], limit=limit, exclude_tests=exclude_tests)[term]

    def body_mentions(self, mentions, *, repo_ids=None, limit=50, exclude_tests=False):
        self.body_flags.append(exclude_tests)
        return {}

    def resolve_symbol(self, name, repo_id=None):
        return []

    def find_routes(self, path):
        return []

    def get_symbol(self, sid):
        return None


def test_anchor_sources_ask_the_repository_to_exclude_tests():
    repo = _FlagRepo()
    find_anchors(repo, task_text="theme `localStorage`", lexical_terms=["theme"])
    assert repo.lexical_flags == [True]
    assert repo.body_flags and all(repo.body_flags)
    repo = _FlagRepo()
    find_anchors(
        repo, task_text="theme `localStorage`", lexical_terms=["theme"], include_tests=True
    )
    assert repo.lexical_flags == [False]
    assert repo.body_flags and not any(repo.body_flags)


class _LegacyRepo(_FlagRepo):
    """A repository from before the flag existed: called without it, still works."""

    def lexical_search_many(self, terms, *, repo_ids=None, limit=20):
        self.lexical_flags.append(None)
        return {
            t: [{"symbol_id": "s::Theme", "name": "Theme", "file_id": "r:Theme.ts"}] for t in terms
        }

    def body_mentions(self, mentions, *, repo_ids=None, limit=50):
        self.body_flags.append(None)
        return {}


def test_repositories_without_the_flag_still_work():
    repo = _LegacyRepo()
    result = find_anchors(repo, task_text="theme `localStorage`", lexical_terms=["theme"])
    assert result.strength_of("s::Theme") > 0
    assert repo.lexical_flags == [None] and repo.body_flags == [None]


# --- repository: the is_test column ----------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.statements.append((sql, params))


class _Client:
    def __init__(self) -> None:
        self.conn = _Conn()

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        pass


def _repo(*, fts: bool, body: bool, is_test: bool) -> GraphRepository:
    repo = GraphRepository(_Client())  # type: ignore[arg-type]
    repo._fts_ready = fts
    repo._fts_body_ready = body
    repo._fts_is_test_ready = is_test
    return repo


def test_test_filter_sql_only_when_requested_and_available():
    repo = _repo(fts=True, body=True, is_test=True)
    assert repo._test_filter_sql(True) == " AND NOT is_test"
    assert repo._test_filter_sql(True, alias="f.") == " AND NOT f.is_test"
    assert repo._test_filter_sql(False) == ""
    # Snapshot without migration 0013: no filter, callers keep their Python-side check.
    assert _repo(fts=True, body=True, is_test=False)._test_filter_sql(True) == ""


def test_fts_upsert_writes_is_test_from_path_and_name():
    repo = _repo(fts=True, body=True, is_test=True)
    prod = GraphNode(
        NodeLabel.SYMBOL,
        "java::com.acme::Foo::run#1",
        {"name": "run", "kind": "method", "file_id": "r:src/main/java/com/acme/Foo.java"},
    )
    test = GraphNode(
        NodeLabel.SYMBOL,
        "java::com.acme::FooTest::testRun#1",
        {"name": "testRun", "kind": "method", "file_id": "r:src/test/java/com/acme/FooTest.java"},
    )
    repo._upsert_symbol_fts(prod)
    repo._upsert_symbol_fts(test)
    statements = repo.client.conn.statements
    assert len(statements) == 2
    for sql, _params in statements:
        assert sql.startswith("INSERT INTO symbol_fts (")
        assert ", is_test)" in sql and "is_test = EXCLUDED.is_test" in sql
    assert statements[0][1][-1] is False
    assert statements[1][1][-1] is True
    # Without the column (older snapshot) the statement is the pre-0013 one.
    legacy = _repo(fts=True, body=True, is_test=False)
    legacy._upsert_symbol_fts(prod)
    sql, params = legacy.client.conn.statements[0]
    assert "is_test" not in sql and len(params) == 10
