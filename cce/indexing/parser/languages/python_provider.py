"""Python language provider (tree-sitter).

Scope (deterministic, AST-only): File + Symbol nodes, DEFINED_IN / BELONGS_TO / IMPORTS /
INHERITS / CALLS edges, with intra-file resolution for inheritance and calls, plus
REFERENCES edges carrying ``ref_kind`` (define/write/read/pass) for scoring (§6.4). Cross-file and
cross-repo exact resolution is elevated by the optional SCIP adapter; the schema carries those
fields regardless.
"""

from __future__ import annotations

from typing import Any

import tree_sitter_python

from cce.domain.enums import EdgeLabel, NodeLabel, RefKind, SymbolKind
from cce.domain.models import GraphEdge, GraphFragment, GraphNode
from cce.indexing.extractor.routes import add_route
from cce.indexing.parser._treesitter import load_language, make_parser, node_text
from cce.indexing.parser.base import LanguageProvider, ParseContext
from cce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_PY_LANGUAGE = load_language(tree_sitter_python)

# Decorator attribute -> HTTP method for FastAPI/Flask-style ``@app.get("/x")`` routes.
_HTTP_DECORATOR_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Strength ordering for ref_kind (higher wins when a symbol is used multiple ways in one body).
# Mirrors scoring's weighting: define/write outrank read/pass (§6.4).
_REF_KIND_RANK = {RefKind.READ: 0, RefKind.PASS: 1, RefKind.WRITE: 2, RefKind.DEFINE: 3}


class _Def:
    """A collected definition pending edge extraction in pass 2."""

    __slots__ = ("symbol_id", "name", "kind", "namespace", "body", "class_name")

    def __init__(self, symbol_id, name, kind, namespace, body, class_name):
        self.symbol_id = symbol_id
        self.name = name
        self.kind = kind
        self.namespace = namespace
        self.body = body
        self.class_name = class_name


class PythonProvider(LanguageProvider):
    language = "python"
    line_comment_markers = ("#",)

    def __init__(self) -> None:
        self._parser = make_parser(_PY_LANGUAGE)

    def extensions(self) -> tuple[str, ...]:
        return (".py", ".pyi")

    def parse(self, source: bytes) -> Any:
        return self._parser.parse(source)

    def extract(self, tree: Any, ctx: ParseContext) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        package = ctx.package or self.derive_package(ctx)
        root = tree.root_node

        # File node + membership.
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

        # Pass 1: collect definitions + name -> symbol_id maps for intra-file resolution.
        module_symbols: dict[str, str] = {}
        class_methods: dict[str, dict[str, str]] = {}
        defs: list[_Def] = []

        for child in root.named_children:
            self._collect_top_level(
                child, ctx, package, source, frag, module_symbols, class_methods, defs
            )

        # IMPORTS edges.
        for child in root.named_children:
            self._collect_imports(child, ctx, source, frag)

        # Pass 2: resolve calls within each definition body.
        for d in defs:
            if d.body is None:
                continue
            for call in self._iter_calls(d.body):
                target = self._resolve_call(call, source, module_symbols, class_methods, d.class_name)
                if target is not None and target != d.symbol_id:
                    frag.add_edge(GraphEdge(EdgeLabel.CALLS, d.symbol_id, target))

        # Pass 3: REFERENCES edges with ref_kind (define/write/read/pass) for scoring (§6.4).
        for d in defs:
            if d.body is None:
                continue
            self._collect_references(d, source, module_symbols, frag)

        return frag

    # --- pass 1 helpers ---

    def _collect_top_level(
        self, node, ctx, package, source, frag, module_symbols, class_methods, defs
    ) -> None:
        node = self._unwrap_decorated(node)
        if node.type == "function_definition":
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.FUNCTION)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
        elif node.type == "class_definition":
            d = self._add_symbol(node, ctx, package, "", source, frag, SymbolKind.CLASS)
            module_symbols[d.name] = d.symbol_id
            defs.append(d)
            self._add_inherits(node, source, frag, d.symbol_id, module_symbols)
            methods = class_methods.setdefault(d.name, {})
            body = node.child_by_field_name("body")
            if body is not None:
                for member in body.named_children:
                    member = self._unwrap_decorated(member)
                    if member.type == "function_definition":
                        md = self._add_symbol(
                            member, ctx, package, d.name, source, frag, SymbolKind.METHOD
                        )
                        methods[md.name] = md.symbol_id
                        defs.append(md)
        elif node.type == "expression_statement":
            self._collect_module_assignment(node, ctx, package, source, frag, module_symbols, defs)

    def _collect_module_assignment(
        self, expr_stmt, ctx, package, source, frag, module_symbols, defs
    ) -> None:
        for child in expr_stmt.named_children:
            if child.type != "assignment":
                continue
            left = child.child_by_field_name("left")
            if left is None or left.type != "identifier":
                continue
            name = node_text(left, source)
            kind = SymbolKind.CONSTANT if name.isupper() else SymbolKind.VARIABLE
            d = self._add_symbol(
                child, ctx, package, "", source, frag, kind, name_override=name
            )
            module_symbols.setdefault(d.name, d.symbol_id)
            defs.append(d)

    def _add_symbol(
        self, node, ctx, package, namespace, source, frag, kind, name_override=None
    ) -> _Def:
        if name_override is not None:
            name = name_override
        else:
            name_node = node.child_by_field_name("name")
            name = node_text(name_node, source) if name_node is not None else "<anonymous>"

        params = node.child_by_field_name("parameters")
        signature = node_text(params, source) if params is not None else None
        body = node.child_by_field_name("body")
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
                    "visibility": "private" if name.startswith("_") else "public",
                    "docstring": self._docstring(body, source),
                    "namespace": namespace or package or "",
                    "file_id": ctx.file_id,
                    "line": node.start_point[0] + 1,
                    "indexed_at_commit": ctx.indexed_at_commit,
                },
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, ctx.file_id))
        class_name = namespace if kind is SymbolKind.METHOD else None
        return _Def(symbol_id, name, kind, namespace, body, class_name)

    def _add_inherits(self, class_node, source, frag, class_symbol_id, module_symbols) -> None:
        supers = class_node.child_by_field_name("superclasses")
        if supers is None:
            return
        for arg in supers.named_children:
            base_name = node_text(arg, source) if arg.type == "identifier" else None
            if base_name and base_name in module_symbols:
                frag.add_edge(
                    GraphEdge(EdgeLabel.INHERITS, class_symbol_id, module_symbols[base_name])
                )

    def _import_name(self, child, source) -> str | None:
        if child.type == "dotted_name":
            return node_text(child, source)
        if child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                return node_text(name_node, source)
        return None

    def _collect_imports(self, node, ctx, source, frag) -> None:
        if node.type == "import_statement":
            for child in node.named_children:
                name = self._import_name(child, source)
                if name:
                    self._add_import(ctx, name, frag)
        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            if module_node is not None:
                name = node_text(module_node, source)
                if name:
                    self._add_import(ctx, name, frag)

    def _add_import(self, ctx, module_name, frag) -> None:
        module_id = make_module_id(ctx.repo_id, module_name)
        frag.add_node(
            GraphNode(
                NodeLabel.MODULE,
                module_id,
                {"namespace": module_name, "repo_id": ctx.repo_id},
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))

    # --- route extraction (feature 1: FastAPI/Flask decorators) ---

    def extract_routes(self, tree, ctx: ParseContext, symbol_lines: dict[int, str]) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        root = tree.root_node
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "decorated_definition":
                    self._routes_from_decorated(child, ctx, source, symbol_lines, frag)
                stack.append(child)
        return frag

    def _routes_from_decorated(self, node, ctx, source, symbol_lines, frag) -> None:
        inner = self._unwrap_decorated(node)
        if inner.type != "function_definition":
            return
        handler_line = inner.start_point[0] + 1
        handler_id = symbol_lines.get(handler_line)
        for decorator in node.named_children:
            if decorator.type != "decorator":
                continue
            call = self._decorator_call(decorator)
            if call is None:
                continue
            self._route_from_call(call, ctx, source, handler_id, decorator, frag)

    @staticmethod
    def _decorator_call(decorator):
        for child in decorator.named_children:
            if child.type == "call":
                return child
        return None

    def _route_from_call(self, call, ctx, source, handler_id, decorator, frag) -> None:
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            return
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        if attr is None or obj is None:
            return
        method_token = node_text(attr, source).lower()
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        path = self._first_string_arg(args, source)
        if path is None:
            return
        line = decorator.start_point[0] + 1

        if method_token in _HTTP_DECORATOR_METHODS:
            add_route(
                frag,
                repo_id=ctx.repo_id,
                framework="fastapi",
                http_method=method_token,
                path_pattern=path,
                file_id=ctx.file_id,
                line=line,
                indexed_at_commit=ctx.indexed_at_commit,
                handler_symbol_id=handler_id,
            )
        elif method_token == "route":  # Flask: @app.route("/x", methods=["POST"])
            for method in self._flask_methods(args, source):
                add_route(
                    frag,
                    repo_id=ctx.repo_id,
                    framework="flask",
                    http_method=method,
                    path_pattern=path,
                    file_id=ctx.file_id,
                    line=line,
                    indexed_at_commit=ctx.indexed_at_commit,
                    handler_symbol_id=handler_id,
                )

    @staticmethod
    def _first_string_arg(args, source) -> str | None:
        for arg in args.named_children:
            if arg.type == "string":
                return node_text(arg, source).strip("\"'")
        return None

    @staticmethod
    def _flask_methods(args, source) -> list[str]:
        methods: list[str] = []
        for arg in args.named_children:
            if arg.type != "keyword_argument":
                continue
            name = arg.child_by_field_name("name")
            value = arg.child_by_field_name("value")
            if name is None or value is None or node_text(name, source) != "methods":
                continue
            for element in value.named_children:
                if element.type == "string":
                    methods.append(node_text(element, source).strip("\"'"))
        return methods or ["GET"]

    # --- pass 2 helpers ---

    def _iter_calls(self, node):
        # Do not descend into nested scopes; calls there belong to their own symbol.
        stop = {"function_definition", "class_definition", "decorated_definition", "lambda"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call":
                    yield child
                if child.type not in stop:
                    stack.append(child)

    def _resolve_call(self, call, source, module_symbols, class_methods, class_name):
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return module_symbols.get(node_text(fn, source))
        if fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            attr = fn.child_by_field_name("attribute")
            if obj is not None and attr is not None and node_text(obj, source) == "self" and class_name:
                return class_methods.get(class_name, {}).get(node_text(attr, source))
        return None

    # --- pass 3 helpers (REFERENCES.ref_kind) ---

    def _collect_references(self, d, source, module_symbols, frag) -> None:
        """Emit REFERENCES edges from ``d`` to module-level symbols it uses.

        ref_kind classifies the strongest role of each name within the body:
        ``define`` (bound as an assignment target), ``write`` (augmented/attribute assignment),
        ``read`` (plain use), ``pass`` (used as a call argument). The strongest kind per target wins
        so scoring's ``define/write >> read/pass`` rule (§6.4) is fed a stable, single value.
        """
        strongest: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = module_symbols.get(name)
            if target is None or target == d.symbol_id:
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

    def _walk_refs(self, node, source, note) -> None:
        stop = {"function_definition", "class_definition", "decorated_definition", "lambda"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                self._classify_ref_node(child, source, note)
                if child.type not in stop:
                    stack.append(child)

    def _classify_ref_node(self, child, source, note) -> None:
        ctype = child.type
        if ctype == "assignment":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.DEFINE)
            elif left is not None and left.type == "attribute":
                note(self._attr_root(left, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "augmented_assignment":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "call":
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
    def _attr_root(attr_node, source) -> str:
        obj = attr_node.child_by_field_name("object")
        if obj is not None:
            return node_text(obj, source)
        return node_text(attr_node, source)

    # --- misc ---

    @staticmethod
    def _unwrap_decorated(node):
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                return inner
            for child in reversed(node.named_children):
                if child.type in ("function_definition", "class_definition"):
                    return child
        return node

    @staticmethod
    def _docstring(body, source) -> str | None:
        if body is None:
            return None
        for child in body.named_children:
            if child.type == "expression_statement":
                inner = child.named_children[0] if child.named_children else None
                if inner is not None and inner.type == "string":
                    raw = node_text(inner, source).strip()
                    return raw.strip("\"'").strip()
            break
        return None
