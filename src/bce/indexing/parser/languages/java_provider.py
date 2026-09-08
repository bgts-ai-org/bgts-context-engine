"""Java language provider (tree-sitter).

Scope mirrors the Python/JS providers: File + Symbol nodes and
DEFINED_IN / BELONGS_TO / IMPORTS / INHERITS / IMPLEMENTS / CALLS edges with intra-file resolution,
plus REFERENCES edges carrying ``ref_kind`` and Spring route extraction
(``@GetMapping`` / ``@RequestMapping`` etc.).

The grammar (``tree-sitter-java``) is an optional dependency; the registry only registers this
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

# Spring MVC mapping annotations -> HTTP method (RequestMapping resolves method from its args).
_SPRING_MAPPING = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
}


class _Def:
    __slots__ = ("symbol_id", "name", "kind", "body", "class_name")

    def __init__(self, symbol_id, name, kind, body, class_name):
        self.symbol_id = symbol_id
        self.name = name
        self.kind = kind
        self.body = body
        self.class_name = class_name


class JavaProvider(LanguageProvider):
    language = "java"
    line_comment_markers = ("//",)

    def __init__(self) -> None:
        import tree_sitter_java

        self._grammar = load_language(tree_sitter_java)
        self._parser = make_parser(self._grammar)

    def extensions(self) -> tuple[str, ...]:
        return (".java",)

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
        for child in root.named_children:
            if child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                yield child

    def _collect_type(
        self, node, ctx, package, source, frag, module_symbols, class_methods, defs, unresolved
    ) -> None:
        kind = SymbolKind.INTERFACE if node.type == "interface_declaration" else SymbolKind.CLASS
        d = self._add_symbol(node, ctx, package, "", source, frag, kind, None)
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
                md = self._add_symbol(member, ctx, package, d.name, source, frag, mkind, member)
                methods[md.name] = md.symbol_id
                defs.append(md)

    def _add_symbol(self, node, ctx, package, namespace, source, frag, kind, body_holder) -> _Def:
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
            if child.type == "modifiers":
                text = node_text(child, source)
                if "private" in text:
                    return "private"
                if "protected" in text:
                    return "protected"
                if "public" in text:
                    return "public"
        return "package"

    def _add_heritage(
        self, node, source, frag, class_symbol_id, module_symbols, unresolved
    ) -> None:
        line = node.start_point[0] + 1
        for field in ("superclass", "interfaces"):
            sub = node.child_by_field_name(field)
            if sub is None:
                continue
            label = EdgeLabel.INHERITS if field == "superclass" else EdgeLabel.IMPLEMENTS
            kind = "inherits" if field == "superclass" else "implements"
            for ident in self._iter_type_identifiers(sub, source):
                if ident in module_symbols:
                    frag.add_edge(GraphEdge(label, class_symbol_id, module_symbols[ident]))
                else:
                    unresolved.append(UnresolvedRef(class_symbol_id, ident, kind=kind, line=line))

    def _iter_type_identifiers(self, node, source):
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ("type_identifier", "identifier"):
                yield node_text(current, source)
            for child in current.named_children:
                stack.append(child)

    def _collect_imports(self, root, ctx, source, frag, bindings) -> None:
        for child in root.named_children:
            if child.type != "import_declaration":
                continue
            raw = node_text(child, source)
            is_static = " static " in f" {raw} " or raw.startswith("import static")
            name = raw.replace("import", "", 1).replace("static", "", 1)
            name = name.strip().rstrip(";").strip()
            if not name:
                continue
            module_id = make_module_id(ctx.repo_id, name)
            frag.add_node(
                GraphNode(NodeLabel.MODULE, module_id, {"namespace": name, "repo_id": ctx.repo_id})
            )
            frag.add_edge(GraphEdge(EdgeLabel.IMPORTS, ctx.file_id, module_id))
            if name.endswith(".*"):
                bindings.append(ImportBinding(local_name="*", module_path=name[:-2]))
            elif "." in name:
                module_path, _, member = name.rpartition(".")
                if is_static and "." in module_path:
                    # ``import static a.b.C.max`` binds ``max`` to class C in package a.b.
                    pkg, _, cls = module_path.rpartition(".")
                    bindings.append(
                        ImportBinding(
                            local_name=member, module_path=pkg, imported_name=f"{cls}.{member}"
                        )
                    )
                else:
                    bindings.append(
                        ImportBinding(
                            local_name=member, module_path=module_path, imported_name=member
                        )
                    )

    def _unresolved_call_parts(self, call, source) -> tuple[str, str | None] | None:
        """Extract ``(name, qualifier)`` for a method invocation not resolved intra-file."""
        name_node = call.child_by_field_name("name")
        if name_node is None:
            return None
        obj = call.child_by_field_name("object")
        if obj is None:
            return node_text(name_node, source), None
        qualifier = self._qualifier_chain(obj, source)
        if qualifier is None or qualifier == "this":
            return None
        return node_text(name_node, source), qualifier

    @staticmethod
    def _qualifier_chain(node, source) -> str | None:
        if node.type == "identifier":
            return node_text(node, source)
        if node.type == "field_access":
            obj = node.child_by_field_name("object")
            field = node.child_by_field_name("field")
            if obj is None or field is None:
                return None
            left = JavaProvider._qualifier_chain(obj, source)
            if left is None:
                return None
            return f"{left}.{node_text(field, source)}"
        return None

    # --- route extraction (Spring MVC) ---

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
                if child.type == "method_declaration":
                    self._spring_route(child, ctx, source, symbol_lines, frag)
                stack.append(child)
        return frag

    def _spring_route(self, method_node, ctx, source, symbol_lines, frag) -> None:
        handler_line = method_node.start_point[0] + 1
        handler_id = symbol_lines.get(handler_line)
        for ann in self._annotations(method_node):
            name = self._annotation_name(ann, source)
            http = _SPRING_MAPPING.get(name)
            path = self._annotation_string(ann, source)
            if http is None and name == "RequestMapping":
                http = self._request_mapping_method(ann, source)
            if http is None:
                continue
            add_route(
                frag,
                repo_id=ctx.repo_id,
                framework="spring",
                http_method=http,
                path_pattern=path or "/",
                file_id=ctx.file_id,
                line=ann.start_point[0] + 1,
                indexed_at_commit=ctx.indexed_at_commit,
                handler_symbol_id=handler_id,
            )

    @staticmethod
    def _annotations(node):
        for child in node.named_children:
            if child.type == "modifiers":
                for m in child.named_children:
                    if m.type in ("annotation", "marker_annotation"):
                        yield m

    @staticmethod
    def _annotation_name(ann, source) -> str:
        name_node = ann.child_by_field_name("name")
        return node_text(name_node, source) if name_node is not None else ""

    @staticmethod
    def _annotation_string(ann, source) -> str | None:
        args = ann.child_by_field_name("arguments")
        if args is None:
            return None
        stack = [args]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "string_literal":
                    return node_text(child, source).strip('"')
                stack.append(child)
        return None

    @staticmethod
    def _request_mapping_method(ann, source) -> str | None:
        text = node_text(ann, source)
        for verb in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            if f"RequestMethod.{verb}" in text:
                return verb
        return "ANY"

    # --- pass 2 (CALLS) ---

    def _iter_calls(self, node):
        stop = {
            "method_declaration",
            "constructor_declaration",
            "class_declaration",
            "lambda_expression",
        }
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                if child.type == "method_invocation":
                    yield child
                if child.type not in stop:
                    stack.append(child)

    def _resolve_call(self, call, source, module_symbols, class_methods, class_name):
        name_node = call.child_by_field_name("name")
        obj = call.child_by_field_name("object")
        if name_node is None:
            return None
        method_name = node_text(name_node, source)
        if obj is None or node_text(obj, source) == "this":
            if class_name:
                hit = class_methods.get(class_name, {}).get(method_name)
                if hit:
                    return hit
        return module_symbols.get(method_name)

    # --- REFERENCES.ref_kind ---

    def _collect_references(self, d, source, module_symbols, frag, unresolved) -> None:
        strongest: dict[str, RefKind] = {}
        unresolved_strongest: dict[str, RefKind] = {}

        def note(name: str, kind: RefKind) -> None:
            target = module_symbols.get(name)
            if target is None:
                # Java resolves same-package and imported types at link time (no intra-file gate).
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
        stop = {
            "method_declaration",
            "constructor_declaration",
            "class_declaration",
            "lambda_expression",
        }
        stack = [node]
        while stack:
            current = stack.pop()
            for child in current.named_children:
                self._classify_ref_node(child, source, note)
                if child.type not in stop:
                    stack.append(child)

    def _classify_ref_node(self, child, source, note) -> None:
        ctype = child.type
        if ctype == "local_variable_declaration":
            for decl in child.named_children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None:
                        note(node_text(name_node, source), RefKind.DEFINE)
                    self._note_reads(decl.child_by_field_name("value"), source, note)
        elif ctype == "assignment_expression":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                note(node_text(left, source), RefKind.WRITE)
            self._note_reads(child.child_by_field_name("right"), source, note)
        elif ctype == "method_invocation":
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

    def derive_package(self, ctx: ParseContext) -> str | None:
        # Java packages come from the file's `package` statement when present; fall back to path.
        try:
            tree = self.parse(ctx.source)
            for child in tree.root_node.named_children:
                if child.type == "package_declaration":
                    text = node_text(child, ctx.source)
                    return text.replace("package", "").strip().rstrip(";").strip() or None
        except Exception:
            pass
        return super().derive_package(ctx)
