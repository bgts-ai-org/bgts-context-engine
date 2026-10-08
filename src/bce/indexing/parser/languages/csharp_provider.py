"""C# language provider (tree-sitter).

Scope: File + Symbol nodes and DEFINED_IN / BELONGS_TO / IMPORTS / INHERITS / IMPLEMENTS / CALLS /
REFERENCES edges with intra-file resolution, plus ASP.NET route extraction
(``[HttpGet]`` / ``[Route("...")]`` attributes).

The grammar (``tree-sitter-c-sharp``) is an optional dependency; the registry only registers this
provider when the grammar import succeeds.
"""

from __future__ import annotations

import re
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
from bce.indexing.parser._treesitter import (
    body_snippet,
    load_language,
    make_parser,
    node_text,
    search_text,
)
from bce.indexing.parser.base import LanguageProvider, ParseContext
from bce.indexing.parser.symbol_id import make_module_id, make_symbol_id

_REF_KIND_RANK = {RefKind.READ: 0, RefKind.PASS: 1, RefKind.WRITE: 2, RefKind.DEFINE: 3}

#: Type declarations that become symbols, at the top level and nested in other types.
_TYPE_KINDS: dict[str, SymbolKind] = {
    "class_declaration": SymbolKind.CLASS,
    "interface_declaration": SymbolKind.INTERFACE,
    "struct_declaration": SymbolKind.CLASS,
    "record_declaration": SymbolKind.CLASS,
    "record_struct_declaration": SymbolKind.CLASS,
    "enum_declaration": SymbolKind.ENUM,
    "delegate_declaration": SymbolKind.TYPE,
}
_TYPE_DECLS = frozenset(_TYPE_KINDS)
#: Containers the top-level walk looks through to find type declarations.
_TRANSPARENT = frozenset(
    {"namespace_declaration", "file_scoped_namespace_declaration", "declaration_list"}
)
#: Source rewrites for what the grammar (tree-sitter-c-sharp 0.23) cannot parse. Every rewrite
#: keeps byte length and newlines, so node positions from the repaired parse are valid against
#: the original source. See :func:`_repair_source`.
#:
#: Conditional-compilation directives: handled at statement boundaries (``preproc_if`` nodes) but
#: not *inside* a statement (an ``#if`` splitting an ``if / else if`` chain, a ``catch`` list, an
#: initializer). Blanking the directive lines lets both branches parse; ``#pragma warning
#: suppress`` (analyzer-specific) is rejected outright and blanked too.
_PREPROC_LINE_RE = re.compile(rb"^[ \t]*#[ \t]*(if|elif|else|endif|pragma)\b[^\r\n]*", re.MULTILINE)
#: ``async`` used as a plain identifier (``bool async``, ``async: true``, ``return async._x``):
#: a contextual keyword in C#, a reserved one to the grammar. Only value positions - followed by
#: a delimiter - are renamed, never the modifier (followed by a type or a lambda).
_ASYNC_IDENT_RE = re.compile(rb"\basync\b(?=\s*[,;:)\].?])")
#: Null-conditional assignment (C# 14): ``x?.P = v`` / ``xs?[i] = v``. Dropping the ``?`` in
#: front of a ``.`` / ``[`` that leads to an assignment on the same line yields ordinary member
#: access / indexing.
_NULL_COND_ASSIGN_RE = re.compile(rb"\?(?=[.\[][^;\n]*?[^=!<>]=[^=>])")
#: Collection expressions (C# 12) in argument or assignment position (``Add(x, [item])``,
#: ``xs = [a, b];``): parsed as attributes and rejected. The brackets become parentheses (a
#: parenthesized expression or a tuple) - spreads and empty collections are left alone. The
#: trailing delimiter keeps ``[Attr] type name`` parameter attributes out.
_COLLECTION_EXPR_RE = re.compile(rb"([(,=]\s*)\[([^\[\]\n]+)\](?=\s*[,);])")
#: A verbatim string that starts and ends with an escaped quote - the ``$@"""{x}"""`` idiom for
#: quoting a value - is lexed as a raw string literal opener. The two escape pairs become spaces
#: (single-line only; the interpolation hole and any strings inside it are left as they are).
_VERBATIM_QUOTED_RE = re.compile(rb'((?:\$@|@\$|@)")""([^\n]*?[^"])""("(?!"))')

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


def _blank(text: bytes) -> bytes:
    """Same length, same line structure, nothing to parse."""
    return re.sub(rb"[^\r\n]", b" ", text)


def _repair_source(source: bytes) -> bytes:
    """Rewrite the constructs the grammar cannot parse, preserving every byte offset.

    Applied only to files whose first parse has errors, and the result is used only when it has
    fewer errors (see :meth:`CSharpProvider.parse`). Surveyed on EF Core (66 error files of
    2 947) and PowerShell (34 of 1 182): conditional-compilation lines inside statements,
    ``async`` as an identifier, null-conditional assignment and collection expressions account
    for all but a handful (unsafe pointer code, function pointers, a verbatim string nested in
    an interpolation hole). A declaration duplicated across ``#if`` branches collapses to one
    symbol id at upsert.
    """

    def collection_to_parens(match: re.Match[bytes]) -> bytes:
        prefix, inner = match.groups()
        if b".." in inner:  # spread: no same-length equivalent
            return match.group(0)
        return prefix + b"(" + inner + b")"

    repaired = _PREPROC_LINE_RE.sub(lambda m: _blank(m.group(0)), source)
    repaired = _ASYNC_IDENT_RE.sub(b"asyn_", repaired)
    repaired = _NULL_COND_ASSIGN_RE.sub(b" ", repaired)
    repaired = _VERBATIM_QUOTED_RE.sub(rb"\1  \2  \3", repaired)
    return _COLLECTION_EXPR_RE.sub(collection_to_parens, repaired)


def _error_count(root: Any) -> int:
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
        stack.extend(node.children)
    return count


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
        """Parse; when the tree has errors, retry on a *position-preserving* repair of the source.

        A handful of constructs the grammar cannot take derail whole files (EF Core: 66 of
        2 947 source files, PowerShell: 34 of 1 182 - among them a 3 000-line cmdlet file that
        yielded zero symbols). The repair (:func:`_repair_source`) rewrites them byte-for-byte so
        every node position is valid against the original source, which is what the extractor
        keeps reading; the repaired tree is used only when it has fewer errors than the first.
        """
        tree = self._parser.parse(source)
        if not tree.root_node.has_error:
            return tree
        repaired = _repair_source(source)
        if repaired == source:
            return tree
        retry = self._parser.parse(repaired)
        return retry if _error_count(retry.root_node) < _error_count(tree.root_node) else tree

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
        """Top-level type declarations: under namespaces (block or file-scoped) and under any
        ``#if`` / ``#else`` region wrapping them. A ``#if !UNIX`` around a whole file is common
        in cross-platform .NET code (PowerShell: 280 types) and used to hide every type in it."""
        stack = [root]
        while stack:
            current = stack.pop()
            for child in reversed(current.named_children):
                if child.type in _TYPE_DECLS:
                    yield child
                elif child.type in _TRANSPARENT or child.type.startswith("preproc_"):
                    stack.append(child)

    def _collect_type(
        self,
        node,
        ctx,
        package,
        source,
        frag,
        module_symbols,
        class_methods,
        defs,
        unresolved,
        outer: str = "",
    ) -> None:
        """One type declaration and, recursively, the types nested in it.

        A nested type is addressed by its container chain (``Outer.Inner``) - what the symbol id
        carries and what a task means by ``SelectExpression.Helper`` - and registered under its
        simple name too so an intra-file ``Inner.Helper()`` resolves. Partial-class files that
        hold one nested visitor, and the visitor's methods with them, were invisible before.
        """
        kind = _TYPE_KINDS[node.type]
        d = self._add_symbol(node, ctx, package, outer, source, frag, kind)
        qualified = f"{outer}.{d.name}" if outer else d.name
        module_symbols.setdefault(d.name, d.symbol_id)
        if qualified != d.name:
            module_symbols[qualified] = d.symbol_id
        defs.append(d)
        self._add_heritage(node, source, frag, d.symbol_id, module_symbols, unresolved)

        body = node.child_by_field_name("body")
        if body is None:
            return
        methods = class_methods.setdefault(qualified, {})
        if qualified != d.name:
            class_methods.setdefault(d.name, methods)
        for member in self._iter_members(body):
            if member.type in ("method_declaration", "constructor_declaration"):
                mkind = (
                    SymbolKind.CONSTRUCTOR
                    if member.type == "constructor_declaration"
                    else SymbolKind.METHOD
                )
                md = self._add_symbol(member, ctx, package, qualified, source, frag, mkind)
                methods[md.name] = md.symbol_id
                defs.append(md)
            elif member.type == "property_declaration":
                pd = self._add_symbol(
                    member, ctx, package, qualified, source, frag, SymbolKind.PROPERTY
                )
                defs.append(pd)
            elif member.type in _TYPE_DECLS:
                self._collect_type(
                    member,
                    ctx,
                    package,
                    source,
                    frag,
                    module_symbols,
                    class_methods,
                    defs,
                    unresolved,
                    outer=qualified,
                )
            elif member.type == "field_declaration":
                self._collect_fields(member, ctx, package, qualified, source, frag, defs)

    @staticmethod
    def _iter_members(body):
        """Direct members of a type body, looking through ``#if`` / ``#else`` regions."""
        stack = [body]
        while stack:
            current = stack.pop()
            for member in reversed(current.named_children):
                if member.type.startswith("preproc_") and member.named_children:
                    stack.append(member)
                else:
                    yield member

    def _collect_fields(self, node, ctx, package, namespace, source, frag, defs) -> None:
        """``const`` and ``static`` fields as symbols of their own (see the Java provider): the
        setting names, well-known keys and singletons a task names. Instance fields stay part of
        their type's search text."""
        modifiers = {
            node_text(child, source) for child in node.named_children if child.type == "modifier"
        }
        if not modifiers & {"const", "static"}:
            return
        declaration = next(
            (c for c in node.named_children if c.type == "variable_declaration"), None
        )
        if declaration is None:
            return
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            name = node_text(name_node, source)
            kind = SymbolKind.CONSTANT if "const" in modifiers else SymbolKind.FIELD
            symbol_id = make_symbol_id(
                language=self.language,
                package=package,
                namespace=namespace,
                name=name,
                signature=None,
                kind=str(kind),
            )
            symbol = GraphNode(
                NodeLabel.SYMBOL,
                symbol_id,
                {
                    "name": name,
                    "kind": str(kind),
                    "signature": None,
                    "visibility": self._visibility(node, source),
                    "docstring": None,
                    "body": body_snippet(node, source),
                    "namespace": namespace or package or "",
                    "file_id": ctx.file_id,
                    "line": declarator.start_point[0] + 1,
                    "indexed_at_commit": ctx.indexed_at_commit,
                },
            )
            symbol.search_text = search_text(node, source)
            frag.add_node(symbol)
            frag.add_edge(GraphEdge(EdgeLabel.DEFINED_IN, symbol_id, ctx.file_id))
            defs.append(_Def(symbol_id, name, kind, None, None))

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
        symbol = GraphNode(
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
        symbol.search_text = search_text(node, source)
        frag.add_node(symbol)
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

    def _add_heritage(
        self, node, source, frag, class_symbol_id, module_symbols, unresolved
    ) -> None:
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
                unresolved.append(UnresolvedRef(class_symbol_id, ident, kind="inherits", line=line))

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
                                ImportBinding(
                                    local_name="*", module_path=name.removeprefix("static ").strip()
                                )
                            )
                elif child.type in (
                    "namespace_declaration",
                    "file_scoped_namespace_declaration",
                ) or child.type.startswith("preproc_"):
                    stack.append(child)

    # --- route extraction (ASP.NET) ---

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
                    return text.strip('"@$')
                stack.append(child)
        return None

    # --- pass 2 (CALLS) ---

    def _iter_calls(self, node):
        stop = {
            "method_declaration",
            "constructor_declaration",
            "lambda_expression",
            "local_function_statement",
            *_TYPE_DECLS,  # nested types are symbols of their own (pass 1)
        }
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
            if expr is None or name is None:
                return None
            method = node_text(name, source)
            if node_text(expr, source) == "this":
                return class_methods.get(class_name, {}).get(method) if class_name else None
            # ``Helper.Run()`` / ``Outer.Inner.Run()``: a static or nested-type call on a type
            # declared in this file (types are registered under both spellings in pass 1).
            qualifier = self._qualifier_chain(expr, source)
            if qualifier is None:
                return None
            methods = class_methods.get(qualifier)
            if methods is None:
                # ``Inner.Deep.Run()`` written from the enclosing type: match the chain suffix.
                suffix = "." + qualifier
                candidates = [key for key in class_methods if key.endswith(suffix)]
                if len(candidates) != 1:
                    return None
                methods = class_methods[candidates[0]]
            return methods.get(method)
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
        stop = {
            "method_declaration",
            "constructor_declaration",
            "lambda_expression",
            "local_function_statement",
            *_TYPE_DECLS,  # nested types are symbols of their own (pass 1)
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
