"""Golden extraction tests for the JS/TS providers (multi-language proof: same extractor logic)."""

from conftest import has_edge, module_namespaces, symbols_by_name

from cce.domain.enums import EdgeLabel, SymbolKind
from cce.indexing.extractor import Extractor

JS_SOURCE = b"""import { foo } from "./util";

const PI = 3.14;

function helper(x) {
  return x + 1;
}

class Base {
  greet() {
    return "hi";
  }
}

class Service extends Base {
  run() {
    return helper(PI);
  }
}
"""

TS_SOURCE = b"""interface Greeter {
  greet(): string;
}

function helper(): string {
  return "hi";
}

export class Service implements Greeter {
  greet(): string {
    return helper();
  }
}
"""


def test_javascript_symbols_and_edges():
    frag = Extractor().extract_file(repo_id="demo", path="src/app.js", source=JS_SOURCE)
    names = symbols_by_name(frag)
    assert {"PI", "helper", "Base", "greet", "Service", "run"} <= set(names)
    assert has_edge(frag, EdgeLabel.INHERITS, names["Service"], names["Base"])
    assert has_edge(frag, EdgeLabel.CALLS, names["run"], names["helper"])
    # Relative specifiers are normalized against the importing file's directory (linker input).
    assert "src.util" in module_namespaces(frag)


def test_javascript_const_kind():
    frag = Extractor().extract_file(repo_id="demo", path="src/app.js", source=JS_SOURCE)
    pi = next(n for n in frag.nodes if n.properties.get("name") == "PI")
    assert pi.properties["kind"] == str(SymbolKind.CONSTANT)


def test_typescript_interface_and_call():
    frag = Extractor().extract_file(repo_id="demo", path="src/app.ts", source=TS_SOURCE)
    names = symbols_by_name(frag)
    assert "Greeter" in names
    assert "Service" in names
    assert "helper" in names
    greeter = next(n for n in frag.nodes if n.properties.get("name") == "Greeter")
    assert greeter.properties["kind"] == str(SymbolKind.INTERFACE)
    assert has_edge(frag, EdgeLabel.CALLS, names["greet"], names["helper"])


def test_typescript_language_tag():
    frag = Extractor().extract_file(repo_id="demo", path="src/app.ts", source=TS_SOURCE)
    file_node = next(n for n in frag.nodes if n.label.value == "File")
    assert file_node.properties["language"] == "typescript"
