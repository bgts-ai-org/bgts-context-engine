"""JavaScript / TypeScript language providers (tree-sitter).

JS and TS share extraction logic (the TS grammar is largely a superset), so a single base
implements ``extract`` and the concrete providers only differ by grammar + extensions. This keeps
the abstraction-first promise: TypeScript was added without touching the extraction code.

Scope mirrors the Python provider: File + Symbol nodes and
DEFINED_IN / BELONGS_TO / IMPORTS / INHERITS / CALLS edges with intra-file resolution, plus
REFERENCES edges carrying ``ref_kind`` (define/write/read/pass) for scoring (§6.4). Import
bindings (named/namespace/require) and unresolved calls/references are recorded on the fragment's
:class:`FragmentLinkData` for the repo-wide cross-file linker.
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
from bce.indexing.extractor.routes import add_route
from bce.indexing.parser._treesitter import body_snippet, make_parser, node_text
from bce.indexing.parser.base import LanguageProvider, ParseContext
from bce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_FUNCTION_DECLS = {"function_declaration", "generator_function_declaration"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_FUNCTION_VALUES = {"arrow_function", "function", "function_expression"}

# Strength ordering for ref_kind (higher wins when a symbol is used multiple ways in one body).
_REF_KIND_RANK = {RefKind.READ: 0, RefKind.PASS: 1, RefKind.WRITE: 2, RefKind.DEFINE: 3}

# Express-style ``app.get('/x', handler)`` / NestJS ``@Get('/x')`` HTTP verbs.
_EXPRESS_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
_NEST_DECORATORS = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
    "Head": "HEAD",
    "Options": "OPTIONS",
    "All": "ANY",
}


class _Def:
    __slots__ = ("symbol_id", "name", "body", "class_name")

    def __init__(self, symbol_id, name, body, class_name):
        self.symbol_id = symbol_id
        self.name = name
        self.body = body
        self.class_name = class_name


class _JsFamilyProvider(LanguageProvider):
    """Shared JS/TS extraction. Subclasses provide the grammar Language + extensions."""

    _grammar = None  # set by subclass (tree_sitter.Language)
    _exts: tuple[str, ...] = ()
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return self._exts

    def parse(self, source: bytes) -> Any:
        return self._parser.parse(source)

    def extract(self, tree: Any, ctx: ParseContext) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        package = ctx.package or self.derive_package(ctx)
        root = tree.root_node

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

        module_symbols: dict[str, str] = {}
        class_methods: dict[str, dict[str, str]] = {}
        defs: list[_Def] = []
        unresolved: list[UnresolvedRef] = []

        for child in root.named_children:
            self._collect(
                child, ctx, package, source, frag, module_symbols, class_methods, defs, unresolved
            )

        bindings: list[ImportBinding] = []
        for child in root.named_children:
            self._collect_imports(child, ctx, source, frag, bindings)
        bound_locals = {b.local_name for b in bindings}
        has_wildcard = "*" in bound_locals

        for d in defs:
            if d.body is None:
                continue
            seen_unresolved: set[tuple[str, str | None]] = set()
            for call in self._iter_calls(d.body):
                line = call.start_point[0] + 1
                target = self._resolve_call(
                    call, source, module_symbols, class_methods, d.class_name
                )
                if target is not None and target != d.symbol_id:
                    frag.add_edge(
                        GraphEdge(EdgeLabel.CALLS, d.symbol_id, target, properties={"line": line})
                    )
                    continue
                if target is not None:
                    continue
                parts = self._unresolved_call_parts(call, source)
                if parts is None:
                    continue
                name, qualifier = parts
                key = (name, qualifier)
                if key in seen_unresolved:
                    continue
                seen_unresolved.add(key)
                unresolved.append(
                    UnresolvedRef(d.symbol_id, name, qualifier=qualifier, kind="call", line=line)
                )

        # REFERENCES edges with ref_kind (define/write/read/pass) for scoring (§6.4).
        for d in defs:
            if d.body is None:
                continue
            self._collect_references(
                d, source, module_symbols, frag, unresolved, bound_locals, has_wildcard
            )

        exports = dict(module_symbols)
        for cls, methods in class_methods.items():
            for method, sid in methods.items():
                exports.setdefault(f"{cls}.{method}", sid)
        frag.link_data = FragmentLinkData(
            file_id=ctx.file_id,
            package=package,
            language=self.language,
            exports=exports,
            imports=bindings,
            unresolved=unresolved,
        )
        return frag

    # --- pass 1 ---

    def _collect(
        self, node, ctx, package, source, frag, module_symbols, class_methods, defs, unresolved
    ) -> None:
        node = self._unwrap_export(node)
        if node.type in _FUNCTION_DECLS:
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.FUNCTION, node)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
        elif node.type in _CLASS_DECLS:
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.CLASS, None)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
            self._add_heritage(node, source, frag, d.symbol_id, module_symbols, unresolved)
            self._collect_methods(node, ctx, package, source, frag, class_methods, defs, d.name)
        elif node.type == "interface_declaration":
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.INTERFACE, None)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
        elif node.type in ("lexical_declaration", "variable_declaration"):
            self._collect_variable(node, ctx, package, source, frag, module_symbols, defs)

    def _collect_methods(
        self, class_node, ctx, package, source, frag, class_methods, defs, class_name
    ) -> None:
        body = class_node.child_by_field_name("body")
        if body is None:
            return
        methods = class_methods.setdefault(class_name, {})
        for member in body.named_children:
            if member.type in ("method_definition", "method_signature"):
                md = self._add_symbol(
                    member, ctx, package, class_name, source, frag, SymbolKind.METHOD, member
                )
                methods[md.name] = md.symbol_id
                defs.append(md)

    def _collect_variable(self, node, ctx, package, source, frag, module_symbols, defs) -> None:
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue
            name = node_text(name_node, source)
            value = declarator.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUES:
                d = self._add_symbol(
                    declarator,
                    ctx,
                    package,
                    "",
                    source,
                    frag,
                    SymbolKind.FUNCTION,
                    value,
                    name_override=name,
                )
            else:
                kind = (
                    SymbolKind.CONSTANT
                    if node.type == "lexical_declaration"
                    else SymbolKind.VARIABLE
                )
                d = self._add_symbol(
                    declarator, ctx, package, "", source, frag, kind, None, name_override=name
                )
            module_symbols.setdefault(d.name, d.symbol_id)
            defs.append(d)

    def _add_symbol(
        self, node, ctx, package, namespace, source, frag, kind, body_holder, name_override=None
    ) -> _Def:
        if name_override is not None:
            name = name_override
        else:
            name_node = node.child_by_field_name("name")
            name = node_text(name_node, source) if name_node is not None else "<anonymous>"

        params = body_holder.child_by_field_name("parameters") if body_holder is not None else None
        signature = node_text(params, source) if params is not None else None
        body = body_holder.child_by_field_name("body") if body_holder is not None else None
        symbol_id = make_symbol_id(
            language=self.language,
            package=package,
            namespace=namespace,
            name=name,
            signature=signature,
            kind=str(kind),
        )
        frag.add_node(
            GraphNode(
                NodeLabel.SYMBOL,
                symbol_id,
                {
                    "name": name,
                    "kind": str(kind),
                    "signature": signature,
                    "visibility": "private" if name.startswith(("_", "#")) else "public",
                    "docstring": None,
                    "body": body_snippet(body, source),
                    "namespace": namespace or package or "",
                    "file_id": ctx.file_id,
                    "line": node.start_point[0] + 1,
                    "indexed_at_commit": ctx.indexed_at_commit,
                },
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, ctx.file_id))
        class_name = namespace if kind is SymbolKind.METHOD else None
        return _Def(symbol_id, name, body, class_name)

    def _add_heritage(
        self, class_node, source, frag, class_symbol_id, module_symbols, unresolved
    ) -> None:
        line = class_node.start_point[0] + 1
        for child in class_node.named_children:
            if child.type != "class_heritage":
                continue
            for ident in self._iter_type_identifiers(child, source):
                if ident in module_symbols:
                    frag.add_edge(
                        GraphEdge(EdgeLabel.INHERITS, class_symbol_id, module_symbols[ident])
                    )
                else:
                    unresolved.append(
                        UnresolvedRef(class_symbol_id, ident, kind="inherits", line=line)
                    )

    def _iter_type_identifiers(self, node, source):
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type in ("identifier", "type_identifier"):
                    yield node_text(child, source)
                stack.append(child)

    def _normalize_module(self, spec: str, ctx: ParseContext) -> str:
        """Resolve relative specifiers against the importing file's directory into dotted form.

        ``./util`` in ``src/app.js`` becomes ``src.util`` (matching the target file's package);
        bare package specifiers (``react``) are kept verbatim.
        """
        spec = spec.strip()
        if not spec.startswith("."):
            return spec
        base = posixpath.dirname(ctx.path.replace("\\", "/"))
        joined = posixpath.normpath(posixpath.join(base, spec)).replace("\\", "/")
        lowered = joined.lower()
        for ext in (".d.ts", ".tsx", ".ts", ".jsx", ".mjs", ".cjs", ".js"):
            if lowered.endswith(ext):
                joined = joined[: -len(ext)]
                break
        return joined.strip("/").replace("/", ".")

    def _add_module(self, ctx, namespace, frag) -> None:
        module_id = make_module_id(ctx.repo_id, namespace)
        frag.add_node(
            GraphNode(
                NodeLabel.MODULE,
                module_id,
                {"namespace": namespace, "repo_id": ctx.repo_id},
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))

    def _collect_imports(self, node, ctx, source, frag, bindings) -> None:
        node = self._unwrap_export(node)
        if node.type == "import_statement":
            src = node.child_by_field_name("source")
            if src is None:
                return
            spec = node_text(src, source).strip("\"'`")
            if not spec:
                return
            module_path = self._normalize_module(spec, ctx)
            self._add_module(ctx, module_path, frag)
            self._collect_import_clause(node, source, module_path, bindings)
        elif node.type in ("lexical_declaration", "variable_declaration"):
            # CommonJS: const x = require('./m') / const { a, b } = require('./m').
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                spec = self._require_spec(value, source)
                if spec is None:
                    continue
                module_path = self._normalize_module(spec, ctx)
                self._add_module(ctx, module_path, frag)
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                if name_node.type == "identifier":
                    bindings.append(
                        ImportBinding(
                            local_name=node_text(name_node, source), module_path=module_path
                        )
                    )
                elif name_node.type == "object_pattern":
                    for prop in name_node.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            name = node_text(prop, source)
                            bindings.append(
                                ImportBinding(
                                    local_name=name, module_path=module_path, imported_name=name
                                )
                            )
                        elif prop.type == "pair_pattern":
                            key = prop.child_by_field_name("key")
                            val = prop.child_by_field_name("value")
                            if key is not None and val is not None and val.type == "identifier":
                                bindings.append(
                                    ImportBinding(
                                        local_name=node_text(val, source),
                                        module_path=module_path,
                                        imported_name=node_text(key, source),
                                    )
                                )

    @staticmethod
    def _require_spec(value, source) -> str | None:
        if value is None or value.type != "call_expression":
            return None
        fn = value.child_by_field_name("function")
        if fn is None or fn.type != "identifier" or node_text(fn, source) != "require":
            return None
        args = value.child_by_field_name("arguments")
        if args is None:
            return None
        for arg in args.named_children:
            if arg.type == "string":
                return node_text(arg, source).strip("\"'`")
        return None

    def _collect_import_clause(self, import_node, source, module_path, bindings) -> None:
        for child in import_node.named_children:
            if child.type != "import_clause":
                continue
            for part in child.named_children:
                if part.type == "identifier":
                    # Default import: bind under the SCIP-ish "default" export name (conservative).
                    bindings.append(
                        ImportBinding(
                            local_name=node_text(part, source),
                            module_path=module_path,
                            imported_name="default",
                        )
                    )
                elif part.type == "namespace_import":
                    for ident in part.named_children:
                        if ident.type == "identifier":
                            bindings.append(
                                ImportBinding(
                                    local_name=node_text(ident, source), module_path=module_path
                                )
                            )
                elif part.type == "named_imports":
                    for spec_node in part.named_children:
                        if spec_node.type != "import_specifier":
                            continue
                        name_node = spec_node.child_by_field_name("name")
                        alias_node = spec_node.child_by_field_name("alias")
                        if name_node is None:
                            continue
                        name = node_text(name_node, source)
                        local = node_text(alias_node, source) if alias_node is not None else name
                        bindings.append(
                            ImportBinding(
                                local_name=local, module_path=module_path, imported_name=name
                            )
                        )

    # --- route extraction (feature 1: Express + NestJS) ---

    def extract_routes(
        self, tree, ctx: ParseContext, symbol_lines: dict[int, str]
    ) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        root = tree.root_node
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call_expression":
                    self._express_route(child, ctx, source, frag)
                elif child.type == "decorator":
                    self._nest_route(child, ctx, source, symbol_lines, frag)
                stack.append(child)
        return frag

    def _express_route(self, call, ctx, source, frag) -> None:
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        prop = fn.child_by_field_name("property")
        if prop is None:
            return
        method = node_text(prop, source).lower()
        if method not in _EXPRESS_METHODS:
            return
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        path = self._first_string_arg(args, source)
        if path is None:
            return
        add_route(
            frag,
            repo_id=ctx.repo_id,
            framework="express",
            http_method=method,
            path_pattern=path,
            file_id=ctx.file_id,
            line=call.start_point[0] + 1,
            indexed_at_commit=ctx.indexed_at_commit,
            handler_symbol_id=None,
        )

    def _nest_route(self, decorator, ctx, source, symbol_lines, frag) -> None:
        call = None
        for child in decorator.named_children:
            if child.type == "call_expression":
                call = child
                break
        if call is None:
            return
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "identifier":
            return
        method = _NEST_DECORATORS.get(node_text(fn, source))
        if method is None:
            return
        args = call.child_by_field_name("arguments")
        path = self._first_string_arg(args, source) if args is not None else ""
        handler_id = self._nest_handler_id(decorator, symbol_lines)
        add_route(
            frag,
            repo_id=ctx.repo_id,
            framework="nestjs",
            http_method=method,
            path_pattern=path or "/",
            file_id=ctx.file_id,
            line=decorator.start_point[0] + 1,
            indexed_at_commit=ctx.indexed_at_commit,
            handler_symbol_id=handler_id,
        )

    @staticmethod
    def _nest_handler_id(decorator, symbol_lines: dict[int, str]) -> str | None:
        """The decorated method is the sibling starting on the line after the decorator(s)."""
        parent = decorator.parent
        if parent is None:
            return None
        target_line = None
        for member in parent.named_children:
            if member.type in ("method_definition", "method_signature"):
                target_line = member.start_point[0] + 1
                break
        if target_line is None:
            return None
        return symbol_lines.get(target_line)

    @staticmethod
    def _first_string_arg(args, source) -> str | None:
        for arg in args.named_children:
            if arg.type in ("string", "template_string"):
                return node_text(arg, source).strip("\"'`")
        return None

    # --- pass 2 ---

    def _iter_calls(self, node):
        # Do not descend into nested scopes; calls there belong to their own symbol.
        stop = (
            _FUNCTION_DECLS
            | _CLASS_DECLS
            | _FUNCTION_VALUES
            | {
                "method_definition",
                "method_signature",
            }
        )
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call_expression":
                    yield child
                if child.type not in stop:
                    stack.append(child)

    def _resolve_call(self, call, source, module_symbols, class_methods, class_name):
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return module_symbols.get(node_text(fn, source))
        if fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            if (
                obj is not None
                and prop is not None
                and node_text(obj, source) == "this"
                and class_name
            ):
                return class_methods.get(class_name, {}).get(node_text(prop, source))
        return None

    def _unresolved_call_parts(self, call, source) -> tuple[str, str | None] | None:
        """Extract ``(name, qualifier)`` for a call the intra-file pass could not resolve."""
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return node_text(fn, source), None
        if fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            if obj is None or prop is None:
                return None
            qualifier = self._member_chain(obj, source)
            if qualifier is None or qualifier == "this":
                return None
            return node_text(prop, source), qualifier
        return None

    @staticmethod
    def _member_chain(node, source) -> str | None:
        """Text of an identifier / member-of-identifiers chain (``a.b.c``), else ``None``."""
        if node.type == "identifier":
            return node_text(node, source)
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj is None or prop is None:
                return None
            left = _JsFamilyProvider._member_chain(obj, source)
            if left is None:
                return None
            return f"{left}.{node_text(prop, source)}"
        return None

    # --- REFERENCES.ref_kind ---

    def _collect_references(
        self, d, source, module_symbols, frag, unresolved, bound_locals, has_wildcard
    ) -> None:
        """Emit REFERENCES edges from ``d`` to module symbols it uses, tagged with ref_kind.

        ``define`` (declared/assigned as target), ``write`` (augmented/member assignment),
        ``read`` (plain use), ``pass`` (used as a call argument). The strongest kind per target wins.
        Imported names not defined in this file are recorded as unresolved references (linker input).
        """
        strongest: dict[str, RefKind] = {}
        unresolved_strongest: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = module_symbols.get(name)
            if target is None:
                if name in bound_locals or has_wildcard:
                    current = unresolved_strongest.get(name)
                    if current is None or _REF_KIND_RANK[kind] > _REF_KIND_RANK[current]:
                        unresolved_strongest[name] = kind
                return
            if target == d.symbol_id:
                return
            current = strongest.get(target)
            if current is None or _REF_KIND_RANK[kind] > _REF_KIND_RANK[current]:
                strongest[target] = kind

        self._walk_refs(d.body, source, note)

        for target in sorted(strongest):
            frag.add_edge(
                GraphEdge(
                    EdgeLabel.REFERENCES,
                    d.symbol_id,
                    target,
                    ref_kind=strongest[target],
                )
            )
        for name in sorted(unresolved_strongest):
            unresolved.append(
                UnresolvedRef(
                    d.symbol_id,
                    name,
                    kind="reference",
                    ref_kind=unresolved_strongest[name],
                )
            )

    def _walk_refs(self, node, source, note) -> None:
        stop = (
            _FUNCTION_DECLS
            | _CLASS_DECLS
            | _FUNCTION_VALUES
            | {
                "method_definition",
                "method_signature",
            }
        )
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                self._classify_ref_node(child, source, note)
                if child.type not in stop:
                    stack.append(child)

    def _classify_ref_node(self, child, source, note) -> None:
        ctype = child.type
        if ctype == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier":
                note(node_text(name_node, source), RefKind.DEFINE)
            self._note_reads(child.child_by_field_name("value"), source, note)
        elif ctype == "assignment_expression":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.WRITE)
            elif left is not None and left.type == "member_expression":
                obj = left.child_by_field_name("object")
                if obj is not None and obj.type == "identifier":
                    note(node_text(obj, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "augmented_assignment_expression":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "call_expression":
            args = child.child_by_field_name("arguments")
            if args is not None:
                for arg in args.named_children:
                    if arg.type == "identifier":
                        note(node_text(arg, source), RefKind.PASS)

    def _note_reads(self, node, source, note) -> None:
        if node is None:
            return
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier":
                note(node_text(current, source), RefKind.READ)
            for kid in current.named_children:
                stack.append(kid)

    @staticmethod
    def _unwrap_export(node):
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                return decl
        return node


def _load_js():
    import tree_sitter_javascript

    from bce.indexing.parser._treesitter import load_language

    return load_language(tree_sitter_javascript)


def _load_ts():
    import tree_sitter_typescript

    from bce.indexing.parser._treesitter import language_from

    return (
        language_from(tree_sitter_typescript.language_typescript(), "typescript"),
        language_from(tree_sitter_typescript.language_tsx(), "tsx"),
    )


class JavaScriptProvider(_JsFamilyProvider):
    language = "javascript"
    _grammar = _load_js()
    _exts = (".js", ".jsx", ".mjs", ".cjs")


_TS_GRAMMAR, _TSX_GRAMMAR = _load_ts()


class TypeScriptProvider(_JsFamilyProvider):
    language = "typescript"
    _grammar = _TS_GRAMMAR
    _exts = (".ts",)


class TsxProvider(_JsFamilyProvider):
    language = "typescript"
    _grammar = _TSX_GRAMMAR
    _exts = (".tsx",)
