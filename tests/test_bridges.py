"""Tests for cross-language bridge extraction (heuristic edges)."""

from __future__ import annotations

from bce.domain.enums import EdgeLabel, Provenance
from bce.indexing.extractor.bridges import (
    BRIDGE_RN,
    BRIDGE_RN_EVENT,
    BridgeFile,
    SymbolRef,
    extract_bridges,
)


def _resolver(table):
    def resolve(name):
        return table.get(name, [])

    return resolve


def test_rn_legacy_bridge_links_js_to_native():
    files = [
        BridgeFile("ios/MyModule.m", 'RCT_EXPORT_METHOD(doThing:(NSString *)a) { }'),
        BridgeFile("src/app.js", 'NativeModules.MyModule.doThing("x");'),
    ]
    table = {
        "doThing": [
            SymbolRef("id:objc:doThing", "objc"),
            SymbolRef("id:js:doThing", "javascript"),
        ]
    }
    frag = extract_bridges(files, _resolver(table))
    edges = [e for e in frag.edges if e.label is EdgeLabel.CALLS]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.src_id == "id:js:doThing"
    assert edge.dst_id == "id:objc:doThing"
    assert edge.provenance is Provenance.HEURISTIC
    assert edge.synthesized_by == BRIDGE_RN


def test_rn_event_channel_bridge():
    files = [
        BridgeFile("ios/Evt.swift", 'func send() { sendEvent(withName: "onPing", body: nil) }'),
        BridgeFile("src/listen.ts", 'emitter.addListener("onPing", cb);'),
    ]
    table = {
        "onPing": [SymbolRef("id:swift:onPing", "swift"), SymbolRef("id:ts:onPing", "typescript")]
    }
    frag = extract_bridges(files, _resolver(table))
    tags = {e.synthesized_by for e in frag.edges}
    assert BRIDGE_RN_EVENT in tags


def test_bridge_requires_both_sides_resolvable():
    files = [
        BridgeFile("ios/MyModule.m", 'RCT_EXPORT_METHOD(onlyNative:(NSString *)a) { }'),
        BridgeFile("src/app.js", 'NativeModules.MyModule.onlyNative("x");'),
    ]
    # Only native side resolves -> no edge (we never fabricate the JS endpoint).
    table = {"onlyNative": [SymbolRef("id:objc:onlyNative", "objc")]}
    frag = extract_bridges(files, _resolver(table))
    assert not frag.edges


def test_bridge_extraction_is_deterministic():
    files = [
        BridgeFile("ios/MyModule.m", 'RCT_EXPORT_METHOD(a:(int)x) { }\nRCT_EXPORT_METHOD(b:(int)x) { }'),
        BridgeFile("src/app.js", 'NativeModules.M.a(); NativeModules.M.b();'),
    ]
    table = {
        "a": [SymbolRef("id:objc:a", "objc"), SymbolRef("id:js:a", "javascript")],
        "b": [SymbolRef("id:objc:b", "objc"), SymbolRef("id:js:b", "javascript")],
    }
    r1 = extract_bridges(files, _resolver(table))
    r2 = extract_bridges(files, _resolver(table))
    assert [e.dedup_key for e in r1.edges] == [e.dedup_key for e in r2.edges]
