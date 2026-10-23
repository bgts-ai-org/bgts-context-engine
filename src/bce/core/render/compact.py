"""The compact Markdown rendering of a ``get_context_for_task`` answer (the agent-facing view).

Layout; a section without content is left out::

    # BCE context · <repo> @ <commit> · confidence <c> [· coverage unknown]
      [· index parses <P> % of the code] · ~<N> tokens
    ## Primary edit targets
    [Unconfirmed: ... (best p <p>). Confirm the location with a search before you edit.]
    ### <path>:<start>-<end> · <kind> <name> · <edit|dependency> · <state> · <content hash>
    ```<lang>
    <line>→<code>
    ⋯ lines <a>-<b> omitted
    ```
    ## Other symbols in primary files
    - `<path>:<line>` <kind> `<name><signature>` (L<line>-<end>)
    ## Related files
    - `<path>` — <symbol> (L<line>-<end>), ... (p <relevance>)
    ## Related tests
    - `<path>` — `<Class::test>` (L<line>-<end>) · <calls X | test file of P>
    ## Gaps
    - <what the engine knows the answer lacks (``coverage.gaps``)>
    ## Workflow - BCE_AGENT_MODE=<mode> (<label>)
    1. <the agent mode's steps, worded for this layout>

``<state>`` is ``complete`` (the whole declaration), ``partial`` (windows of a long one),
``truncated`` (the stored text ends before the declaration does), ``skeleton`` (a class's
header and members), ``outline`` (a file's top-level lines and declaration headers) or
``snippet`` (an index without stored declaration text). Code lines carry
the file's own line numbers and indentation in the ``<line>→<text>`` form editors' read tools
print, so an edit can be written from them without re-reading the file.

The token budget applies to this text as a whole (about four characters per token): parts are
admitted in priority order - header, workflow, gaps, related files, the first span, related tests,
further spans, other symbols - and a span that does not fit keeps the lines nearest to its signature
and to what the task names before it is dropped; a file left without a shown span is then listed
under related files. ``~N tokens`` in the header is the estimate of the final text, which never
exceeds ``max_tokens``.

"Related files" lists, with the context selector's verdicts, the files it rates likely to be edited
(:data:`RELATED_MIN_RELEVANCE`) and the files the task names; the unconfirmed line appears when it
rates none of the files shown in full as likely (``coverage.target_unconfirmed``).

Pure: the same payload and budget always give the same text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from bce.core.agent_mode import AGENT_MODE_VAR, LABELS, normalize_agent_mode, workflow_steps
from bce.core.assembler.files_summary import files_summary
from bce.core.defaults import DEFAULT_MAX_TOKENS
from bce.core.render.evidence import PRIMARY_FILES_UNTIERED, build_evidence, path_of

CHARS_PER_TOKEN = 4
#: ``compact``: this Markdown; ``locate``: the same without code (one line per span);
#: ``debug``: the full JSON envelope.
VIEWS = ("compact", "locate", "debug")
DEFAULT_VIEW = "compact"
_ALIASES = {"evidence": "compact", "markdown": "compact", "md": "compact", "json": "debug"}
#: "Related files" with the selector's verdicts: the files without a shown span that it rates at
#: least this likely to be edited (and the files the task names), most probable first, at most
#: :data:`RELATED_MAX`. Below 0.5, 3 of 126 listed files were edited by the change.
RELATED_MAX = 6
RELATED_MIN_RELEVANCE = 0.5
#: Without the selector: the full-detail files, then at most this many reference-level files.
RELATED_MAX_UNTIERED = 3
#: Symbols (with their line ranges) named per related file.
RELATED_SYMBOLS = 4
OTHER_MAX = 8
GAPS_MAX = 6
TESTS_MAX = 2
_SIGNATURE_MAX_CHARS = 120
_REASON_MAX_CHARS = 80
#: Distances from the focus lines a span short of budget is cut to, in turn.
_SHRINK_DISTANCES = (12, 6, 2, 0)
#: The header is reserved at this many token digits; the final count never has more.
_TOKEN_DIGITS = 6
_HEADINGS = {
    "spans": "## Primary edit targets",
    "other": "## Other symbols in primary files",
    "related": "## Related files",
    "tests": "## Related tests",
    "gaps": "## Gaps",
}


def normalize_view(value: str | None) -> str:
    """The view named by ``value`` (empty means :data:`DEFAULT_VIEW`); an unknown name raises."""
    view = (value or "").strip().lower()
    if not view:
        return DEFAULT_VIEW
    view = _ALIASES.get(view, view)
    if view not in VIEWS:
        raise ValueError(f"view {value!r} is not one of: {', '.join(VIEWS)}")
    return view


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


class _Budget:
    def __init__(self, chars: int) -> None:
        self.left = chars

    def fits(self, lines: list[str]) -> bool:
        return sum(len(line) + 1 for line in lines) <= self.left

    def take(self, lines: list[str]) -> bool:
        if not self.fits(lines):
            return False
        self.left -= sum(len(line) + 1 for line in lines)
        return True


def _short(commit: str) -> str:
    return (
        commit[:7] if len(commit) >= 12 and all(c in "0123456789abcdef" for c in commit) else commit
    )


def _header(payload: dict[str, Any], spans: list[dict[str, Any]], tokens: int | str) -> str:
    items = (payload.get("context") or {}).get("items") or []
    repos = sorted({str(it.get("repo_id")) for it in items if it.get("repo_id")})
    commit = next((str(s["commit"]) for s in spans if s.get("commit")), None) or payload.get(
        "commit"
    )
    where = repos[0] if len(repos) == 1 else (f"{len(repos)} repos" if repos else "")
    if commit:
        where = f"{where} @ {_short(str(commit))}".strip()
    coverage = payload.get("coverage") or {}
    conf = coverage.get("confidence") or "unknown"
    state = "coverage unknown" if coverage.get("state") == "unknown" else ""
    index = coverage.get("index") or {}
    parsed = (
        f"index parses {_percent(index.get('ratio'))} of the code" if index.get("partial") else ""
    )
    parts = ["# BCE context", where, f"confidence {conf}", state, parsed, f"~{tokens} tokens"]
    return " · ".join(p for p in parts if p)


def _percent(ratio: Any) -> str:
    return f"{round(float(ratio or 0.0) * 100)} %"


def _index_line(coverage: dict[str, Any]) -> str | None:
    """The Gaps line for a repository the index only partly parses (``coverage.index``)."""
    index = coverage.get("index") or {}
    if not index.get("partial"):
        return None
    langs = []
    for row in (index.get("unparsed") or [])[:3]:
        missing = int(row.get("files") or 0) - int(row.get("indexed") or 0)
        where = ", ".join(f"`{d}`" for d in (row.get("dirs") or [])[:2])
        langs.append(
            f"{row.get('language')} ({missing} files" + (f", in {where}" if where else "") + ")"
        )
    text = f"- The index parses {_percent(index.get('ratio'))} of this repository's code files"
    if langs:
        text += "; not parsed: " + "; ".join(langs)
    if coverage.get("confidence") == "unsupported":
        return text + ". Rely on your own search: this answer cannot speak for the rest."
    return text + ". Search those parts with your own tools."


def _state(span: dict[str, Any]) -> str:
    shape = span.get("shape")
    if shape in ("skeleton", "snippet", "outline"):
        return str(shape)
    if span.get("truncated"):
        return "truncated"
    return "complete" if span.get("complete") else "partial"


def _span_heading(span: dict[str, Any]) -> str:
    start = int(span.get("start") or 1)
    end = int(span.get("end") or start)
    label = " ".join(str(p) for p in (span.get("kind"), span.get("name")) if p)
    parts = [f"{span.get('path')}:{start}-{end}", label, span.get("role") or "", _state(span)]
    if span.get("content_hash"):
        parts.append(str(span["content_hash"]))
    return "### " + " · ".join(p for p in parts if p)


def _span_lines(span: dict[str, Any], keep: set[int] | None) -> list[str]:
    """Heading and fenced code of ``span``, restricted to the line numbers in ``keep``."""
    lang = str(span.get("lang") or "")
    rows = span.get("lines")
    if rows is None:
        body = str(span.get("snippet") or "").split("\n")
        fence = "~~~" if any("```" in b for b in body) else "```"
        return [_span_heading(span), fence + lang, *body, fence]
    skeleton = span.get("shape") == "skeleton"
    ranges = {int(a): int(b) for a, b in span.get("ranges") or []}
    shown = [(int(n), str(t)) for n, t in rows if keep is None or int(n) in keep]
    fence = "~~~" if any("```" in t for _, t in shown) else "```"
    out = [_span_heading(span), fence + lang]
    prev: int | None = None
    for n, text in shown:
        if not skeleton and prev is not None and n > prev + 1:
            out.append(f"⋯ lines {prev + 1}-{n - 1} omitted")
        out.append(f"{n}→{text}" + (f"  [L{n}-{ranges[n]}]" if n in ranges else ""))
        prev = n
    end = span.get("end")
    if not skeleton and prev is not None and end and prev < int(end):
        out.append(f"⋯ lines {prev + 1}-{int(end)} omitted")
    if skeleton:
        hidden = len(rows) - len(shown) + int(span.get("more_members") or 0)
        if hidden:
            out.append(f"⋯ {hidden} more members")
    out.append(fence)
    return out


def _variants(span: dict[str, Any]) -> Iterator[list[str]]:
    """Renderings of ``span``, fullest first, then cut toward its focus lines."""
    yield _span_lines(span, None)
    rows = span.get("lines")
    if rows is None:
        return
    numbers = [int(n) for n, _ in rows]
    focus = sorted(int(f) for f in span.get("focus") or numbers[:1])
    if span.get("shape") == "skeleton":
        members = [n for n in numbers if n not in set(focus)]
        for k in (len(members) // 2, 8, 0):
            if k < len(members):
                yield _span_lines(span, set(focus) | set(members[:k]))
        return
    for distance in _SHRINK_DISTANCES:
        keep = {n for n in numbers if any(abs(n - f) <= distance for f in focus)}
        if len(keep) < len(numbers):
            yield _span_lines(span, keep)


def _fit_span(budget: _Budget, span: dict[str, Any], *, required: bool = False) -> list[str]:
    for lines in _variants(span):
        if budget.take(lines):
            return lines
    if required and budget.take([_span_heading(span)]):
        return [_span_heading(span)]
    return []


def _take_section(budget: _Budget, key: str, lines: list[str]) -> list[str]:
    heading = _HEADINGS[key]
    if not lines or not budget.take([heading, lines[0]]):
        return []
    out = [heading, lines[0]]
    for line in lines[1:]:
        if budget.take([line]):
            out.append(line)
    return out


def _code_list(names: list[Any]) -> str:
    return ", ".join(f"`{n}`" for n in names if n)


def _gap_lines(coverage: dict[str, Any]) -> list[str]:
    gaps = coverage.get("gaps") or {}
    out: list[str] = []
    index = _index_line(coverage)
    if index:
        out.append(index)
    for gap in coverage.get("unresolved_identifiers") or []:
        name = gap.get("name")
        if not name:
            continue
        where = ", ".join(f"`{path_of(str(f))}`" for f in (gap.get("defined_in") or [])[:3])
        out.append(f"- `{name}` is not covered here" + (f"; defined in {where}" if where else ""))
    for entry in gaps.get("unsupported_paths") or []:
        path = entry.get("path")
        if entry.get("reason") == "language":
            ext = entry.get("extension") or "these"
            out.append(f"- `{path}` is not in the index ({ext} files are not parsed).")
        elif path:
            out.append(f"- `{path}` has no indexed symbols (not indexed, or no such file).")
    unknown = _code_list(gaps.get("unknown_identifiers") or [])
    if unknown:
        out.append(
            f"- No indexed symbol is named {unknown} (new names, or code the index does not cover)."
        )
    selector = coverage.get("selector") or {}
    if selector.get("status") in ("timeout", "error"):
        out.append("- The context selector did not answer; the files are unfiltered.")
    degraded = gaps.get("semantic_degraded") if gaps else coverage.get("semantic_error")
    if degraded:
        reason = str(degraded).splitlines()[0][:_REASON_MAX_CHARS]
        out.append(
            f"- Semantic search did not contribute ({reason}); the answer rests on names, "
            "keywords and the graph."
        )
    truncated = _code_list(gaps.get("truncated_evidence") or [])
    if truncated:
        out.append(
            f"- {truncated} end where the index's stored text does; the rest is in the file."
        )
    if gaps.get("missing_tests"):
        out.append("- No existing test of the primary edit targets was found in the index.")
    return out[:GAPS_MAX]


def _test_lines(tests: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for entry in tests[:TESTS_MAX]:
        path = entry.get("path") or path_of(str(entry.get("file_id") or ""))
        if not path:
            continue
        symbols = []
        for sym in entry.get("symbols") or []:
            line, end = int(sym.get("line") or 0), int(sym.get("end_line") or 0)
            rng = f" (L{line}-{end})" if end > line > 0 else (f" (L{line})" if line else "")
            symbols.append(f"`{sym.get('qualified') or sym.get('name')}`{rng}")
        text = f"- `{path}`" + (f" — {', '.join(symbols)}" if symbols else "")
        if entry.get("why"):
            text += f" · {entry['why']}"
        out.append(text)
    return out


def _locate_line(span: dict[str, Any]) -> str:
    start = int(span.get("start") or 1)
    end = int(span.get("end") or start)
    label = " ".join(str(p) for p in (span.get("kind"), span.get("name")) if p)
    parts = [f"- `{span.get('path')}:{start}-{end}`", label, span.get("role") or ""]
    return " · ".join(p for p in parts if p)


def workflow_lines(mode: str, *, view: str = "compact") -> list[str]:
    """The agent mode's workflow as this layout's last section."""
    mode = normalize_agent_mode(mode)
    steps = workflow_steps(mode, style="locate" if view == "locate" else "compact")
    head = f"## Workflow - {AGENT_MODE_VAR}={mode} ({LABELS[mode]})"
    return [head, *(f"{i}. {s}" for i, s in enumerate(steps, 1))]


def _stub_names(items: list[dict[str, Any]]) -> dict[str, str]:
    marker = "candidate symbol(s): "
    out: dict[str, str] = {}
    for it in items:
        content = str(it.get("content") or "")
        if it.get("tier") == "stub" and marker in content:
            out.setdefault(str(it.get("file_id")), content.split(marker, 1)[1])
    return out


def _line_range(line: int, end: int) -> str:
    return f" (L{line}-{end})" if end > line > 0 else (f" (L{line})" if line else "")


def _symbol_ranges(items: list[dict[str, Any]], fid: str) -> str:
    """The first :data:`RELATED_SYMBOLS` full-detail symbols of ``fid`` with their line ranges."""
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        name = str(it.get("name") or "")
        if str(it.get("file_id")) != fid or not name or name in seen:
            continue
        if it.get("tier") == "stub" or it.get("detail_level") == "reference":
            continue
        seen.add(name)
        out.append(f"{name}{_line_range(int(it.get('line') or 0), int(it.get('end_line') or 0))}")
        if len(out) >= RELATED_SYMBOLS:
            break
    return ", ".join(out)


def _related_line(f: dict[str, Any], symbols: str) -> str:
    fid = str(f.get("file_id") or "")
    line = f"- `{f.get('path') or path_of(fid)}`" + (f" — {symbols}" if symbols else "")
    rel = f.get("relevance")
    return line + (f" (p {float(rel):.2f})" if rel is not None else "")


def _related_lines(
    files: list[dict[str, Any]], primary: list[str], items: list[dict[str, Any]]
) -> list[str]:
    """The answer's files without a span (see :data:`RELATED_MAX` / :data:`RELATED_MAX_UNTIERED`)."""
    names = _stub_names(items)
    pinned = {str(it.get("file_id")) for it in items if it.get("pinned")}
    tiered = any(f.get("relevance") is not None for f in files)
    rows: list[tuple[float, int, dict[str, Any]]] = []
    stubs = 0
    for pos, f in enumerate(files):
        fid = str(f.get("file_id") or "")
        if not fid or fid in primary:
            continue
        rel = f.get("relevance")
        if tiered:
            if fid not in pinned and (rel is None or float(rel) < RELATED_MIN_RELEVANCE):
                continue
        elif f.get("tier") == "stub":
            if stubs >= RELATED_MAX_UNTIERED:
                continue
            stubs += 1
        if tiered:
            key = -(float(rel) if rel is not None else 1.0)
        else:
            key = 1.0 if f.get("tier") == "stub" else 0.0
        rows.append((key, pos, f))
    rows.sort(key=lambda r: (r[0], r[1]))
    out = []
    for _, _, f in rows[:RELATED_MAX] if tiered else rows:
        fid = str(f.get("file_id") or "")
        symbols = (names.get(fid) if f.get("tier") == "stub" else _symbol_ranges(items, fid)) or (
            ", ".join(str(s) for s in (f.get("symbols") or [])[:RELATED_SYMBOLS])
        )
        out.append(_related_line(f, symbols))
    return out


def _overflow_lines(
    spans: list[dict[str, Any]], shown: set[str], listed: list[str], files: list[dict[str, Any]]
) -> list[str]:
    """One related-file line per file whose spans did not fit the budget."""
    by_id = {str(f.get("file_id") or ""): f for f in files}
    out: list[str] = []
    seen = set(shown)
    for span in spans:
        fid = str(span.get("file_id") or "")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        path = span.get("path") or path_of(fid)
        if any(line.startswith(f"- `{path}`") for line in listed):
            continue
        start = int(span.get("start") or 0)
        rng = _line_range(start, int(span.get("end") or start))
        out.append(_related_line(by_id.get(fid) or {"file_id": fid, "path": path},
                                 f"{span.get('name') or ''}{rng}".strip()))  # fmt: skip
    return out


def _unconfirmed_line(coverage: dict[str, Any]) -> str | None:
    """The warning that no file of the answer is a clear edit target (``coverage``'s verdict)."""
    if not coverage.get("target_unconfirmed"):
        return None
    best = float(coverage.get("target_relevance") or 0.0)
    return (
        f"Unconfirmed: the context selector rates no file here as a likely edit target (best "
        f"p {best:.2f}). Confirm the location with a search before you edit."
    )


def one_line_signature(name: Any, signature: Any, max_chars: int = _SIGNATURE_MAX_CHARS) -> str:
    """``name`` + ``signature`` on one line (a wrapped parameter list's whitespace collapsed),
    cut to ``max_chars``, in backticks unless it holds one."""
    sig = re.sub(r"\s+", " ", f"{name or ''}{signature or ''}").strip()
    sig = re.sub(r"([(\[]) ", r"\1", re.sub(r",? ([)\]])", r"\1", sig))
    if len(sig) > max_chars:
        sig = sig[: max_chars - 1] + "…"
    return f"`{sig}`" if "`" not in sig else sig


def _other_lines(
    items: list[dict[str, Any]], primary: list[str], spans: list[dict[str, Any]]
) -> list[str]:
    shown = {str(s.get("symbol_id")) for s in spans}
    skeletons = [
        (s.get("file_id"), int(s.get("start") or 0), int(s.get("end") or 0))
        for s in spans
        if s.get("shape") == "skeleton"
    ]
    out: list[str] = []
    for it in items:
        fid = it.get("file_id")
        line = int(it.get("line") or 0)
        if fid not in primary or str(it.get("symbol_id")) in shown:
            continue
        if it.get("detail_level") == "reference" or it.get("tier") == "stub":
            continue
        if any(fid == f and a <= line <= b for f, a, b in skeletons):
            continue  # its member line and range are in the skeleton
        sig = one_line_signature(it.get("name"), it.get("signature"))
        end = it.get("end_line")
        rng = f" (L{line}-{int(end)})" if end and int(end) > line else ""
        out.append(f"- `{path_of(str(fid))}:{line}` {it.get('kind') or 'symbol'} {sig}{rng}")
        if len(out) >= OTHER_MAX:
            break
    return out


def render_compact(
    payload: dict[str, Any],
    *,
    max_tokens: int | None = None,
    mode: str | None = None,
    view: str = "compact",
) -> str:
    """The compact Markdown answer for ``payload`` within ``max_tokens`` (module docstring).

    ``max_tokens`` defaults to the budget the answer was assembled with. The workflow section
    states ``mode``, else the mode of ``payload.workflow`` (set by the MCP dispatcher); without
    either there is none. A payload without ``evidence`` / ``files`` (an older producer) is
    rendered from its items, unnumbered. ``view="locate"`` lists each span as one line - its
    range, kind, name and role - instead of its code.
    """
    mode = mode or ((payload.get("workflow") or {}).get("mode"))
    context = payload.get("context") or {}
    items = list(context.get("items") or [])
    coverage = payload.get("coverage") or {}
    tokens = int(max_tokens or context.get("budget") or DEFAULT_MAX_TOKENS)
    spans = payload.get("evidence")
    if spans is None:
        spans = build_evidence(items, [], task_text="")
    spans = list(spans)
    files = payload.get("files")
    if files is None:
        files = files_summary(items)
    primary = (
        list(dict.fromkeys(str(s.get("file_id")) for s in spans))
        or [str(f.get("file_id")) for f in files if f.get("tier") == "full"][
            :PRIMARY_FILES_UNTIERED
        ]
    )

    reserve = len(_header(payload, spans, "9" * _TOKEN_DIGITS)) + 1
    budget = _Budget(tokens * CHARS_PER_TOKEN - reserve)
    flow = workflow_lines(mode, view=view) if mode else []
    if flow and not budget.take(flow):
        flow = []
    unconfirmed = _unconfirmed_line(coverage)
    gap_lines = _gap_lines(coverage)
    if unconfirmed and not spans:
        gap_lines = [f"- {unconfirmed}", *gap_lines][:GAPS_MAX]
    gaps = _take_section(budget, "gaps", gap_lines)
    blocks: list[str] = []
    related: list[str] = []
    rendered: list[dict[str, Any]] = []
    if view == "locate":
        blocks = _take_section(budget, "spans", [_locate_line(s) for s in spans])
        rendered = spans if blocks else []
        related = _take_section(budget, "related", _related_lines(files, primary, items))
    else:
        # The file list goes before the code: it is a line per file, and the code of a long
        # first span would otherwise crowd out the other files the change touches.
        related = _take_section(budget, "related", _related_lines(files, primary, items))
        head = [_HEADINGS["spans"], *([unconfirmed] if unconfirmed else [])]
        if spans and budget.take(head):
            first = _fit_span(budget, spans[0], required=True)
            if first:
                blocks, rendered = [*head, *first], [spans[0]]
            else:
                budget.left += sum(len(line) + 1 for line in head)
    tests = _take_section(budget, "tests", _test_lines(list(payload.get("tests") or [])))
    if blocks and view != "locate":
        for span in spans[1:]:
            lines = _fit_span(budget, span)
            if lines:
                blocks += lines
                rendered.append(span)
    shown = [str(s.get("file_id")) for s in rendered]
    overflow = _overflow_lines(spans, set(shown), related, files)
    if overflow and not related:
        related = _take_section(budget, "related", overflow)
    else:
        related += [line for line in overflow if budget.take([line])]
    other = _take_section(budget, "other", _other_lines(items, shown, rendered))

    body = "\n".join([*blocks, *other, *related, *tests, *gaps, *flow])
    count = 0
    for _ in range(3):
        head = _header(payload, spans, count)
        text = f"{head}\n{body}" if body else head
        if estimate_tokens(text) == count:
            break
        count = estimate_tokens(text)
    return text + "\n"
