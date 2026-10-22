"""Rust language provider (tree-sitter).

Scope: File + Symbol nodes (functions, structs / unions, enums, traits, type aliases, consts and
statics, ``macro_rules!`` macros, and the methods of ``impl`` and ``trait`` blocks, also inside
inline ``mod`` blocks) and DEFINED_IN / BELONGS_TO / IMPORTS / IMPLEMENTS / CALLS / REFERENCES
edges with intra-file resolution; ``use`` declarations become import bindings for the linker.

A file's package is its module path inside the crate: ``src/engine/run.rs`` is ``engine.run``,
``src/engine/mod.rs`` and ``src/lib.rs`` stand for their directory, and a workspace crate's
directory name leads (``crates/nu-protocol/src/value/mod.rs`` is ``nu_protocol.value``), so
``use crate::engine::Engine`` / ``super::`` / ``self::`` resolve to the defining file. Methods are
addressed ``Type.method`` whichever file the ``impl`` block is in. Calls on a receiver whose type
the file does not show (``v.len()``) are not linked: guessing the type would invent edges.

The grammar (``tree-sitter-rust``) is an optional dependency; the registry only registers this
provider when the grammar import succeeds.
"""

from __future__ import annotations

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

#: Item kinds that become symbols of their own (functions and impl members are handled apart).
_ITEM_KINDS: dict[str, str] = {
    "struct_item": str(SymbolKind.CLASS),
    "union_item": str(SymbolKind.CLASS),
    "enum_item": str(SymbolKind.ENUM),
    "trait_item": str(SymbolKind.INTERFACE),
    "type_item": str(SymbolKind.TYPE),
    "const_item": str(SymbolKind.CONSTANT),
    "static_item": str(SymbolKind.CONSTANT),
    "macro_definition": "macro",
}
#: Nodes a body walk does not enter: they are symbols (or scopes) of their own.
_STOP = frozenset({"function_item", "impl_item", "mod_item", "trait_item", "macro_definition"})
#: File stems that stand for their directory's module.
_MODULE_FILES = frozenset({"mod", "lib", "main"})
#: Upper-case names that are prelude items, never a symbol of the repository.
_PRELUDE = frozenset({"Self", "Some", "None", "Ok", "Err"})


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
        self.macros: dict[str, str] = {}
        self.methods: dict[str, dict[str, str]] = {}
        self.exports: dict[str, str] = {}
        self.defs: list[_Def] = []
        self.unresolved: list[UnresolvedRef] = []
        self.impls: list[tuple[str, str, int]] = []


def crate_and_package(path: str) -> tuple[str, str]:
    """``(crate prefix, module path)`` of a ``.rs`` file (see module docstring)."""
    rel = path.replace("\\", "/").lstrip("./")
    if rel.endswith(".rs"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    if parts and parts[-1] in _MODULE_FILES:
        parts = parts[:-1]
    if "src" not in parts:
        return "", ".".join(parts)
    at = len(parts) - 1 - parts[::-1].index("src")
    crate = parts[at - 1].replace("-", "_") if at > 0 else ""
    return crate, ".".join(([crate] if crate else []) + parts[at + 1 :])


class RustProvider(LanguageProvider):
    language = "rust"
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        import tree_sitter_rust

        self._grammar = load_language(tree_sitter_rust)
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return (".rs",)

    def parse(self, source: bytes) -> Any:
        return self._parser.parse(source)

    def derive_package(self, ctx: ParseContext) -> str | None:
        return crate_and_package(ctx.path)[1] or None

    def extract(self, tree: Any, ctx: ParseContext) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        package = ctx.package or ""
        crate = crate_and_package(ctx.path)[0]
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
        self._items(tree.root_node, ctx, package, "", source, frag, st)
        for owner, trait, line in st.impls:
            src = st.symbols.get(owner)
            if src is None:
                continue
            target = st.symbols.get(trait)
            if target is not None:
                frag.add_edge(GraphEdge(EdgeLabel.IMPLEMENTS, src, target))
            else:
                st.unresolved.append(UnresolvedRef(src, trait, kind="implements", line=line))

        bindings: list[ImportBinding] = []
        for child in tree.root_node.named_children:
            if child.type == "use_declaration":
                self._use(child, ctx, package, crate, source, frag, bindings)
        imported = {b.local_name for b in bindings}

        for d in st.defs:
            if d.body is not None:
                self._calls(d, source, st, frag)
                self._references(d, source, st, imported, frag)

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

    def _items(self, container, ctx, package, prefix, source, frag, st: _State) -> None:
        for child in container.named_children:
            kind = child.type
            if kind == "function_item":
                d = self._symbol(child, ctx, package, "", str(SymbolKind.FUNCTION), source, frag)
                self._register(st, d, prefix)
            elif kind in _ITEM_KINDS:
                d = self._symbol(child, ctx, package, "", _ITEM_KINDS[kind], source, frag)
                if kind == "macro_definition":
                    st.macros.setdefault(d.name, d.symbol_id)
                    continue
                self._register(st, d, prefix)
                if kind == "trait_item":
                    self._members(child, d.name, ctx, package, source, frag, st)
            elif kind == "impl_item":
                owner = _type_name(child.child_by_field_name("type"), source)
                if not owner:
                    continue
                trait = _type_name(child.child_by_field_name("trait"), source)
                if trait:
                    st.impls.append((owner, trait, child.start_point[0] + 1))
                self._members(child, owner, ctx, package, source, frag, st)
            elif kind == "mod_item":
                body = child.child_by_field_name("body")
                name = _field_text(child, "name", source)
                if body is not None and name:
                    inner = f"{package}.{name}" if package else name
                    self._items(body, ctx, inner, f"{prefix}{name}.", source, frag, st)

    def _members(self, item, owner, ctx, package, source, frag, st: _State) -> None:
        body = item.child_by_field_name("body")
        if body is None:
            return
        methods = st.methods.setdefault(owner, {})
        for member in body.named_children:
            if member.type in ("function_item", "function_signature_item"):
                kind = str(SymbolKind.METHOD)
            elif member.type == "const_item":
                kind = str(SymbolKind.CONSTANT)
            else:
                continue
            d = self._symbol(member, ctx, package, owner, kind, source, frag)
            d.owner = owner
            methods.setdefault(d.name, d.symbol_id)
            st.exports.setdefault(f"{owner}.{d.name}", d.symbol_id)
            st.defs.append(d)

    @staticmethod
    def _register(st: _State, d: _Def, prefix: str) -> None:
        st.symbols.setdefault(d.name, d.symbol_id)
        st.exports.setdefault(d.name, d.symbol_id)
        if prefix:
            st.exports.setdefault(f"{prefix}{d.name}", d.symbol_id)
        st.defs.append(d)

    def _symbol(self, node, ctx, package, namespace, kind, source, frag) -> _Def:
        name = _field_text(node, "name", source) or "<anonymous>"
        params = node.child_by_field_name("parameters")
        signature = node_text(params, source) if params is not None else None
        ret = node.child_by_field_name("return_type")
        if signature is not None and ret is not None:
            signature = f"{signature} -> {node_text(ret, source)}"
        body = node.child_by_field_name("body")
        symbol_id = make_symbol_id(
            language=self.language,
            package=package,
            namespace=namespace,
            name=name,
            signature=signature,
            kind=kind,
        )
        visible = any(c.type == "visibility_modifier" for c in node.named_children)
        symbol = GraphNode(
            NodeLabel.SYMBOL,
            symbol_id,
            {
                "name": name,
                "kind": kind,
                "signature": signature,
                "visibility": "public" if visible else "private",
                "docstring": doc_comment(node, source),
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
        callable_body = kind in (str(SymbolKind.FUNCTION), str(SymbolKind.METHOD))
        return _Def(symbol_id, name, kind, body if callable_body else None, None)

    # --- imports ---

    def _use(self, node, ctx, package, crate, source, frag, bindings) -> None:
        argument = node.child_by_field_name("argument")
        if argument is None:
            return
        for local, module, imported in _use_bindings(argument, [], source):
            path = _module_path(module, package, crate)
            if path is None:
                continue
            target = f"{path}.{imported}" if imported and local != "*" else path
            if target:
                module_id = make_module_id(ctx.repo_id, target)
                frag.add_node(
                    GraphNode(
                        NodeLabel.MODULE, module_id, {"namespace": target, "repo_id": ctx.repo_id}
                    )
                )
                frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))
            if local == "*":
                bindings.append(ImportBinding(local_name="*", module_path=path))
            elif imported:
                bindings.append(ImportBinding(local, module_path=path, imported_name=imported))
            else:
                bindings.append(ImportBinding(local, module_path=path))

    # --- pass 2: calls ---

    def _calls(self, d: _Def, source, st: _State, frag) -> None:
        seen: set[tuple[str, str | None]] = set()
        for name, qualifier, line, receiver_self, macro in _iter_calls(d.body, source):
            if macro:
                # only a macro this file defines: ``format!`` is not a call of a ``format`` fn
                target = st.macros.get(name)
                if target is not None and target != d.symbol_id:
                    frag.add_edge(
                        GraphEdge(EdgeLabel.CALLS, d.symbol_id, target, properties={"line": line})
                    )
                continue
            if qualifier == "Self" or receiver_self:
                qualifier = d.owner
                if qualifier is None:
                    continue
            if qualifier:
                target = st.methods.get(qualifier, {}).get(name) or st.exports.get(
                    f"{qualifier}.{name}"
                )
            else:
                target = st.symbols.get(name)
            if target is not None:
                if target != d.symbol_id:
                    frag.add_edge(
                        GraphEdge(EdgeLabel.CALLS, d.symbol_id, target, properties={"line": line})
                    )
                continue
            if (name, qualifier) in seen:
                continue
            seen.add((name, qualifier))
            st.unresolved.append(
                UnresolvedRef(d.symbol_id, name, qualifier=qualifier, kind="call", line=line)
            )

    # --- REFERENCES.ref_kind ---

    def _references(self, d: _Def, source, st: _State, imported: set[str], frag) -> None:
        strongest: dict[str, RefKind] = {}
        pending: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = st.symbols.get(name)
            bucket, key = (strongest, target) if target is not None else (pending, name)
            if target is None and not (name in imported or name[:1].isupper()):
                return
            if target == d.symbol_id or name in _PRELUDE:
                return
            current = bucket.get(key)
            if current is None or _REF_KIND_RANK[kind] > _REF_KIND_RANK[current]:
                bucket[key] = kind

        stack = [d.body]
        while stack:
            current = stack.pop()
            # the callee of a call is a CALLS edge, not a reference as well
            callee = _callee(current)
            for child in current.named_children:
                if callee is not None and child.start_byte == callee.start_byte:
                    continue
                ctype = child.type
                if ctype in ("assignment_expression", "compound_assignment_expr"):
                    left = child.child_by_field_name("left")
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


def _callee(node):
    if node.type == "call_expression":
        return node.child_by_field_name("function")
    if node.type == "macro_invocation":
        return node.child_by_field_name("macro")
    return None


def _field_text(node, field: str, source: bytes) -> str:
    child = node.child_by_field_name(field)
    return node_text(child, source) if child is not None else ""


def _type_name(node, source: bytes) -> str:
    """The type a node names: ``Foo`` for ``Foo``, ``Foo<T>``, ``a::Foo``, ``&mut Foo``."""
    while node is not None:
        if node.type in ("type_identifier", "identifier", "primitive_type"):
            return node_text(node, source)
        nxt = node.child_by_field_name("name") or node.child_by_field_name("type")
        if nxt is None:
            named = [c for c in node.named_children if c.type != "lifetime"]
            nxt = named[0] if named else None
        node = nxt
    return ""


def doc_comment(node, source: bytes) -> str | None:
    """The ``///`` / ``/** */`` doc comment above ``node`` (attributes in between skipped)."""
    lines: list[str] = []
    expected = node.start_point[0]
    sibling = node.prev_named_sibling
    while sibling is not None:
        if sibling.type == "attribute_item":
            expected = sibling.start_point[0]
            sibling = sibling.prev_named_sibling
            continue
        if sibling.type not in ("line_comment", "block_comment"):
            break
        if sibling.end_point[0] < expected - 1:
            break
        text = node_text(sibling, source).strip()
        if text.startswith("///") and not text.startswith("////"):
            lines.insert(0, text[3:].strip())
        elif text.startswith("/**"):
            body = [ln.strip().lstrip("*").strip() for ln in text[3:-2].splitlines()]
            lines[:0] = [ln for ln in body if ln]
        else:
            break
        expected = sibling.start_point[0]
        sibling = sibling.prev_named_sibling
    return "\n".join(lines).strip() or None


def _path_segments(node, source: bytes) -> list[str]:
    """``a::b::c`` (``scoped_identifier`` / ``identifier`` / ``crate`` / ``self`` / ``super``)."""
    if node is None:
        return []
    if node.type == "scoped_identifier":
        return _path_segments(node.child_by_field_name("path"), source) + [
            _field_text(node, "name", source)
        ]
    return [node_text(node, source)]


def _use_bindings(node, prefix: list[str], source: bytes):
    """``(local name, module segments, imported name)`` per name a ``use`` tree brings in.

    ``imported`` is ``None`` for a module binding (``use a::b;`` / ``a::{self}``) and the local
    name is ``*`` for a glob.
    """
    kind = node.type
    if kind in ("identifier", "crate", "self", "super", "scoped_identifier"):
        segments = prefix + _path_segments(node, source)
        if segments and segments[-1] == "self":
            segments = segments[:-1]
            if segments:
                yield segments[-1], segments, None
            return
        if len(segments) == 1:
            yield segments[0], segments, None
        elif segments:
            yield segments[-1], segments[:-1], segments[-1]
    elif kind == "use_as_clause":
        segments = prefix + _path_segments(node.child_by_field_name("path"), source)
        alias = _field_text(node, "alias", source)
        if segments and alias and alias != "_":
            yield alias, segments[:-1], segments[-1]
    elif kind == "use_wildcard":
        inner = [c for c in node.named_children]
        segments = prefix + (_path_segments(inner[0], source) if inner else [])
        yield "*", segments, None
    elif kind == "scoped_use_list":
        path = node.child_by_field_name("path")
        segments = prefix + _path_segments(path, source)
        listing = node.child_by_field_name("list")
        if listing is not None:
            yield from _use_bindings(listing, segments, source)
    elif kind == "use_list":
        for child in node.named_children:
            yield from _use_bindings(child, prefix, source)


def _module_path(segments: list[str], package: str, crate: str) -> str | None:
    """Dotted module path of ``use`` segments, ``crate`` / ``self`` / ``super`` resolved."""
    if not segments:
        return package
    head = segments[0]
    if head == "crate":
        base, rest = ([crate] if crate else []), segments[1:]
    elif head in ("self", "super"):
        base = package.split(".") if package else []
        i = 0
        while i < len(segments) and segments[i] in ("self", "super"):
            if segments[i] == "super":
                base = base[:-1]
            i += 1
        rest = segments[i:]
    else:
        base, rest = [], segments
    return ".".join(base + rest)


def _iter_calls(body, source: bytes):
    """``(name, qualifier, line, on_self, macro)`` of the calls in ``body``.

    ``foo()`` has no qualifier, ``Type::f()`` / ``module::f()`` the last path segment
    (``Self`` included), ``self.f()`` is ``on_self``; calls on other receivers are skipped.
    A macro's arguments are a token tree, so ``name(`` inside one counts as a call too.
    """
    stack = [body]
    while stack:
        current = stack.pop()
        for child in current.named_children:
            if child.type == "call_expression":
                call = _call_target(child.child_by_field_name("function"), source)
                if call is not None:
                    yield (*call[:2], child.start_point[0] + 1, call[2], False)
            elif child.type == "macro_invocation":
                macro = child.child_by_field_name("macro")
                if macro is not None and macro.type == "identifier":
                    yield node_text(macro, source), None, child.start_point[0] + 1, False, True
            elif child.type == "token_tree":
                kids = child.children
                for i, (left, right) in enumerate(zip(kids, kids[1:], strict=False)):
                    if (
                        left.type == "identifier"
                        and right.type == "token_tree"
                        and source[right.start_byte : right.start_byte + 1] == b"("
                        and left.end_byte == right.start_byte
                        and (i == 0 or kids[i - 1].type not in (".", "::"))
                    ):
                        yield node_text(left, source), None, left.start_point[0] + 1, False, False
            if child.type not in _STOP:
                stack.append(child)


def _call_target(fn, source: bytes) -> tuple[str, str | None, bool] | None:
    if fn is None:
        return None
    if fn.type == "generic_function":
        fn = fn.child_by_field_name("function")
        if fn is None:
            return None
    if fn.type == "identifier":
        return node_text(fn, source), None, False
    if fn.type == "scoped_identifier":
        name = _field_text(fn, "name", source)
        path = _path_segments(fn.child_by_field_name("path"), source)
        qualifier = path[-1] if path and path[-1] not in ("crate", "self", "super") else None
        return (name, qualifier, False) if name else None
    if fn.type == "field_expression":
        value = fn.child_by_field_name("value")
        field = fn.child_by_field_name("field")
        if value is not None and field is not None and value.type == "self":
            return node_text(field, source), None, True
    return None
