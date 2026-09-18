"""Retrieval v2 regression tests: anchor evidence strength, stop-word filtering, test-symbol
handling, evidence-scaled scoring, diversity-aware narrowing and agreement-based confidence.

Each test pins one of the failure modes observed on natural-language tasks with the v1 pipeline
(dozens of single-word anchors + a flat anchor floor => the top-N was "highest-degree anchors").
"""

from __future__ import annotations

from typing import Any

import pytest

from bce.core.assembler import DetailLevel, assemble
from bce.core.coverage import compute_coverage
from bce.core.orchestrator import bulk
from bce.core.orchestrator.anchors import (
    EXPLICIT_CONTAINER_MEMBER_SCALE,
    LEXICAL_ANCHOR_LIMIT,
    SOURCE_STRENGTH,
    AnchorResult,
    find_anchors,
)
from bce.core.orchestrator.expand import (
    EXPAND_ROOT_LIMIT,
    HOP_DECAY,
    SIBLING_LIMIT,
    expand_from_anchors,
    expansion_roots,
    task_signal_for,
    to_candidates,
)
from bce.core.orchestrator.orchestrator import (
    RetrievalOrchestrator,
    RetrievalResult,
    narrow,
)
from bce.core.orchestrator.text import (
    content_tokens,
    explicit_candidates,
    explicit_references,
    is_test_symbol,
    looks_like_identifier,
    query_terms,
    split_identifier,
)
from bce.core.scoring.engine import (
    CONTAINER_MEMBER_PENALTY,
    Candidate,
    demote_redundant_containers,
    kind_prior_of,
    member_prefix_of,
    score_candidate,
    score_candidates,
)
from bce.domain.enums import Provenance, RefKind
from bce.tools.layer3.orchestration import AUTO_SEMANTIC_LIMIT, _auto_semantic_candidates

# --- text helpers ---------------------------------------------------------------------------


def test_content_tokens_drop_stopwords_and_split_identifiers():
    toks = content_tokens("Period comparison is missing the authorization check for diff_command")
    assert "the" not in toks and "missing" not in toks and "check" not in toks
    assert "period" in toks and "comparison" in toks and "authorization" in toks
    # Compound identifier kept whole and split into parts.
    assert "diff_command" in toks and "diff" in toks and "command" in toks


def test_turkish_task_text_tokenises_as_whole_words_and_drops_turkish_stopwords():
    toks = content_tokens("İptal edilen toplantılar şu takvimde görünmüyor")
    # Unicode-aware: the word is "toplantılar", not an ASCII prefix with the tail dropped.
    assert "toplantılar" in toks and "toplant" not in toks
    # Dotted capital İ folds to plain i; "şu" is a stop-word and never was matchable before.
    assert "iptal" in toks and "şu" not in toks
    assert all("\u0307" not in t for t in toks)  # no combining-dot leftovers


def test_query_terms_add_english_equivalents_of_turkish_domain_words():
    terms = query_terms("Kullanıcı bildirimleri gönderilmiyor, toplantı iptal edilince")
    # Suffixed forms resolve to the stem: bildirimleri -> bildirim, edilince has no entry.
    assert "user" in terms and "notification" in terms and "meeting" in terms
    assert "send" in terms and "cancel" in terms
    # Original tokens are kept, order is deterministic, nothing is duplicated.
    assert terms.index("kullanıcı") < terms.index("user")
    assert len(terms) == len(set(terms))
    # English text is untouched.
    english = "Fix the diff_command when period is empty"
    assert query_terms(english) == content_tokens(english)


def test_short_turkish_stems_only_match_exactly():
    # "al" (get) must not turn "alarm" or "algorithm" into "get"; "ses" must not fire on "session".
    assert "get" not in query_terms("alarm algorithm")
    assert "audio" not in query_terms("session")
    assert "audio" in query_terms("ses kaydı")


def test_turkish_stemming_needs_a_turkish_suffix_so_english_words_are_left_alone():
    # serial / series / listener / indirect / surely start with dictionary stems but are English.
    text = "serial series listener indirect surely serializer"
    assert query_terms(text) == content_tokens(text)  # nothing added
    # Real inflections still resolve: plural, possessive+case, passive+negation+tense, buffer y.
    assert "meeting" in query_terms("toplantılar")
    assert "meeting" in query_terms("toplantısını")
    assert "send" in query_terms("gönderilmiyor")
    assert "user" in query_terms("kullanıcıya")
    assert "refresh" in query_terms("yenilendi")
    # Equivalents that are themselves stop-words ("update", "add") stay out, like any stop-word.
    assert "update" not in query_terms("güncellendi")


def test_explicit_candidates_only_identifier_looking_tokens():
    text = "authorization check on Period; see `acting_user`, api.diff and getUser or MAX_RETRIES"
    explicit = explicit_candidates(text)
    assert "check" not in explicit  # plain English word
    assert "Period" not in explicit  # capitalised word, not an identifier
    assert "acting_user" in explicit  # backticks
    assert "diff" in explicit  # dotted reference tail
    assert "getUser" in explicit  # camelCase
    assert "MAX_RETRIES" in explicit  # constant


def test_qualified_reference_keeps_its_container_and_suppresses_the_bare_tail():
    refs = explicit_references("`MongoDbService.CancelAsync` and getUser are broken")
    assert ("MongoDbService", "CancelAsync") in refs
    # The tail was written qualified, so it is not offered bare as well.
    assert (None, "CancelAsync") not in refs
    # The container itself is still a name the author used, and unrelated tokens are unaffected.
    assert (None, "MongoDbService") in refs and (None, "getUser") in refs


def test_looks_like_identifier_rules():
    assert looks_like_identifier("diff_command")
    assert looks_like_identifier("loginHandler")
    assert looks_like_identifier("sha256")
    assert not looks_like_identifier("check")
    assert not looks_like_identifier("Period")
    assert not looks_like_identifier("the_")  # underscore only at the edge, stop-word core


def test_split_identifier_snake_camel_dotted():
    assert split_identifier("diff_command") == ["diff", "command"]
    assert split_identifier("getUserById") == ["get", "user", "by", "id"]
    assert split_identifier("HTTPServerError") == ["http", "server", "error"]


def test_is_test_symbol_from_paths_module_ids_and_names():
    assert is_test_symbol("python::tests.test_api_ui::::test_x#1")
    assert is_test_symbol("py::app::auth::x#1", file_id="repo:tests/test_auth.py")
    assert is_test_symbol("ts::src/foo.test::x#1", file_id="repo:src/foo.test.ts")
    assert is_test_symbol("go::pkg::x#1", file_id="repo:pkg/foo_test.go")
    assert is_test_symbol("java::com.acme::FooTest::x#1", file_id="repo:src/FooTest.java")
    assert is_test_symbol("py::app::mod::x#1", name="test_period_requires_auth")
    assert not is_test_symbol("python::src.bce.api::::diff#1", file_id="repo:src/bce/api.py")
    assert not is_test_symbol("py::app::latest::contest#1", file_id="repo:app/latest.py")


# --- anchors ---------------------------------------------------------------------------------


class _AnchorRepo:
    """Lexical index over a few symbols; ``pool_full`` terms simulate saturation."""

    def __init__(self) -> None:
        self.symbols: dict[str, dict[str, Any]] = {
            "s::diff": {"symbol_id": "s::diff", "name": "diff", "file_id": "r:api.py"},
            "s::diff_command": {
                "symbol_id": "s::diff_command",
                "name": "diff_command",
                "file_id": "r:cli.py",
            },
            "s::acting_user": {
                "symbol_id": "s::acting_user",
                "name": "acting_user",
                "file_id": "r:api.py",
            },
            "s::test_diff": {
                "symbol_id": "s::test_diff",
                "name": "test_diff_requires_auth",
                "file_id": "r:tests/test_api.py",
            },
            "s::Principal.check": {
                "symbol_id": "s::Principal.check",
                "name": "check",
                "file_id": "r:auth.py",
            },
        }
        self.lexical: dict[str, list[str]] = {
            "period": ["s::diff", "s::diff_command", "s::test_diff"],
            "comparison": ["s::diff"],
            "authorization": ["s::acting_user", "s::test_diff", "s::Principal.check"],
            "check": ["s::Principal.check"],
        }
        self.saturated: set[str] = set()
        self.explicit_calls: list[str] = []

    def resolve_symbol(self, name, repo_id=None):
        self.explicit_calls.append(name)
        return [s for s in self.symbols.values() if s["name"] == name]

    def find_routes(self, path):
        return []

    def symbols_in_file(self, file_id):
        return [s for s in self.symbols.values() if s["file_id"] == file_id]

    def lexical_search(self, term, repo_ids=None, limit=20):
        rows = [self.symbols[s] for s in self.lexical.get(term, [])]
        if term in self.saturated:
            rows = rows + [
                {"symbol_id": f"s::noise{i}", "name": f"noise{i}", "file_id": f"r:n{i}.py"}
                for i in range(limit)
            ]
        return rows[:limit]

    def get_symbol(self, sid):
        return self.symbols.get(sid)


def test_plain_words_never_resolve_as_explicit_symbols():
    repo = _AnchorRepo()
    result = find_anchors(repo, task_text="Period comparison is missing the authorization check")
    assert "check" not in repo.explicit_calls
    assert "explicit" not in result.anchors.get("s::Principal.check", [])


def test_stopword_only_task_yields_no_lexical_anchors():
    repo = _AnchorRepo()
    repo.lexical["the"] = ["s::acting_user"]
    result = find_anchors(repo, task_text="fix the thing and check it")
    assert "s::acting_user" not in result.anchors


def test_lexical_coverage_ranks_multi_term_match_above_single_term():
    repo = _AnchorRepo()
    result = find_anchors(repo, task_text="Period comparison is missing the authorization check")
    # diff matches period + comparison; diff_command only period; acting_user only authorization.
    assert result.strength_of("s::diff") > result.strength_of("s::diff_command")
    assert result.strength_of("s::diff") > result.strength_of("s::acting_user")


def test_saturated_term_contributes_less_evidence():
    repo = _AnchorRepo()
    base = find_anchors(repo, task_text="Period comparison").strength_of("s::diff_command")
    repo.saturated.add("period")
    saturated = find_anchors(repo, task_text="Period comparison").strength_of("s::diff_command")
    assert saturated < base


def test_lexical_anchor_count_is_bounded():
    repo = _AnchorRepo()
    for i in range(60):
        repo.symbols[f"s::x{i}"] = {"symbol_id": f"s::x{i}", "name": f"x{i}", "file_id": "r:x.py"}
    repo.lexical["period"] = [f"s::x{i}" for i in range(60)]
    result = find_anchors(repo, task_text="Period comparison", lexical_terms=["period"])
    assert len([s for s, v in result.anchors.items() if "lexical" in v]) <= LEXICAL_ANCHOR_LIMIT


def test_test_symbols_excluded_from_lexical_and_semantic_but_not_explicit():
    repo = _AnchorRepo()
    result = find_anchors(
        repo,
        task_text="Period comparison authorization",
        semantic_candidates=["s::test_diff", "s::diff"],
    )
    assert "s::test_diff" not in result.anchors
    assert result.anchors["s::diff"] == ["lexical", "semantic"]

    explicit = find_anchors(repo, task_text="see `test_diff_requires_auth`")
    assert explicit.anchors["s::test_diff"] == ["explicit"]

    with_tests = find_anchors(
        repo, task_text="Period", semantic_candidates=["s::test_diff"], include_tests=True
    )
    assert "s::test_diff" in with_tests.anchors


def test_semantic_strength_decays_with_rank_and_sources_combine_noisy_or():
    repo = _AnchorRepo()
    result = find_anchors(
        repo,
        task_text="Period comparison",
        semantic_candidates=["s::acting_user", "s::Principal.check", "s::diff"],
    )
    # acting_user (#1) beats Principal.check (#2) - both semantic-only here.
    assert result.strength_of("s::acting_user") > result.strength_of("s::Principal.check")
    # diff is lexical + semantic -> stronger than either alone, still <= 1.
    lexical_only = find_anchors(repo, task_text="Period comparison").strength_of("s::diff")
    assert lexical_only < result.strength_of("s::diff") <= 1.0
    assert result.anchors["s::diff"] == ["lexical", "semantic"]


def test_anchor_result_add_clamps_and_is_idempotent_on_source():
    r = AnchorResult()
    r.add("x", "explicit")
    r.add("x", "explicit")
    assert r.anchors["x"] == ["explicit"]
    assert r.strength_of("x") == 1.0
    r.add("y", "lexical", 5.0)  # clamped to the lexical cap
    assert r.strength_of("y") == 0.8


def test_semantic_rank_is_recorded_and_the_decay_stays_above_half_the_cap():
    repo = _AnchorRepo()
    ranked = ["s::acting_user", "s::Principal.check", "s::diff_command", "s::diff"]
    result = find_anchors(repo, task_text="unrelated", semantic_candidates=ranked)
    assert [result.semantic_rank[sid] for sid in ranked] == [0, 1, 2, 3]
    strengths = [result.strength_of(sid) for sid in ranked]
    # cap / (1 + rank/n): the model's #1 is full semantic evidence, the tail never halves.
    assert strengths[0] == SOURCE_STRENGTH["semantic"]
    assert strengths == sorted(strengths, reverse=True)
    assert strengths[-1] > SOURCE_STRENGTH["semantic"] / 2


# --- explicit-name resolution ----------------------------------------------------------------


class _ExplicitRepo:
    """Name resolution only, with the ambiguity shapes real repositories produce."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {
            # A class and its constructor share the type's name.
            "MongoDbService": [
                {"symbol_id": "cs::Acme.Data::::MongoDbService", "kind": "class"},
                {
                    "symbol_id": "cs::Acme.Data::MongoDbService::MongoDbService",
                    "kind": "constructor",
                },
            ],
            # A method name shared by four unrelated types.
            "CancelAsync": [
                {"symbol_id": f"cs::Acme.{ns}::{ns}Service::CancelAsync", "kind": "method"}
                for ns in ("Data", "Web", "Jobs", "Mail")
            ],
            # A generic member name: a dozen DTOs declare it.
            "Content": [
                {"symbol_id": f"cs::Acme.Models::Dto{i}::Content", "kind": "property"}
                for i in range(12)
            ],
            "Flush": [
                {"symbol_id": "cs::Acme.Data::Writer::Flush", "kind": "method"},
                {"symbol_id": "cs::Acme.Mail::Spool::Flush", "kind": "method"},
            ],
        }

    def resolve_symbol(self, name, repo_id=None):
        return list(self.rows.get(name, []))

    def find_routes(self, path):
        return []

    def symbols_in_file(self, file_id):
        return []

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def get_symbol(self, sid):
        return None


def test_explicit_evidence_is_divided_by_name_ambiguity():
    repo = _ExplicitRepo()
    result = find_anchors(repo, task_text="`MongoDbService` no longer honours `CancelAsync`")
    # 2 matches -> 1/sqrt(2); 4 matches -> 1/sqrt(4). An unambiguous name would be 1.0.
    assert result.strength_of("cs::Acme.Data::::MongoDbService") == pytest.approx(
        0.707107, abs=1e-5
    )
    assert result.strength_of("cs::Acme.Web::WebService::CancelAsync") == pytest.approx(0.5)


def test_generic_name_matching_many_symbols_is_not_explicit_evidence():
    repo = _ExplicitRepo()
    result = find_anchors(repo, task_text="the `Content` field is dropped")
    # 12 > EXPLICIT_MAX_MATCHES: naming a dozen unrelated DTO properties is not naming anything.
    assert result.anchors == {}


def test_container_member_echoing_its_name_is_secondary_evidence():
    repo = _ExplicitRepo()
    result = find_anchors(repo, task_text="`MongoDbService` is misconfigured")
    cls = result.strength_of("cs::Acme.Data::::MongoDbService")
    ctor = result.strength_of("cs::Acme.Data::MongoDbService::MongoDbService")
    # The author named the type; its constructor is where the type lives, not what it does.
    assert ctor == pytest.approx(cls * EXPLICIT_CONTAINER_MEMBER_SCALE, abs=1e-5)


def test_qualified_reference_resolves_inside_its_container():
    repo = _ExplicitRepo()
    result = find_anchors(repo, task_text="`JobsService.CancelAsync` swallows the token")
    # Only the one inside JobsService, and at full strength: qualified, it is unambiguous.
    assert result.anchors == {"cs::Acme.Jobs::JobsService::CancelAsync": ["explicit"]}
    assert result.strength_of("cs::Acme.Jobs::JobsService::CancelAsync") == 1.0


def test_qualified_reference_falls_back_only_while_the_bare_name_is_specific():
    repo = _ExplicitRepo()
    # Container absent from the graph, 2 matches <= EXPLICIT_QUALIFIED_FALLBACK -> both kept.
    fallback = find_anchors(repo, task_text="`Buffer.Flush` is never called")
    assert sorted(fallback.anchors) == [
        "cs::Acme.Data::Writer::Flush",
        "cs::Acme.Mail::Spool::Flush",
    ]
    # 4 matches is past the fallback: picking every CancelAsync would be a guess, not a name.
    guess = find_anchors(repo, task_text="`Legacy.CancelAsync` is never called")
    assert guess.anchors == {}


def test_namespace_qualified_reference_matches_the_module_path():
    repo = _ExplicitRepo()
    result = find_anchors(repo, task_text="`Mail.Flush` leaks a handle")
    assert sorted(result.anchors) == ["cs::Acme.Mail::Spool::Flush"]


# --- scoring ---------------------------------------------------------------------------------


def test_weak_anchor_is_beatable_by_strong_neighbour():
    weak_anchor = Candidate(symbol_id="anchor", anchor=True, anchor_strength=0.2, graph_distance=0)
    neighbour = Candidate(
        symbol_id="neigh",
        ref_kind=str(RefKind.DEFINE),
        provenance=str(Provenance.TREESITTER),
        graph_distance=1,
        task_signal=1.0,
    )
    ranked = score_candidates([weak_anchor, neighbour])
    assert ranked[0].symbol_id == "neigh"


def test_strong_anchor_still_beats_plain_neighbour():
    anchor = Candidate(symbol_id="anchor", anchor=True, anchor_strength=1.0, graph_distance=0)
    neighbour = Candidate(symbol_id="neigh", graph_distance=1, degree=30)
    ranked = score_candidates([neighbour, anchor])
    assert ranked[0].symbol_id == "anchor"


def test_anchor_without_recorded_strength_is_fully_trusted():
    legacy = Candidate(symbol_id="a", anchor=True, graph_distance=0)
    score_candidate(legacy)
    assert legacy.features["anchor_strength"] == 1.0


def test_unknown_ref_kind_and_provenance_never_beat_known_values():
    unknown = Candidate(symbol_id="u", graph_distance=1)
    read = Candidate(symbol_id="r", ref_kind=str(RefKind.READ), graph_distance=1)
    heuristic = Candidate(symbol_id="h", provenance=str(Provenance.HEURISTIC), graph_distance=1)
    score_candidate(unknown)
    score_candidate(read)
    score_candidate(heuristic)
    assert unknown.features["ref_kind_weight"] <= read.features["ref_kind_weight"]
    assert unknown.features["provenance_weight"] <= heuristic.features["provenance_weight"]


def test_test_symbols_are_penalised():
    prod = Candidate(symbol_id="prod", graph_distance=1)
    test = Candidate(symbol_id="test", graph_distance=1, is_test=True)
    ranked = score_candidates([test, prod])
    assert ranked[0].symbol_id == "prod"
    assert test.features["test_penalty"] == 1.0


def test_proximity_distinguishes_anchor_from_first_hop():
    a = Candidate(symbol_id="a", graph_distance=0)
    b = Candidate(symbol_id="b", graph_distance=1)
    c = Candidate(symbol_id="c", graph_distance=2)
    for cand in (a, b, c):
        score_candidate(cand)
    assert a.features["proximity"] > b.features["proximity"] > c.features["proximity"]


def test_centrality_is_log_scaled_and_capped():
    hub = Candidate(symbol_id="hub", degree=200)
    mid = Candidate(symbol_id="mid", degree=4)
    score_candidate(hub)
    score_candidate(mid)
    assert hub.features["centrality"] == 1.0
    assert 0.4 < mid.features["centrality"] < 0.7


# --- scoring v3: kind prior, evidence scaling, container dedup -------------------------------


def test_kind_prior_puts_the_change_site_above_its_surroundings():
    # A task describes a change; the change lands in a body, not in a declaration or a shell.
    assert kind_prior_of("method") == kind_prior_of("function") == 1.0
    assert kind_prior_of("method") > kind_prior_of("class") > kind_prior_of("interface")
    assert kind_prior_of("constructor") < kind_prior_of("class")
    # An unknown kind must not outrank a known change-site kind.
    assert kind_prior_of(None) < kind_prior_of("method")
    assert kind_prior_of("no_such_kind") == kind_prior_of(None)


def test_kind_prior_orders_otherwise_identical_candidates():
    specs = [("m", "method"), ("c", "class"), ("i", "interface")]
    ranked = score_candidates(
        [Candidate(symbol_id=sid, kind=kind, graph_distance=1) for sid, kind in specs]
    )
    assert [c.symbol_id for c in ranked] == ["m", "c", "i"]


def test_weak_evidence_discounts_proximity_and_reference_kind():
    strong = Candidate(
        symbol_id="strong", ref_kind=str(RefKind.DEFINE), graph_distance=1, source_strength=0.9
    )
    weak = Candidate(
        symbol_id="weak", ref_kind=str(RefKind.DEFINE), graph_distance=1, source_strength=0.1
    )
    ranked = score_candidates([weak, strong])
    # A `define` edge off a generic name is not worth what one off the best-evidenced anchor is.
    assert ranked[0].symbol_id == "strong"
    assert strong.features["evidence"] == 0.9 and weak.features["evidence"] == 0.1


def test_unrecorded_evidence_is_fully_trusted():
    legacy = Candidate(symbol_id="legacy", graph_distance=1)
    score_candidate(legacy)
    assert legacy.features["evidence"] == 1.0


def test_member_prefix_of_only_matches_container_id_shapes():
    assert member_prefix_of("cs::Acme.Data::::MongoDbService") == "cs::Acme.Data::MongoDbService::"
    # Already a member (container slot filled), or too few segments: not a container id.
    assert member_prefix_of("cs::Acme.Data::MongoDbService::GetAsync") is None
    assert member_prefix_of("py::mod::fn") is None


def _container_pool() -> list[Candidate]:
    return [
        Candidate(symbol_id="cs::Acme.Data::::MongoDbService", kind="class", score=9.0),
        Candidate(symbol_id="cs::Acme.Data::MongoDbService::GetAsync", kind="method", score=8.0),
        Candidate(symbol_id="cs::Acme.Mail::::Spool", kind="class", score=7.0),
    ]


def test_container_whose_member_already_ranks_is_demoted_below_it():
    ranked = demote_redundant_containers(_container_pool(), horizon=3)
    ids = [c.symbol_id for c in ranked]
    # The class repeats what its own method already says, so the method leads.
    assert ids[0] == "cs::Acme.Data::MongoDbService::GetAsync"
    by_id = {c.symbol_id: c for c in ranked}
    svc = by_id["cs::Acme.Data::::MongoDbService"]
    assert svc.score == 9.0 - CONTAINER_MEMBER_PENALTY
    assert svc.features["container_member_penalty"] == CONTAINER_MEMBER_PENALTY


def test_container_with_no_member_in_the_pool_keeps_its_score():
    ranked = demote_redundant_containers(_container_pool(), horizon=3)
    spool = next(c for c in ranked if c.symbol_id == "cs::Acme.Mail::::Spool")
    # Nothing inside Spool is known, so the class is still the best available pointer.
    assert spool.score == 7.0
    assert "container_member_penalty" not in spool.features


def test_container_dedup_only_looks_within_its_horizon():
    pool = _container_pool()
    unchanged = demote_redundant_containers(pool, horizon=1)
    assert next(c for c in unchanged if c.kind == "class").score == 9.0
    assert demote_redundant_containers([], horizon=3) == []


# --- task signal / to_candidates ------------------------------------------------------------


def test_task_signal_partial_match_on_identifier_parts():
    tokens = set(content_tokens("the diff command lacks an authorization check"))
    assert task_signal_for("diff_command", tokens) == 1.0  # both parts present
    assert 0 < task_signal_for("diff_view", tokens) < 1.0  # one of two parts
    assert task_signal_for("check", tokens) == 0.0  # stop-word never signals
    assert task_signal_for("acting_user", tokens) == 0.0


class _GraphRepo:
    """anchor -CALLS-> callee; caller -CALLS-> anchor. ``features_calls`` counts bulk usage."""

    def __init__(self) -> None:
        self.symbols = {
            "a": {"symbol_id": "a", "name": "diff", "file_id": "r:api.py", "line": 10},
            "callee": {
                "symbol_id": "callee",
                "name": "acting_user",
                "file_id": "r:api.py",
                "line": 50,
            },
            "caller": {
                "symbol_id": "caller",
                "name": "diff_command",
                "file_id": "r:cli.py",
                "line": 5,
            },
            "test": {
                "symbol_id": "test",
                "name": "test_diff",
                "file_id": "r:tests/test_api.py",
                "line": 1,
            },
        }
        self.features_calls = 0

    def repo_of_symbol(self, sid):
        return "r"

    def get_symbol(self, sid):
        return self.symbols.get(sid)

    def get_callers(self, sid):
        if sid == "a":
            return [
                {"symbol_id": "caller", "provenance": "treesitter"},
                {"symbol_id": "test", "provenance": "treesitter"},
            ]
        return []

    def get_callees(self, sid):
        return [{"symbol_id": "callee", "provenance": "treesitter"}] if sid == "a" else []

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def find_implementers(self, sid):
        return []

    def symbols_in_file(self, fid):
        return [s for s in self.symbols.values() if s["file_id"] == fid]

    def symbol_degree(self, sid):
        return 3

    def symbol_features(self, ids):
        self.features_calls += 1
        return {
            sid: {
                **self.symbols[sid],
                "degree": 3,
                "has_callers": sid == "a",
                "has_callees": sid == "a",
            }
            for sid in ids
            if sid in self.symbols
        }

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def find_routes(self, path):
        return []


def test_to_candidates_uses_bulk_features_and_signals_every_candidate():
    repo = _GraphRepo()
    anchors = AnchorResult()
    anchors.add("a", "explicit")
    expansion = expand_from_anchors(repo, anchors)
    cands = to_candidates(repo, expansion, task_text="the diff command is broken")
    by_id = {c.symbol_id: c for c in cands}
    assert repo.features_calls == 1
    # The graph neighbour (not an anchor) receives its task signal in the single pass.
    assert by_id["caller"].task_signal == 1.0
    assert by_id["a"].task_signal == 1.0
    assert by_id["test"].is_test is True
    assert by_id["callee"].is_test is False
    assert by_id["a"].is_leaf is False


def test_retrieve_single_pass_ranks_signalled_neighbour_above_test_and_reports_pool():
    repo = _GraphRepo()
    anchors = AnchorResult()
    anchors.add("a", "explicit")
    result = RetrievalOrchestrator(repo).retrieve(
        anchors, task_text="the diff command is broken", max_candidates=3
    )
    ids = [c.symbol_id for c in result.candidates]
    assert ids[0] == "a"
    assert ids.index("caller") < ids.index("callee")
    assert "test" not in ids
    assert result.pool_size == 4
    # test_diff shares the "diff" part -> partial signal; its test penalty still keeps it out.
    assert result.task_signals == {"a": 1.0, "caller": 1.0, "test": 0.3}


def test_anchors_below_the_expansion_floor_are_kept_but_never_walked():
    repo = _GraphRepo()
    weak = AnchorResult()
    weak.add("a", "lexical", 0.2)
    strong = AnchorResult()
    strong.add("a", "explicit")
    assert "callee" in expand_from_anchors(repo, strong)  # via CALLS and as a file sibling
    weak_exp = expand_from_anchors(repo, weak)
    # A single-term lexical hit stays a candidate but drags none of its neighbourhood along.
    assert set(weak_exp) == {"a"}
    assert weak_exp["a"].anchor_strength == 0.2


def test_expansion_roots_are_budgeted_by_strength_then_count():
    strengths = {f"s{i:02d}": 1.0 - i * 0.02 for i in range(40)}
    strengths["weak"] = 0.1
    roots = expansion_roots(sorted(strengths), strengths)
    assert len(roots) == EXPAND_ROOT_LIMIT
    assert "weak" not in roots  # below EXPAND_MIN_STRENGTH
    # The strongest survive, and the result is keyed deterministically.
    assert "s00" in roots and "s39" not in roots
    assert list(roots) == sorted(roots)


class _ChainRepo:
    """``top -CALLS-> mid -CALLS-> anchor``, plus a class whose file holds two other symbols."""

    def __init__(self) -> None:
        self.symbols = {
            s["symbol_id"]: s
            for s in (
                {"symbol_id": "anchor", "name": "run", "file_id": "r:a.py", "line": 1},
                {"symbol_id": "mid", "name": "mid", "file_id": "r:b.py", "line": 1},
                {"symbol_id": "top", "name": "top", "file_id": "r:c.py", "line": 1},
                {
                    "symbol_id": "cs::Acme::::Svc",
                    "name": "Svc",
                    "kind": "class",
                    "file_id": "r:svc.cs",
                    "line": 1,
                },
                {
                    "symbol_id": "member",
                    "name": "member",
                    "kind": "method",
                    "file_id": "r:svc.cs",
                    "line": 20,
                },
                {
                    "symbol_id": "other",
                    "name": "other",
                    "kind": "method",
                    "file_id": "r:svc.cs",
                    "line": 40,
                },
            )
        }

    def repo_of_symbol(self, sid):
        return "r"

    def get_symbol(self, sid):
        return self.symbols.get(sid)

    def get_callers(self, sid):
        return {"anchor": [{"symbol_id": "mid"}], "mid": [{"symbol_id": "top"}]}.get(sid, [])

    def get_callees(self, sid):
        return []

    def get_referrers(self, sid):
        return []

    def get_supertypes(self, sid):
        return []

    def get_subtypes(self, sid):
        return []

    def symbols_in_file(self, fid):
        return [s for s in self.symbols.values() if s["file_id"] == fid]

    def symbol_degree(self, sid):
        return 1

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def find_routes(self, path):
        return []


def test_evidence_decays_once_per_hop():
    repo = _ChainRepo()
    anchors = AnchorResult()
    anchors.add("anchor", "semantic", 0.9)
    found = expand_from_anchors(repo, anchors)
    assert found["anchor"].source_strength == pytest.approx(0.9)
    assert found["mid"].source_strength == pytest.approx(0.9 * HOP_DECAY)
    assert found["top"].source_strength == pytest.approx(0.9 * HOP_DECAY**2)


def test_neighbours_of_a_weak_anchor_carry_weaker_evidence_than_those_of_a_strong_one():
    repo = _ChainRepo()
    strong, weak = AnchorResult(), AnchorResult()
    strong.add("anchor", "explicit")
    weak.add("anchor", "lexical", 0.4)
    assert (
        expand_from_anchors(repo, weak)["mid"].source_strength
        < expand_from_anchors(repo, strong)["mid"].source_strength
    )


def test_container_anchor_does_not_pull_in_its_file_siblings():
    repo = _ChainRepo()
    container = AnchorResult()
    container.add("cs::Acme::::Svc", "explicit")
    # A class's members arrive through the graph; expanding its file would flood the pool twice.
    assert set(expand_from_anchors(repo, container)) == {"cs::Acme::::Svc"}

    method = AnchorResult()
    method.add("member", "explicit")
    assert "other" in expand_from_anchors(repo, method)


def test_sibling_expansion_is_capped_and_nearest_by_line():
    repo = _ChainRepo()
    for i in range(30):
        sid = f"far{i:02d}"
        repo.symbols[sid] = {
            "symbol_id": sid,
            "name": sid,
            "kind": "method",
            "file_id": "r:svc.cs",
            "line": 100 + i,
        }
    anchors = AnchorResult()
    anchors.add("member", "explicit")  # line 20
    found = expand_from_anchors(repo, anchors)
    siblings = set(found) - {"member"}
    assert len(siblings) == SIBLING_LIMIT
    assert "other" in siblings  # line 40, nearest
    assert "far29" not in siblings  # line 129, furthest


# --- narrowing -------------------------------------------------------------------------------


def _ranked(specs: list[tuple[str, float, str | None, bool]]) -> list[Candidate]:
    cands = [
        Candidate(symbol_id=sid, score=score, file_id=fid, anchor=anchor)
        for sid, score, fid, anchor in specs
    ]
    return sorted(cands, key=lambda c: (-c.score, c.symbol_id))


def test_narrow_caps_symbols_per_file():
    ranked = _ranked(
        [(f"f1_{i}", 10 - i * 0.1, "f1", True) for i in range(8)]
        + [("f2_a", 5.0, "f2", True), ("f3_a", 4.0, "f3", True), ("f4_a", 3.0, "f4", True)]
    )
    selected = narrow(ranked, 4)
    files = [c.file_id for c in selected]
    assert files.count("f1") <= 3  # ceil(4 * 0.6)
    assert "f2" in files  # the third f1 symbol yields to the best other file at slot 3


def _pool(*cands: Candidate) -> list[Candidate]:
    return sorted(cands, key=lambda c: (-c.score, c.symbol_id))


def test_narrow_puts_the_models_first_rank_first_and_reserves_its_top_ranks():
    ranked = _pool(
        *[Candidate(symbol_id=f"top{i}", score=9.0 - i, file_id=f"f{i}") for i in range(6)],
        Candidate(symbol_id="sem0", score=1.0, file_id="s0", semantic_rank=0),
        Candidate(symbol_id="sem1", score=0.9, file_id="s1", semantic_rank=1),
    )
    ids = [c.symbol_id for c in narrow(ranked, 4)]
    # The model's #1 is position 1 whatever the engine scored (MRR is decided here); with a 0.5
    # floor and no agreement between the streams they alternate: model, engine, model, engine.
    assert ids == ["sem0", "top0", "sem1", "top1"]


def test_semantic_reserve_ignores_the_per_file_cap():
    ranked = _pool(
        *[Candidate(symbol_id=f"top{i}", score=9.0 - i, file_id="svc") for i in range(8)],
        *[
            Candidate(symbol_id=f"sem{i}", score=1.0 - i * 0.1, file_id="svc", semantic_rank=i)
            for i in range(6)
        ],
    )
    ids = [c.symbol_id for c in narrow(ranked, 10)]
    # A task reworking one big service class: everything is in "svc". Engine picks stop at the
    # file cap (ceil(slot * 0.6)); the model's ranks ignore it, so the list still fills to 10
    # and every one of the model's six ranks is in it.
    assert len(ids) == 10
    assert {f"sem{i}" for i in range(6)} <= set(ids)


def test_semantic_reserve_takes_the_best_ranks_not_the_best_scores():
    ranked = _pool(
        Candidate(symbol_id="rank0", score=1.0, file_id="f0", semantic_rank=0),
        Candidate(symbol_id="rank1", score=2.0, file_id="f1", semantic_rank=1),
        Candidate(symbol_id="rank9", score=8.0, file_id="f2", semantic_rank=9),
        *[Candidate(symbol_id=f"other{i}", score=5.0 - i, file_id=f"o{i}") for i in range(4)],
    )
    ids = [c.symbol_id for c in narrow(ranked, 4)]
    # Slot 1 is the model's #1 although it scores lowest; slot 2 is the engine's best, which
    # happens to be the model's rank 9 - both channels agree, so it also counts toward the
    # model's share (2 of the first 2). Slots 3 and 4 owe the model nothing (2 >= 3 * 0.5 and
    # 2 >= 4 * 0.5), so they stay with the engine in score order; slot 5 (2 < 2.5) is owed to
    # the model and takes rank 1 rather than a higher-scoring "other".
    assert ids == ["rank0", "rank9", "other0", "other1"]
    assert [c.symbol_id for c in narrow(ranked, 5)][-1] == "rank1"


def test_agreed_picks_count_toward_the_models_share():
    """An engine pick that is also a model rank satisfies the reserve; the engine's own hits
    (anchors the model never returned) are not pushed down by forced model-only picks."""
    model = [
        Candidate(symbol_id=f"sem{i}", score=8.0 - i, file_id=f"s{i}", semantic_rank=i)
        for i in range(4)
    ]
    own = [Candidate(symbol_id=f"own{i}", score=7.5 - i, file_id=f"o{i}") for i in range(4)]
    ids = [c.symbol_id for c in narrow(_pool(*model, *own), 6)]
    # Score order interleaves the two naturally (sem0 8.0, own0 7.5, sem1 7.0, own1 6.5 ...);
    # since every sem pick counts for the model, no extra model-only pick is forced and the
    # list is exactly the score order.
    assert ids == ["sem0", "own0", "sem1", "own1", "sem2", "own2"]


def test_narrow_is_k_monotonic():
    """The K=n list is exactly the first n of the K=n+1 list (a K=20 run can be cut to K=10)."""
    pool = _pool(
        *[
            Candidate(
                symbol_id=f"c{i:02d}",
                score=float(40 - i) + (i % 3),
                file_id=f"f{i % 4}",
                kind="class" if i % 5 == 0 else "method",
                semantic_rank=i if i % 2 == 0 else None,
            )
            for i in range(40)
        ]
    )
    lists = [[c.symbol_id for c in narrow(pool, n)] for n in range(1, 25)]
    for shorter, longer in zip(lists, lists[1:], strict=False):
        assert longer[: len(shorter)] == shorter
    assert len(lists[-1]) == 24


def test_engine_picks_that_are_model_ranks_push_the_reserve_deeper():
    """When the engine's best is itself one of the model's ranks, both channels agree on it and
    the model stream moves past it, so more of the model's list survives."""
    model = [
        Candidate(symbol_id=f"sem{i}", score=5.0 - i * 0.1, file_id=f"s{i}", semantic_rank=i)
        for i in range(10)
    ]
    neighbour = Candidate(symbol_id="neighbour", score=4.0, file_id="n1")
    ids = [c.symbol_id for c in narrow(_pool(*model, neighbour), 6)]
    # Engine slots (2, 4, 6) take the best-scoring remaining candidates, which are model ranks
    # until the scores fall under the neighbour's; the list is the model's list plus one pick.
    assert ids[0] == "sem0"
    assert set(ids) == {"sem0", "sem1", "sem2", "sem3", "sem4", "sem5"}
    ids = [c.symbol_id for c in narrow(_pool(*model, neighbour), 12)]
    assert ids.index("neighbour") == 10 and len(ids) == 11


def test_no_anchor_source_preempts_the_models_first_slot():
    """A high-scoring explicit anchor takes the first *engine* slot, never position 1."""
    named = Candidate(
        symbol_id="svc::::Named",
        score=9.0,
        file_id="n",
        anchor=True,
        anchor_strength=1.0,
        sources=["explicit"],
    )
    model = [
        Candidate(symbol_id=f"sem{i}", score=5.0 - i * 0.1, file_id=f"s{i}", semantic_rank=i)
        for i in range(5)
    ]
    ids = [c.symbol_id for c in narrow(_pool(named, *model), 4)]
    assert ids[0] == "sem0" and "svc::::Named" in ids


def test_narrow_caps_members_of_one_container_among_engine_picks():
    members = [
        Candidate(
            symbol_id=f"cs::Acme::Svc::m{i}", score=9.0 - i * 0.1, file_id="svc", kind="method"
        )
        for i in range(8)
    ]
    others = [
        Candidate(symbol_id=f"cs::Acme::::Other{i}", score=5.0 - i * 0.1, file_id=f"o{i}")
        for i in range(8)
    ]
    ids = [c.symbol_id for c in narrow(_pool(*members, *others), 8)]
    # ceil(8 * 0.3) = 3 members of Svc at most, while the per-file cap ceil(8 * 0.4) = 4 would
    # have let a fourth through.
    assert sum(1 for sid in ids if "::Svc::" in sid) == 3
    assert len(ids) == 8


def test_narrow_caps_container_symbols():
    ranked = _pool(
        *[
            Candidate(symbol_id=f"cls{i}", score=9.0 - i * 0.1, file_id=f"c{i}", kind="class")
            for i in range(10)
        ],
        *[
            Candidate(symbol_id=f"m{i}", score=5.0 - i * 0.1, file_id=f"m{i}", kind="method")
            for i in range(10)
        ],
    )
    selected = narrow(ranked, 10)
    assert sum(1 for c in selected if c.is_container) == 2  # ceil(10 * 0.2)
    assert len(selected) == 10


def test_narrow_no_longer_forces_a_neighbour_into_the_selection():
    ranked = _ranked(
        [(f"anchor{i}", 9 - i * 0.1, f"a{i}", True) for i in range(10)]
        + [("neigh1", 4.0, "n1", False), ("neigh2", 3.5, "n2", False)]
    )
    ids = [c.symbol_id for c in narrow(ranked, 8)]
    # Evidence-scaled scoring lets a real neighbour outrank a weak anchor on its own, so nothing
    # spends a slot on the best-scoring neighbour regardless of whether it earned one.
    assert "neigh1" not in ids and "neigh2" not in ids
    assert ids == [f"anchor{i}" for i in range(8)]


def test_narrow_is_identity_when_pool_fits_and_deterministic():
    ranked = _ranked([("a", 2.0, "f", True), ("b", 1.0, "g", False)])
    assert [c.symbol_id for c in narrow(ranked, 8)] == ["a", "b"]
    big = _ranked([(f"s{i:02d}", float(50 - i), f"f{i % 3}", i % 2 == 0) for i in range(50)])
    assert narrow(big, 8) == narrow(list(big), 8)
    assert narrow(big, 0) == []
    only_classes = _pool(
        *[
            Candidate(symbol_id=f"cls{i}", score=9.0 - i, file_id="f", kind="class")
            for i in range(4)
        ]
    )
    # Caps never shorten the list below what the pool allows.
    assert len(narrow(only_classes, 3)) == 3


# --- bulk graph reads ------------------------------------------------------------------------


class _BulkRepo:
    """Exposes the batched API and counts the queries it received."""

    def __init__(self, *, batched: bool = True, implemented: bool = True) -> None:
        self.calls: list[tuple[str, int]] = []
        self.implemented = implemented
        if not batched:
            # Shadow the batched methods with a non-callable: what an older repository looks like.
            self.lexical_search_many = None
            self.callers_of = None

    def lexical_search_many(self, terms, *, repo_ids=None, limit=20):
        self.calls.append(("many", len(terms)))
        if not self.implemented:
            raise NotImplementedError
        return {term: [{"symbol_id": f"s::{term}"}] for term in terms}

    def lexical_search(self, term, repo_ids=None, limit=20):
        self.calls.append(("one", 1))
        return [{"symbol_id": f"s::{term}"}]

    def callers_of(self, ids):
        self.calls.append(("many", len(ids)))
        return {sid: [{"symbol_id": f"{sid}-caller"}] for sid in ids}

    def get_callers(self, sid):
        self.calls.append(("one", 1))
        return [{"symbol_id": f"{sid}-caller"}]


def test_bulk_reads_keep_the_same_shape_whether_batched_or_not():
    terms = ["alpha", "beta", "gamma"]
    expected = {term: [{"symbol_id": f"s::{term}"}] for term in terms}

    batched = _BulkRepo()
    assert bulk.lexical_hits(batched, terms, repo_ids=None, limit=20) == expected
    assert batched.calls == [("many", 3)]

    # Unit-test fakes and repositories without the batched method fall back per term.
    single = _BulkRepo(batched=False)
    assert bulk.lexical_hits(single, terms, repo_ids=None, limit=20) == expected
    assert single.calls == [("one", 1)] * 3

    # A declared-but-unimplemented batched method falls back too, without losing rows.
    stub = _BulkRepo(implemented=False)
    assert bulk.lexical_hits(stub, terms, repo_ids=None, limit=20) == expected
    assert stub.calls == [("many", 3), ("one", 1), ("one", 1), ("one", 1)]


def test_bulk_neighbours_asks_once_per_frontier_and_falls_back_per_symbol():
    ids = ["a", "b"]
    expected = {"a": [{"symbol_id": "a-caller"}], "b": [{"symbol_id": "b-caller"}]}

    batched = _BulkRepo()
    assert bulk.neighbours(batched, ids, "callers_of", "get_callers") == expected
    assert batched.calls == [("many", 2)]

    single = _BulkRepo(batched=False)
    assert bulk.neighbours(single, ids, "callers_of", "get_callers") == expected
    assert single.calls == [("one", 1)] * 2

    assert bulk.neighbours(batched, [], "callers_of", "get_callers") == {}


def test_bulk_symbol_meta_tolerates_a_repository_without_metadata():
    class _RouteOnly:
        def find_routes(self, path):
            return []

    # Scope probes and route-only fakes simply have nothing to return; this must not raise.
    assert bulk.symbols_meta(_RouteOnly(), ["a"]) == {}


# --- confidence ------------------------------------------------------------------------------


def _result(cands, anchors, pool=None):
    return RetrievalResult(
        commit=None, anchors=anchors, candidates=cands, pool_size=pool or len(cands)
    )


def test_flood_of_weak_single_source_anchors_is_not_high_confidence():
    anchors = AnchorResult()
    cands = []
    for i in range(8):
        sid = f"noise{i}"
        anchors.add(sid, "lexical", 0.2)
        cands.append(
            Candidate(
                symbol_id=sid,
                anchor=True,
                anchor_strength=0.2,
                graph_distance=0,
                score=9.0 - i,
            )
        )
    # A semantic-only tail anchor also exists, so v1's "3 sources" heuristic would say high.
    anchors.add("sem", "semantic")
    anchors.add("expl", "explicit")
    cov = compute_coverage(_result(cands, anchors, pool=600))
    assert cov["confidence"] == "low"
    assert cov["weak_anchor_ratio"] == 1.0
    assert cov["pool_size"] == 600


def test_corroborated_anchor_with_agreeing_neighbours_is_high_confidence():
    anchors = AnchorResult()
    anchors.add("diff", "lexical", 0.6)
    anchors.add("diff", "semantic", 0.7)
    cands = [
        Candidate(
            symbol_id="diff",
            anchor=True,
            anchor_strength=anchors.strength_of("diff"),
            graph_distance=0,
            score=8.0,
        ),
        Candidate(symbol_id="diff_command", graph_distance=1, score=6.0),
        Candidate(symbol_id="acting_user", graph_distance=1, score=4.0),
    ]
    cov = compute_coverage(_result(cands, anchors))
    assert cov["confidence"] == "high"
    assert cov["corroborated_anchor_count"] == 1
    assert cov["strong_anchor_count"] == 1
    assert cov["anchor_agreement_ratio"] == 1.0


def test_test_heavy_selection_is_low_confidence():
    anchors = AnchorResult()
    anchors.add("x", "explicit")
    cands = [Candidate(symbol_id="x", anchor=True, graph_distance=0, score=8.0)] + [
        Candidate(symbol_id=f"t{i}", graph_distance=1, is_test=True, score=3.0) for i in range(3)
    ]
    cov = compute_coverage(_result(cands, anchors))
    assert cov["test_ratio"] == 0.75
    assert cov["confidence"] == "low"


def test_empty_result_is_low_confidence():
    assert compute_coverage(_result([], AnchorResult()))["confidence"] == "low"


# --- assembler -------------------------------------------------------------------------------


def test_assembler_treats_zero_distance_as_near_not_missing():
    items = [
        {"symbol_id": "anchor", "graph_distance": 0, "name": "anchor", "body": "def anchor(): ..."},
        {"symbol_id": "unknown", "name": "unknown", "file_id": "f", "line": 1},
    ]
    pkg = assemble(items, max_tokens=200)
    by_id = {it["symbol_id"]: it for it in pkg["items"]}
    assert by_id["anchor"]["detail_level"] == str(DetailLevel.FULL)
    assert by_id["anchor"]["content"] == "def anchor(): ..."
    assert by_id["unknown"]["detail_level"] == str(DetailLevel.REFERENCE)


# --- auto semantic ---------------------------------------------------------------------------


class _Store:
    def __init__(self, ids):
        self.ids = ids
        self.limits: list[int] = []

    def search(self, vector, *, limit=20, repo_ids=None, kind=None):
        self.limits.append(limit)
        return [{"ref_id": sid} for sid in self.ids[:limit]]


def test_auto_semantic_overfetches_and_skips_test_symbols(monkeypatch):
    import bce.indexing.embedder.encoder as enc

    class _Enc:
        def encode_query(self, text):
            return [0.0]

    monkeypatch.setattr(enc, "build_default_encoder", lambda: _Enc())
    ids = [f"py::tests.test_mod::::test_{i}#x" for i in range(10)] + [
        f"py::app.mod::::fn{i}#x" for i in range(10)
    ]
    store = _Store(ids)
    out = _auto_semantic_candidates(store, "task", None, repository=None)
    assert out == [f"py::app.mod::::fn{i}#x" for i in range(10)]
    assert store.limits == [AUTO_SEMANTIC_LIMIT * 4]
    with_tests = _auto_semantic_candidates(store, "task", None, include_tests=True)
    assert with_tests == ids[:AUTO_SEMANTIC_LIMIT]
