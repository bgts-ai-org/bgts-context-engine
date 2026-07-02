"""Golden tests for REFERENCES.ref_kind extraction (define/write/read/pass), Python + JS/TS."""

from __future__ import annotations

from cce.domain.enums import EdgeLabel, RefKind
from cce.indexing.extractor import Extractor

PY_SOURCE = b'''
CONFIG = {}
DATA = []


def helper(x):
    return x + 1


def setup():
    CONFIG = {"a": 1}
    n = helper(DATA)
    total = CONFIG
    return n, total
'''

JS_SOURCE = b'''
let config = {};
const data = [];

function helper(x) { return x + 1; }

function setup() {
  config = {a: 1};
  const n = helper(data);
  let total = config;
  return [n, total];
}
'''


def _ref_map(frag):
    """Map target symbol NAME -> ref_kind (str) for REFERENCES edges."""
    names = {n.node_id: n.properties.get("name") for n in frag.nodes if n.properties.get("name")}
    out = {}
    for e in frag.edges:
        if e.label is EdgeLabel.REFERENCES:
            out[names.get(e.dst_id)] = str(e.ref_kind) if e.ref_kind else None
    return out


def test_python_ref_kind_define_pass_read():
    frag = Extractor().extract_file(repo_id="demo", path="mod.py", source=PY_SOURCE)
    refs = _ref_map(frag)
    assert refs.get("CONFIG") == str(RefKind.DEFINE)
    assert refs.get("DATA") == str(RefKind.PASS)
    assert refs.get("helper") == str(RefKind.READ)


def test_js_ref_kind_write_pass_read():
    frag = Extractor().extract_file(repo_id="demo", path="mod.js", source=JS_SOURCE)
    refs = _ref_map(frag)
    assert refs.get("config") == str(RefKind.WRITE)
    assert refs.get("data") == str(RefKind.PASS)
    assert refs.get("helper") == str(RefKind.READ)


def test_ref_kind_extraction_is_deterministic():
    a = Extractor().extract_file(repo_id="demo", path="mod.py", source=PY_SOURCE)
    b = Extractor().extract_file(repo_id="demo", path="mod.py", source=PY_SOURCE)
    assert [e.dedup_key for e in a.edges] == [e.dedup_key for e in b.edges]


def test_references_feed_scoring_via_expansion():
    """A referrer edge should tag an expansion candidate with its ref_kind."""
    from cce.core.orchestrator.expand import expand_from_anchors

    class _Repo:
        def repo_of_symbol(self, sid):
            return "r1"

        def get_symbol(self, sid):
            return None

        def symbols_in_file(self, fid):
            return []

        def get_callers(self, sid):
            return []

        def get_callees(self, sid):
            return []

        def get_referrers(self, sid):
            if sid == "target":
                return [{"symbol_id": "writer", "ref_kind": "write", "provenance": "treesitter"}]
            return []

        def get_supertypes(self, sid):
            return []

        def get_subtypes(self, sid):
            return []

        def find_implementers(self, sid):
            return []

    expansion = expand_from_anchors(_Repo(), ["target"])
    assert "writer" in expansion
    assert expansion["writer"].ref_kind == "write"
