"""Deferred (cross-file batched) embedding writes."""

from __future__ import annotations

from typing import Any

from bce.domain.enums import NodeLabel
from bce.domain.models import GraphFragment, GraphNode
from bce.indexing.embedder import embedder as embedder_module
from bce.indexing.embedder.embedder import Embedder


class _Encoder:
    model_id = "fake-3"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def encode_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [[float(len(t)), 0.0, 0.0] for t in texts]


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def upsert(self, **row: Any) -> None:
        self.rows.append(row)


def _fragment(prefix: str, symbols: int) -> GraphFragment:
    fragment = GraphFragment()
    fragment.add_node(GraphNode(NodeLabel.FILE, f"repo:{prefix}.py", {"path": f"{prefix}.py"}))
    for i in range(symbols):
        fragment.add_node(
            GraphNode(
                NodeLabel.SYMBOL,
                f"py::{prefix}::::fn{i}#x",
                {"name": f"fn{i}", "kind": "function", "signature": f"def fn{i}()"},
            )
        )
    return fragment


def test_immediate_mode_encodes_once_per_fragment() -> None:
    encoder, store = _Encoder(), _Store()
    emb = Embedder(store, encoder)  # type: ignore[arg-type]
    for name in ("a", "b", "c"):
        emb.embed_fragment(_fragment(name, 2), repo_id="repo", indexed_at_commit="c1")
    assert encoder.calls == [3, 3, 3]
    assert len(store.rows) == 9 and emb.requests == 3


def test_deferred_mode_batches_across_files_and_flushes_the_tail() -> None:
    encoder, store = _Encoder(), _Store()
    emb = Embedder(store, encoder, defer=True)  # type: ignore[arg-type]
    for name in ("a", "b", "c"):
        assert emb.embed_fragment(_fragment(name, 2), repo_id="repo", indexed_at_commit="c1") == 3
    # Nothing has been encoded or written yet.
    assert encoder.calls == [] and store.rows == [] and emb.pending == 9
    assert emb.flush() == 9
    assert encoder.calls == [9] and emb.requests == 1
    assert emb.pending == 0 and emb.flush() == 0
    # Every row keeps its own repo / commit / model and is written in collection order.
    assert [r["ref_id"] for r in store.rows][:2] == ["repo:a.py", "py::a::::fn0#x"]
    assert {r["model"] for r in store.rows} == {"fake-3"}
    assert {r["indexed_at_commit"] for r in store.rows} == {"c1"}


def test_deferred_mode_flushes_at_the_text_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(embedder_module, "FLUSH_TEXTS", 4)
    encoder, store = _Encoder(), _Store()
    emb = Embedder(store, encoder, defer=True)  # type: ignore[arg-type]
    for name in ("a", "b", "c"):  # 3 rows each -> flush after the 4th row lands
        emb.embed_fragment(_fragment(name, 2), repo_id="repo", indexed_at_commit="c1")
    emb.flush()
    assert encoder.calls == [4, 4, 1]
    assert len(store.rows) == 9


def test_deferred_mode_flushes_at_the_char_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(embedder_module, "FLUSH_CHARS", 40)
    encoder, store = _Encoder(), _Store()
    emb = Embedder(store, encoder, defer=True)  # type: ignore[arg-type]
    emb.embed_fragment(_fragment("a", 3), repo_id="repo", indexed_at_commit="c1")
    emb.flush()
    # No single request exceeds the ceiling unless one text alone is bigger than it.
    assert all(n < 4 for n in encoder.calls) and sum(encoder.calls) == 4
