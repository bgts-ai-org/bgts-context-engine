"""Deterministic cross-file resolution (the linker).

Extraction is file-local (pass 1); this module is the repo-wide pass 2. It builds a global
``(package, name) -> symbol_id`` table from every fragment's exports, then resolves each
unresolved call/reference/inheritance through the importing file's bindings:

1. **Explicit bindings** — ``from m import x`` / named JS imports / Java single-type imports.
2. **Module-object bindings** — ``import m`` / ``import * as m`` / Go package imports; a
   qualified use ``m.f()`` resolves inside module ``m`` (JS ``index`` files are also tried).
3. **Same-package space** — Go (directory package), Java/C# (declared package/namespace), and
   ``Class.method`` qualified names within the file's own package.
4. **Wildcard imports** — ``from m import *`` / ``import a.b.*`` / C# ``using``, tried last in
   sorted module order.

Everything is order-independent: inputs are iterated in sorted order, the first table hit for a
deterministic candidate sequence wins, and the output fragment is deduped/sorted. No placeholder
nodes are ever created - a ref that resolves to nothing is dropped.
"""

from __future__ import annotations

from bce.domain.enums import EdgeLabel, NodeLabel, Provenance
from bce.domain.models import (
    FragmentLinkData,
    GraphEdge,
    GraphFragment,
    GraphNode,
    ImportBinding,
    UnresolvedRef,
)

_KIND_TO_LABEL = {
    "call": EdgeLabel.CALLS,
    "reference": EdgeLabel.REFERENCES,
    "inherits": EdgeLabel.INHERITS,
    "implements": EdgeLabel.IMPLEMENTS,
}

#: Symbol kinds whose SCIP-synthesized incoming edges are calls rather than plain references.
_CALLABLE_KINDS = {"function", "method", "constructor"}


def _module_paths(path: str) -> list[str]:
    """Dotted candidates for a module path, longest first.

    Slash-form paths (Go import paths) are matched by progressively shorter dotted suffixes,
    because the import path root (module host/repo prefix) is not part of the file's package:
    ``example.com/repo/utils`` -> ``example.com.repo.utils``, ``repo.utils``, ``utils``.
    Dotted paths (Python/JS/Java/C#) are exact.
    """
    if "/" not in path:
        return [path]
    segments = [s for s in path.split("/") if s]
    return [".".join(segments[i:]) for i in range(len(segments))]


def _bindings_by_local(bindings: list[ImportBinding]) -> dict[str, list[ImportBinding]]:
    grouped: dict[str, list[ImportBinding]] = {}
    for binding in sorted(bindings, key=lambda b: b.sort_key):
        grouped.setdefault(binding.local_name, [])
        if binding not in grouped[binding.local_name]:
            grouped[binding.local_name].append(binding)
    return grouped


def _candidates(
    ref: UnresolvedRef,
    bindings: dict[str, list[ImportBinding]],
    wildcard_modules: list[str],
    package: str,
) -> list[tuple[str, str]]:
    """Deterministic (package, name) lookup sequence for one unresolved ref."""
    out: list[tuple[str, str]] = []
    if ref.qualifier:
        q = ref.qualifier
        for b in bindings.get(q, []):
            if b.imported_name:
                # Imported symbol used as a qualifier: Class.method, or submodule function.
                for m in _module_paths(b.module_path):
                    out.append((m, f"{b.imported_name}.{ref.name}"))
                for m in _module_paths(f"{b.module_path}/{b.imported_name}" if "/" in b.module_path else f"{b.module_path}.{b.imported_name}"):
                    out.append((m, ref.name))
            else:
                for m in _module_paths(b.module_path):
                    out.append((m, ref.name))
                    out.append((f"{m}.index", ref.name))
        # Same-package class-qualified use (Java/C# static call, Python classmethod).
        out.append((package, f"{q}.{ref.name}"))
        for m in wildcard_modules:
            out.append((m, f"{q}.{ref.name}"))
    else:
        for b in bindings.get(ref.name, []):
            if b.imported_name:
                for m in _module_paths(b.module_path):
                    out.append((m, b.imported_name))
                    out.append((f"{m}.index", b.imported_name))
        # Same-package space (Go directory package, Java package, C# namespace).
        out.append((package, ref.name))
        for m in wildcard_modules:
            out.append((m, ref.name))
    return out


def _package_suffixes(package: str) -> list[str]:
    """Proper dotted suffixes of a package, longest first (``a.b.c`` -> ``b.c``, ``c``).

    Monorepos commonly root absolute imports at a *service* directory rather than the repo root
    (``from app.core.pricing import x`` inside ``services/ai-analytics/``), so the repo-derived
    package (``services.ai-analytics.app.core.pricing``) only matches by suffix.
    """
    parts = package.split(".")
    return [".".join(parts[i:]) for i in range(1, len(parts))]


def link_fragments(
    fragments: list[GraphFragment],
    *,
    scip_resolution=None,
) -> GraphFragment:
    """Resolve all unresolved refs across ``fragments`` into a single edge fragment.

    Also maps Module nodes onto the repo file that defines them (``file_id`` property + a
    ``Module -[IMPORTS]-> File`` edge), which is what makes transitive file-dependency walks
    possible. When a ``scip_resolution`` is supplied, edges it confirms are elevated to
    ``provenance='scip'``.
    """
    link_data = [f.link_data for f in fragments if f.link_data is not None]
    link_data.sort(key=lambda ld: ld.file_id)

    table, suffix_table, symbol_files = _build_table(link_data)
    symbol_names = _build_symbol_names(fragments)

    out = GraphFragment()
    for ld in link_data:
        bindings = _bindings_by_local(ld.imports)
        wildcard_modules = sorted(
            {m for b in bindings.get("*", []) for m in _module_paths(b.module_path)}
        )
        package = (ld.package or "").strip()
        for ref in sorted(ld.unresolved, key=lambda u: u.sort_key):
            label = _KIND_TO_LABEL.get(ref.kind)
            if label is None:
                continue
            target = _resolve(ref, bindings, wildcard_modules, package, table, suffix_table)
            if target is None or target == ref.src_symbol_id:
                continue
            provenance = Provenance.TREESITTER
            if scip_resolution is not None:
                src_name = symbol_names.get(ref.src_symbol_id)
                if src_name and scip_resolution.confirms(src_name, ref.name):
                    provenance = Provenance.SCIP
            properties = {"line": ref.line} if ref.line else {}
            out.add_edge(
                GraphEdge(
                    label,
                    ref.src_symbol_id,
                    target,
                    provenance=provenance,
                    ref_kind=ref.ref_kind,
                    properties=properties,
                )
            )

    _link_modules_to_files(fragments, link_data, table, symbol_files, out)
    return out.deduped()


def _resolve(
    ref: UnresolvedRef,
    bindings: dict[str, list[ImportBinding]],
    wildcard_modules: list[str],
    package: str,
    table: dict[tuple[str, str], str],
    suffix_table: dict[tuple[str, str], str],
) -> str | None:
    candidates = _candidates(ref, bindings, wildcard_modules, package)
    for key in candidates:
        target = table.get(key)
        if target is not None:
            return target
    # Monorepo fallback: the import root may be a subdirectory (service root), so the binding's
    # module path only matches a *suffix* of the repo-derived package. Exact matches above always
    # win; the suffix table itself is deterministic (first entry over sorted files wins).
    for key in candidates:
        target = suffix_table.get(key)
        if target is not None:
            return target
    return None


def _build_table(
    link_data: list[FragmentLinkData],
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], dict[str, str]]:
    """Global ``(package, name) -> symbol_id`` tables + ``symbol_id -> file_id`` map.

    Returns the exact-package table, a package-suffix table (monorepo service-rooted imports),
    and the symbol->file map. First entry wins on collisions; iteration over sorted file_ids and
    sorted export names makes the winner deterministic.
    """
    table: dict[tuple[str, str], str] = {}
    suffix_table: dict[tuple[str, str], str] = {}
    symbol_files: dict[str, str] = {}
    for ld in link_data:
        package = (ld.package or "").strip()
        suffixes = _package_suffixes(package)
        for name in sorted(ld.exports):
            sid = ld.exports[name]
            table.setdefault((package, name), sid)
            for suffix in suffixes:
                suffix_table.setdefault((suffix, name), sid)
            symbol_files.setdefault(sid, ld.file_id)
    return table, suffix_table, symbol_files


def _build_symbol_names(fragments: list[GraphFragment]) -> dict[str, str]:
    names: dict[str, str] = {}
    for frag in fragments:
        for node in frag.nodes:
            if node.label is NodeLabel.SYMBOL:
                name = node.properties.get("name")
                if isinstance(name, str):
                    names.setdefault(node.node_id, name)
    return names


def _link_modules_to_files(
    fragments: list[GraphFragment],
    link_data: list[FragmentLinkData],
    table: dict[tuple[str, str], str],
    symbol_files: dict[str, str],
    out: GraphFragment,
) -> None:
    """Bind Module nodes to the repo file defining them (A5: enables transitive dependencies)."""
    package_files: dict[str, str] = {}
    for ld in link_data:
        package = (ld.package or "").strip()
        if package:
            package_files.setdefault(package, ld.file_id)
    # Monorepo suffix aliases (service-rooted imports), registered after all exact packages so
    # an exact package always beats another file's suffix.
    for ld in link_data:
        package = (ld.package or "").strip()
        for suffix in _package_suffixes(package):
            package_files.setdefault(suffix, ld.file_id)

    seen: set[str] = set()
    modules: list[GraphNode] = []
    for frag in fragments:
        for node in frag.nodes:
            if node.label is NodeLabel.MODULE and node.node_id not in seen:
                seen.add(node.node_id)
                modules.append(node)
    modules.sort(key=lambda n: n.node_id)

    for module in modules:
        namespace = module.properties.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            continue
        file_id = _module_file(namespace, package_files, table, symbol_files)
        if file_id is None:
            continue
        out.add_node(GraphNode(NodeLabel.MODULE, module.node_id, {"file_id": file_id}))
        out.add_edge(GraphEdge(EdgeLabel.IMPORTS, module.node_id, file_id))


def _module_file(
    namespace: str,
    package_files: dict[str, str],
    table: dict[tuple[str, str], str],
    symbol_files: dict[str, str],
) -> str | None:
    for candidate in _module_paths(namespace):
        hit = package_files.get(candidate)
        if hit is not None:
            return hit
        # JS directory imports resolve to the directory's index module.
        hit = package_files.get(f"{candidate}.index")
        if hit is not None:
            return hit
        # Java-style single-type import (``a.b.C``): the file defining symbol C in package a.b.
        if "." in candidate:
            pkg, _, name = candidate.rpartition(".")
            sid = table.get((pkg, name))
            if sid is not None:
                return symbol_files.get(sid)
    return None


def synthesize_scip_edges(resolution, fragments: list[GraphFragment]) -> GraphFragment:
    """Create cross-file edges directly from a SCIP resolution (provenance='scip').

    SCIP names are matched to symbols by short name; only unambiguous names (exactly one symbol
    repo-wide) are synthesized, keeping the output exact and deterministic. Callable targets get
    CALLS edges, everything else REFERENCES.
    """
    by_name: dict[str, list[tuple[str, str]]] = {}
    for frag in fragments:
        for node in frag.nodes:
            if node.label is not NodeLabel.SYMBOL:
                continue
            name = node.properties.get("name")
            kind = node.properties.get("kind") or ""
            if isinstance(name, str) and name:
                entries = by_name.setdefault(name, [])
                pair = (node.node_id, str(kind))
                if pair not in entries:
                    entries.append(pair)

    out = GraphFragment()
    for src_name, dst_name in sorted(resolution.edges):
        sources = by_name.get(src_name, [])
        targets = by_name.get(dst_name, [])
        if len(sources) != 1 or len(targets) != 1:
            continue
        src_id, _src_kind = sources[0]
        dst_id, dst_kind = targets[0]
        if src_id == dst_id:
            continue
        label = EdgeLabel.CALLS if dst_kind in _CALLABLE_KINDS else EdgeLabel.REFERENCES
        out.add_edge(GraphEdge(label, src_id, dst_id, provenance=Provenance.SCIP))
    return out.deduped()
