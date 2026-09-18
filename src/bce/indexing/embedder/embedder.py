"""Embedder (write path, Phase 2).

Turns the symbols/files of a :class:`GraphFragment` into embedding rows and writes them to pgvector
in the caller's transaction (P5). The embedded content is a deterministic string built from
name + kind + signature + docstring + a trimmed body snippet, so the same source always produces
the same content and, with a pinned encoder, the same vector (P2 determinism). Adding the body
snippet improved retrieval precision but requires a re-index of previously embedded repos.

Content is collected first and encoded in one batch (``encode_many``). In **deferred** mode the
batch spans files: fragments are buffered until :data:`FLUSH_TEXTS` texts or :data:`FLUSH_CHARS`
characters are pending (the encoder's own per-request ceilings, so one flush is one API request)
and the indexer flushes the tail after its extraction pass. A repository of thousands of small
files therefore costs dozens of embedding requests instead of one per file - on a batch API with
per-request latency that was the whole indexing wall clock. Nothing is committed in between: the
graph rows and the embedding rows of the whole run still land in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from bce.domain.enums import NodeLabel
from bce.domain.models import GraphFragment
from bce.indexing.embedder.encoder import Encoder, build_default_encoder
from bce.storage.vector.store import VectorStore

#: Deferred-mode flush ceilings. They mirror ``VoyageEncoder``'s request limits so that a flush
#: normally maps onto a single request; a smaller encoder limit still splits it correctly.
FLUSH_TEXTS = 500
FLUSH_CHARS = 400_000


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
    repo_id: str
    indexed_at_commit: str


class Embedder:
    def __init__(
        self, store: VectorStore, encoder: Encoder | None = None, *, defer: bool = False
    ) -> None:
        self.store = store
        self.encoder = encoder or build_default_encoder()
        #: Buffer fragments and encode across files (see module docstring). The caller must call
        #: :meth:`flush` before committing.
        self.defer = defer
        self._pending: list[_Pending] = []
        self._pending_chars = 0
        #: Embedding requests issued so far (observability: one per flush / per file).
        self.requests = 0

    def embed_fragment(
        self, fragment: GraphFragment, *, repo_id: str, indexed_at_commit: str
    ) -> int:
        """Embed every Symbol (and File) in a fragment. Returns the number of rows collected.

        In deferred mode the rows are written by a later :meth:`flush`; otherwise immediately.
        """
        pending: list[_Pending] = []
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                content = _symbol_content(node.properties)
                if content:
                    pending.append(
                        _Pending("symbol", node.node_id, content, repo_id, indexed_at_commit)
                    )
            elif node.label is NodeLabel.FILE:
                content = str(node.properties.get("path") or "")
                if content:
                    pending.append(
                        _Pending("file", node.node_id, content, repo_id, indexed_at_commit)
                    )

        if not pending:
            return 0
        if not self.defer:
            self._write(pending)
            return len(pending)

        for item in pending:
            if self._pending and (
                len(self._pending) >= FLUSH_TEXTS
                or self._pending_chars + len(item.content) > FLUSH_CHARS
            ):
                self.flush()
            self._pending.append(item)
            self._pending_chars += len(item.content)
        return len(pending)

    @property
    def pending(self) -> int:
        """Rows buffered and not yet written (deferred mode)."""
        return len(self._pending)

    def flush(self) -> int:
        """Encode and write everything buffered. Returns the number of rows written."""
        if not self._pending:
            return 0
        batch, self._pending, self._pending_chars = self._pending, [], 0
        self._write(batch)
        return len(batch)

    def _write(self, batch: list[_Pending]) -> None:
        vectors = self.encoder.encode_many([p.content for p in batch])
        self.requests += 1
        for item, vector in zip(batch, vectors, strict=True):
            self.store.upsert(
                kind=item.kind,
                ref_id=item.ref_id,
                repo_id=item.repo_id,
                content=item.content,
                model=self.encoder.model_id,
                embedding=vector,
                indexed_at_commit=item.indexed_at_commit,
            )
