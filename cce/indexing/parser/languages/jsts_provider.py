"""JavaScript / TypeScript language providers (tree-sitter).

JS and TS share extraction logic (the TS grammar is largely a superset), so a single base
implements ``extract`` and the concrete providers only differ by grammar + extensions. This keeps
the abstraction-first promise: TypeScript was added without touching the extraction code.

Phase 0 scope mirrors the Python provider: File + Symbol nodes and
DEFINED_IN / BELONGS_TO / IMPORTS / INHERITS / CALLS edges with intra-file resolution.
"""

from __future__ import annotations

from typing import Any

from cce.domain.enums import EdgeLabel, NodeLabel, SymbolKind
from cce.domain.models import GraphEdge, GraphFragment, GraphNode
from cce.indexing.extractor.routes import add_route
from cce.indexing.parser._treesitter import make_parser, node_text
from cce.indexing.parser.base import LanguageProvider, ParseContext
from cce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_FUNCTION_DECLS = {"function_declaration", "generator_function_declaration"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_FUNCTION_VALUES = {"arrow_function", "function", "function_expression"}

# Express-style ``app.get('/x', handler)`` / NestJS ``@Get('/x')`` HTTP verbs.
_EXPRESS_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
_NEST_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Head": "HEAD", "Options": "OPTIONS", "All": "ANY",
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

        for child in root.named_children:
            self._collect(child, ctx, package, source, frag, module_symbols, class_methods, defs)

        for child in root.named_children:
            self._collect_imports(child, ctx, source, frag)

        for d in defs:
            if d.body is None:
                continue
            for call in self._iter_calls(d.body):
                target = self._resolve_call(call, source, module_symbols, class_methods, d.class_name)
                if target is not None and target != d.symbol_id:
                    frag.add_edge(GraphEdge(EdgeLabel.CALLS, d.symbol_id, target))

        return frag

    # --- pass 1 ---

    def _collect(self, node, ctx, package, source, frag, module_symbols, class_methods, defs) -> None:
        node = self._unwrap_export(node)
        if node.type in _FUNCTION_DECLS:
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.FUNCTION, node)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
        elif node.type in _CLASS_DECLS:
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.CLASS, None)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
            self._add_heritage(node, source, frag, d.symbol_id, module_symbols)
            self._collect_methods(node, ctx, package, source, frag, class_methods, defs, d.name)
        elif node.type == "interface_declaration":
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.INTERFACE, None)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
        elif node.type in ("lexical_declaration", "variable_declaration"):
            self._collect_variable(node, ctx, package, source, frag, module_symbols, defs)

    def _collect_methods(self, class_node, ctx, package, source, frag, class_methods, defs, class_name) -> None:
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
                    declarator, ctx, package, "", source, frag, SymbolKind.FUNCTION, value,
                    name_override=name,
                )
            else:
                kind = SymbolKind.CONSTANT if node.type == "lexical_declaration" else SymbolKind.VARIABLE
                d = self._add_symbol(
                    declarator, ctx, package, "", source, frag, kind, None, name_override=name
                )
            module_symbols.setdefault(d.name, d.symbol_id)
            defs.append(d)

    def _add_symbol(self, node, ctx, package, namespace, source, frag, kind, body_holder, name_override=None) -> _Def:
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

    def _add_heritage(self, class_node, source, frag, class_symbol_id, module_symbols) -> None:
        for child in class_node.named_children:
            if child.type != "class_heritage":
                continue
            for ident in self._iter_type_identifiers(child, source):
                if ident in module_symbols:
                    frag.add_edge(
                        GraphEdge(EdgeLabel.INHERITS, class_symbol_id, module_symbols[ident])
                    )

    def _iter_type_identifiers(self, node, source):
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type in ("identifier", "type_identifier"):
                    yield node_text(child, source)
                stack.append(child)

    def _collect_imports(self, node, ctx, source, frag) -> None:
        node = self._unwrap_export(node)
        if node.type == "import_statement":
            src = node.child_by_field_name("source")
            if src is not None:
                name = node_text(src, source).strip("\"'`")
                if name:
                    module_id = make_module_id(ctx.repo_id, name)
                    frag.add_node(
                        GraphNode(
                            NodeLabel.MODULE,
                            module_id,
                            {"namespace": name, "repo_id": ctx.repo_id},
                        )
                    )
                    frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))

    # --- route extraction (feature 1: Express + NestJS) ---

    def extract_routes(self, tree, ctx: ParseContext, symbol_lines: dict[int, str]) -> GraphFragment:
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
        stop = _FUNCTION_DECLS | _CLASS_DECLS | _FUNCTION_VALUES | {
            "method_definition",
            "method_signature",
        }
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
            if obj is not None and prop is not None and node_text(obj, source) == "this" and class_name:
                return class_methods.get(class_name, {}).get(node_text(prop, source))
        return None

    @staticmethod
    def _unwrap_export(node):
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is not None:
                return decl
        return node


def _load_js():
    import tree_sitter_javascript

    from cce.indexing.parser._treesitter import load_language

    return load_language(tree_sitter_javascript)


def _load_ts():
    import tree_sitter_typescript

    from cce.indexing.parser._treesitter import language_from

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
