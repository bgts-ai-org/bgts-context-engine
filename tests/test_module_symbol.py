"""The file's ``kind="module"`` outline symbol: when a file gets one, what it holds, how it shows."""

from __future__ import annotations

from bce.core.render.compact import _span_heading, _span_lines
from bce.core.render.evidence import span_for
from bce.core.scoring.engine import CONTAINER_MEMBER_PENALTY, Candidate, demote_redundant_containers
from bce.indexing.extractor.extractor import Extractor
from bce.indexing.extractor.module import module_name

CONSTANTS = b'''"""Interpreter version gates and checker names."""

import sys

PY38 = sys.version_info >= (3, 8)
PY39 = sys.version_info >= (3, 9)
MAIN_CHECKER_NAME = "main"


def full_version():
    major, minor = sys.version_info[:2]
    return f"{major}.{minor}"
'''

FUNCTIONS_ONLY = b"""import os
import sys


def a():
    return os.sep


def b():
    return sys.argv
"""


def _modules(path: str, source: bytes, **kw):
    frag = Extractor(**kw).extract_file(repo_id="r", path=path, source=source)
    return [n for n in frag.nodes if n.properties.get("kind") == "module"]


def test_a_file_with_top_level_code_gets_an_outline_symbol():
    (module,) = _modules("pylint/constants.py", CONSTANTS)
    assert module.properties["name"] == "constants"
    assert module.properties["file_id"] == "r:pylint/constants.py"
    lines = module.search_text.split("\n")
    # Line 5 is PY38; the function keeps its header, its body becomes one marker line.
    assert lines[4] == "PY38 = sys.version_info >= (3, 8)"
    assert lines[9] == "def full_version():"
    assert lines[10:] == ["\u22ef 11-12"]
    assert module.properties["end_line"] == 12


def test_a_file_of_imports_and_functions_gets_none():
    assert _modules("pkg/helpers.py", FUNCTIONS_ONLY) == []


def test_module_symbols_can_be_switched_off():
    assert _modules("pylint/constants.py", CONSTANTS, module_symbols=False) == []


def test_the_module_of_an_index_file_is_named_after_its_directory():
    assert module_name("src/auth/__init__.py") == "auth"
    assert module_name("crates/nu-cmd/src/filters/mod.rs") == "filters"
    assert module_name("lib/index.ts") == "lib"
    assert module_name("src/tokens.py") == "tokens"


def test_a_module_span_is_the_outline_with_bodies_omitted():
    (module,) = _modules("pylint/constants.py", CONSTANTS)
    item = {**module.properties, "symbol_id": module.node_id}
    span = span_for(item, module.search_text, task_text="PY38 gate is wrong")
    assert span["shape"] == "outline" and not span["complete"]
    assert [n for n, _ in span["lines"]] == list(range(1, 11))
    assert span["lines"][4] == [5, "PY38 = sys.version_info >= (3, 8)"]
    assert "· outline ·" in _span_heading(span)
    assert _span_lines(span, None)[-2:] == ["⋯ lines 11-12 omitted", "```"]


def test_an_outline_marker_keeps_the_numbering_of_the_lines_after_it():
    source = b"X = 1\nY = 2\nZ = 3\n\ndef f():\n    a = 1\n    b = 2\n    return a + b\n\nW = 4\n"
    (module,) = _modules("pkg/vals.py", source)
    item = {**module.properties, "symbol_id": module.node_id}
    span = span_for(item, module.search_text)
    assert span["lines"][-1] == [10, "W = 4"]
    assert "⋯ lines 6-8 omitted" in _span_lines(span, None)


def test_a_module_is_demoted_once_a_symbol_of_its_file_ranks():
    module = Candidate(symbol_id="m", kind="module", file_id="r:a.py", score=5.0)
    func = Candidate(symbol_id="f", kind="function", file_id="r:a.py", score=4.0)
    other = Candidate(symbol_id="o", kind="module", file_id="r:b.py", score=3.0)
    ranked = demote_redundant_containers([module, func, other], horizon=3)
    assert module.score == 5.0 - CONTAINER_MEMBER_PENALTY
    assert other.score == 3.0
    assert [c.symbol_id for c in ranked] == ["f", "m", "o"]
