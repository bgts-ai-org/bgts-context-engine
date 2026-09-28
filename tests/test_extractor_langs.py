"""Golden tests for the Java / C# / Go language providers (optional grammars)."""

from __future__ import annotations

import pytest

from bce.domain.enums import EdgeLabel, NodeLabel


def _extract(path: str, source: bytes):
    from bce.indexing.extractor import Extractor

    return Extractor().extract_file(repo_id="demo", path=path, source=source)


def _symbol_names(frag):
    return {n.properties["name"] for n in frag.nodes if n.label is NodeLabel.SYMBOL}


def _routes(frag):
    return {
        (n.properties["http_method"], n.properties["path_pattern"], n.properties["framework"])
        for n in frag.nodes
        if n.label is NodeLabel.ROUTE
    }


def _calls(frag):
    return [e for e in frag.edges if e.label is EdgeLabel.CALLS]


JAVA_SOURCE = b"""
package com.example.api;

class UserController {
    @GetMapping("/users")
    public String list() {
        return helper();
    }

    private String helper() {
        return "ok";
    }
}
"""

GO_SOURCE = b"""
package main

func helper(x int) int { return x + 1 }

func setup(r *Engine) {
    r.GET("/ping", handler)
    n := helper(2)
    _ = n
}

func handler() {}
"""

CSHARP_SOURCE = b"""
namespace Api {
    public class UserController {
        [HttpGet("/users")]
        public string List() {
            return Helper();
        }

        private string Helper() {
            return "ok";
        }
    }
}
"""


def test_java_provider_symbols_routes_calls():
    pytest.importorskip("tree_sitter_java")
    frag = _extract("UserController.java", JAVA_SOURCE)
    names = _symbol_names(frag)
    assert {"UserController", "list", "helper"} <= names
    assert ("GET", "/users", "spring") in _routes(frag)
    assert _calls(frag), "expected a CALLS edge list->helper"


def test_go_provider_symbols_routes_calls():
    pytest.importorskip("tree_sitter_go")
    frag = _extract("main.go", GO_SOURCE)
    names = _symbol_names(frag)
    assert {"helper", "setup", "handler"} <= names
    assert ("GET", "/ping", "gin") in _routes(frag)
    assert _calls(frag)


def test_csharp_provider_symbols_routes():
    pytest.importorskip("tree_sitter_c_sharp")
    frag = _extract("UserController.cs", CSHARP_SOURCE)
    names = _symbol_names(frag)
    assert {"UserController", "List", "Helper"} <= names
    assert ("GET", "/users", "aspnet") in _routes(frag)


def test_java_extraction_deterministic():
    pytest.importorskip("tree_sitter_java")
    a = _extract("UserController.java", JAVA_SOURCE)
    b = _extract("UserController.java", JAVA_SOURCE)
    assert [n.dedup_key for n in a.nodes] == [n.dedup_key for n in b.nodes]
    assert [e.dedup_key for e in a.edges] == [e.dedup_key for e in b.edges]


# --- nested types, enums, records, constants (Guava / Netty / EF Core / PowerShell shapes) -----


def _symbols(frag) -> dict[str, dict]:
    """``symbol_id -> properties`` for every Symbol node."""
    return {n.node_id: n.properties for n in frag.nodes if n.label is NodeLabel.SYMBOL}


def _by_container(frag) -> set[tuple[str, str, str]]:
    """``(namespace, name, kind)`` triples - the container chain is what a task names."""
    return {(p["namespace"], p["name"], p["kind"]) for p in _symbols(frag).values()}


def _call_pairs(frag, symbols) -> set[tuple[str, str]]:
    def label(sid: str) -> str:
        p = symbols[sid]
        return f"{p['namespace']}::{p['name']}"

    return {(label(e.src_id), label(e.dst_id)) for e in _calls(frag)}


JAVA_NESTED_SOURCE = b"""
package com.example.cache;

import java.util.List;

public class LocalCache<K, V> {
    static final int MAX_SEGMENTS = 1 << 16;
    private static final Logger logger = Logger.getLogger(LocalCache.class.getName());
    private int count;

    public V get(K key) {
        return Segment.lookup(key);
    }

    static class Segment<K, V> {
        static V lookup(Object key) {
            return LoadingValueReference.wait(key);
        }

        static final class LoadingValueReference<K, V> {
            static V wait(Object key) { return null; }
        }
    }

    enum Strength {
        STRONG {
            @Override Equivalence<Object> defaultEquivalence() { return Equivalence.equals(); }
        },
        WEAK;

        abstract Equivalence<Object> defaultEquivalence();

        static Strength parse(String name) { return valueOf(name); }
    }

    interface StatsCounter {
        int LIMIT = 10;
        void recordHits(int count);
    }

    record Entry(String key, int weight) {
        static Entry of(String key) { return new Entry(key, 1); }
    }

    @interface Weak {
        String value() default "";
    }
}
"""


def test_java_nested_types_enum_bodies_records_and_constants():
    pytest.importorskip("tree_sitter_java")
    frag = _extract("src/main/java/com/example/cache/LocalCache.java", JAVA_NESTED_SOURCE)
    triples = _by_container(frag)
    # Nested types carry their container chain; their members hang off the chain.
    assert ("LocalCache", "Segment", "class") in triples
    assert ("LocalCache.Segment", "lookup", "method") in triples
    assert ("LocalCache.Segment", "LoadingValueReference", "class") in triples
    assert ("LocalCache.Segment.LoadingValueReference", "wait", "method") in triples
    # Enum: the type, the methods in its body (after the constants), the record and annotation.
    assert ("LocalCache", "Strength", "enum") in triples
    assert ("LocalCache.Strength", "parse", "method") in triples
    assert ("LocalCache.Strength", "defaultEquivalence", "method") in triples
    assert ("LocalCache", "Entry", "class") in triples
    assert ("LocalCache.Entry", "of", "method") in triples
    assert ("LocalCache", "Weak", "interface") in triples
    # static / interface constants become symbols; instance fields do not.
    assert ("LocalCache", "MAX_SEGMENTS", "constant") in triples
    assert ("LocalCache", "logger", "field") in triples
    assert ("LocalCache.StatsCounter", "LIMIT", "constant") in triples
    assert not any(name == "count" for _, name, _ in triples)
    # Static calls through a nested type resolve inside the file.
    pairs = _call_pairs(frag, _symbols(frag))
    assert ("LocalCache::get", "LocalCache.Segment::lookup") in pairs
    assert (
        "LocalCache.Segment::lookup",
        "LocalCache.Segment.LoadingValueReference::wait",
    ) in pairs
    # Every symbol id is unique - the same simple name in two containers never collides.
    ids = [n.node_id for n in frag.nodes if n.label is NodeLabel.SYMBOL]
    assert len(ids) == len(set(ids))


CSHARP_NESTED_SOURCE = b"""
#if !UNIX
using System;
using System.Collections.Generic;

namespace Microsoft.Data.Query
{
    public static class SelectExpression
    {
        public const string Key = "select";
        public static readonly int MaxTables = 64;
        private int _instance;

        public enum Mode { Fast, Safe }
        public delegate void Handler(object sender);
        public record Point(int X, int Y);
        public struct Pair { public static Pair Zero() => default; }

        private sealed class Helper
        {
            public void Run()
            {
                Visitor.Visit();
                SelectExpression.Helper.Deep.Run2();
            }

            public static class Deep { public static void Run2() { } }
        }

        internal static class Visitor
        {
#if NET8_0
            public static void Visit() { Console.WriteLine(Key); }
#else
            public static void Visit() { }
#endif
        }

        public static void Process(bool async)
        {
            Visitor.Visit();
            if (async)
            {
            }
#if !UNIX
            else if (Key.Length > 0)
            {
                Helper.Deep.Run2();
            }
#endif
            else
            {
            }
        }
    }
}
#endif
"""


def test_csharp_preproc_regions_nested_types_enums_and_constants():
    pytest.importorskip("tree_sitter_c_sharp")
    frag = _extract("src/Query/SelectExpression.cs", CSHARP_NESTED_SOURCE)
    triples = _by_container(frag)
    # The whole file sits under ``#if !UNIX``: everything in it is still indexed.
    assert ("Microsoft.Data.Query", "SelectExpression", "class") in triples
    assert ("SelectExpression", "Process", "method") in triples
    # Nested types and their members, by container chain.
    assert ("SelectExpression", "Helper", "class") in triples
    assert ("SelectExpression.Helper", "Run", "method") in triples
    assert ("SelectExpression.Helper", "Deep", "class") in triples
    assert ("SelectExpression.Helper.Deep", "Run2", "method") in triples
    # A member declared once per ``#if`` branch is one symbol; enum / delegate / record / struct.
    visits = [
        sid
        for sid, p in _symbols(frag).items()
        if p["namespace"] == "SelectExpression.Visitor" and p["name"] == "Visit"
    ]
    assert len(visits) == 1
    assert ("SelectExpression", "Mode", "enum") in triples
    assert ("SelectExpression", "Handler", "type") in triples
    assert ("SelectExpression", "Point", "class") in triples
    assert ("SelectExpression.Pair", "Zero", "method") in triples
    # const -> constant, static -> field, instance fields are not symbols.
    assert ("SelectExpression", "Key", "constant") in triples
    assert ("SelectExpression", "MaxTables", "field") in triples
    assert not any(name == "_instance" for _, name, _ in triples)
    # Static calls on a type declared in the file resolve, including through a nested chain.
    pairs = _call_pairs(frag, _symbols(frag))
    assert ("SelectExpression::Process", "SelectExpression.Visitor::Visit") in pairs
    assert ("SelectExpression.Helper::Run", "SelectExpression.Visitor::Visit") in pairs
    assert ("SelectExpression.Helper::Run", "SelectExpression.Helper.Deep::Run2") in pairs
    assert ("SelectExpression::Process", "SelectExpression.Helper.Deep::Run2") in pairs
    # ``using`` directives under the ``#if`` are imports.
    imports = {e.dst_id for e in frag.edges if e.label is EdgeLabel.IMPORTS}
    assert any(m.endswith("System.Collections.Generic") for m in imports)


CSHARP_UNPARSEABLE_SOURCE = b"""
namespace Demo;

public class Printer
{
    public void Append(string value, bool async)
    {
        var quoted = $@\"\"\"{value}\"\"\";
        _builder.Append(quoted ?? "Unknown");
        _visited?[0] = value;
        _groups.Add(value, [value]);
        Log(quoted, async: async);
    }

    public string Unaffected() => "ok";
}
"""


def test_csharp_parse_recovery_keeps_positions_and_recovers_symbols():
    """Constructs the grammar rejects (``async`` as an identifier, null-conditional assignment,
    collection expressions, ``$@\"\"\"x\"\"\"``) no longer take the rest of the file with them."""
    pytest.importorskip("tree_sitter_c_sharp")
    from bce.indexing.parser.languages.csharp_provider import (
        CSharpProvider,
        _error_count,
        _repair_source,
    )

    provider = CSharpProvider()
    raw = provider._parser.parse(CSHARP_UNPARSEABLE_SOURCE)
    assert raw.root_node.has_error, "fixture must exercise the recovery path"
    repaired = _repair_source(CSHARP_UNPARSEABLE_SOURCE)
    assert len(repaired) == len(CSHARP_UNPARSEABLE_SOURCE)
    assert repaired.count(b"\n") == CSHARP_UNPARSEABLE_SOURCE.count(b"\n")
    tree = provider.parse(CSHARP_UNPARSEABLE_SOURCE)
    assert _error_count(tree.root_node) < _error_count(raw.root_node)

    frag = _extract("src/Printer.cs", CSHARP_UNPARSEABLE_SOURCE)
    symbols = _symbols(frag)
    names = {(p["namespace"], p["name"]) for p in symbols.values()}
    assert {("Demo", "Printer"), ("Printer", "Append"), ("Printer", "Unaffected")} <= names
    # Positions and text are read from the *original* source.
    append = next(p for p in symbols.values() if p["name"] == "Append")
    assert append["line"] == 6
    assert append["signature"] == "(string value, bool async)"
    assert b"Unknown" in CSHARP_UNPARSEABLE_SOURCE and "Unknown" in (append["body"] or "")


def test_csharp_repair_leaves_valid_code_alone():
    pytest.importorskip("tree_sitter_c_sharp")
    from bce.indexing.parser.languages.csharp_provider import _repair_source

    valid = b"""
class A {
    async Task M(int[] xs, [NotNull] string s) {
        await Task.Run(async () => 1);
        var t = xs?[0];
        var v = @"a ""b"" c" + \"\"\"
            raw "text"
            \"\"\";
        Func<int, Task> f = async x => await N(x);
    }
}
"""
    assert _repair_source(valid) == valid
