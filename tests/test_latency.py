"""Latency work: stage timings, the overlapped and cached query embedding, unranked word pairs."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

import pytest

import bce.indexing.embedder.encoder as enc_module
from bce.core.orchestrator import bulk
from bce.core.orchestrator.anchors import _lexical_anchors, find_anchors
from bce.tools.layer3 import orchestration as orch


class _Repo:
    """setup -> helper, both in r1."""

    def resolve_symbol(self, name, repo_id=None):
        if name in ("setup", "helper"):
            return [{"symbol_id": name, "name": name, "file_id": f"r1:{name}.py", "line": 1}]
        return []

    def find_routes(self, path_substring=None):
        return []

    def repo_of_symbol(self, sid):
        return "r1" if sid in ("setup", "helper") else None

    def get_symbol(self, sid):
        if sid in ("setup", "helper"):
            return {"symbol_id": sid, "name": sid, "kind": "function", "file_id": f"r1:{sid}.py"}
        return None

    def symbols_in_file(self, fid):
        return []

    def get_callers(self, sid):
        return [{"symbol_id": "setup"}] if sid == "helper" else []

    def get_callees(self, sid):
        return [{"symbol_id": "helper"}] if sid == "setup" else []

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def find_implementers(self, sid):
        return []

    def symbol_degree(self, sid):
        return 1

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def get_design_notes(self, sid):
        return []


class _Store:
    def __init__(self, ids, model="m-1"):
        self.ids, self.model = ids, model
        self.searches = 0

    def stored_model(self, kind="symbol"):
        return self.model

    def search(self, vector, *, limit=20, repo_ids=None, kind=None):
        self.searches += 1
        return [{"ref_id": sid} for sid in self.ids[:limit]]


class _Enc:
    model_id = "m-1"

    def __init__(self, delay=0.0):
        self.delay = delay
        self.threads: list[str] = []

    def encode_query(self, text):
        self.threads.append(threading.current_thread().name)
        time.sleep(self.delay)
        return [0.5]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(orch, "_query_vectors", OrderedDict())
    monkeypatch.setattr(orch, "selector_from_settings", lambda: None)


def _context(store, **kw):
    return orch.get_context_for_task(
        _Repo(), task_text="fix setup which calls helper", store=store, select=False, **kw
    )


def test_the_envelope_carries_stage_timings_beside_the_payload(monkeypatch):
    monkeypatch.setattr(enc_module, "build_default_encoder", lambda: _Enc())
    result = _context(_Store(["helper"]))
    timings = result["timings"]
    assert {"encode_ms", "semantic_ms", "anchors_ms", "retrieve_ms", "total_ms"} <= set(timings)
    assert all(v >= 0 for v in timings.values())
    assert timings["total_ms"] >= timings["anchors_ms"]
    assert "timings" not in result["payload"]


def test_the_query_is_embedded_off_the_calling_thread_once_per_text(monkeypatch):
    encoder = _Enc()
    monkeypatch.setattr(enc_module, "build_default_encoder", lambda: encoder)
    store = _Store(["helper"])
    first = _context(store)
    second = _context(store)
    assert len(encoder.threads) == 1 and encoder.threads[0].startswith("bce-query-vector")
    assert second["timings"]["encode_ms"] == 0.0
    assert first["payload"] == second["payload"]
    assert store.searches == 2


def test_the_embedding_overlaps_the_sources_before_the_semantic_one(monkeypatch):
    encoder = _Enc(delay=0.2)
    monkeypatch.setattr(enc_module, "build_default_encoder", lambda: encoder)
    slow_sources = 0.15

    def slow_lexical(*args, **kwargs):
        time.sleep(slow_sources)
        return []

    monkeypatch.setattr("bce.core.orchestrator.anchors._lexical_anchors", slow_lexical)
    started = time.perf_counter()
    result = _context(_Store(["helper"]))
    elapsed = time.perf_counter() - started
    assert result["timings"]["encode_ms"] >= 200
    assert elapsed < 0.2 + slow_sources


def test_no_query_is_sent_for_an_index_embedded_with_another_model(monkeypatch):
    class _Refusing(_Enc):
        def encode_query(self, text):  # pragma: no cover - the guard must stop before it
            raise AssertionError("query encoded against a mismatched index")

    monkeypatch.setattr(enc_module, "build_default_encoder", lambda: _Refusing())
    store = _Store(["helper"], model="another-model")
    result = _context(store)
    assert "another-model" in result["payload"]["coverage"]["semantic_error"]
    assert store.searches == 0


def test_a_semantic_callable_is_read_once_and_ranks_like_the_list():
    calls = []

    def semantic():
        calls.append(1)
        return ["helper"]

    lazy = find_anchors(_Repo(), task_text="fix setup", semantic_candidates=semantic)
    eager = find_anchors(_Repo(), task_text="fix setup", semantic_candidates=["helper"])
    assert calls == [1]
    assert lazy.anchors == eager.anchors and lazy.strength == eager.strength


class _LexicalRepo:
    def __init__(self, *, with_flag=True):
        self.calls: list[tuple[tuple[str, ...], bool, bool]] = []
        if not with_flag:
            self.lexical_search_many = self._without_flag

    def lexical_search_many(
        self, terms, *, repo_ids=None, limit=20, exclude_tests=False, ranked=True
    ):
        self.calls.append((tuple(terms), ranked, exclude_tests))
        return {t: [] for t in terms}

    def _without_flag(self, terms, *, repo_ids=None, limit=20, exclude_tests=False):
        self.calls.append((tuple(terms), True, exclude_tests))
        return {t: [] for t in terms}


def test_word_pairs_are_looked_up_unranked():
    repo = _LexicalRepo()
    _lexical_anchors(repo, ["alpha", "beta"], None, False, phrases=["alpha beta"], pool=5)
    assert repo.calls == [(("alpha", "beta"), True, True), (("alpha beta",), False, True)]


def test_a_repository_without_the_flag_ranks_and_keeps_the_test_filter():
    repo = _LexicalRepo(with_flag=False)
    bulk.lexical_hits(
        repo, ["alpha beta"], repo_ids=None, limit=5, exclude_tests=True, ranked=False
    )
    assert repo.calls == [(("alpha beta",), True, True)]


def test_the_mcp_log_line_carries_the_stage_timings_and_render_time():
    from bce.api.mcp.server import _call_record

    result = {"payload": {}, "timings": {"anchors_ms": 4.0, "total_ms": 9.0}}
    record = _call_record(
        "get_context_for_task", "compact", result, "text", time.perf_counter(), 1.5
    )
    assert record["timings"] == {"anchors_ms": 4.0, "total_ms": 9.0, "render_ms": 1.5}
    assert "timings" not in _call_record("resolve_symbol", "debug", {"payload": {}}, "x", 0.0)


def test_the_cli_prints_the_stage_timings_on_stderr(capsys):
    from bce.cli import _echo_timings

    _echo_timings({"semantic_ms": 12.4, "total_ms": 30.0})
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "timings (ms): semantic 12 · total 30\n"
