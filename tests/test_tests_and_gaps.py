"""Related tests (``payload.tests``) and the gap signals (``coverage.gaps`` / ``coverage.state``)."""

from __future__ import annotations

from typing import Any

import pytest

from bce.core.orchestrator import gaps as gaps_mod
from bce.core.orchestrator.gaps import coverage_gaps, coverage_state, unsupported_paths
from bce.core.orchestrator.related_tests import MAX_TEST_FILES, related_tests
from bce.core.selector.pins import identifier_gaps, unresolved_identifiers
from bce.domain.testness import conventional_test_basenames


def _sid(module: str, name: str, namespace: str = "") -> str:
    return f"python::{module}::{namespace}::{name}#0"


CHECK = _sid("app.auth", "check_token")
TEST_EMPTY = _sid("tests.test_auth", "test_empty", "TestCheckToken")
HELPER = _sid("app.views", "login")


class _Repo:
    """The reads the related-tests lookup and the gap signals use."""

    def __init__(self) -> None:
        self.callers = {
            CHECK: [
                {"symbol_id": TEST_EMPTY, "name": "test_empty", "file_id": "r:tests/test_auth.py",
                 "line": 40},
                {"symbol_id": HELPER, "name": "login", "file_id": "r:app/views.py", "line": 5},
            ]
        }  # fmt: skip
        self.basenames: dict[str, list[str]] = {}
        self.file_symbols: dict[str, list[dict[str, Any]]] = {}
        self.meta = {TEST_EMPTY: {"name": "test_empty", "line": 40, "end_line": 52}}
        self.names: dict[str, list[dict[str, Any]]] = {}
        self.missing: list[str] = []

    def callers_of(self, ids):
        return {i: self.callers.get(i, []) for i in ids}

    def referrers_of(self, ids):
        return {}

    def files_by_basename_pattern(self, patterns, *, repo_ids=None, limit=8):
        return {p: self.basenames[p] for p in patterns if p in self.basenames}

    def symbols_in_files(self, fids):
        return {f: self.file_symbols.get(f, []) for f in fids}

    def symbols_meta(self, ids):
        return {i: self.meta[i] for i in ids if i in self.meta}

    def resolve_symbols(self, names):
        return {n: self.names.get(n, []) for n in names}

    def missing_paths(self, paths, *, repo_ids=None):
        return [p for p in paths if p in self.missing]


TARGET = {"symbol_id": CHECK, "file_id": "r:app/auth.py", "name": "check_token", "role": "edit"}


# ----------------------------------------------------------------------------------------------
# conventions
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/auth.py", ["test_auth.py", "auth_test.py"]),
        ("src/store.ts", ["store.test.ts", "store.test.tsx", "store.spec.ts", "store.spec.tsx"]),
        ("lib/x.js", ["x.test.js", "x.test.jsx", "x.test.mjs", "x.test.cjs", "x.spec.js",
                      "x.spec.jsx", "x.spec.mjs", "x.spec.cjs"]),
        ("pkg/account.go", ["account_test.go"]),
        ("src/Account.java", ["AccountTest.java", "AccountTests.java", "TestAccount.java"]),
        ("src/Account.cs", ["AccountTests.cs", "AccountTest.cs"]),
        ("lib/account.rb", ["account_spec.rb", "account_test.rb"]),
        ("pkg/__init__.py", []),
        ("tests/test_auth.py", []),  # already a test
        ("README.md", []),
    ],
)  # fmt: skip
def test_conventional_test_basenames_per_language(path, expected):
    assert conventional_test_basenames(path) == expected


# ----------------------------------------------------------------------------------------------
# related_tests
# ----------------------------------------------------------------------------------------------


def test_graph_callers_that_are_tests_come_with_their_ranges():
    out = related_tests(_Repo(), [TARGET])
    assert out == [
        {
            "file_id": "r:tests/test_auth.py",
            "path": "tests/test_auth.py",
            "why": "calls check_token",
            "symbols": [
                {
                    "symbol_id": TEST_EMPTY,
                    "name": "test_empty",
                    "qualified": "TestCheckToken::test_empty",
                    "line": 40,
                    "end_line": 52,
                }
            ],
        }
    ]  # fmt: skip  (the production caller app/views.py is not a test)


def test_naming_convention_finds_the_nearest_test_file_without_graph_edges():
    repo = _Repo()
    repo.callers = {}
    repo.basenames = {"test_auth.py": ["r:tests/other/test_auth.py", "r:tests/app/test_auth.py"]}
    repo.file_symbols = {
        "r:tests/app/test_auth.py": [
            {"symbol_id": "t0", "name": "fake_check_token", "kind": "function", "line": 1},
            {"symbol_id": "t1", "name": "TestLogin", "kind": "class", "line": 3},
            {"symbol_id": "t2", "name": "test_check_token_rejects", "kind": "function", "line": 30},
        ]
    }  # fmt: skip
    out = related_tests(repo, [TARGET])
    assert [t["file_id"] for t in out] == ["r:tests/app/test_auth.py"]  # nearest by directory
    assert out[0]["why"] == "test file of app/auth.py"
    # a test named after the target; the fake that shares its name is not a test
    assert [s["name"] for s in out[0]["symbols"]] == ["test_check_token_rejects"]
    repo.file_symbols["r:tests/app/test_auth.py"].pop()
    assert [s["name"] for s in related_tests(repo, [TARGET])[0]["symbols"]] == ["TestLogin"]


def test_both_channels_rank_and_the_file_count_is_bounded():
    repo = _Repo()
    repo.basenames = {
        "test_auth.py": ["r:tests/test_auth.py"],
        "auth_test.py": ["r:tests/auth_test.py"],
    }
    repo.callers[CHECK].append(
        {"symbol_id": "x", "name": "test_other", "file_id": "r:tests/test_misc.py", "line": 1}
    )
    out = related_tests(repo, [TARGET])
    assert len(out) == MAX_TEST_FILES
    assert out[0]["file_id"] == "r:tests/test_auth.py"  # graph + convention


def test_scope_and_failures_yield_no_tests():
    assert related_tests(_Repo(), [TARGET], repo_ids=["other"]) == []
    assert related_tests(object(), [TARGET]) == []
    assert related_tests(_Repo(), []) == []


# ----------------------------------------------------------------------------------------------
# identifiers
# ----------------------------------------------------------------------------------------------


def test_identifiers_unknown_to_the_index_are_reported_apart_from_unresolved_ones():
    repo = _Repo()
    repo.names = {
        "TokenStore": [{"symbol_id": "s", "name": "TokenStore", "file_id": "r:app/store.py"}],
        "test_helper_fn": [
            {"symbol_id": "t", "name": "test_helper_fn", "file_id": "r:tests/test_x.py"}
        ],
    }
    task = (
        "<issue_description>Use TokenStore in check_token and add RefreshPolicy; test_helper_fn "
        "breaks, and so does my test_reproduces_it.\\nStatement: see AUTHORS, the JavaScript docs and "
        "`chain.from_iterable`.\n```python\ndef repro_case():\n    Store.flush_all()\n```"
        "</issue_description>"
    )
    items = [{"name": "check_token", "file_id": "r:app/auth.py"}]
    out = identifier_gaps(repo, items, task, repo_ids=["r"])
    assert out["unresolved"] == [{"name": "TokenStore", "defined_in": ["r:app/store.py"]}]
    # only a test defines test_helper_fn; the snippet's own names, a member after a dot and an
    # all-caps word are not gaps of the index
    assert out["unknown"] == ["RefreshPolicy"]
    assert unresolved_identifiers(repo, items, task, repo_ids=["r"]) == out["unresolved"]


# ----------------------------------------------------------------------------------------------
# gaps
# ----------------------------------------------------------------------------------------------


def test_unsupported_paths_name_the_language_or_the_missing_file(monkeypatch):
    monkeypatch.setattr(gaps_mod, "indexed_extensions", lambda: frozenset({".py", ".ts"}))
    repo = _Repo()
    repo.missing = ["app/gone.py"]
    task = "Fix src/lib.rs, app/gone.py and app/auth.py; note it in changelog/README.rst"
    out = unsupported_paths(repo, task, ["r:app/auth.py"], repo_ids=None)
    assert out == [
        {"path": "src/lib.rs", "reason": "language", "extension": ".rs"},
        {"path": "app/gone.py", "reason": "missing"},
    ]  # docs are not code the index should hold
    assert coverage_state({"unsupported_paths": out[1:]}) == "known"  # maybe a file to create


def test_gap_block_and_state(monkeypatch):
    monkeypatch.setattr(gaps_mod, "indexed_extensions", lambda: frozenset({".py"}))
    spans = [
        {"path": "app/auth.py", "start": 10, "end": 900, "truncated": True, "role": "edit"},
    ]
    common = dict(
        task_text="fix check_token",
        answer_file_ids=["r:app/auth.py"],
        evidence=spans,
        unknown_identifiers=["RefreshPolicy"],
        repo_ids=None,
        semantic_error=None,
    )
    out = coverage_gaps(_Repo(), tests=[], semantic_ran=True, embedding_provider="voyage", **common)
    assert out == {
        "unknown_identifiers": ["RefreshPolicy"],
        "unsupported_paths": [],
        "truncated_evidence": ["app/auth.py:10-900"],
        "missing_tests": True,
        "semantic_degraded": None,
    }
    assert coverage_state(out) == "known"
    hashing = coverage_gaps(
        _Repo(), tests=[{"file_id": "t"}], semantic_ran=True, embedding_provider="hashing", **common
    )
    assert hashing["missing_tests"] is False and "hashing" in hashing["semantic_degraded"]
    assert coverage_state(hashing) == "unknown"
    assert coverage_state({"unsupported_paths": [{"path": "a.rs", "reason": "language"}]}) == (
        "unknown"
    )
