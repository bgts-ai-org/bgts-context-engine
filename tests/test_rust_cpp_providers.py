"""Rust and C/C++ providers: symbols, intra-file edges, import bindings and cross-file links."""

from __future__ import annotations

import pytest

from bce.domain.enums import EdgeLabel, NodeLabel
from bce.indexing.extractor.extractor import Extractor
from bce.indexing.linker.linker import link_fragments
from bce.indexing.parser.registry import build_default_registry

pytest.importorskip("tree_sitter_rust")
pytest.importorskip("tree_sitter_cpp")

from bce.indexing.parser.languages.rust_provider import crate_and_package  # noqa: E402

RUST_ENGINE = b"""
/// Runs jobs.
pub struct Engine { jobs: usize }

impl Engine {
    pub fn start() -> Self { Engine { jobs: limit() } }
    fn stop(&self) { self.flush() }
    fn flush(&self) {}
}

pub const LIMIT: usize = 4;
fn limit() -> usize { LIMIT }
"""

RUST_CLI = b"""
use crate::engine::{Engine, LIMIT as MAX};
use super::util::*;

pub trait Command { fn run(&self) -> u8; }

pub struct Cli;

impl Command for Cli {
    fn run(&self) -> u8 { let e = Engine::start(); report!(e); println!("{}", helper(MAX)); 0 }
}

macro_rules! report { ($e:expr) => { () }; }
fn helper(v: usize) -> usize { v }
"""

CPP_HEADER = b"""
#ifndef PARSER_H
#define PARSER_H
#include <vector>
namespace dom {
/** Parses documents. */
class parser : public base {
public:
  int parse(const char *buf) { return this->depth() + count(buf); }
  int depth() const;
};
int count(const char *buf);
}
#endif
"""

CPP_SOURCE = b"""
#include "parser.h"
#define MAX_DEPTH 1024
namespace dom {
int parser::depth() const { return MAX_DEPTH; }
int count(const char *buf) { return helper(buf); }
static int helper(const char *b) { return 0; }
}
"""

CPP_USER = b"""
#include "dom/parser.h"
int main() { return count("x"); }
"""


def _extract(path: str, source: bytes):
    return Extractor().extract_file(repo_id="r", path=path, source=source)


def _symbols(frag) -> dict[str, dict]:
    return {
        n.node_id.split("#", 1)[0]: n.properties
        for n in frag.nodes
        if n.label is NodeLabel.SYMBOL and n.properties.get("kind") != "module"
    }


def _edges(frags, label: EdgeLabel) -> set[tuple[str, str]]:
    def short(sid: str) -> str:
        return sid.split("#", 1)[0].split("::", 1)[-1]

    out = set()
    for frag in frags:
        for e in frag.edges:
            if e.label is label:
                out.add((short(e.src_id), short(e.dst_id)))
    return out


def test_the_registry_lists_rust_and_cpp():
    registry = build_default_registry()
    assert {"rust", "cpp"} <= set(registry.languages())
    for ext in (".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".hh"):
        assert ext in registry.supported_extensions()


def test_a_rust_files_package_is_its_module_path_in_the_crate():
    assert crate_and_package("src/engine/run.rs") == ("", "engine.run")
    assert crate_and_package("src/engine/mod.rs") == ("", "engine")
    assert crate_and_package("src/lib.rs") == ("", "")
    assert crate_and_package("crates/nu-protocol/src/value/mod.rs") == (
        "nu_protocol",
        "nu_protocol.value",
    )


def test_rust_items_impl_methods_and_doc_comments():
    frag = _extract("src/engine.rs", RUST_ENGINE)
    symbols = _symbols(frag)
    assert symbols["rust::engine::::Engine"]["kind"] == "class"
    assert symbols["rust::engine::::Engine"]["docstring"] == "Runs jobs."
    assert symbols["rust::engine::Engine::start"]["kind"] == "method"
    assert symbols["rust::engine::::LIMIT"]["kind"] == "constant"
    calls = _edges([frag], EdgeLabel.CALLS)
    assert ("engine::Engine::start", "engine::::limit") in calls
    assert ("engine::Engine::stop", "engine::Engine::flush") in calls
    assert ("engine::::limit", "engine::::LIMIT") in _edges([frag], EdgeLabel.REFERENCES)
    assert {"Engine", "Engine.start", "LIMIT", "limit"} <= set(frag.link_data.exports)


def test_rust_use_trees_become_bindings_and_link_across_files():
    engine = _extract("src/engine.rs", RUST_ENGINE)
    cli = _extract("src/cmd/cli.rs", RUST_CLI)
    bindings = {(b.local_name, b.module_path, b.imported_name) for b in cli.link_data.imports}
    assert ("Engine", "engine", "Engine") in bindings
    assert ("MAX", "engine", "LIMIT") in bindings
    assert ("*", "cmd.util", None) in bindings  # super:: of cmd::cli is cmd
    symbols = _symbols(cli)
    assert symbols["rust::cmd.cli::Cli::run"]["kind"] == "method"
    assert ("cmd.cli::::Cli", "cmd.cli::::Command") in _edges([cli], EdgeLabel.IMPLEMENTS)
    calls = _edges([cli], EdgeLabel.CALLS)
    assert ("cmd.cli::Cli::run", "cmd.cli::::report") in calls  # the file's own macro
    assert ("cmd.cli::Cli::run", "cmd.cli::::helper") in calls  # a call inside println!
    assert not any(dst.endswith("println") for _, dst in calls)
    linked = link_fragments([engine, cli])
    assert ("cmd.cli::Cli::run", "engine::Engine::start") in _edges([linked], EdgeLabel.CALLS)


def test_cpp_classes_methods_macros_and_include_guards():
    frag = _extract("src/dom/parser.h", CPP_HEADER)
    symbols = _symbols(frag)
    assert symbols["cpp::src.dom.parser::::parser"]["kind"] == "class"
    assert symbols["cpp::src.dom.parser::::parser"]["docstring"] == "Parses documents."
    assert symbols["cpp::src.dom.parser::parser::parse"]["kind"] == "method"
    # a declaration without a body is not a symbol: the definition is
    assert "cpp::src.dom.parser::parser::depth" not in symbols
    assert "cpp::src.dom.parser::::count" not in symbols
    unresolved = {(u.name, u.qualifier, u.kind) for u in frag.link_data.unresolved}
    assert ("depth", "parser", "call") in unresolved
    assert ("base", None, "inherits") in unresolved
    # <vector> is the standard library, not a file to bind
    assert all("vector" not in b.module_path for b in frag.link_data.imports)


def test_cpp_header_and_source_share_a_package_and_link():
    header = _extract("src/dom/parser.h", CPP_HEADER)
    source = _extract("src/dom/parser.cpp", CPP_SOURCE)
    user = _extract("tools/main.cpp", CPP_USER)
    symbols = _symbols(source)
    assert symbols["cpp::src.dom.parser::parser::depth"]["kind"] == "method"
    assert symbols["cpp::src.dom.parser::::MAX_DEPTH"]["line"] == 3
    assert symbols["cpp::src.dom.parser::::MAX_DEPTH"]["end_line"] == 3
    assert ("src.dom.parser::::count", "src.dom.parser::::helper") in _edges(
        [source], EdgeLabel.CALLS
    )
    linked = link_fragments([header, source, user])
    calls = _edges([linked], EdgeLabel.CALLS)
    # the header's inline method reaches the out-of-line definition in the .cpp
    assert ("src.dom.parser::parser::parse", "src.dom.parser::parser::depth") in calls
    # an include written from another include root still finds the defining file
    assert ("tools.main::::main", "src.dom.parser::::count") in calls
