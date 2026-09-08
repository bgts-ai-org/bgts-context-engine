"""Go language provider (tree-sitter).

Scope: File + Symbol nodes and DEFINED_IN / BELONGS_TO / IMPORTS / CALLS / REFERENCES edges with
intra-file resolution, plus Gin route extraction (``r.GET("/x", handler)`` style).

The grammar (``tree-sitter-go``) is an optional dependency; the registry only registers this
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

# Gin/router HTTP verbs (``r.GET(...)`` / ``group.POST(...)``).
_GIN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY"}


class _Def:
    __slots__ = ("symbol_id", "name", "kind", "body")

    def __init__(self, symbol_id, name, kind, body):
        self.symbol_id = symbol_id
        self.name = name
        self.kind = kind
        self.body = body


class GoProvider(LanguageProvider):
    language = "go"
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        import tree_sitter_go

        self._grammar = load_language(tree_sitter_go)
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return (".go",)

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
        defs: list[_Def] = []
        unresolved: list[UnresolvedRef] = []

        for child in root.named_children:
            if child.type in ("function_declaration", "method_declaration"):
                d = self._add_function(child, ctx, package, source, frag)
                module_symbols[d.name] = d.symbol_id
                defs.append(d)
            elif child.type == "type_declaration":
                for spec in child.named_children:
                    if spec.type == "type_spec":
                        d = self._add_type(spec, ctx, package, source, frag)
                        if d is not None:
                            module_symbols[d.name] = d.symbol_id
                            defs.append(d)

        bindings: list[ImportBinding] = []
        self._collect_imports(root, ctx, source, frag, bindings)

        for d in defs:
            if d.body is None:
                continue
            seen_unresolved: set[tuple[str, str | None]] = set()
            for call in self._iter_calls(d.body):
                line = call.start_point[0] + 1
                target = self._resolve_call(call, source, module_symbols)
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

        frag.link_data = FragmentLinkData(
            file_id=ctx.file_id,
            package=package,
            language=self.language,
            exports=dict(module_symbols),
            imports=bindings,
            unresolved=unresolved,
        )
        return frag

    # --- pass 1 ---

    def _add_function(self, node, ctx, package, source, frag) -> _Def:
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, source) if name_node is not None else "<anonymous>"
        params = node.child_by_field_name("parameters")
        signature = node_text(params, source) if params is not None else None
        body = node.child_by_field_name("body")
        kind = SymbolKind.METHOD if node.type == "method_declaration" else SymbolKind.FUNCTION
        symbol_id = self._make_id(package, name, signature, kind)
        self._add_node(frag, ctx, symbol_id, name, kind, signature, node, package, body=body)
        return _Def(symbol_id, name, kind, body)

    def _add_type(self, spec, ctx, package, source, frag) -> _Def | None:
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            return None
        name = node_text(name_node, source)
        type_node = spec.child_by_field_name("type")
        kind = (
            SymbolKind.INTERFACE
            if type_node is not None and type_node.type == "interface_type"
            else SymbolKind.TYPE
        )
        symbol_id = self._make_id(package, name, None, kind)
        self._add_node(frag, ctx, symbol_id, name, kind, None, spec, package)
        return _Def(symbol_id, name, kind, None)

    def _make_id(self, package, name, signature, kind) -> str:
        return make_symbol_id(
            language=self.language,
            package=package,
            namespace="",
            name=name,
            signature=signature,
            kind=str(kind),
        )

    def _add_node(self, frag, ctx, symbol_id, name, kind, signature, node, package, body=None) -> None:
        frag.add_node(
            GraphNode(
                NodeLabel.SYMBOL,
                symbol_id,
                {
                    "name": name,
                    "kind": str(kind),
                    "signature": signature,
                    # Go visibility is by capitalization (exported vs unexported).
                    "visibility": "public" if name[:1].isupper() else "private",
                    "docstring": None,
                    "body": body_snippet(body, ctx.source),
                    "namespace": package or "",
                    "file_id": ctx.file_id,
                    "line": node.start_point[0] + 1,
                    "indexed_at_commit": ctx.indexed_at_commit,
                },
            )
        )
        frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, ctx.file_id))

    def _collect_imports(self, root, ctx, source, frag, bindings) -> None:
        for child in root.named_children:
            if child.type != "import_declaration":
                continue
            for spec in self._iter_import_specs(child):
                path_node = spec.child_by_field_name("path") or spec
                name = node_text(path_node, source).strip("\"")
                if not name:
                    continue
                module_id = make_module_id(ctx.repo_id, name)
                frag.add_node(
                    GraphNode(
                        NodeLabel.MODULE, module_id, {"namespace": name, "repo_id": ctx.repo_id}
                    )
                )
                frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))
                alias_node = spec.child_by_field_name("name")
                if alias_node is not None:
                    local = node_text(alias_node, source)
                else:
                    local = name.rstrip("/").rpartition("/")[2]
                if local and local not in ("_", "."):
                    # Slash-form module path preserved: the linker matches by dotted suffix.
                    bindings.append(ImportBinding(local_name=local, module_path=name))

    def _unresolved_call_parts(self, call, source) -> tuple[str, str | None] | None:
        """Extract ``(name, qualifier)`` for a call the intra-file pass could not resolve."""
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return node_text(fn, source), None
        if fn.type == "selector_expression":
            operand = fn.child_by_field_name("operand")
            field = fn.child_by_field_name("field")
            if operand is None or field is None:
                return None
            if operand.type != "identifier":
                return None
            return node_text(field, source), node_text(operand, source)
        return None

    @staticmethod
    def _iter_import_specs(node):
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "import_spec":
                    yield child
                else:
                    stack.append(child)

    # --- route extraction (Gin) ---

    def extract_routes(self, tree, ctx: ParseContext, symbol_lines: dict[int, str]) -> GraphFragment:
        frag = GraphFragment()
        source = ctx.source
        root = tree.root_node
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call_expression":
                    self._gin_route(child, ctx, source, frag)
                stack.append(child)
        return frag

    def _gin_route(self, call, ctx, source, frag) -> None:
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression":
            return
        field = fn.child_by_field_name("field")
        if field is None:
            return
        method = node_text(field, source).upper()
        if method not in _GIN_METHODS:
            return
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        path = None
        for arg in args.named_children:
            if arg.type in ("interpreted_string_literal", "raw_string_literal"):
                path = node_text(arg, source).strip("\"`")
                break
        if path is None:
            return
        add_route(
            frag,
            repo_id=ctx.repo_id,
            framework="gin",
            http_method=method,
            path_pattern=path,
            file_id=ctx.file_id,
            line=call.start_point[0] + 1,
            indexed_at_commit=ctx.indexed_at_commit,
            handler_symbol_id=None,
        )

    # --- pass 2 (CALLS) ---

    def _iter_calls(self, node):
        stop = {"function_declaration", "method_declaration", "func_literal"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "call_expression":
                    yield child
                if child.type not in stop:
                    stack.append(child)

    def _resolve_call(self, call, source, module_symbols):
        fn = call.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return module_symbols.get(node_text(fn, source))
        return None

    # --- REFERENCES.ref_kind ---

    def _collect_references(self, d, source, module_symbols, frag, unresolved) -> None:
        strongest: dict[str, RefKind] = {}
        unresolved_strongest: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = module_symbols.get(name)
            if target is None:
                # Go resolves same-package (directory) symbols at link time (no import needed).
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
        stop = {"function_declaration", "method_declaration", "func_literal"}
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                self._classify_ref_node(child, source, note)
                if child.type not in stop:
                    stack.append(child)

    def _classify_ref_node(self, child, source, note) -> None:
        ctype = child.type
        if ctype in ("short_var_declaration", "var_spec"):
            left = child.child_by_field_name("left") or child.child_by_field_name("name")
            if left is not None:
                for ident in self._idents(left, source):
                    note(ident, RefKind.DEFINE)
            self._note_reads(child.child_by_field_name("right") or child.child_by_field_name("value"), source, note)
        elif ctype == "assignment_statement":
            left = child.child_by_field_name("left")
            if left is not None:
                for ident in self._idents(left, source):
                    note(ident, RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "call_expression":
            args = child.child_by_field_name("arguments")
            if args is not None:
                for arg in args.named_children:
                    if arg.type == "identifier":
                        note(node_text(arg, source), RefKind.PASS)

    def _idents(self, node, source):
        out = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier":
                out.append(node_text(current, source))
            for kid in current.named_children:
                stack.append(kid)
        return out

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
        # Go packages are per-directory: two files in the same directory share a package,
        # which is what enables same-package cross-file resolution at link time.
        path = ctx.path.replace("\\", "/").lstrip("./")
        directory = path.rpartition("/")[0]
        return directory.strip("/").replace("/", ".") or None
