"""Golden tests for the Java / C# / Go language providers (optional grammars)."""

from __future__ import annotations

import pytest

from cce.domain.enums import EdgeLabel, NodeLabel


def _extract(path: str, source: bytes):
    from cce.indexing.extractor import Extractor

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


JAVA_SOURCE = b'''
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
'''

GO_SOURCE = b'''
package main

func helper(x int) int { return x + 1 }

func setup(r *Engine) {
    r.GET("/ping", handler)
    n := helper(2)
    _ = n
}

func handler() {}
'''

CSHARP_SOURCE = b'''
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
'''


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
