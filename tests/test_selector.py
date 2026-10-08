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
        # dropped files stay nameable: the engine ranked them, only the model voted them down
        "dropped_files": [{"file_id": "r:d.py", "path": "d.py", "file_relevance": 0.01}],
        "symbols_in": 5,
        "symbols_out": 3,
        "symbols_dropped_in_full_files": 1,
        "symbol_pruning": True,
        "files_pinned": 0,
        "files_pinned_to_full": 0,
        "files_demoted_by_budget": 0,
        "full_bytes": 324,
        "listing_capped": False,
    }


def test_policy_never_empties_a_full_file_and_caps_only_the_stub_listing():
    groups = group_files(ITEMS)
    sym = {"a1": 0.0, "a2": 0.0, "b1": 0.0, "c1": 0.0, "d1": 0.0}
    # the cut is by probability: four files above the full threshold are all shown in full,
    # whatever ``max_files`` says
    file_p = {"r:a.py": 0.9, "r:b.py": 0.9, "r:c.py": 0.9, "r:d.py": 0.9}
    out, stats = apply_policy(ITEMS, groups, file_p, sym, SelectorConfig(max_files=2))
    assert stats["files_full"] == 4 and stats["files_dropped"] == 0
    # a.py keeps its best symbol although every symbol scored 0
    assert [it["symbol_id"] for it in out] == ["a1", "b1", "c1", "d1"]
    # ...while the stub listing stops at the cap and says so
    file_p = {"r:a.py": 0.9, "r:b.py": 0.3, "r:c.py": 0.2, "r:d.py": 0.1}
    out, stats = apply_policy(ITEMS, groups, file_p, sym, SelectorConfig(max_files=2))
    assert [(it["file_id"], it["tier"]) for it in out] == [("r:a.py", "full"), ("r:b.py", "stub")]
    assert stats["listing_capped"] is True and stats["files_dropped"] == 2


def test_policy_pins_files_the_task_names_against_the_models_vote():
    groups = group_files(ITEMS)
    file_p = {"r:a.py": 0.9, "r:b.py": 0.9, "r:c.py": 0.01, "r:d.py": 0.0}
    cfg = SelectorConfig(pinned_full_limit=1)
    out, stats = apply_policy(
        ITEMS, groups, file_p, None, cfg, pinned={"r:c.py", "r:d.py", "r:zzz.py"}
    )
    tiers = {it["file_id"]: it["tier"] for it in out}
    # c.py (the better pinned file) is promoted to full; d.py exceeds the promotion limit but
    # is still listed; a file not among the items is ignored
    assert tiers == {"r:a.py": "full", "r:b.py": "full", "r:c.py": "full", "r:d.py": "stub"}
    assert all(it.get("pinned") for it in out if it["file_id"] in ("r:c.py", "r:d.py"))
    assert stats["files_pinned"] == 2 and stats["files_pinned_to_full"] == 1
    assert stats["files_dropped"] == 0 and stats["dropped_files"] == []


def test_policy_demotes_least_probable_full_files_over_the_byte_budget():
    items = [
        _item("a1", "a.py", 9.0, body="x" * 3000),
        _item("b1", "b.py", 8.0, body="y" * 3000),
        _item("c1", "c.py", 7.0, body="z" * 3000),
        _item("p1", "p.py", 6.0, body="w" * 3000),
    ]
    groups = group_files(items)
    file_p = {"r:a.py": 0.6, "r:b.py": 0.95, "r:c.py": 0.7, "r:p.py": 0.55}
    cfg = SelectorConfig(full_content_bytes=6500)
    out, stats = apply_policy(items, groups, file_p, None, cfg, pinned={"r:p.py"})
    tiers = [(it["file_id"], it["tier"]) for it in out]
    # a.py (engine's first) and p.py (pinned) are exempt; c.py is the least probable of the
    # rest and is demoted first (12000 -> 9000 bytes), still over budget so b.py follows
    # (-> 6000); both are listed as stubs, best first.
    assert tiers == [
        ("r:a.py", "full"),
        ("r:p.py", "full"),
        ("r:b.py", "stub"),
        ("r:c.py", "stub"),
    ]
    assert stats["files_demoted_by_budget"] == 2 and stats["full_bytes"] == 6000
    # the budget off: everything above the threshold stays full
    out, stats = apply_policy(items, groups, file_p, None, SelectorConfig(full_content_bytes=0))
    assert stats["files_full"] == 4 and stats["files_demoted_by_budget"] == 0


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


def test_selector_returns_unselected_when_the_file_decision_outlives_the_budget():
    import threading

    release = threading.Event()

    def transport(url, body, headers, timeout):
        first = next(iter(json.loads(body)["questions"]))
        if first.startswith("f"):
            release.wait(5.0)  # a server that accepted the request and stalls
        return {"answers": {}, "usage": {}}

    client = JevClient(api_key="k", model="m", url="http://x", timeout=0.2, transport=transport)
    t0 = __import__("time").perf_counter()
    out, info = Selector(client).select(ITEMS, task_text="t")
    elapsed = __import__("time").perf_counter() - t0
    release.set()
    # budget = max(1, timeout) + grace
    assert out == ITEMS and info["status"] == "timeout" and "1.5 s" in info["reason"]
    assert elapsed < 3.0  # the stalled request was not waited for


def test_selector_does_not_wait_for_the_symbol_decision_past_the_budget():
    import threading

    release = threading.Event()

    def transport(url, body, headers, timeout):
        first = next(iter(json.loads(body)["questions"]))
        if first.startswith("s"):
            release.wait(5.0)
            return {"answers": {}, "usage": {}}
        return {
            "answers": {"f0": {"noul": 0.9}, "f1": {"noul": 0.0}, "f2": {"noul": 0.0}},
            "usage": {},
        }

    client = JevClient(api_key="k", model="m", url="http://x", timeout=0.2, transport=transport)
    out, info = Selector(client).select(ITEMS, task_text="t")
    release.set()
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


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_selector_from_settings_modes(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BCE_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BCE_SELECTOR", raising=False)
    # off by default, even with a key configured: the selector is chosen explicitly
    assert selector_from_settings(_settings(openrouter_api_key="k")) is None
    assert selector_from_settings(_settings(selector="off", openrouter_api_key="k")) is None
    assert selector_from_settings(_settings(selector="jev")) is None  # no key -> warn + off
    assert selector_from_settings(_settings(selector="bogus", openrouter_api_key="k")) is None
    sel = selector_from_settings(
        _settings(selector="jev", openrouter_api_key="k", selector_max_files=7)
    )
    assert sel is not None and sel.cfg.max_files == 7 and sel.method == "jev"
    c = sel.client
    assert (c.model, c.url, c.timeout, c.api_key) == (
        "typesafe/jev-1.13",
        "https://openrouter.ai/api/alpha/decisions",
        3.0,
        "k",
    )
    # Jev on TypeSafe's own API: its key and URL win over the OpenRouter defaults
    sel = selector_from_settings(
        _settings(
            selector="jev",
            openrouter_api_key="or",
            selector_api_key="ts",
            selector_url="https://api.typesafe.ai/v1/systemone",
            selector_model="jev-latest",
        )
    )
    assert (sel.client.api_key, sel.client.model) == ("ts", "jev-latest")
    # the un-prefixed key name is accepted
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert Settings(_env_file=None).openrouter_api_key == "sk-or-test"


@pytest.mark.parametrize("mode", ["decider-2b", "decider-4b", "DECIDER_4B"])
def test_selector_from_settings_decider_needs_no_key(monkeypatch, mode):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BCE_OPENROUTER_API_KEY", raising=False)
    sel = selector_from_settings(_settings(selector=mode, openrouter_api_key="or"))
    name = mode.lower().replace("_", "-")
    assert sel is not None and sel.method == name and sel.expect_served == name
    c = sel.client
    # an OpenRouter key is never forwarded to a self-hosted server
    assert (c.model, c.url, c.timeout, c.api_key) == (
        name,
        "http://127.0.0.1:8000/v1/systemone",
        10.0,
        "",
    )
    sel = selector_from_settings(
        _settings(
            selector=mode,
            selector_url="http://gpu:11049/v1/systemone",
            selector_timeout=30,
            selector_api_key="proxy",
        )
    )
    assert (sel.client.url, sel.client.timeout, sel.client.api_key) == (
        "http://gpu:11049/v1/systemone",
        30.0,
        "proxy",
    )


def test_client_sends_bearer_only_with_a_key():
    seen = []

    def transport(url, body, headers, timeout):
        seen.append(headers)
        return {"answers": {}, "model": "decider-4b-v2.1"}

    for key in ("", "k"):
        d = JevClient(api_key=key, model="m", url="http://x", timeout=1.0, transport=transport)
        assert d.decide({}, {}).served_model == "decider-4b-v2.1"
    assert "Authorization" not in seen[0] and seen[1]["Authorization"] == "Bearer k"


def test_selector_reports_served_model_and_warns_on_mismatch(monkeypatch):
    from bce.core.selector import selector as selector_mod

    warnings: list[tuple] = []
    monkeypatch.setattr(selector_mod.logger, "warning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(selector_mod, "_MISMATCH_LOGGED", set())

    def transport(url, body, headers, timeout):
        first = next(iter(json.loads(body)["questions"]))
        ans = {f"f{i}": {"noul": 0.9} for i in range(4)} if first.startswith("f") else {}
        return {"answers": ans, "model": "decider-4b-v2.1"}

    client = JevClient(
        api_key="", model="decider-2b", url="http://x", timeout=1.0, transport=transport
    )
    _, info = Selector(client, method="decider-2b", expect_served="decider-2b").select(
        ITEMS, task_text="t"
    )
    Selector(client, method="decider-2b", expect_served="decider-2b").select(ITEMS, task_text="t")
    assert info["method"] == "decider-2b" and info["model"] == "decider-2b"
    assert info["served_model"] == "decider-4b-v2.1" and "cost_usd" not in info
    # once per process although every request builds its own selector
    assert [w for w in warnings if "decider-4b-v2.1" in w] == [warnings[0]] and len(warnings) == 1
    Selector(client, method="decider-4b", expect_served="decider-4b").select(ITEMS, task_text="t")
    assert len(warnings) == 1


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
