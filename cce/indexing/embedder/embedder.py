"""Embedder (write path, Phase 2).

Turns the symbols/files of a :class:`GraphFragment` into embedding rows and writes them to pgvector
in the caller's transaction (P5). The embedded content is a deterministic string built from
signature + docstring (+ name/kind), so the same source always produces the same content and, with
a pinned encoder, the same vector (P2 determinism).
"""

from __future__ import annotations

from cce.domain.enums import NodeLabel
from cce.domain.models import GraphFragment
from cce.indexing.embedder.encoder import Encoder, build_default_encoder
from cce.storage.vector.store import VectorStore


def _symbol_content(props: dict) -> str:
    parts = [
        str(props.get("name") or ""),
        str(props.get("kind") or ""),
        str(props.get("signature") or ""),
        str(props.get("docstring") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()


class Embedder:
    def __init__(self, store: VectorStore, encoder: Encoder | None = None) -> None:
        self.store = store
        self.encoder = encoder or build_default_encoder()

    def embed_fragment(self, fragment: GraphFragment, *, repo_id: str, indexed_at_commit: str) -> int:
        """Embed every Symbol (and File name) in a fragment. Returns the number of rows written."""
        written = 0
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                content = _symbol_content(node.properties)
                if not content:
                    continue
                self.store.upsert(
                    kind="symbol",
                    ref_id=node.node_id,
                    repo_id=repo_id,
                    content=content,
                    model=self.encoder.model_id,
                    embedding=self.encoder.encode(content),
                    indexed_at_commit=indexed_at_commit,
                )
                written += 1
            elif node.label is NodeLabel.FILE:
                content = str(node.properties.get("path") or "")
                if not content:
                    continue
                self.store.upsert(
                    kind="file",
                    ref_id=node.node_id,
                    repo_id=repo_id,
                    content=content,
                    model=self.encoder.model_id,
                    embedding=self.encoder.encode(content),
                    indexed_at_commit=indexed_at_commit,
                )
                written += 1
        return written
