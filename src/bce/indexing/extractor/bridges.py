"""Cross-language bridge extraction (heuristic, feature: cross-language edges).

Native mobile stacks connect code across language boundaries that no single-language AST can see:

- **React Native legacy bridge**: ``RCT_EXPORT_METHOD(foo:...)`` in ObjC/ObjC++ exposes a native
  method to JS under the module name; JS calls it via ``NativeModules.<Module>.foo(...)``.
- **Swift <-> ObjC**: ``@objc(fooBar:)`` / ``@objc func fooBar`` exposes a Swift symbol to the ObjC
  runtime under a selector; ObjC code calls it by that selector.
- **Expo Modules DSL**: ``Function("foo") { ... }`` / ``AsyncFunction("foo")`` inside a Swift/Kotlin
  ``ModuleDefinition`` exposes ``foo`` to JS.
- **RN event channels**: ``sendEvent(withName: "onX", ...)`` (native) paired with
  ``addListener("onX")`` (JS) is a de-facto call edge.

Every edge produced here is inferred from naming conventions, not exact resolution, so it is tagged
``provenance='heuristic'`` with a specific ``synthesized_by`` tag. Extraction is regex-based (no
grammar dependency for ObjC/Swift/Kotlin) and fully deterministic: inputs are processed in sorted
order and outputs are de-duplicated/sorted by :meth:`GraphFragment.deduped`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from bce.domain.enums import EdgeLabel, Provenance
from bce.domain.models import GraphEdge, GraphFragment

_NATIVE_LANGS = {"objc", "objcpp", "swift", "kotlin"}
_JS_LANGS = {"javascript", "typescript"}

# --- native-side export patterns ---
_RCT_EXPORT = re.compile(r"RCT_EXPORT_METHOD\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)")
_RCT_REMAP = re.compile(
    r"RCT_REMAP_METHOD\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)"
)
_OBJC_ANNOTATED = re.compile(r"@objc\s*\(\s*([A-Za-z_][A-Za-z0-9_:]*)\s*\)")
_SWIFT_FUNC = re.compile(r"@objc\b[^\n]*\bfunc\s+([A-Za-z_][A-Za-z0-9_]*)")
_EXPO_FUNC = re.compile(r"(?:Async)?Function\s*\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")
_SEND_EVENT = re.compile(r"sendEvent\s*\(\s*withName:\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")

# --- JS-side consumption patterns ---
_NATIVE_MODULE_CALL = re.compile(
    r"NativeModules\.[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_ADD_LISTENER = re.compile(r"addListener\s*\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")

_NATIVE_EXTS = (".m", ".mm", ".swift", ".kt")
_JS_EXTS = (".js", ".jsx", ".ts", ".tsx")

# synthesized_by tags (stable identifiers used in provenance auditing).
BRIDGE_RN = "rn-bridge"
BRIDGE_SWIFT_OBJC = "swift-objc-bridge"
BRIDGE_EXPO = "expo-module-extract"
BRIDGE_RN_EVENT = "rn-event-channel"


@dataclass(slots=True)
class BridgeFile:
    """A source file participating in cross-language bridge detection."""

    path: str
    text: str


@dataclass(slots=True, frozen=True)
class SymbolRef:
    """A resolved symbol: its id + language (language distinguishes bridge endpoints)."""

    symbol_id: str
    language: str


#: Resolver: symbol name -> list of matching symbols (repo-scoped, possibly across languages).
SymbolResolver = Callable[[str], list[SymbolRef]]


def extract_bridges(files: list[BridgeFile], resolve: SymbolResolver) -> GraphFragment:
    """Detect cross-language bridges over a set of files and return synthesized edges.

    ``resolve`` maps a symbol *name* to the resolved symbols carrying it (across languages). For each
    exposed native symbol that a JS side consumes by the same name, we emit a JS -> native CALLS edge
    tagged ``heuristic`` + the bridge's ``synthesized_by``. Only real, resolvable endpoints are used;
    no nodes are fabricated.
    """
    exposed = _collect_native_exports(files)  # name -> synthesized_by tag
    consumed: dict[str, str] = {}
    for name, tag in _iter_js_consumers(files):
        consumed.setdefault(name, tag)

    fragment = GraphFragment()
    for name in sorted(set(exposed) & set(consumed)):
        tag = exposed[name]
        refs = resolve(name)
        native = sorted((r for r in refs if r.language in _NATIVE_LANGS), key=lambda r: r.symbol_id)
        js = sorted((r for r in refs if r.language in _JS_LANGS), key=lambda r: r.symbol_id)
        if not native or not js:
            continue
        for js_ref in js:
            for native_ref in native:
                if js_ref.symbol_id == native_ref.symbol_id:
                    continue
                fragment.add_edge(
                    GraphEdge(
                        EdgeLabel.CALLS,
                        js_ref.symbol_id,
                        native_ref.symbol_id,
                        provenance=Provenance.HEURISTIC,
                        synthesized_by=tag,
                    )
                )
    return fragment.deduped()


def _collect_native_exports(files: list[BridgeFile]) -> dict[str, str]:
    """Map each exposed native symbol name to the bridge tag that exposed it (deterministic)."""
    exposed: dict[str, str] = {}

    def add(name: str, tag: str) -> None:
        # First tag wins for a given name (sorted file order makes this deterministic).
        exposed.setdefault(name, tag)

    for f in sorted(files, key=lambda x: x.path):
        if not f.path.lower().endswith(_NATIVE_EXTS):
            continue
        for m in _RCT_EXPORT.finditer(f.text):
            add(m.group(1), BRIDGE_RN)
        for m in _RCT_REMAP.finditer(f.text):
            add(m.group(2), BRIDGE_RN)
        for m in _OBJC_ANNOTATED.finditer(f.text):
            add(m.group(1).split(":")[0], BRIDGE_SWIFT_OBJC)
        for m in _SWIFT_FUNC.finditer(f.text):
            add(m.group(1), BRIDGE_SWIFT_OBJC)
        for m in _EXPO_FUNC.finditer(f.text):
            add(m.group(1), BRIDGE_EXPO)
        for m in _SEND_EVENT.finditer(f.text):
            add(m.group(1), BRIDGE_RN_EVENT)
    return exposed


def _iter_js_consumers(files: list[BridgeFile]):
    """Yield ``(name, synthesized_by)`` for each JS-side bridge consumption, in file+match order."""
    for f in sorted(files, key=lambda x: x.path):
        if not f.path.lower().endswith(_JS_EXTS):
            continue
        for m in _NATIVE_MODULE_CALL.finditer(f.text):
            yield m.group(1), BRIDGE_RN
        for m in _ADD_LISTENER.finditer(f.text):
            yield m.group(1), BRIDGE_RN_EVENT
