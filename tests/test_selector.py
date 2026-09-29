"""Context selector (bce.core.selector): policy, request shape, fail-open behaviour, assembler hooks."""

from __future__ import annotations

import json

import pytest

from bce.config import Settings
from bce.core.assembler import assemble
from bce.core.selector import (
    JevClient,
    JevError,
    Selector,
    SelectorConfig,
    apply_policy,
    group_files,
    selector_from_settings,
)
from bce.core.selector.selector import (
    file_questions,
    file_state,
    parse_file_answers,
    parse_symbol_answers,
    symbol_questions,
    symbol_state,
)


def _item(sid: str, path: str, score: float, *, body: str = "", distance: int = 0) -> dict:
    return {
        "symbol_id": sid,
        "repo_id": "r",
        "name": sid.split("::")[-1],
        "kind": "function",
        "file_id": f"r:{path}",
        "line": 1,
        "graph_distance": distance,
        "score": score,
        "body": body or f"def {sid}(): pass",
    }


# a.py (2 symbols, engine's first file), b.py, c.py, d.py
ITEMS = [
    _item("a1", "a.py", 9.0, body="def a1():\n    return compute()\n" * 10),
    _item("b1", "b.py", 8.0),
    _item("a2", "a.py", 7.0),
    _item("c1", "c.py", 6.0),
    _item("d1", "d.py", 5.0),
]


def test_group_files_keeps_first_seen_order_and_strips_repo_prefix():
    groups = group_files(ITEMS)
    assert [g.path for g in groups] == ["a.py", "b.py", "c.py", "d.py"]
    assert [g.pos for g in groups] == [0, 1, 2, 3]
    assert [it["symbol_id"] for it in groups[0].items] == ["a1", "a2"]


def test_request_shape_one_question_per_file_and_symbol():
    groups = group_files(ITEMS)
    st, qs = file_state("fix a", "r", groups), file_questions(groups)
    assert set(qs) == {"f0", "f1", "f2", "f3"} == set(st["candidate_files"])
    assert all(q["type"] == "noul" and set(q["criteria"]) == {"true", "false"} for q in qs.values())
    assert st["candidate_files"]["f0"]["engine_rank"] == 1
    assert st["candidate_files"]["f0"]["path"] == "a.py"
    sst, sqs = symbol_state("fix a", "r", ITEMS), symbol_questions(ITEMS)
    assert set(sqs) == {f"s{i}" for i in range(5)} == set(sst["candidate_symbols"])
    assert all(q["type"] == "score" and len(q["criteria"]) == 4 for q in sqs.values())
    # body excerpts are bounded so K=50 stays well inside the model's context
    assert len(sst["candidate_symbols"]["s0"]["excerpt"]) <= 240


def test_policy_tiers_full_stub_dropped_and_prunes_symbols():
    groups = group_files(ITEMS)
    file_p = {"r:a.py": 0.2, "r:b.py": 0.9, "r:c.py": 0.1, "r:d.py": 0.01}
    sym = {"a1": 3.0, "a2": 0.0, "b1": 2.0, "c1": 1.0, "d1": 0.0}
    out, stats = apply_policy(ITEMS, groups, file_p, sym, SelectorConfig())
    tiers = [(it["file_id"], it["tier"]) for it in out]
    # b.py (p=.9) full and first; a.py always full as the engine's first file; c.py stub; d dropped
    assert tiers == [("r:b.py", "full"), ("r:a.py", "full"), ("r:c.py", "stub")]
    assert [it["symbol_id"] for it in out] == ["b1", "a1", "c1"]  # a2 pruned (score 0 < 1)
    assert out[2]["detail_level"] == "reference" and out[2]["summary"].startswith(
        "c.py - 1 candidate"
    )
    assert out[0]["file_relevance"] == 0.9 and out[0]["symbol_relevance"] == 2.0
    assert stats == {
        "files_in": 4,
        "files_full": 2,
        "files_stub": 1,
        "files_dropped": 1,
        "symbols_in": 5,
        "symbols_out": 3,
        "symbols_dropped_in_full_files": 1,
        "symbol_pruning": True,
    }


def test_policy_never_empties_a_full_file_and_respects_max_files():
    groups = group_files(ITEMS)
    file_p = {"r:a.py": 0.9, "r:b.py": 0.9, "r:c.py": 0.9, "r:d.py": 0.9}
    sym = {"a1": 0.0, "a2": 0.0, "b1": 0.0, "c1": 0.0, "d1": 0.0}
    out, stats = apply_policy(ITEMS, groups, file_p, sym, SelectorConfig(max_files=2))
    assert stats["files_full"] == 2 and stats["files_stub"] == 0 and stats["files_dropped"] == 2
    # a.py keeps its best symbol although every symbol scored 0
    assert [it["symbol_id"] for it in out] == ["a1", "b1"]


def test_policy_round_robins_full_files_so_a_tight_budget_keeps_each_one():
    items = [_item(f"a{i}", "a.py", 9.0 - i) for i in range(4)] + [_item("b1", "b.py", 1.0)]
    groups = group_files(items)
    sym = {"a0": 2.0, "a1": 3.0, "a2": 2.0, "a3": 2.0, "b1": 2.0}
    out, _ = apply_policy(items, groups, {"r:a.py": 0.9, "r:b.py": 0.8}, sym, SelectorConfig())
    # a.py's best symbol (a1 by symbol score), then b.py's, then a.py's remaining symbols
    assert [it["symbol_id"] for it in out] == ["a1", "b1", "a0", "a2", "a3"]


def test_policy_without_symbol_scores_keeps_all_symbols_of_full_files():
    groups = group_files(ITEMS)
    out, stats = apply_policy(ITEMS, groups, {"r:a.py": 0.9}, None, SelectorConfig())
    assert [it["symbol_id"] for it in out] == ["a1", "a2"]
    assert stats["symbol_pruning"] is False and "symbol_relevance" not in out[0]


def _fake_transport(file_answers: dict | Exception, sym_answers: dict | Exception):
    calls = []

    def transport(url, body, headers, timeout):
        req = json.loads(body)
        calls.append(req)
        first = next(iter(req["questions"]))
        ans = file_answers if first.startswith("f") else sym_answers
        if isinstance(ans, Exception):
            raise ans
        return {"answers": ans, "usage": {"input_tokens": 100, "cost": 0.000004}}

    return transport, calls


def _client(transport) -> JevClient:
    return JevClient(
        api_key="k", model="typesafe/jev-1.13", url="http://x", timeout=1.0, transport=transport
    )


def test_selector_end_to_end_with_fake_model():
    transport, calls = _fake_transport(
        {"f0": {"noul": 0.3}, "f1": {"noul": 0.8}, "f2": {"noul": 0.06}, "f3": {"noul": 0.0}},
        {f"s{i}": {"score": s} for i, s in enumerate([3.0, 2.5, 0.2, 1.0, 0.0])},
    )
    out, info = Selector(_client(transport)).select(ITEMS, task_text="fix b")
    assert len(calls) == 2 and {c["model"] for c in calls} == {"typesafe/jev-1.13"}
    assert (
        info["status"] == "ok"
        and info["input_tokens"] == 200
        and info["cost_usd"] == pytest.approx(8e-6)
    )
    assert [(it["file_id"], it["tier"]) for it in out] == [
        ("r:b.py", "full"),
        ("r:a.py", "full"),
        ("r:c.py", "stub"),
    ]
    assert info["files_dropped"] == 1 and info["policy"]["full_threshold"] == 0.5
    # the assembler renders the stub as one reference line carrying the selector's fields
    pkg = assemble(out, max_tokens=10_000)
    stub = pkg["items"][-1]
    assert stub["detail_level"] == "reference" and stub["content"].startswith("c.py - 1 candidate")
    assert stub["tier"] == "stub" and stub["file_relevance"] == 0.06
    assert pkg["items"][0]["tier"] == "full" and pkg["items"][0]["symbol_relevance"] == 2.5


def test_selector_fails_open_when_file_decision_fails():
    transport, _ = _fake_transport(JevError("HTTP 503"), {})
    out, info = Selector(_client(transport)).select(ITEMS, task_text="t")
    assert out == ITEMS and info["status"] == "error" and "503" in info["reason"]


def test_selector_partial_when_only_symbol_decision_fails():
    transport, _ = _fake_transport(
        {"f0": {"noul": 0.9}, "f1": {"noul": 0.0}, "f2": {"noul": 0.0}, "f3": {"noul": 0.0}},
        JevError("timeout"),
    )
    out, info = Selector(_client(transport)).select(ITEMS, task_text="t")
    assert info["status"] == "partial" and info["symbol_pruning"] is False
    assert [it["symbol_id"] for it in out] == ["a1", "a2"]


def test_selector_skips_empty_input():
    transport, calls = _fake_transport({}, {})
    out, info = Selector(_client(transport)).select([], task_text="t")
    assert out == [] and info["status"] == "skipped" and calls == []


def test_parse_answers_tolerate_missing_or_malformed_values():
    groups = group_files(ITEMS)
    p = parse_file_answers({"f0": {"noul": "0.7"}, "f1": {"noul": 5}, "f2": {}}, groups)
    assert p == {"r:a.py": 0.7, "r:b.py": 1.0, "r:c.py": 0.0, "r:d.py": 0.0}
    s = parse_symbol_answers({"s0": {"score": "x"}, "s1": {"score": 2.2}}, ITEMS)
    assert s["a1"] == 0.0 and s["b1"] == 2.2 and s["d1"] == 0.0


def test_client_rejects_response_without_answers():
    client = _client(lambda *a: {"error": "nope"})
    with pytest.raises(JevError):
        client.decide({}, {})


def test_selector_from_settings_modes(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BCE_OPENROUTER_API_KEY", raising=False)
    assert (
        selector_from_settings(Settings(_env_file=None, selector="off", openrouter_api_key="k"))
        is None
    )
    assert (
        selector_from_settings(Settings(_env_file=None, selector="auto")) is None
    )  # no key -> off
    assert (
        selector_from_settings(Settings(_env_file=None, selector="jev")) is None
    )  # no key -> warn + off
    sel = selector_from_settings(
        Settings(_env_file=None, selector="auto", openrouter_api_key="k", selector_max_files=7)
    )
    assert sel is not None and sel.cfg.max_files == 7 and sel.client.model == "typesafe/jev-1.13"
    # the un-prefixed key name is accepted
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert Settings(_env_file=None).openrouter_api_key == "sk-or-test"


def test_assembler_reserves_budget_for_stubs():
    big = "x" * 4000  # ~1000 tokens per full body
    items = [
        {"symbol_id": f"f{i}", "graph_distance": 0, "body": big, "tier": "full"} for i in range(3)
    ]
    items += [
        {
            "symbol_id": f"s{i}",
            "graph_distance": 0,
            "detail_level": "reference",
            "tier": "stub",
            "summary": f"p{i}.py - 2 candidate symbol(s): a, b",
        }
        for i in range(4)
    ]
    pkg = assemble(items, max_tokens=1500)
    tiers = [it["tier"] for it in pkg["items"]]
    # bodies no longer crowd out the listings: every stub fits, the full tier keeps the rest
    assert tiers.count("stub") == 4
    assert tiers[0] == "full" and pkg["used_tokens"] <= 1500


def test_precontext_renders_stub_summary():
    from bce.integrations.precontext import render_context_markdown

    payload = {
        "coverage": {"confidence": "high"},
        "context": {
            "items": [
                {
                    "symbol_id": "a",
                    "repo_id": "r",
                    "file_id": "r:a.py",
                    "line": 3,
                    "name": "f",
                    "kind": "function",
                    "graph_distance": 0,
                    "detail_level": "full",
                    "content": "def f(): ...",
                    "tier": "full",
                },
                {
                    "symbol_id": "b",
                    "repo_id": "r",
                    "file_id": "r:b.py",
                    "line": 1,
                    "name": "g",
                    "kind": "function",
                    "graph_distance": 0,
                    "detail_level": "reference",
                    "content": "b.py - 2 candidate symbol(s): g, h",
                    "tier": "stub",
                },
            ]
        },
    }
    text = render_context_markdown(payload)
    assert "- `b.py` stub: 2 candidate symbol(s): g, h" in text
    assert "Entries marked `stub`" in text
    assert "- `a.py:3` function `f` d0" in text


def test_get_context_for_task_picks_k_from_the_selector(monkeypatch):
    from bce.core.defaults import DEFAULT_MAX_CANDIDATES, SELECTED_MAX_CANDIDATES
    from bce.tools.layer3 import orchestration as orch

    seen = {}

    class _Stop(Exception):
        pass

    def fake_build_anchors(repository, **kw):
        seen["k"] = kw["max_candidates"]
        raise _Stop

    monkeypatch.setattr(orch, "_build_anchors", fake_build_anchors)
    for configured, select, expected in (
        (object(), True, SELECTED_MAX_CANDIDATES),
        (None, True, DEFAULT_MAX_CANDIDATES),
        (object(), False, DEFAULT_MAX_CANDIDATES),
    ):
        monkeypatch.setattr(orch, "selector_from_settings", lambda c=configured: c)
        with pytest.raises(_Stop):
            orch.get_context_for_task(object(), task_text="t", store=None, select=select)
        assert seen["k"] == expected
    with pytest.raises(_Stop):
        orch.get_context_for_task(object(), task_text="t", store=None, max_candidates=7)
    assert seen["k"] == 7


def test_assembler_honours_requested_detail_level_and_summary():
    items = [
        {
            "symbol_id": "x",
            "graph_distance": 0,
            "body": "def x(): ...",
            "detail_level": "reference",
            "summary": "p.py - 3 symbols",
        },
        {"symbol_id": "y", "graph_distance": 0, "body": "def y(): ...", "detail_level": "bogus"},
    ]
    pkg = assemble(items, max_tokens=1000)
    assert (
        pkg["items"][0]["detail_level"] == "reference"
        and pkg["items"][0]["content"] == "p.py - 3 symbols"
    )
    assert (
        pkg["items"][1]["detail_level"] == "full" and pkg["items"][1]["content"] == "def y(): ..."
    )
