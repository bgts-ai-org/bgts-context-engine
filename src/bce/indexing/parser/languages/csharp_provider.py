"""C# language provider (tree-sitter).

Scope: File + Symbol nodes and DEFINED_IN / BELONGS_TO / IMPORTS / INHERITS / IMPLEMENTS / CALLS /
REFERENCES edges with intra-file resolution, plus ASP.NET route extraction
(``[HttpGet]`` / ``[Route("...")]`` attributes).

The grammar (``tree-sitter-c-sharp``) is an optional dependency; the registry only registers this
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
from bce.indexing.extractor.routes import add_route
from bce.indexing.parser._treesitter import body_snippet, load_language, make_parser, node_text
from bce.indexing.parser.base import LanguageProvider, ParseContext
from bce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_REF_KIND_RANK = {RefKind.READ: 0, RefKind.PASS: 1, RefKind.WRITE: 2, RefKind.DEFINE: 3}

# ASP.NET HTTP verb attributes -> method.
_ASPNET_ATTRS = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpPatch": "PATCH",
    "HttpDelete": "DELETE",
    "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}


class _Def:
    __slots__ = ("symbol_id", "name", "kind", "body", "class_name")

    def __init__(self, symbol_id, name, kind, body, class_name):
        self.symbol_id = symbol_id
        self.name = name
        self.kind = kind
        self.body = body
        self.class_name = class_name


class CSharpProvider(LanguageProvider):
    language = "csharp"
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        import tree_sitter_c_sharp

        self._grammar = load_language(tree_sitter_c_sharp)
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return (".cs",)

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

        for node in self._iter_type_decls(root):
            self._collect_type(
                node, ctx, package, source, frag, module_symbols, class_methods, defs, unresolved
            )

        bindings: list[ImportBinding] = []
        self._collect_imports(root, ctx, source, frag, bindings)

        for d in defs:
            if d.body is None:
                continue
            seen_unresolved: set[tuple[str, str | None]] = set()
            for call in self._iter_calls(d.body):
                line = call.start_point[0] + 1
                target = self._resolve_call(call, source, module_symbols, class_methods, d.class_name)
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

        for d in defs:
            if d.body is None:
                continue
            self._collect_references(d, source, module_symbols, frag, unresolved)

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

    def _iter_type_decls(self, root):
        # Types can be top-level or nested under namespace_declaration -> declaration_list.
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type in ("class_declaration", "interface_declaration", "struct_declaration", "record_declaration"):
                    yield child
                elif child.type in (
                    "namespace_declaration",
                    "file_scoped_namespace_declaration",
                    "declaration_list",
                ):
                    stack.append(child)

    def _collect_type(
        self, node, ctx, package, source, frag, module_symbols, class_methods, defs, unresolved
    ) -> None:
        kind = SymbolKind.INTERFACE if node.type == "interface_declaration" else SymbolKind.CLASS
        d = self._add_symbol(node, ctx, package, "", source, frag, kind)
        module_symbols[d.name] = d.symbol_id
        defs.append(d)
        self._add_heritage(node, source, frag, d.symbol_id, module_symbols, unresolved)

        body = node.child_by_field_name("body")
        if body is None:
            return
        methods = class_methods.setdefault(d.name, {})
        for member in body.named_children:
            if member.type in ("method_declaration", "constructor_declaration"):
                mkind = (
                    SymbolKind.CONSTRUCTOR
                    if member.type == "constructor_declaration"
                    else SymbolKind.METHOD
                )
                md = self._add_symbol(member, ctx, package, d.name, source, frag, mkind)
                methods[md.name] = md.symbol_id
                defs.append(md)
            elif member.type == "property_declaration":
                pd = self._add_symbol(member, ctx, package, d.name, source, frag, SymbolKind.PROPERTY)
                defs.append(pd)

    def _add_symbol(self, node, ctx, package, namespace, source, frag, kind) -> _Def:
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
                    "visibility": self._visibility(node, source),
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
        class_name = namespace if kind in (SymbolKind.METHOD, SymbolKind.CONSTRUCTOR) else None
        return _Def(symbol_id, name, kind, body, class_name)

    @staticmethod
    def _visibility(node, source) -> str:
        for child in node.named_children:
            if child.type == "modifier":
                text = node_text(child, source)
                if text in ("private", "protected", "internal", "public"):
                    return text
        return "internal"

    def _add_heritage(self, node, source, frag, class_symbol_id, module_symbols, unresolved) -> None:
        base_list = None
        for child in node.named_children:
            if child.type == "base_list":
                base_list = child
                break
        if base_list is None:
            return
        line = node.start_point[0] + 1
        for ident in self._iter_type_identifiers(base_list, source):
            if ident in module_symbols:
                # C# can't distinguish base class vs interface syntactically here; default INHERITS.
                frag.add_edge(GraphEdge(EdgeLabel.INHERITS, class_symbol_id, module_symbols[ident]))
            else:
                unresolved.append(
                    UnresolvedRef(class_symbol_id, ident, kind="inherits", line=line)
                )

    def _iter_type_identifiers(self, node, source):
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier":
                yield node_text(current, source)
            for child in current.named_children:
                stack.append(child)

    def _collect_imports(self, root, ctx, source, frag, bindings) -> None:
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "using_directive":
                    name = node_text(child, source).replace("using", "").strip().rstrip(";").strip()
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
                        if "=" in name:
                            # ``using Alias = Some.Namespace.Type;`` -> alias binding.
                            alias, _, target = (p.strip() for p in name.partition("="))
                            if alias and target and "." in target:
                                module_path, _, member = target.rpartition(".")
                                bindings.append(
                                    ImportBinding(
                                        local_name=alias,
                                        module_path=module_path,
                                        imported_name=member,
                                    )
                                )
                        else:
                            # ``using My.App.Utils;`` brings the namespace's types into scope.
                            bindings.append(
                                ImportBinding(local_name="*", module_path=name.removeprefix("static ").strip())
                            )
                elif child.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                    stack.append(child)

    # --- route extraction (ASP.NET) ---

    def extract_routes(self, tree, ctx: ParseContext, symbol_lines: dict[int, str]) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        root = tree.root_node
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "method_declaration":
                    self._aspnet_route(child, ctx, source, symbol_lines, frag)
                stack.append(child)
        return frag

    def _aspnet_route(self, method_node, ctx, source, symbol_lines, frag) -> None:
        handler_line = method_node.start_point[0] + 1
        handler_id = symbol_lines.get(handler_line)
        for attr in self._attributes(method_node):
            name = self._attribute_name(attr, source)
            http = _ASPNET_ATTRS.get(name)
            if http is None:
                continue
            path = self._attribute_string(attr, source) or "/"
            add_route(
                frag,
                repo_id=ctx.repo_id,
                framework="aspnet",
                http_method=http,
                path_pattern=path,
                file_id=ctx.file_id,
                line=attr.start_point[0] + 1,
                indexed_at_commit=ctx.indexed_at_commit,
                handler_symbol_id=handler_id,
            )

    @staticmethod
    def _attributes(node):
        for child in node.named_children:
            if child.type == "attribute_list":
                for attr in child.named_children:
                    if attr.type == "attribute":
                        yield attr

    @staticmethod
    def _attribute_name(attr, source) -> str:
        name_node = attr.child_by_field_name("name")
        if name_node is not None:
            return node_text(name_node, source)
        for child in attr.named_children:
            if child.type in ("identifier", "qualified_name"):
                return node_text(child, source)
        return ""

    @staticmethod
    def _attribute_string(attr, source) -> str | None:
        stack = [attr]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "string_literal":
                    text = node_text(child, source)
                    return text.strip("\"@$")
                stack.append(child)
        return None

    # --- pass 2 (CALLS) ---

    def _iter_calls(self, node):
        stop = {"method_declaration", "constructor_declaration", "class_declaration", "lambda_expression", "local_function_statement"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "invocation_expression":
                    yield child
                if child.type not in stop:
                    stack.append(child)

    def _resolve_call(self, call, source, module_symbols, class_methods, class_name):
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            name = node_text(fn, source)
            # Bare call inside a class resolves to a sibling method first, then a top-level type.
            if class_name:
                hit = class_methods.get(class_name, {}).get(name)
                if hit:
                    return hit
            return module_symbols.get(name)
        if fn.type == "member_access_expression":
            expr = fn.child_by_field_name("expression")
            name = fn.child_by_field_name("name")
            if expr is not None and name is not None and node_text(expr, source) == "this" and class_name:
                return class_methods.get(class_name, {}).get(node_text(name, source))
        return None

    def _unresolved_call_parts(self, call, source) -> tuple[str, str | None] | None:
        """Extract ``(name, qualifier)`` for an invocation not resolved intra-file."""
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return node_text(fn, source), None
        if fn.type == "member_access_expression":
            expr = fn.child_by_field_name("expression")
            name = fn.child_by_field_name("name")
            if expr is None or name is None:
                return None
            qualifier = self._qualifier_chain(expr, source)
            if qualifier is None or qualifier == "this":
                return None
            return node_text(name, source), qualifier
        return None

    @staticmethod
    def _qualifier_chain(node, source) -> str | None:
        if node.type == "identifier":
            return node_text(node, source)
        if node.type == "member_access_expression":
            expr = node.child_by_field_name("expression")
            name = node.child_by_field_name("name")
            if expr is None or name is None:
                return None
            left = CSharpProvider._qualifier_chain(expr, source)
            if left is None:
                return None
            return f"{left}.{node_text(name, source)}"
        return None

    # --- REFERENCES.ref_kind ---

    def _collect_references(self, d, source, module_symbols, frag, unresolved) -> None:
        strongest: dict[str, RefKind] = {}
        unresolved_strongest: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = module_symbols.get(name)
            if target is None:
                # C# resolves same-namespace and ``using``-scoped types at link time.
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
                GraphEdge(EdgeLabel.REFERENCES, d.symbol_id, target, ref_kind=strongest[target])
            )
        for name in sorted(unresolved_strongest):
            unresolved.append(
                UnresolvedRef(
                    d.symbol_id, name, kind="reference", ref_kind=unresolved_strongest[name]
                )
            )

    def _walk_refs(self, node, source, note) -> None:
        stop = {"method_declaration", "constructor_declaration", "class_declaration", "lambda_expression", "local_function_statement"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                self._classify_ref_node(child, source, note)
                if child.type not in stop:
                    stack.append(child)

    def _classify_ref_node(self, child, source, note) -> None:
        ctype = child.type
        if ctype == "local_declaration_statement":
            for decl in self._descendants(child, "variable_declarator"):
                name_node = decl.child_by_field_name("name")
                if name_node is not None:
                    note(node_text(name_node, source), RefKind.DEFINE)
        elif ctype == "assignment_expression":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "invocation_expression":
            args = child.child_by_field_name("arguments")
            if args is not None:
                for arg in args.named_children:
                    for ident in self._descendants(arg, "identifier"):
                        note(node_text(ident, source), RefKind.PASS)

    @staticmethod
    def _descendants(node, type_name):
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == type_name:
                    yield child
                stack.append(child)

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

    def derive_package(self, ctx: ParseContext) -> str | None:
        # C# namespaces come from the ``namespace`` declaration (shared across files),
        # enabling same-namespace cross-file resolution; fall back to the path.
        try:
            tree = self.parse(ctx.source)
            stack = [tree.root_node]
            while stack:
                current = stack.pop()
                for child in current.named_children:
                    if child.type in (
                        "namespace_declaration",
                        "file_scoped_namespace_declaration",
                    ):
                        name_node = child.child_by_field_name("name")
                        if name_node is not None:
                            name = node_text(name_node, ctx.source)
                            if name:
                                return name
        except Exception:
            pass
        return super().derive_package(ctx)
