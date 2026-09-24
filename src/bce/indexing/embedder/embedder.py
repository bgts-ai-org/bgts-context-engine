"""Embedder (write path, Phase 2).

Turns the symbols/files of a :class:`GraphFragment` into embedding rows and writes them to pgvector
in the caller's transaction (P5). The embedded content is a deterministic string built from a
**header** (name, kind, file path, signature, docstring) and the symbol's **search text** - the
whole declaration as the extractor saw it - so the same source always produces the same content
and, with a pinned encoder, the same vector (P2 determinism).

A symbol longer than one embedding window is written as several **chunks** (migration 0011): the
header is repeated on every chunk and the body is split into overlapping windows at line
boundaries, so a toast string 200 lines into a page component is as findable as the component's
first hook. One vector per symbol - the previous design, over a 1200-character snippet - left
everything past the snippet invisible to the semantic channel, which on a React/TypeScript front
end was most of every page. Search collapses the chunks back to one symbol at its best rank.

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
from bce.domain.models import GraphFragment, GraphNode
from bce.indexing.embedder.encoder import Encoder, build_default_encoder
from bce.storage.vector.store import VectorStore

#: Deferred-mode flush ceilings. They mirror ``VoyageEncoder``'s request limits so that a flush
#: normally maps onto a single request; a smaller encoder limit still splits it correctly.
FLUSH_TEXTS = 500
FLUSH_CHARS = 400_000

#: Characters of search text per chunk (~500 tokens of code): small enough that a chunk is
#: *about* one thing - a mutation and its toasts, a hook and its effect - large enough to keep a
#: whole small function together with its header.
CHUNK_CHARS = 2000
#: Overlap between consecutive chunks so a statement cut at a window edge is whole in one of them.
CHUNK_OVERLAP = 200
#: Chunks per symbol. With :data:`CHUNK_CHARS` this covers the extractor's search-text cap.
MAX_CHUNKS = 12


def _path_of(file_id: str | None) -> str:
    """``repo:path/to/file.ts`` -> ``path/to/file.ts`` (the repo prefix carries no meaning)."""
    text = str(file_id or "")
    return text.split(":", 1)[1] if ":" in text else text


def symbol_header(props: dict) -> str:
    """The identity every chunk of a symbol carries: name, kind, file path, signature, docstring."""
    parts = [
        str(props.get("name") or ""),
        str(props.get("kind") or ""),
        _path_of(props.get("file_id")),
        str(props.get("signature") or ""),
        str(props.get("docstring") or ""),
    ]
    return "\n".join(p for p in parts if p).strip()


def split_windows(
    text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP, limit: int = MAX_CHUNKS
) -> list[str]:
    """Split ``text`` into at most ``limit`` overlapping windows of about ``size`` characters.

    Windows end at the last line break before the size limit when one exists in the second half
    of the window (so statements stay whole), else at the hard limit. Deterministic; ``[]`` for
    empty text; a text that fits returns itself.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    windows: list[str] = []
    start = 0
    while start < len(text) and len(windows) < limit:
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        windows.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [w for w in windows if w]


def symbol_contents(node: GraphNode) -> list[str]:
    """Embedding contents (one per chunk) for a Symbol node.

    The header alone is a valid content for a symbol without any text (an abstract method); a
    symbol with text yields ``header + window`` per window of its search text (or, before the
    extractor learned search text, of its body snippet).
    """
    props = node.properties
    header = symbol_header(props)
    text = node.search_text if node.search_text is not None else str(props.get("body") or "")
    windows = split_windows(text)
    if not windows:
        return [header] if header else []
    return [f"{header}\n{window}".strip() if header else window for window in windows]


def _symbol_content(props: dict) -> str:
    """Single-content form kept for callers that embed ad hoc (header + snippet, no chunking)."""
    return symbol_contents(GraphNode(NodeLabel.SYMBOL, "", dict(props)))[0]


@dataclass(slots=True)
class _Pending:
    kind: str
    ref_id: str
    content: str
    repo_id: str
    indexed_at_commit: str
    chunk: int = 0


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
        A symbol's stale chunks (from a longer, earlier version) are dropped right away so the
        row set after the flush is exactly this version's chunks.
        """
        pending: list[_Pending] = []
        trim = getattr(self.store, "trim_chunks", None)
        for node in fragment.nodes:
            if node.label is NodeLabel.SYMBOL:
                contents = symbol_contents(node)
                if not contents:
                    continue
                if callable(trim):
                    trim("symbol", node.node_id, len(contents))
                for index, content in enumerate(contents):
                    pending.append(
                        _Pending("symbol", node.node_id, content, repo_id, indexed_at_commit, index)
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
                chunk=item.chunk,
            )
