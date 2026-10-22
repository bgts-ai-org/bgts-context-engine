"""The agent-facing answer: evidence spans (bce.core.render.evidence) and the compact Markdown
rendering with its budget (bce.core.render.compact), on every surface that hands it out."""

from __future__ import annotations

import json
import re

import pytest

from bce.api.mcp import tools as mcp_tools
from bce.api.mcp.tools import TOOL_SPECS, available_tool_specs, dispatch_tool, result_text
from bce.core.agent_mode import HINT, TRUST, workflow, workflow_text
from bce.core.render import build_evidence, normalize_view, render_compact
from bce.core.render.compact import estimate_tokens
from bce.core.render.evidence import (
    MAX_CODE_SPANS,
    SEARCH_TEXT_MAX_CHARS,
    WHOLE_MAX_LINES,
    content_hash,
    skeleton_entries,
    window_indices,
)
from bce.domain.enums import NodeLabel
from bce.integrations.precontext import render_context_markdown

# ----------------------------------------------------------------------------------------------
# index time: where each declaration's text sits
# ----------------------------------------------------------------------------------------------

PY_SOURCE = b'''class Greeter:
    """Says hello."""

    def __init__(self, name):
        self.name = name

    def greet(self, loud=False):
        text = f"hi {self.name}"
        if loud:
            return text.upper()
        return text


def main():
    return Greeter("x").greet()
'''

TS_SOURCE = b"""export class Store {
  private items: string[] = [];

  add(item: string): void {
    this.items.push(item);
  }
}

export function makeStore(): Store {
  return new Store();
}
"""

JAVA_SOURCE = b"""package demo;

public class Account {
    @Deprecated
    private static int balance;

    public void deposit(int amount) {
        balance += amount;
    }
}
"""

CSHARP_SOURCE = b"""namespace Demo {
    public class Account {
        [Obsolete]
        private static int balance;

        public void Deposit(int amount) {
            balance += amount;
        }
    }
}
"""

GO_SOURCE = b"""package demo

type Account struct {
\tBalance int
}

func (a *Account) Deposit(amount int) {
\ta.Balance += amount
}
"""

_LANGS = [
    ("pkg/greeter.py", PY_SOURCE, None),
    ("src/store.ts", TS_SOURCE, None),
    ("src/Account.java", JAVA_SOURCE, "tree_sitter_java"),
    ("src/Account.cs", CSHARP_SOURCE, "tree_sitter_c_sharp"),
    ("account.go", GO_SOURCE, "tree_sitter_go"),
]


def _symbols(path: str, source: bytes):
    from bce.indexing.extractor import Extractor

    frag = Extractor().extract_file(repo_id="demo", path=path, source=source)
    return [n for n in frag.nodes if n.label is NodeLabel.SYMBOL]


@pytest.mark.parametrize(("path", "source", "grammar"), _LANGS)
def test_every_symbol_records_its_span(path, source, grammar):
    if grammar:
        pytest.importorskip(grammar)
    nodes = _symbols(path, source)
    assert nodes
    file_lines = source.decode().split("\n")
    for node in nodes:
        props = node.properties
        start = props.get("text_line") or props["line"]
        assert props["end_line"] >= props["line"] >= 1, props["name"]
        if node.search_text:
            # the stored text is exactly the file's lines start..end_line, minus the indentation
            # before the first one (``col``)
            text_lines = node.search_text.split("\n")
            assert len(text_lines) == props["end_line"] - start + 1, props["name"]
            assert file_lines[start - 1][props["col"] :].startswith(text_lines[0]), props["name"]


@pytest.mark.parametrize(("path", "source", "grammar"), _LANGS[2:4])
def test_a_field_after_its_annotation_starts_its_text_on_the_annotation(path, source, grammar):
    pytest.importorskip(grammar)
    field = next(n for n in _symbols(path, source) if n.properties["name"] == "balance")
    assert field.properties["text_line"] == field.properties["line"] - 1


def test_the_evidence_cap_follows_the_indexer():
    from bce.indexing.parser._treesitter import SEARCH_TEXT_MAX_CHARS as INDEXER_CAP

    assert SEARCH_TEXT_MAX_CHARS == INDEXER_CAP


# ----------------------------------------------------------------------------------------------
# evidence: windows and skeletons
# ----------------------------------------------------------------------------------------------


def _long_function(n: int = 300) -> list[str]:
    lines = ["def handle(request, cache):"]
    for i in range(1, n):
        if i == 80:
            lines.append("    token = request.headers.get('X-Auth-Token')")
        elif i == 220:
            lines.append("    cache.invalidate(token)")
        elif i % 10 == 0:
            lines.append("")
        else:
            lines.append(f"    step_{i} = compute({i})")
    return lines


def test_a_long_declaration_is_windowed_around_what_the_task_names():
    lines = _long_function()
    task = "The `X-Auth-Token` header is ignored and `cache.invalidate` never runs"
    shown, focus, complete = window_indices(lines, task)
    assert not complete
    assert 0 in shown  # the signature
    assert 80 in shown and 220 in shown and 80 in focus and 220 in focus
    assert 150 not in shown  # the middle is left out
    assert len(shown) <= WHOLE_MAX_LINES
    # windows end on statement boundaries: a blank line follows each window, unless at the end
    runs = []
    for i in shown:
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    for _, end in runs[1:]:
        assert end + 1 >= len(lines) or not lines[end + 1].strip()


def test_a_short_declaration_is_shown_whole():
    lines = _long_function(60)
    shown, _, complete = window_indices(lines, "anything")
    assert complete and shown == list(range(60))


def test_without_a_named_term_a_long_declaration_keeps_its_top():
    shown, focus, complete = window_indices(_long_function(), "rewrite it")
    assert not complete and shown[0] == 0 and focus == [0] and len(shown) < 60


CLASS_TEXT = '''class Cache(Base):
    """A cache.

    Spans lines.
    """

    ttl = 30

    @property
    def size(self):
        return len(self._items)

    def get(self, key):
        # look it up
        value = self._items.get(key)
        return value

    # a comment between members
    def clear(self):
        self._items = {}'''


def test_a_class_is_a_skeleton_of_its_members_with_ranges():
    lines = CLASS_TEXT.split("\n")
    entries, n_head, omitted = skeleton_entries(lines)
    shown = [lines[i].strip() for i, _ in entries]
    assert n_head == 1 and omitted == 0
    assert shown == ["class Cache(Base):", "ttl = 30", "def size(self):", "def get(self, key):",
                     "def clear(self):"]  # fmt: skip
    ranges = {lines[i].strip(): (i, last) for i, last in entries}
    assert ranges["def size(self):"] == (9, 10)
    assert ranges["def get(self, key):"] == (12, 15)  # the next member's comment is not its end
    assert ranges["ttl = 30"] == (6, None)


def _one_span(text: str, *, kind: str, col: int, path: str = "a.go") -> dict:
    item = {"symbol_id": "s", "file_id": f"r:{path}", "name": "X", "kind": kind, "line": 20,
            "col": col, "tier": "full", "detail_level": "full", "symbol_relevance": 3.0}  # fmt: skip
    return build_evidence([item], [item], task_text="", bodies=lambda ids: {"s": text})[0]


@pytest.mark.parametrize(
    ("text", "col", "path", "first"),
    [
        # Go `type CacheWarmer interface {`: the node starts after `type `, the closing brace
        # is at column 0 - nothing is prepended
        ("CacheWarmer interface {\n\tPreCache(id string)\n}", 5, "a.go", "CacheWarmer interface {"),
        # inside a `type ( ... )` group the closing brace is indented like the node
        ("CacheWarmer interface {\n\t\tPreCache(id string)\n\t}", 1, "a.go",
         "\tCacheWarmer interface {"),
        # `export function f() {`: the node starts after `export `
        ("function f() {\n  return 1;\n}", 7, "a.ts", "function f() {"),
        # a Python method: everything after the first line is deeper
        ("def f(self):\n        return 1", 4, "a.py", "    def f(self):"),
        # a one-line declaration cannot tell: shown as stored
        ("x = 1", 4, "a.py", "x = 1"),
    ],
)  # fmt: skip
def test_the_first_line_gets_only_indentation_the_file_has(text, col, path, first):
    span = _one_span(text, kind="function", col=col, path=path)
    assert span["lines"][0] == [20, first]


def test_a_short_class_is_shown_whole_and_a_long_one_as_a_skeleton():
    short = _one_span("CacheWarmer interface {\n\tPreCache(id string)\n}", kind="interface", col=5)
    assert short["shape"] == "whole" and short["complete"]
    assert [t for _, t in short["lines"]] == [
        "CacheWarmer interface {",
        "\tPreCache(id string)",
        "}",
    ]
    long = _one_span(SESSION_TEXT, kind="class", col=0, path="a.py")
    assert long["shape"] == "skeleton"


def test_a_wrapped_method_header_is_one_member():
    text = (
        "class Node:\n    def __init__(\n        self,\n        name: str,\n    ) -> None:\n"
        "        self.name = name\n\n    def stub(\n        self,\n    ) -> None: ...\n"
    )
    lines = text.rstrip("\n").split("\n")
    entries, _, _ = skeleton_entries(lines)
    assert [(lines[i].strip(), last) for i, last in entries][1:] == [
        ("def __init__(", 5),
        ("def stub(", 9),  # its wrapped header's last line ends it
    ]


def test_a_wrapped_signature_is_listed_on_one_line():
    from bce.core.render.compact import one_line_signature

    sig = "(\n        self, name: Optional[str] = None,\n    )"
    assert one_line_signature("iter", sig) == "`iter(self, name: Optional[str] = None)`"


def test_a_java_class_skeleton_keeps_annotations_in_its_header():
    text = "@Entity\npublic class User {\n    private int id;\n\n    void save() {\n        x();\n    }\n}"
    lines = text.split("\n")
    entries, n_head, _ = skeleton_entries(lines)
    assert n_head == 2
    assert [lines[i].strip() for i, _ in entries][2:] == ["private int id;", "void save() {"]
    assert dict(entries)[4] == 6  # the method's closing brace ends it


# ----------------------------------------------------------------------------------------------
# a payload as get_context_for_task builds it
# ----------------------------------------------------------------------------------------------

AUTH_TEXT = "\n".join(
    [
        "def check_token(request):",
        "    raw = request.headers.get('X-Auth-Token')",
        "    if not raw:",
        "        return None",
        "    return decode(raw)",
    ]
)


def _ranked():
    return [
        {
            "symbol_id": "py::auth::check_token#1",
            "repo_id": "demo",
            "file_id": "demo:app/auth.py",
            "name": "check_token",
            "kind": "function",
            "signature": "(request)",
            "line": 10,
            "end_line": 14,
            "col": 0,
            "indexed_at_commit": "0123456789abcdef0123",
            "graph_distance": 0,
            "tier": "full",
            "symbol_relevance": 3.0,
        },
        {
            "symbol_id": "py::auth::Session#2",
            "repo_id": "demo",
            "file_id": "demo:app/auth.py",
            "name": "Session",
            "kind": "class",
            "line": 30,
            "end_line": 61,
            "col": 0,
            "graph_distance": 1,
            "tier": "full",
            "symbol_relevance": 1.0,
        },
        {
            "symbol_id": "py::auth::refresh#3",
            "repo_id": "demo",
            "file_id": "demo:app/auth.py",
            "name": "refresh",
            "kind": "function",
            "signature": "(session, force=False)",
            "line": 60,
            "end_line": 72,
            "graph_distance": 1,
            "tier": "full",
            "symbol_relevance": 0.5,
        },
    ]


def _assembled():
    out = []
    for it in _ranked():
        entry = {k: v for k, v in it.items() if k not in ("col", "indexed_at_commit")}
        entry.update(detail_level="full", content="...", tokens=5)
        out.append(entry)
    out.append(
        {
            "symbol_id": "py::http::send#4",
            "repo_id": "demo",
            "file_id": "demo:app/http.py",
            "name": "send",
            "kind": "function",
            "line": 5,
            "detail_level": "reference",
            "tier": "stub",
            "file_relevance": 0.4,
            "content": "app/http.py - 2 candidate symbol(s): send, retry",
        }
    )
    return out


#: Longer than CONTAINER_WHOLE_MAX_LINES, so it is shown as a skeleton (lines 30-61).
SESSION_TEXT = CLASS_TEXT + "".join(
    f"\n\n    def m{i}(self):\n        return {i}" for i in range(4)
)


def _bodies(ids):
    texts = {"py::auth::check_token#1": AUTH_TEXT, "py::auth::Session#2": SESSION_TEXT}
    return {i: texts[i] for i in ids if i in texts}


TASK = "Requests with an empty `X-Auth-Token` header crash in check_token"


def _payload(**coverage):
    items = _assembled()
    return {
        "commit": None,
        "files": [
            {
                "path": "app/auth.py",
                "file_id": "demo:app/auth.py",
                "tier": "full",
                "relevance": 0.9,
                "symbols": ["check_token", "Session", "refresh"],
            },
            {
                "path": "app/http.py",
                "file_id": "demo:app/http.py",
                "tier": "stub",
                "relevance": 0.4,
                "symbols": ["send"],
            },
            {
                "path": "app/noise.py",
                "file_id": "demo:app/noise.py",
                "tier": "stub",
                "relevance": 0.05,
                "symbols": ["noise"],
            },
        ],  # fmt: skip
        "context": {"items": items, "budget": 1500, "used_tokens": 20},
        "evidence": build_evidence(items, _ranked(), task_text=TASK, bodies=_bodies),
        "coverage": {"confidence": "high", **coverage},
    }


def test_evidence_spans_carry_range_role_hash_and_snapshot():
    spans = _payload()["evidence"]
    assert [s["name"] for s in spans] == ["check_token", "Session", "refresh"]
    first = spans[0]
    assert (first["start"], first["end"], first["role"]) == (10, 14, "edit")
    assert first["complete"] and first["shape"] == "whole"
    assert first["content_hash"] == content_hash(AUTH_TEXT)
    assert first["commit"] == "0123456789abcdef0123"
    assert first["lines"][1] == [11, "    raw = request.headers.get('X-Auth-Token')"]
    assert 11 in first["focus"]  # the line the task names
    assert spans[1]["shape"] == "skeleton" and spans[1]["role"] == "dependency"
    # no stored text (an older index): the display snippet, unnumbered
    assert spans[2]["shape"] == "snippet" and spans[2]["lines"] is None


def test_evidence_is_limited_to_the_primary_files_and_the_span_cap():
    ranked = [
        {**_ranked()[0], "symbol_id": f"s{i}", "name": f"f{i}", "symbol_relevance": i / 10}
        for i in range(6)
    ]
    assembled = [{**it, "detail_level": "full"} for it in ranked]
    assembled.append({**assembled[0], "symbol_id": "x", "file_id": "demo:b.py", "tier": "stub"})
    spans = build_evidence(assembled, ranked, task_text="", bodies=lambda ids: {})
    assert len(spans) == MAX_CODE_SPANS
    assert [s["name"] for s in spans] == ["f5", "f4", "f3"]  # the selector's scores first


def test_a_failing_body_lookup_degrades_to_snippets():
    def boom(ids):
        raise RuntimeError("db gone")

    spans = build_evidence(_assembled(), _ranked(), task_text=TASK, bodies=boom)
    assert spans and all(s["shape"] == "snippet" for s in spans)


# ----------------------------------------------------------------------------------------------
# the compact text
# ----------------------------------------------------------------------------------------------


def test_compact_layout():
    payload = _payload(
        unresolved_identifiers=[{"name": "TokenStore", "defined_in": ["demo:app/store.py"]}],
        likely_incomplete=True,
    )
    text = render_compact(payload)
    lines = text.split("\n")
    assert lines[0].startswith("# BCE context · demo @ 0123456 · confidence high · ~")
    assert "## Primary edit targets" in lines
    head = f"### app/auth.py:10-14 · function check_token · edit · complete · {content_hash(AUTH_TEXT)}"
    assert head in lines
    i = lines.index(head)
    assert lines[i + 1] == "```python"
    assert lines[i + 2] == "10→def check_token(request):"
    assert lines[i + 3] == "11→    raw = request.headers.get('X-Auth-Token')"
    assert "### app/auth.py:30-61 · class Session · dependency · skeleton · " in text
    assert "39→    def size(self):  [L39-40]" in lines
    assert "### app/auth.py:60-72 · function refresh · dependency · snippet" in text
    # the stub with a plausible probability is listed, the improbable one is not
    assert "- `app/http.py` — send, retry (p 0.40)" in lines
    assert "noise.py" not in text
    gaps = lines[lines.index("## Gaps") + 1 :]
    assert gaps[0] == "- `TokenStore` is not covered here; defined in `app/store.py`"
    assert gaps[1] == ""  # likely_incomplete stays in the JSON view; no guidance binds to it
    # no JSON plumbing in the compact view
    assert "symbol_id" not in text and "demo:app" not in text and "payload" not in text


def test_compact_lists_related_tests_and_the_gap_signals():
    payload = _payload(
        state="unknown",
        gaps={
            "unknown_identifiers": ["NewThing", "OtherNew"],
            "unsupported_paths": [
                {"path": "src/lib.rs", "reason": "language", "extension": ".rs"},
                {"path": "app/gone.py", "reason": "missing"},
            ],
            "truncated_evidence": ["app/big.py:1-900"],
            "missing_tests": False,
            "semantic_degraded": "the index uses hashing embeddings, which carry no meaning",
        },
    )
    payload["tests"] = [
        {
            "file_id": "demo:tests/test_auth.py",
            "path": "tests/test_auth.py",
            "why": "calls check_token",
            "symbols": [
                {"name": "test_empty", "qualified": "TestCheckToken::test_empty", "line": 40,
                 "end_line": 52},
                {"name": "test_bad", "qualified": "test_bad", "line": 60, "end_line": None},
            ],
        }
    ]  # fmt: skip
    text = render_compact(payload)
    lines = text.split("\n")
    assert " · confidence high · coverage unknown · ~" in lines[0]
    i = lines.index("## Related tests")
    assert lines[i + 1] == (
        "- `tests/test_auth.py` — `TestCheckToken::test_empty` (L40-52), `test_bad` (L60)"
        " · calls check_token"
    )
    assert lines.index("## Related files") < i < lines.index("## Gaps")
    gaps = lines[lines.index("## Gaps") + 1 :]
    assert gaps[:5] == [
        "- `src/lib.rs` is not in the index (.rs files are not parsed).",
        "- `app/gone.py` has no indexed symbols (not indexed, or no such file).",
        "- No indexed symbol is named `NewThing`, `OtherNew` (new names, or code the index does "
        "not cover).",
        "- Semantic search did not contribute (the index uses hashing embeddings, which carry no "
        "meaning); the answer rests on names, keywords and the graph.",
        "- `app/big.py:1-900` end where the index's stored text does; the rest is in the file.",
    ]
    missing = render_compact({**payload, "coverage": {"gaps": {"missing_tests": True}}})
    assert "- No existing test of the primary edit targets was found in the index." in missing


def test_compact_header_counts_the_final_text():
    text = render_compact(_payload())
    count = int(re.search(r"~(\d+) tokens", text).group(1))
    assert abs(count - estimate_tokens(text)) <= 1


@pytest.mark.parametrize("budget", [300, 1000, 1500, 3000])
def test_the_budget_bounds_the_whole_text(budget):
    items = _assembled()
    long_text = "\n".join(_long_function(110))
    ranked = _ranked()
    ranked[0] = {**ranked[0], "end_line": 10 + 109}
    items[0] = {**items[0], "end_line": 10 + 109}

    def bodies(ids):
        return {"py::auth::check_token#1": long_text, "py::auth::Session#2": CLASS_TEXT}

    payload = {
        **_payload(),
        "evidence": build_evidence(items, ranked, task_text=TASK, bodies=bodies),
    }
    text = render_compact(payload, max_tokens=budget)
    assert estimate_tokens(text) <= budget
    assert "### app/auth.py:10-119 · function check_token" in text  # the first span always stays
    if budget >= 3000:
        assert "119→" in text  # nothing had to be cut


def test_a_span_short_of_budget_keeps_the_lines_it_was_shown_for():
    lines = _long_function(110)
    lines[60] = "    token = request.headers.get('X-Auth-Token')"
    ranked = [{**_ranked()[0], "end_line": 119}]
    assembled = [{**ranked[0], "detail_level": "full"}]
    spans = build_evidence(
        assembled,
        ranked,
        task_text=TASK,
        bodies=lambda ids: {ranked[0]["symbol_id"]: "\n".join(lines)},
    )
    payload = {"context": {"items": assembled}, "evidence": spans, "coverage": {}}
    text = render_compact(payload, max_tokens=250)
    assert "70→    token = request.headers.get('X-Auth-Token')" in text
    assert "10→def handle(request, cache):" in text
    assert "⋯ lines " in text and estimate_tokens(text) <= 250


def test_compact_is_deterministic():
    assert render_compact(_payload()) == render_compact(_payload())


def test_an_older_payload_without_evidence_still_renders():
    payload = {"context": {"items": _assembled()}, "coverage": {"confidence": "low"}}
    text = render_compact(payload)
    assert "### app/auth.py:10-14 · function check_token · edit · snippet" in text
    assert "confidence low" in text


def test_views():
    assert normalize_view(None) == "compact" and normalize_view(" JSON ") == "debug"
    assert normalize_view("evidence") == "compact"
    with pytest.raises(ValueError, match="view 'xml'"):
        normalize_view("xml")


# ----------------------------------------------------------------------------------------------
# surfaces: MCP, precontext
# ----------------------------------------------------------------------------------------------


def test_mcp_returns_the_compact_text_by_default_and_json_on_request():
    result = {"tool": "get_context_for_task", "payload": _payload()}
    assert result_text("get_context_for_task", result) == render_compact(_payload())
    assert json.loads(result_text("get_context_for_task", result, view="debug")) == json.loads(
        json.dumps(result, default=str)
    )
    other = {"tool": "resolve_symbol", "payload": {"k": 1}}
    assert json.loads(result_text("resolve_symbol", other)) == other


def test_mcp_compact_text_falls_back_to_json_when_rendering_fails(monkeypatch):
    def broken(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(mcp_tools, "render_compact", broken)
    result = {"tool": "get_context_for_task", "payload": _payload()}
    assert json.loads(result_text("get_context_for_task", result))["tool"] == "get_context_for_task"


def test_mcp_answer_states_the_mode_workflow_in_compact_terms(monkeypatch):
    monkeypatch.setattr(
        mcp_tools,
        "get_context_for_task",
        lambda repository, **kw: {"tool": "get_context_for_task", "payload": _payload()},
    )
    out = dispatch_tool(None, "get_context_for_task", {"task_text": TASK}, agent_mode=HINT)
    text = result_text("get_context_for_task", out)
    assert "## Workflow - BCE_AGENT_MODE=hint (starting point)" in text
    assert '"Gaps" places in a file' in text and "payload." not in text


def test_the_view_argument_is_advertised_and_the_description_matches_the_view():
    props = TOOL_SPECS["get_context_for_task"]["schema"]["properties"]
    assert props["view"]["enum"] == ["compact", "locate", "debug"]
    compact = available_tool_specs(allow_write=False, agent_mode=TRUST)["get_context_for_task"]
    assert "## Primary edit targets" in compact["description"]
    assert compact["description"].endswith(workflow_text(TRUST, style="compact"))
    debug = available_tool_specs(allow_write=False, agent_mode=TRUST, view="debug")
    assert "payload.files" in debug["get_context_for_task"]["description"]
    assert debug["get_context_for_task"]["description"].endswith(workflow_text(TRUST))


def test_the_locate_view_lists_ranges_without_code():
    full = render_compact(_payload(), mode=HINT)
    text = render_compact(_payload(), mode=HINT, view="locate")
    assert "```" not in text and "→" not in text.split("## Workflow")[0]
    assert "- `app/auth.py:10-14` · function check_token · edit" in text
    assert "## Related files" in text
    assert "each entry is a declaration and its line range" in text
    assert len(text) < len(full) and estimate_tokens(text) <= 800


def test_precontext_block_is_the_mcp_answer():
    payload = _payload()
    mcp = result_text(
        "get_context_for_task",
        {"tool": "get_context_for_task", "payload": {**payload, "workflow": workflow(HINT)}},
    )
    assert render_context_markdown(payload, mode=HINT) == mcp


def test_precontext_char_cap_keeps_the_block_whole():
    text = render_context_markdown(_payload(), mode=HINT, max_chars=1200)
    assert len(text) <= 1200
    assert "## Workflow - BCE_AGENT_MODE=hint" in text
    assert "### app/auth.py:10-14" in text
