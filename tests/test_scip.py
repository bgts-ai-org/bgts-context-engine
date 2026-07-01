"""Tests for the optional SCIP resolver + provenance elevation."""

from __future__ import annotations

from cce.domain.enums import EdgeLabel, Provenance
from cce.indexing.extractor import Extractor
from cce.indexing.parser.scip import (
    NullScipResolver,
    ScipResolution,
    build_scip_resolver,
)

PY_SOURCE = b'''
def helper(x):
    return x + 1


def setup():
    return helper(1)
'''


def test_null_resolver_confirms_nothing():
    r = NullScipResolver()
    assert r.available() is False
    assert r.resolve(".") is None


def test_build_resolver_defaults_to_null_without_binary():
    # No scip binary on PATH in the test environment -> null resolver.
    for lang in ("python", "typescript", "go"):
        assert isinstance(build_scip_resolver(lang), NullScipResolver)


def test_resolution_confirms_predicate():
    res = ScipResolution(edges=frozenset({("setup", "helper")}), definitions=frozenset({"setup"}))
    assert res.confirms("setup", "helper") is True
    assert res.confirms("helper", "setup") is False
    assert res.has_definition("setup") is True


def test_provenance_elevated_when_scip_confirms():
    res = ScipResolution(edges=frozenset({("setup", "helper")}))
    frag = Extractor(scip_resolution=res).extract_file(
        repo_id="demo", path="m.py", source=PY_SOURCE
    )
    calls = [e for e in frag.edges if e.label is EdgeLabel.CALLS]
    assert calls
    assert all(e.provenance is Provenance.SCIP for e in calls)


def test_provenance_unchanged_without_resolution():
    frag = Extractor().extract_file(repo_id="demo", path="m.py", source=PY_SOURCE)
    calls = [e for e in frag.edges if e.label is EdgeLabel.CALLS]
    assert calls
    assert all(e.provenance is Provenance.TREESITTER for e in calls)
