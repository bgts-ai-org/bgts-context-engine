"""Embedder (write path, Phase 2).

Turns the symbols/files of a :class:`GraphFragment` into embedding rows and writes them to pgvector
in the caller's transaction (P5). The embedded content is a deterministic string built from
name + kind + signature + docstring + a trimmed body snippet, so the same source always produces
the same content and, with a pinned encoder, the same vector (P2 determinism). Adding the body
snippet improved retrieval precision but requires a re-index of previously embedded repos.

Content is collected first and encoded in one batch (``encode_many``) so batch-API encoders like
Voyage make a single request per file fragment (rate-limit friendly).
"""

from __future__ import annotations

from dataclasses import dataclass

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
        str(props.get("body") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()


@dataclass(slots=True)
class _Pending:
    kind: str
    ref_id: str
    content: str


class Embedder:
    def __init__(self, store: VectorStore, encoder: Encoder | None = None) -> None:
        self.store = store
        self.encoder = encoder or build_default_encoder()

    def embed_fragment(self, fragment: GraphFragment, *, repo_id: str, indexed_at_commit: str) -> int:
        """Embed every Symbol (and File) in a fragment. Returns the number of rows written."""
        pending: list[_Pending] = []
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                content = _symbol_content(node.properties)
                if content:
                    pending.append(_Pending("symbol", node.node_id, content))
            elif node.label is NodeLabel.FILE:
                content = str(node.properties.get("path") or "")
                if content:
                    pending.append(_Pending("file", node.node_id, content))

        if not pending:
            return 0

        vectors = self.encoder.encode_many([p.content for p in pending])
        for item, vector in zip(pending, vectors, strict=True):
            self.store.upsert(
                kind=item.kind,
                ref_id=item.ref_id,
                repo_id=repo_id,
                content=item.content,
                model=self.encoder.model_id,
                embedding=vector,
                indexed_at_commit=indexed_at_commit,
            )
        return len(pending)
