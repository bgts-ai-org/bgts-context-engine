"""Hybrid-v4 regression tests: the whole symbol is searchable and the task's code fragments,
file paths and dominant names find the places that *use* them.

Pins the failure modes measured on a React/TypeScript front end at K=50 (27 of 31 missed files
never entered the pool): bodies unindexed, ``type`` aliases absent, one vector per symbol, no
body search, no path source, a production ``TestBotTriggerPanel`` taken for a test, one file
taking 14 of 50 slots, and - once bodies were indexed - long components outranking the helpers
named after the task's words.
"""

from __future__ import annotations

from bce.core.orchestrator.anchors import (
    IMPACT_MIN_STRENGTH,
    MATCH_TIER_WEIGHT,
    PATH_FILE_LIMIT,
    SOURCE_STRENGTH,
    USAGE_POOL,
    AnchorResult,
    _impact_anchors,
    _usage_anchors,
    find_anchors,
)
from bce.core.orchestrator.orchestrator import (
    PER_FILE_SHARE,
    PER_FILE_TAPER_FROM,
    file_cap,
    narrow,
)
from bce.core.orchestrator.text import (
    body_literals,
    file_mentions,
    is_test_symbol,
    mention_terms,
    phrase_terms,
)
from bce.core.scoring.engine import Candidate
from bce.domain.enums import NodeLabel
from bce.domain.models import GraphNode
from bce.indexing.embedder.embedder import (
    CHUNK_CHARS,
    CHUNK_OVERLAP,
    MAX_CHUNKS,
    split_windows,
    symbol_contents,
)
from bce.storage.relational.migrator import MIGRATIONS_DIR, split_sql_statements
from bce.tools.layer3.orchestration import (
    lexical_limits_for,
    semantic_limit_for,
    usage_pool_for,
)

# --- text: what a task writes out -----------------------------------------------------------


def test_mention_terms_classify_literals_code_and_identifiers():
    text = (
        "Every place that reads the raw `localStorage` key 'token' or "
        "`error?.response?.data?.message`; see queryClient.invalidateQueries and MAX_RETRIES."
    )
    terms = mention_terms(text)
    assert ("ident", "localStorage") in terms  # backticked bare name -> whole word
    assert ("literal", "token") in terms
    assert ("code", "error?.response?.data?.message") in terms
    assert ("code", "queryClient.invalidateQueries") in terms
    assert ("ident", "MAX_RETRIES") in terms
    # Prose words, short abbreviations and stop-words are not mentions.
    assert all(t[1] not in {"place", "reads", "raw", "key", "see"} for t in terms)
    assert ("code", "e.g") not in mention_terms("e.g. the thing")


def test_mention_terms_split_pasted_expressions_and_dedupe():
    terms = mention_terms("Trace `state?.workflow_id || state?.plan?.workflowId` and `useState`")
    assert ("code", "state?.workflow_id") in terms
    assert ("code", "state?.plan?.workflowId") in terms
    assert ("ident", "useState") in terms
    assert ("code", "||") not in terms
    assert len(terms) == len(set(terms))


def test_file_mentions_paths_bare_code_files_tracebacks_not_urls():
    text = (
        "Fix src/utils/auth.ts and ./flask/app.py; see "
        "https://github.com/pallets/flask/blob/main/src/flask/app.py. Also Layout.tsx, README.md."
    )
    assert file_mentions(text) == ["src/utils/auth.ts", "flask/app.py", "Layout.tsx"]
    trace = (
        'File "/opt/app/flask/app.py", line 12, in run\n  File "C:\\proj\\pkg\\Thing.cs", line 3'
    )
    assert file_mentions(trace) == ["opt/app/flask/app.py", "proj/pkg/Thing.cs"]
    assert file_mentions("prose about i18n and v1.2.3 with no files") == []


def test_phrase_terms_are_adjacent_content_word_pairs():
    pairs = phrase_terms("the access token is not read from shared storage helpers")
    assert "access token" in pairs and "storage helpers" in pairs
    assert all(" the " not in f" {p} " for p in pairs)


def test_camel_case_test_prefix_is_only_trusted_without_a_path():
    # A production component whose name starts with "Test": the path says it is not a test.
    assert not is_test_symbol(
        "typescript::src.components.workspace.triggerPanels.TestBotTriggerPanel::::TestBotTriggerPanel#1",
        name="TestBotTriggerPanel",
        file_id="repo:src/components/workspace/triggerPanels/TestBotTriggerPanel.tsx",
    )
    assert not is_test_symbol("py::app.db::::testConnection#1", name="testConnection")
    # No path at all: the CamelCase convention is the only evidence and is trusted.
    assert is_test_symbol(None, name="TestFooBar")
    # snake_case test prefixes are tests wherever they live; real test files still are.
    assert is_test_symbol("py::app.mod::::test_period#1", name="test_period")
    assert is_test_symbol("java::com.acme::FooTest::x#1", file_id="repo:src/test/FooTest.java")


# --- anchors: lexical match tiers, usage, impact, path -----------------------------------------


class _TierRepo:
    """One term, two hits: ``resolveTheme`` matched by name, ``Login`` only in its body."""

    def lexical_search_many(self, terms, *, repo_ids=None, limit=20):
        rows = [
            {
                "symbol_id": "s::resolveTheme",
                "name": "resolveTheme",
                "file_id": "r:theme.ts",
                "match_tier": "a",
            },
            {"symbol_id": "s::Login", "name": "Login", "file_id": "r:Login.tsx", "match_tier": "d"},
        ]
        return {t: list(rows) for t in terms if t == "theme"}

    def lexical_search(self, term, repo_ids=None, limit=20):
        return self.lexical_search_many([term], limit=limit).get(term, [])

    def resolve_symbol(self, name, repo_id=None):
        return []

    def find_routes(self, path):
        return []

    def get_symbol(self, sid):
        return None


def test_lexical_match_in_the_name_outweighs_a_match_in_the_body():
    result = find_anchors(_TierRepo(), task_text="theme", lexical_terms=["theme"])
    named, body = result.strength_of("s::resolveTheme"), result.strength_of("s::Login")
    assert named > body > 0
    assert abs(body / named - MATCH_TIER_WEIGHT["d"]) < 1e-6
    # Rows without a tier (older repositories, fakes) count as name matches.
    assert MATCH_TIER_WEIGHT["a"] == 1.0


class _BodyRepo:
    """``body_mentions`` over a few symbols; ``generic`` fragments saturate the pool."""

    def __init__(self) -> None:
        self.rows = {
            "s::Login": {"symbol_id": "s::Login", "name": "Login", "file_id": "r:src/Login.tsx"},
            "s::devAuth": {
                "symbol_id": "s::devAuth",
                "name": "ensureDevAuth",
                "file_id": "r:src/devAuth.ts",
            },
            "s::helper": {
                "symbol_id": "s::helper",
                "name": "auth",
                "file_id": "r:e2e/auth-helpers.ts",
            },
        }
        self.by_mention: dict[tuple[str, str], list[str]] = {
            ("ident", "localStorage"): ["s::Login", "s::devAuth", "s::helper"],
            ("literal", "token"): ["s::Login"],
        }
        self.generic: set[tuple[str, str]] = set()
        self.calls: list[list[tuple[str, str]]] = []

    def body_mentions(self, mentions, *, repo_ids=None, limit=50):
        self.calls.append(list(mentions))
        out = {}
        for key in mentions:
            if key in self.generic:
                out[key] = (limit + 5, [self.rows["s::helper"]])
            elif key in self.by_mention:
                ids = self.by_mention[key]
                out[key] = (len(ids), [self.rows[s] for s in ids])
        return out

    # The other sources need these to exist and return nothing.
    def resolve_symbol(self, name, repo_id=None):
        return []

    def find_routes(self, path):
        return []

    def symbols_in_file(self, file_id):
        return []

    def lexical_search(self, term, repo_ids=None, limit=20):
        return []

    def get_symbol(self, sid):
        return self.rows.get(sid)


def test_usage_anchors_strength_falls_with_match_count_and_combines_across_fragments():
    repo = _BodyRepo()
    task = "every place that reads the raw `localStorage` key 'token'"
    anchors = dict(_usage_anchors(repo, task, None, False, pool=USAGE_POOL))
    # Login carries both fragments, devAuth only the common one.
    assert anchors["s::Login"] > anchors["s::devAuth"] > 0
    assert anchors["s::Login"] <= SOURCE_STRENGTH["usage"]
    # e2e helper is a test symbol: excluded unless include_tests.
    assert "s::helper" not in anchors
    assert "s::helper" in dict(_usage_anchors(repo, task, None, True, pool=USAGE_POOL))


def test_generic_fragment_saturating_the_pool_anchors_nothing():
    repo = _BodyRepo()
    repo.generic.add(("ident", "useState"))
    anchors = dict(_usage_anchors(repo, "the `useState` call", None, False))
    assert anchors == {}


def test_usage_source_is_wired_into_find_anchors_and_survives_repos_without_it():
    repo = _BodyRepo()
    result = find_anchors(repo, task_text="reads the raw `localStorage` key 'token'")
    assert "usage" in result.anchors["s::Login"]
    assert result.strength_of("s::Login") > result.strength_of("s::devAuth")

    class _NoBodies(_BodyRepo):
        body_mentions = None  # pre-0011 snapshot / older repository

    assert "s::Login" not in find_anchors(_NoBodies(), task_text="`localStorage`").anchors


class _UsersRepo:
    """``isAdmin`` has three callers and one referrer; ``cn`` is a hub with 200 callers."""

    def __init__(self) -> None:
        self.callers = {
            "s::isAdmin": [
                {"symbol_id": "s::HelpCard", "name": "HelpCard", "file_id": "r:src/HelpCard.tsx"},
                {
                    "symbol_id": "s::Notifications",
                    "name": "NotificationsSection",
                    "file_id": "r:src/N.tsx",
                },
                {
                    "symbol_id": "s::isAdmin.test",
                    "name": "renders",
                    "file_id": "r:src/HelpCard.test.tsx",
                },
            ],
            "s::cn": [
                {"symbol_id": f"s::c{i}", "name": f"c{i}", "file_id": f"r:src/c{i}.tsx"}
                for i in range(200)
            ],
        }
        self.referrers = {
            "s::isAdmin": [
                {
                    "symbol_id": "s::routeGuards",
                    "name": "AdminRoute",
                    "file_id": "r:src/routing.tsx",
                }
            ]
        }

    def callers_of(self, ids):
        return {sid: list(self.callers.get(sid, [])) for sid in ids}

    def referrers_of(self, ids):
        return {sid: list(self.referrers.get(sid, [])) for sid in ids}


def test_impact_anchors_nominate_users_of_dominant_anchors_but_not_of_hubs():
    result = AnchorResult()
    result.add("s::isAdmin", "explicit", 1.0)
    result.add("s::cn", "explicit", 1.0)
    result.add("s::weak", "lexical", 0.4)
    anchors = dict(_impact_anchors(_UsersRepo(), result, False, pool=100))
    assert set(anchors) == {"s::HelpCard", "s::Notifications", "s::routeGuards"}
    # 3 non-test users of a 100-pool: 0.7 * 1.0 * sqrt(0.97)
    assert abs(anchors["s::HelpCard"] - 0.7 * (0.97**0.5)) < 1e-5
    assert all(v <= SOURCE_STRENGTH["impact"] for v in anchors.values())
    # The hub's 200 callers and the test caller are absent; the weak anchor nominated nothing.
    assert not any(k.startswith("s::c") for k in anchors)
    assert "s::isAdmin.test" not in anchors


class _LiteralRepo(_UsersRepo):
    """``invalidateRuns`` calls nobody's attention through the graph, but its query keys are
    quoted by the hook and the page that register those queries; ``'en-US'`` is everywhere."""

    BODY = (
        "export function invalidateRuns(qc) {\n"
        "  qc.invalidateQueries({ queryKey: ['monitoring-executions'] });\n"
        "  qc.invalidateQueries({ queryKey: ['running-workflows'] });\n"
        "  if (typeof window === 'undefined') return;\n"
        "  new Date().toLocaleString('en-US');\n"
        "}\n"
    )

    def symbol_bodies(self, ids):
        return {"s::invalidateRuns": self.BODY} if "s::invalidateRuns" in ids else {}

    def body_mentions(self, mentions, *, repo_ids=None, limit=50):
        rows = {
            "monitoring-executions": ["s::invalidateRuns", "s::useTimeline"],
            "running-workflows": ["s::invalidateRuns", "s::ActivityMonitor", "s::useTimeline"],
            "undefined": [f"s::typeof{i}" for i in range(20)],
            "en-US": [f"s::locale{i}" for i in range(40)],
        }
        out = {}
        for kind, text in mentions:
            hits = rows.get(text, [])
            if hits:
                out[(kind, text)] = (
                    len(hits),
                    [{"symbol_id": s, "name": s[3:], "file_id": f"r:src/{s[3:]}.ts"} for s in hits][
                        :limit
                    ],
                )
        return out


def test_body_literals_keep_namespaced_keys_and_drop_bare_words():
    assert body_literals(_LiteralRepo.BODY) == [
        "monitoring-executions",
        "running-workflows",
        "en-US",
    ]
    assert body_literals("x('ui.theme'); y(\"cortex.accessToken\"); z('/api/runs'); t('../x')") == [
        "ui.theme",
        "cortex.accessToken",
        "/api/runs",
    ]
    assert body_literals("'GET' 'string' 'dark' 'ACTIVE' 'runs'") == []
    assert body_literals("'key-1' 'key-2' 'key-3'", limit=2) == ["key-1", "key-2"]
    assert body_literals("") == []


def test_impact_anchors_reach_the_symbols_quoting_a_dominant_anchors_keys():
    result = AnchorResult()
    result.add("s::invalidateRuns", "explicit", 1.0)
    anchors = dict(_impact_anchors(_LiteralRepo(), result, False, pool=100))
    # Literal pool = 25 of the 100: the two query keys (quoted by 2 and 3 symbols) couple, the
    # locale tag quoted by 40 is a convention and the bare 'undefined' was never a key.
    assert set(anchors) == {"s::useTimeline", "s::ActivityMonitor"}
    assert not any(k.startswith(("s::locale", "s::typeof")) for k in anchors)
    single = 0.7 * (1 - 3 / 25) ** 0.5  # one literal, 3 quoters of a 25-pool
    assert abs(anchors["s::ActivityMonitor"] - single) < 1e-6
    # The hook quotes both keys: noisy-OR of the two, capped at the source strength.
    both = 1 - (1 - 0.7 * (1 - 2 / 25) ** 0.5) * (1 - single)
    assert abs(anchors["s::useTimeline"] - min(both, SOURCE_STRENGTH["impact"])) < 1e-6
    # The root itself is never its own user.
    assert "s::invalidateRuns" not in anchors


def test_impact_anchors_need_a_dominant_root_and_a_graph():
    result = AnchorResult()
    result.add("s::isAdmin", "lexical", IMPACT_MIN_STRENGTH - 0.05)
    assert _impact_anchors(_UsersRepo(), result, False) == []
    result.add("s::isAdmin", "semantic", 0.9)  # noisy-OR lifts it over the threshold
    assert _impact_anchors(_UsersRepo(), result, False)

    class _NoGraph:
        pass

    assert _impact_anchors(_NoGraph(), result, False) == []


class _FilesRepo(_BodyRepo):
    """Suffix lookup over three indexed files, one of them the tail of a traceback path."""

    def __init__(self) -> None:
        super().__init__()
        self.files = {
            "r:src/utils/auth.ts": ["s::isAdmin", "s::isDeveloperOrAbove"],
            "r:src/pages/Login.tsx": ["s::Login"],
            "r:src/i18n/index.ts": ["s::i18n"],
            "r:src/nodes/index.ts": ["s::nodes"],
        }

    def files_by_suffix(self, suffixes, *, repo_ids=None, limit=8):
        out = {}
        for suffix in suffixes:
            hits = [f for f in self.files if f.lower().endswith(("/" + suffix).lower())]
            out[suffix] = hits if 0 < len(hits) <= limit else []
        return out

    def symbols_in_files(self, file_ids):
        return {
            fid: [{"symbol_id": s, "name": s.split("::")[-1]} for s in self.files.get(fid, [])]
            for fid in file_ids
        }


def test_path_source_nominates_the_symbols_of_named_files():
    repo = _FilesRepo()
    result = find_anchors(repo, task_text="see src/utils/auth.ts for the role helpers")
    assert result.anchors["s::isAdmin"] == ["path"]
    assert result.strength_of("s::isAdmin") == SOURCE_STRENGTH["path"]
    assert "s::Login" not in result.anchors


def test_path_source_resolves_traceback_tails_and_splits_ambiguous_names():
    repo = _FilesRepo()
    trace = find_anchors(repo, task_text='File "/opt/app/src/pages/Login.tsx", line 12')
    assert "path" in trace.anchors["s::Login"]
    # index.ts matches two files: both nominated at half strength.
    both = find_anchors(repo, task_text="the index.ts barrel")
    assert both.strength_of("s::i18n") == both.strength_of("s::nodes") > 0
    assert both.strength_of("s::i18n") <= SOURCE_STRENGTH["path"] / 2 + 1e-9
    assert PATH_FILE_LIMIT >= 2


# --- narrowing: per-file cap tapers past the head ----------------------------------------


def test_file_cap_is_linear_in_the_head_then_tapers_and_never_decreases():
    for slot in range(1, PER_FILE_TAPER_FROM + 1):
        assert file_cap(slot) == max(1, -(-slot * PER_FILE_SHARE // 1))
    assert file_cap(50) == 10  # was ceil(50 * 0.6) = 30
    assert file_cap(100) == 15
    caps = [file_cap(s) for s in range(1, 201)]
    assert caps == sorted(caps)


def _cands(n_same_file: int, n_other: int) -> list[Candidate]:
    out = []
    for i in range(n_same_file):
        out.append(
            Candidate(
                symbol_id=f"s::big{i:03d}", kind="function", file_id="r:big.ts", score=100 - i
            )
        )
    for i in range(n_other):
        out.append(
            Candidate(symbol_id=f"s::o{i:03d}", kind="function", file_id=f"r:o{i}.ts", score=50 - i)
        )
    return sorted(out, key=lambda c: (-c.score, c.symbol_id))


def test_narrow_caps_one_files_share_at_large_k_and_stays_k_monotonic():
    ranked = _cands(40, 40)
    picked = narrow(ranked, 50)
    from_big = [c for c in picked if c.file_id == "r:big.ts"]
    assert len(from_big) == file_cap(50) == 10
    assert len({c.file_id for c in picked}) == 41
    # Slots the big file gave up went to other files, in score order.
    assert picked[-1].symbol_id == "s::o039"
    for k in (5, 10, 20, 35):
        assert narrow(ranked, k) == picked[:k]


def test_narrow_head_is_unchanged_by_the_taper():
    ranked = _cands(10, 10)
    head = narrow(ranked, 10)
    # ceil(slot * 0.6) per slot: 1,2,2,3,3,4,5,5,6,6 -> six of the big file's ten fit in 10 slots.
    assert sum(c.file_id == "r:big.ts" for c in head) == 6


# --- layer 3: channel widths and fusion -----------------------------------------------------


def test_channel_widths_scale_with_the_answer_size():
    assert semantic_limit_for(50) == 100 and semantic_limit_for(5) >= 30
    pool, limit = lexical_limits_for(50)
    assert pool == 100 and limit == 50
    assert usage_pool_for(50) == 100 and usage_pool_for(5) == USAGE_POOL


# --- embedder: chunked symbol contents ------------------------------------------------------


def test_split_windows_cover_the_text_with_overlap_and_a_cap():
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(400))
    windows = split_windows(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP, limit=MAX_CHUNKS)
    assert 1 < len(windows) <= MAX_CHUNKS
    assert all(len(w) <= CHUNK_CHARS + 60 for w in windows)  # line-boundary slack
    assert windows[0].startswith("line 000")
    # Consecutive windows overlap: the tail of one reappears at the head of the next.
    assert windows[0][-40:].strip().split()[-1] in windows[1]
    assert split_windows("short", size=CHUNK_CHARS, overlap=CHUNK_OVERLAP, limit=MAX_CHUNKS) == [
        "short"
    ]


def test_symbol_contents_repeat_the_header_on_every_chunk():
    node = GraphNode(
        NodeLabel.SYMBOL,
        "typescript::src.components.TestBotTriggerPanel::::TestBotTriggerPanel#1",
        {
            "name": "TestBotTriggerPanel",
            "kind": "function",
            "file_id": "repo:src/components/TestBotTriggerPanel.tsx",
            "signature": "function TestBotTriggerPanel(props)",
            "docstring": "Panel for the test bot trigger.",
        },
    )
    node.search_text = "function TestBotTriggerPanel(props) {\n" + "  const x = 1;\n" * 400 + "}"

    chunks = symbol_contents(node)
    assert len(chunks) > 1
    assert all(
        "TestBotTriggerPanel" in c and "Panel for the test bot trigger." in c for c in chunks
    )
    assert chunks[0] != chunks[1]

    bare = GraphNode(
        NodeLabel.SYMBOL, "ts::a::::x#1", {"name": "x", "kind": "constant", "file_id": "r:a.ts"}
    )
    assert len(symbol_contents(bare)) == 1
    # search_text is not a persisted property and does not take part in node equality.
    assert "search_text" not in bare.merged_properties()


# --- migrations: static shape checks ---------------------------------------------------------


def test_search_text_migration_adds_body_chunk_and_rebuilds_the_document():
    sql = (MIGRATIONS_DIR / "0011_search_text.sql").read_text(encoding="utf-8-sig")
    statements = split_sql_statements(sql)
    assert any("ADD COLUMN IF NOT EXISTS body TEXT" in s for s in statements)
    assert any("ADD COLUMN IF NOT EXISTS chunk INTEGER NOT NULL DEFAULT 0" in s for s in statements)
    document = next(s for s in statements if "GENERATED ALWAYS AS" in s)
    assert "bce_file_path_words" in document and "body" in document
    assert any(
        "idx_embeddings_kind_ref_chunk" in s and "(kind, ref_id, chunk)" in s for s in statements
    )


def test_body_trgm_migration_is_conditional_on_the_extension():
    sql = (MIGRATIONS_DIR / "0012_body_trgm.sql").read_text(encoding="utf-8-sig")
    statements = split_sql_statements(sql)
    assert len(statements) == 1 and statements[0].count("$$") == 2
    assert "pg_available_extensions" in sql and "gin_trgm_ops" in sql
    assert "idx_symbol_fts_body_trgm" in sql
