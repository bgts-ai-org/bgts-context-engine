"""B5 golden tests: multi-file fixtures for the cross-file linker (Phase A).

Covers the plan's acceptance criteria at the extractor+linker level (no database):

- cross-file CALLS (the ``calculate_cost`` scenario) with a call-site ``line`` property (B4)
- cross-file INHERITS through an import binding
- Module -> File mapping (A5) that enables transitive dependency walks
- determinism: double extraction + input-order independence yield identical fragments
- incremental relink: re-extracting a changed file and relinking preserves incoming edges (A6)
"""

from __future__ import annotations

from conftest import symbols_by_name

from cce.domain.enums import EdgeLabel, NodeLabel
from cce.indexing.extractor import Extractor
from cce.indexing.linker import link_fragments
from cce.indexing.parser.symbol_id import make_file_id

COST_PY = b'''
def calculate_cost(tokens, rate):
    """Cost for a token count."""
    return tokens * rate
'''

AI_SERVICE_PY = b'''
from utils.cost import calculate_cost


def estimate(tokens):
    return calculate_cost(tokens, 0.002)
'''

BASE_PY = b'''
class BaseHandler:
    def handle(self):
        return None
'''

DERIVED_PY = b'''
from core.base import BaseHandler


class JsonHandler(BaseHandler):
    def handle(self):
        return {}
'''

UTIL_JS = b"""
export function formatPrice(value) {
  return value.toFixed(2);
}
"""

APP_JS = b"""
import { formatPrice } from "./util";

export function render(total) {
  return formatPrice(total);
}
"""


def _extract(path: str, source: bytes):
    frag = Extractor().extract_file(repo_id="demo", path=path, source=source)
    assert frag is not None
    return frag


def _cost_fragments():
    return [
        _extract("utils/cost.py", COST_PY),
        _extract("ai_service.py", AI_SERVICE_PY),
    ]


def _edges(fragment, label):
    return [e for e in fragment.edges if e.label is label]


def test_cross_file_call_is_linked_with_call_line():
    frags = _cost_fragments()
    linked = link_fragments(frags)

    callee = symbols_by_name(frags[0])["calculate_cost"]
    caller = symbols_by_name(frags[1])["estimate"]

    calls = _edges(linked, EdgeLabel.CALLS)
    assert any(e.src_id == caller and e.dst_id == callee for e in calls)

    edge = next(e for e in calls if e.src_id == caller and e.dst_id == callee)
    assert isinstance(edge.properties.get("line"), int)
    assert edge.properties["line"] > 0


def test_cross_file_inherits_is_linked():
    base = _extract("core/base.py", BASE_PY)
    derived = _extract("handlers/json_handler.py", DERIVED_PY)
    linked = link_fragments([base, derived])

    base_id = symbols_by_name(base)["BaseHandler"]
    derived_id = symbols_by_name(derived)["JsonHandler"]
    inherits = _edges(linked, EdgeLabel.INHERITS)
    assert any(e.src_id == derived_id and e.dst_id == base_id for e in inherits)


def test_cross_file_call_is_linked_js():
    util = _extract("src/util.js", UTIL_JS)
    app = _extract("src/app.js", APP_JS)
    linked = link_fragments([util, app])

    callee = symbols_by_name(util)["formatPrice"]
    caller = symbols_by_name(app)["render"]
    calls = _edges(linked, EdgeLabel.CALLS)
    assert any(e.src_id == caller and e.dst_id == callee for e in calls)


def test_module_node_is_mapped_to_defining_file():
    linked = link_fragments(_cost_fragments())

    cost_file = make_file_id("demo", "utils/cost.py")
    modules = [n for n in linked.nodes if n.label is NodeLabel.MODULE]
    assert any(n.properties.get("file_id") == cost_file for n in modules)

    module_id = next(n.node_id for n in modules if n.properties.get("file_id") == cost_file)
    imports = _edges(linked, EdgeLabel.IMPORTS)
    assert any(e.src_id == module_id and e.dst_id == cost_file for e in imports)


def _fingerprint(fragment):
    return (
        [n.dedup_key for n in fragment.nodes],
        [(e.dedup_key, e.properties.get("line")) for e in fragment.edges],
    )


def test_double_extraction_linking_is_deterministic():
    a = link_fragments(_cost_fragments())
    b = link_fragments(_cost_fragments())
    assert _fingerprint(a) == _fingerprint(b)


def test_linking_is_input_order_independent():
    a = link_fragments(_cost_fragments())
    b = link_fragments(list(reversed(_cost_fragments())))
    assert _fingerprint(a) == _fingerprint(b)


def test_monorepo_service_rooted_import_is_linked():
    """Absolute imports rooted at a service subdirectory (``from app.core.pricing import x``
    inside ``services/ai-analytics/``) resolve via package-suffix matching."""
    cost = _extract("services/ai-analytics/app/core/pricing.py", COST_PY)
    svc = _extract(
        "services/ai-analytics/app/services/ai_service.py",
        b"from app.core.pricing import calculate_cost\n\n\n"
        b"def estimate(tokens):\n    return calculate_cost(tokens, 0.002)\n",
    )
    linked = link_fragments([cost, svc])

    callee = symbols_by_name(cost)["calculate_cost"]
    caller = symbols_by_name(svc)["estimate"]
    calls = _edges(linked, EdgeLabel.CALLS)
    assert any(e.src_id == caller and e.dst_id == callee for e in calls)


def test_incremental_relink_preserves_incoming_cross_file_edges():
    """A6: after the callee's file changes, relinking all fragments re-derives the incoming edge."""
    frags = _cost_fragments()
    before = link_fragments(frags)

    changed_cost = COST_PY + b"\n\ndef discount(amount):\n    return amount * 0.9\n"
    recost = _extract("utils/cost.py", changed_cost)
    after = link_fragments([recost, frags[1]])

    callee = symbols_by_name(recost)["calculate_cost"]
    caller = symbols_by_name(frags[1])["estimate"]
    assert any(
        e.src_id == caller and e.dst_id == callee for e in _edges(after, EdgeLabel.CALLS)
    )
    # The pre-existing cross-file CALLS edge set is preserved (same dedup keys).
    before_calls = {e.dedup_key for e in _edges(before, EdgeLabel.CALLS)}
    after_calls = {e.dedup_key for e in _edges(after, EdgeLabel.CALLS)}
    assert before_calls <= after_calls
