"""Shared test helpers."""

from __future__ import annotations

from bce.domain.enums import EdgeLabel, NodeLabel
from bce.domain.models import GraphFragment


def symbols_by_name(fragment: GraphFragment) -> dict[str, str]:
    """Map symbol name -> symbol_id for the symbols in a fragment."""
    out: dict[str, str] = {}
    for node in fragment.nodes:
        if node.label is NodeLabel.SYMBOL:
            out[node.properties["name"]] = node.node_id
    return out


def has_edge(fragment: GraphFragment, label: EdgeLabel, src: str, dst: str) -> bool:
    return any(e.label is label and e.src_id == src and e.dst_id == dst for e in fragment.edges)


def module_namespaces(fragment: GraphFragment) -> set[str]:
    return {
        node.properties["namespace"] for node in fragment.nodes if node.label is NodeLabel.MODULE
    }
