"""Deterministic test-symbol detection, shared by indexing and retrieval.

A task description almost always resembles the tests that assert it, so retrieval keeps test
symbols out of its lexical, semantic and usage channels unless the caller asks for them. The
same rule is applied when a symbol is written to the search side table (``symbol_fts.is_test``),
so those channels can exclude tests *in the query* instead of fetching them and dropping them
afterwards - on a repository whose symbols are 60 % tests (Guava, EF Core) the post-hoc filter
left every lexical pool saturated with test methods and pushed the production symbols out.

Pure string processing; no imports from the storage or orchestrator layers so both can use it.
"""

from __future__ import annotations

import re

_TEST_DIR_PARTS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "fixtures"}
)
_TEST_FILE_RE = re.compile(
    r"(^|[/\\.])("
    r"test_[^/\\]*"  # test_foo.py
    r"|[^/\\]*_test\.[a-z]+"  # foo_test.go / foo_test.py
    r"|[^/\\]*\.(test|spec)\.[a-z]+"  # foo.test.ts / foo.spec.js
    r"|[^/\\]*(Test|Tests|Spec|Specs)\.(java|cs|kt|scala|php|rb)"  # FooTest.java / FooTests.cs
    r"|conftest\.py"
    r")$",
)
#: ``test_x`` / ``should_x`` are tests wherever they live (pytest, rspec, JUnit-snake); the
#: CamelCase forms are only trusted when no path is known - ``TestBotTriggerPanel`` and
#: ``testConnection`` are production names, and real ``TestFoo`` / ``testFoo`` tests sit in
#: test directories or ``*Test.java`` files that the path rules already catch.
_TEST_NAME_RE = re.compile(r"^(test_|it_|should_|spec_)")
_TEST_NAME_CAMEL_RE = re.compile(r"^(test[A-Z]|Test[A-Z])")


def is_test_symbol(
    symbol_id: str | None, name: str | None = None, file_id: str | None = None
) -> bool:
    """Deterministic test detection from the file path, module path and symbol name.

    Works on symbol ids of the form ``<lang>::<module.path>::<Class>::<name>#<hash>`` as well as
    on ``repo:path/to/file.py`` file ids; any of the three inputs may be missing.
    """
    paths: list[str] = []
    if file_id:
        paths.append(file_id.split(":", 1)[1] if ":" in file_id else file_id)
    if symbol_id:
        segments = symbol_id.split("::")
        if len(segments) >= 2 and segments[1]:
            paths.append(segments[1].replace(".", "/"))
        if name is None and segments:
            name = segments[-1].split("#", 1)[0] or None
    for path in paths:
        norm = path.replace("\\", "/")
        parts = norm.split("/")
        if any(p.lower() in _TEST_DIR_PARTS for p in parts[:-1]):
            return True
        if _TEST_FILE_RE.search(parts[-1]) or _TEST_FILE_RE.search(norm):
            return True
        # Module-path form has no extension: "tests/test_api_ui" -> last part "test_api_ui".
        if parts[-1].startswith("test_") or parts[-1].endswith("_test"):
            return True
    if name and _TEST_NAME_RE.match(name):
        return True
    if name and not paths and _TEST_NAME_CAMEL_RE.match(name):
        return True
    return False
