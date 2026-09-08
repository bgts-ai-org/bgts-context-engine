"""Golden extraction tests for the Python provider (Phase 0 graph correctness)."""

from conftest import has_edge, module_namespaces, symbols_by_name

from bce.domain.enums import EdgeLabel, SymbolKind
from bce.indexing.extractor import Extractor

PY_SOURCE = b'''"""Module docstring."""
import os
from collections import OrderedDict

CONST = 1


def helper(x):
    """Helper function."""
    return x + 1


class Base:
    def greet(self):
        return "hi"


class Service(Base):
    def run(self):
        return helper(CONST)
'''


def _extract():
    return Extractor().extract_file(repo_id="demo", path="pkg/mod.py", source=PY_SOURCE)


def test_extracts_expected_symbols():
    frag = _extract()
    names = symbols_by_name(frag)
    assert {"CONST", "helper", "Base", "greet", "Service", "run"} <= set(names)


def test_function_symbol_metadata():
    frag = _extract()
    helper = next(n for n in frag.nodes if n.properties.get("name") == "helper")
    assert helper.properties["kind"] == str(SymbolKind.FUNCTION)
    assert helper.properties["docstring"] == "Helper function."
    assert helper.properties["visibility"] == "public"


def test_method_namespace_is_class():
    frag = _extract()
    greet = next(n for n in frag.nodes if n.properties.get("name") == "greet")
    assert greet.properties["kind"] == str(SymbolKind.METHOD)
    assert greet.properties["namespace"] == "Base"


def test_inherits_edge_resolved_in_file():
    frag = _extract()
    names = symbols_by_name(frag)
    assert has_edge(frag, EdgeLabel.INHERITS, names["Service"], names["Base"])


def test_calls_edge_resolved_in_file():
    frag = _extract()
    names = symbols_by_name(frag)
    assert has_edge(frag, EdgeLabel.CALLS, names["run"], names["helper"])


def test_imports_modules():
    frag = _extract()
    assert {"os", "collections"} <= module_namespaces(frag)


def test_extraction_is_deterministic():
    a = _extract()
    b = _extract()
    assert [n.dedup_key for n in a.nodes] == [n.dedup_key for n in b.nodes]
    assert [e.dedup_key for e in a.edges] == [e.dedup_key for e in b.edges]
