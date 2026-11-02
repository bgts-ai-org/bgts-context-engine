"""C and C++ language provider (tree-sitter, one C++ grammar for both).

Scope: File + Symbol nodes - function definitions (free, ``Class::method`` out of line, inline in
a class body, templates), classes / structs / unions / enums with a body, typedefs and ``using``
aliases, ``#define`` macros and file-scope variables - and DEFINED_IN / BELONGS_TO / IMPORTS /
INHERITS / CALLS / REFERENCES edges with intra-file resolution. Declarations without a body
(prototypes, in-class method declarations) are not symbols: the definition is.

A file's package is its path without the extension, so ``parser.h`` and ``parser.cpp`` share
one and a method defined in the ``.cpp`` resolves against the class in the header. Each
``#include`` becomes a wildcard import of the included file's package (relative to the including
file, and as written, which the linker matches as a package suffix), so a call to a function a
header declares links to the file defining it. Namespaces do not enter ids: a method is
``Class.method``, a free function its bare name. Calls on a receiver whose type the file does
not show (``v.push_back()``) are not linked.

The grammar (``tree-sitter-cpp``) is an optional dependency; the registry only registers this
provider when the grammar import succeeds.
"""

from __future__ import annotations

import posixpath
from typing import Any

from bce.domain.enums import EdgeLabel, NodeLabel, RefKind, SymbolKind
from bce.domain.models import (
    FragmentLinkData,
    GraphEdge,
    GraphFragment,
    GraphNode,
    ImportBinding,
    UnresolvedRef,
)
from bce.indexing.parser._treesitter import (
    body_snippet,
    load_language,
    make_parser,
    node_text,
    search_text,
    span_properties,
)
from bce.indexing.parser.base import LanguageProvider, ParseContext
from bce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_REF_KIND_RANK = {RefKind.READ: 0, RefKind.PASS: 1, RefKind.WRITE: 2, RefKind.DEFINE: 3}

_EXTENSIONS = (".c", ".h", ".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp")
_TYPE_KINDS: dict[str, str] = {
    "class_specifier": str(SymbolKind.CLASS),
    "struct_specifier": str(SymbolKind.CLASS),
    "union_specifier": str(SymbolKind.CLASS),
    "enum_specifier": str(SymbolKind.ENUM),
}
#: Containers whose children are walked as file-scope items.
_SCOPES = frozenset(
    {
        "namespace_definition",
        "linkage_specification",
        "declaration_list",
        "preproc_if",
        "preproc_ifdef",
        "preproc_else",
        "preproc_elif",
        "preproc_elifdef",
        "template_declaration",
    }
)
_STOP = frozenset({"function_definition", "lambda_expression", *_TYPE_KINDS})
_DECLARATORS = frozenset(
    {
        "pointer_declarator",
        "reference_declarator",
        "init_declarator",
        "array_declarator",
        "attributed_declarator",
        "parenthesized_declarator",
    }
)


class _Def:
    __slots__ = ("symbol_id", "name", "kind", "body", "owner")

    def __init__(self, symbol_id, name, kind, body, owner):
        self.symbol_id = symbol_id
        self.name = name
        self.kind = kind
        self.body = body
        self.owner = owner


class _State:
    def __init__(self) -> None:
        self.symbols: dict[str, str] = {}
        self.methods: dict[str, dict[str, str]] = {}
        self.exports: dict[str, str] = {}
        self.defs: list[_Def] = []
        self.unresolved: list[UnresolvedRef] = []
        self.bases: list[tuple[str, str, int]] = []


class CppProvider(LanguageProvider):
    language = "cpp"
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        import tree_sitter_cpp

        self._grammar = load_language(tree_sitter_cpp)
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return _EXTENSIONS

    def parse(self, source: bytes) -> Any:
        return self._parser.parse(source)

    def derive_package(self, ctx: ParseContext) -> str | None:
        return _package_of(ctx.path) or None

    def extract(self, tree: Any, ctx: ParseContext) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        package = ctx.package or ""
        frag.add_node(
            GraphNode(
                NodeLabel.FILE,
                ctx.file_id,
                {
                    "path": ctx.path.replace("\\", "/"),
                    "language": self.language,
                    "repo_id": ctx.repo_id,
                    "indexed_at_commit": ctx.indexed_at_commit,
                },
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.BELONGS_TO, ctx.file_id, ctx.repo_id))

        st = _State()
        bindings: list[ImportBinding] = []
        self._items(tree.root_node, ctx, package, "", source, frag, st, bindings)
        for src, base, line in st.bases:
            target = st.symbols.get(base)
            if target is not None and target != src:
                frag.add_edge(GraphEdge(EdgeLabel.INHERITS, src, target))
            else:
                st.unresolved.append(UnresolvedRef(src, base, kind="inherits", line=line))

        for d in st.defs:
            if d.body is not None:
                self._calls(d, source, st, frag)
                self._references(d, source, st, frag)

        frag.link_data = FragmentLinkData(
            file_id=ctx.file_id,
            package=package or None,
            language=self.language,
            exports=st.exports,
            imports=bindings,
            unresolved=st.unresolved,
        )
        return frag

    # --- pass 1: items ---

    def _items(self, container, ctx, package, owner, source, frag, st, bindings) -> None:
        for child in container.named_children:
            kind = child.type
            if kind == "function_definition":
                self._function(child, ctx, package, owner, source, frag, st)
            elif kind in _TYPE_KINDS:
                self._type(child, ctx, package, owner, source, frag, st, bindings)
            elif kind == "declaration" and not owner:
                self._declaration(child, ctx, package, source, frag, st, bindings)
            elif kind == "type_definition":
                self._typedef(child, ctx, package, source, frag, st, bindings)
            elif kind == "alias_declaration":
                name = _field_text(child, "name", source)
                if name:
                    d = self._symbol(child, ctx, package, "", name, "type", None, source, frag)
                    self._register(st, d)
            elif kind in ("preproc_def", "preproc_function_def"):
                name = _field_text(child, "name", source)
                if name:
                    params = child.child_by_field_name("parameters")
                    signature = node_text(params, source) if params is not None else None
                    macro_kind = "macro" if params is not None else str(SymbolKind.CONSTANT)
                    d = self._symbol(
                        child, ctx, package, "", name, macro_kind, signature, source, frag
                    )
                    self._register(st, d)
            elif kind == "preproc_include" and not owner:
                self._include(child, ctx, package, source, frag, bindings)
            elif kind in _SCOPES:
                body = child.child_by_field_name("body")
                self._items(body or child, ctx, package, owner, source, frag, st, bindings)

    def _function(self, node, ctx, package, owner, source, frag, st: _State) -> None:
        declarator = _function_declarator(node.child_by_field_name("declarator"))
        if declarator is None:
            return
        target = declarator.child_by_field_name("declarator")
        qualified = _name_parts(target, source)
        if not qualified:
            return
        name = qualified[-1]
        cls = owner or (qualified[-2] if len(qualified) > 1 else "")
        params = declarator.child_by_field_name("parameters")
        signature = node_text(params, source) if params is not None else None
        kind = str(SymbolKind.METHOD) if cls else str(SymbolKind.FUNCTION)
        if cls and name in (cls, f"~{cls}"):
            kind = str(SymbolKind.CONSTRUCTOR)
        d = self._symbol(node, ctx, package, cls, name, kind, signature, source, frag)
        d.body = node.child_by_field_name("body")
        d.owner = cls or None
        if cls:
            st.methods.setdefault(cls, {}).setdefault(name, d.symbol_id)
            st.exports.setdefault(f"{cls}.{name}", d.symbol_id)
            st.defs.append(d)
        else:
            self._register(st, d)

    def _type(self, node, ctx, package, owner, source, frag, st, bindings, name=None) -> None:
        body = node.child_by_field_name("body")
        name = name or _field_text(node, "name", source)
        if body is None or not name:
            return
        d = self._symbol(
            node, ctx, package, owner, name, _TYPE_KINDS[node.type], None, source, frag
        )
        if owner:
            st.exports.setdefault(f"{owner}.{name}", d.symbol_id)
            st.defs.append(d)
        else:
            self._register(st, d)
        for clause in node.named_children:
            if clause.type == "base_class_clause":
                for base in clause.named_children:
                    parts = _name_parts(base, source)
                    if parts:
                        st.bases.append((d.symbol_id, parts[-1], node.start_point[0] + 1))
        if node.type == "enum_specifier":
            return
        for member in body.named_children:
            if member.type == "function_definition":
                self._function(member, ctx, package, name, source, frag, st)
            elif member.type in _TYPE_KINDS:
                self._type(member, ctx, package, name, source, frag, st, bindings)
            elif member.type == "template_declaration":
                self._items(member, ctx, package, name, source, frag, st, bindings)
            elif member.type == "field_declaration":
                inner = member.child_by_field_name("type")
                if inner is not None and inner.type in _TYPE_KINDS:
                    self._type(inner, ctx, package, name, source, frag, st, bindings)

    def _declaration(self, node, ctx, package, source, frag, st, bindings) -> None:
        """A file-scope declaration: a type defined in it, and its variables (not prototypes)."""
        inner = node.child_by_field_name("type")
        if inner is not None and inner.type in _TYPE_KINDS:
            self._type(inner, ctx, package, "", source, frag, st, bindings)
        if any(c.type == "storage_class_specifier" and node_text(c, source) == "extern"
               for c in node.named_children):  # fmt: skip
            return
        for declarator in node.children_by_field_name("declarator"):
            if _function_declarator(declarator) is not None:
                continue
            parts = _name_parts(declarator, source)
            if not parts:
                continue
            name = parts[-1]
            kind = str(SymbolKind.CONSTANT) if name.isupper() else str(SymbolKind.VARIABLE)
            d = self._symbol(node, ctx, package, "", name, kind, None, source, frag)
            self._register(st, d)

    def _typedef(self, node, ctx, package, source, frag, st, bindings) -> None:
        names = [p[-1] for p in (_name_parts(d, source) for d in
                 node.children_by_field_name("declarator")) if p]  # fmt: skip
        if not names:
            return
        inner = node.child_by_field_name("type")
        if inner is not None and inner.type in _TYPE_KINDS and inner.child_by_field_name("body"):
            # ``typedef struct { ... } pair_t;``: the struct is the typedef's name.
            tag = _field_text(inner, "name", source)
            self._type(inner, ctx, package, "", source, frag, st, bindings, name=tag or names[0])
            return
        for name in names:
            d = self._symbol(node, ctx, package, "", name, "type", None, source, frag)
            self._register(st, d)

    @staticmethod
    def _register(st: _State, d: _Def) -> None:
        st.symbols.setdefault(d.name, d.symbol_id)
        st.exports.setdefault(d.name, d.symbol_id)
        st.defs.append(d)

    def _symbol(self, node, ctx, package, namespace, name, kind, signature, source, frag) -> _Def:
        symbol_id = make_symbol_id(
            language=self.language,
            package=package,
            namespace=namespace,
            name=name,
            signature=signature,
            kind=kind,
        )
        static = any(
            c.type == "storage_class_specifier" and node_text(c, source) == "static"
            for c in node.named_children
        )
        body = node.child_by_field_name("body")
        symbol = GraphNode(
            NodeLabel.SYMBOL,
            symbol_id,
            {
                "name": name,
                "kind": kind,
                "signature": signature,
                "visibility": "private" if static else "public",
                "docstring": _doc_comment(node, source),
                "body": body_snippet(body, source),
                "namespace": namespace or package or "",
                "file_id": ctx.file_id,
                "line": node.start_point[0] + 1,
                **span_properties(node),
                "indexed_at_commit": ctx.indexed_at_commit,
            },
        )
        symbol.search_text = search_text(node, source)
        frag.add_node(symbol)
        frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, ctx.file_id))
        return _Def(symbol_id, name, kind, None, None)

    # --- includes ---

    def _include(self, node, ctx, package, source, frag, bindings) -> None:
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        system = path_node.type == "system_lib_string"
        raw = node_text(path_node, source).strip().strip('"<>').strip()
        if not raw or (system and "." not in raw.rsplit("/", 1)[-1]):
            return  # ``<vector>``: the standard library, not a file of the repository
        candidates = [raw]
        directory = posixpath.dirname(ctx.path.replace("\\", "/").lstrip("./"))
        if not system and directory:
            candidates.insert(0, posixpath.normpath(posixpath.join(directory, raw)))
        packages: list[str] = []
        for candidate in candidates:
            pkg = _package_of(candidate)
            if pkg and not candidate.startswith("..") and pkg not in packages:
                packages.append(pkg)
        if not packages:
            return
        # Slash form: the linker binds the module to the file whose package ends with it.
        namespace = _package_of(raw).replace(".", "/") or packages[0]
        module_id = make_module_id(ctx.repo_id, namespace)
        frag.add_node(
            GraphNode(NodeLabel.MODULE, module_id, {"namespace": namespace, "repo_id": ctx.repo_id})
        )
        frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))
        for pkg in packages:
            if pkg != package:
                bindings.append(ImportBinding(local_name="*", module_path=pkg))

    # --- pass 2: calls ---

    def _calls(self, d: _Def, source, st: _State, frag) -> None:
        seen: set[tuple[str, str | None]] = set()
        stack = [d.body]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call_expression":
                    self._call(d, child, source, st, frag, seen)
                if child.type not in _STOP:
                    stack.append(child)

    def _call(self, d: _Def, call, source, st: _State, frag, seen) -> None:
        fn = call.child_by_field_name("function")
        if fn is not None and fn.type == "template_function":
            fn = fn.child_by_field_name("name")
        if fn is None:
            return
        line = call.start_point[0] + 1
        qualifier: str | None = None
        if fn.type == "identifier":
            name = node_text(fn, source)
            target = st.methods.get(d.owner or "", {}).get(name) or st.symbols.get(name)
        elif fn.type == "qualified_identifier":
            parts = _name_parts(fn, source)
            if not parts:
                return
            name, qualifier = parts[-1], (parts[-2] if len(parts) > 1 else None)
            target = st.methods.get(qualifier or "", {}).get(name)
        elif fn.type == "field_expression":
            receiver = fn.child_by_field_name("argument")
            if receiver is None or receiver.type != "this" or not d.owner:
                return
            name, qualifier = _field_text(fn, "field", source), d.owner
            target = st.methods.get(d.owner, {}).get(name)
        else:
            return
        if not name:
            return
        if target is not None:
            if target != d.symbol_id:
                frag.add_edge(
                    GraphEdge(EdgeLabel.CALLS, d.symbol_id, target, properties={"line": line})
                )
            return
        if (name, qualifier) in seen:
            return
        seen.add((name, qualifier))
        st.unresolved.append(
            UnresolvedRef(d.symbol_id, name, qualifier=qualifier, kind="call", line=line)
        )

    # --- REFERENCES.ref_kind ---

    def _references(self, d: _Def, source, st: _State, frag) -> None:
        strongest: dict[str, RefKind] = {}
        pending: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = st.symbols.get(name)
            if target is None and not name[:1].isupper():
                return
            if target == d.symbol_id:
                return
            bucket, key = (strongest, target) if target is not None else (pending, name)
            current = bucket.get(key)
            if current is None or _REF_KIND_RANK[kind] > _REF_KIND_RANK[current]:
                bucket[key] = kind

        stack = [d.body]
        while stack:
            current = stack.pop()
            # the callee of a call is a CALLS edge, not a reference as well
            callee = (
                current.child_by_field_name("function")
                if current.type == "call_expression"
                else None
            )
            for child in current.named_children:
                if callee is not None and child.start_byte == callee.start_byte:
                    continue
                ctype = child.type
                if ctype in ("assignment_expression", "update_expression"):
                    left = child.child_by_field_name("left") or child.child_by_field_name(
                        "argument"
                    )
                    if left is not None and left.type == "identifier":
                        note(node_text(left, source), RefKind.WRITE)
                elif ctype in ("identifier", "type_identifier"):
                    note(node_text(child, source), RefKind.READ)
                if ctype not in _STOP:
                    stack.append(child)
        for target in sorted(strongest):
            frag.add_edge(
                GraphEdge(EdgeLabel.REFERENCES, d.symbol_id, target, ref_kind=strongest[target])
            )
        for name in sorted(pending):
            st.unresolved.append(
                UnresolvedRef(d.symbol_id, name, kind="reference", ref_kind=pending[name])
            )


def _package_of(path: str) -> str:
    """``include/simdjson/dom/parser.h`` -> ``include.simdjson.dom.parser``."""
    rel = path.replace("\\", "/").lstrip("./")
    stem, dot, ext = rel.rpartition(".")
    if dot and "/" not in ext:
        rel = stem
    return ".".join(p for p in rel.split("/") if p)


def _field_text(node, field: str, source: bytes) -> str:
    child = node.child_by_field_name(field)
    return node_text(child, source) if child is not None else ""


def _function_declarator(node):
    """The ``function_declarator`` inside a (pointer / reference) declarator, if any."""
    while node is not None:
        if node.type == "function_declarator":
            return node
        if node.type not in _DECLARATORS:
            return None
        node = node.child_by_field_name("declarator")
    return None


def _name_parts(node, source: bytes) -> list[str]:
    """``a::b::f`` -> ``[a, b, f]`` for a declarator / qualified name (templates unwrapped)."""
    while node is not None and node.type in _DECLARATORS:
        node = node.child_by_field_name("declarator")
    if node is None:
        return []
    kind = node.type
    if kind == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        head = _name_parts(scope, source) if scope is not None else []
        return head + _name_parts(node.child_by_field_name("name"), source)
    if kind in ("template_type", "template_function"):
        return _name_parts(node.child_by_field_name("name"), source)
    if kind in (
        "identifier",
        "field_identifier",
        "type_identifier",
        "namespace_identifier",
        "destructor_name",
        "operator_name",
        "primitive_type",
    ):
        return [node_text(node, source)]
    return []


def _doc_comment(node, source: bytes) -> str | None:
    """The ``/** */`` / ``///`` / ``//`` comment block directly above ``node``."""
    lines: list[str] = []
    expected = node.start_point[0]
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type == "comment":
        if sibling.end_point[0] < expected - 1:
            break
        text = node_text(sibling, source).strip()
        if text.startswith("/*"):
            body = text[2:-2].lstrip("*!")
            parts = [ln.strip().lstrip("*").strip() for ln in body.splitlines()]
            lines[:0] = [ln for ln in parts if ln]
        else:
            lines.insert(0, text.lstrip("/!").strip())
        expected = sibling.start_point[0]
        sibling = sibling.prev_named_sibling
    return "\n".join(lines).strip() or None
