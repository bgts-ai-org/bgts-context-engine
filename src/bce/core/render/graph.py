"""The Markdown rendering of an ``inspect_symbol_graph`` answer.

Layout per mode (a section without content is left out)::

    # BCE graph · <mode> of `<name>` (`<path>:<line>`) · <count> · ~<N> tokens
    ### <path>:<start>-<end> · <kind> <name> · <state>        (definition, callers, references)
    ```<lang>
    <line>→<code>
    ```
    - `<path>:<line>-<end>` <kind> `<name><signature>`        (callees, further sites)
    ## Changed symbols / ## Dependents / ## Tests to run      (impact)

An ambiguous name lists its candidates as queries that name one of them (``path:line``); an
unknown one says so. The token budget (about four characters per token) applies to the whole
text, as in :mod:`bce.core.render.compact`: entries are admitted in order and the rest are
counted in a ``⋯ N more`` line. Pure.
"""

from __future__ import annotations

from typing import Any

from bce.core.render.compact import (
    CHARS_PER_TOKEN,
    _Budget,
    _fit_span,
    estimate_tokens,
    one_line_signature,
)
from bce.core.render.evidence import lang_of

#: Budget of an answer whose call names none.
DEFAULT_MAX_TOKENS = 2000
_SIGNATURE_MAX_CHARS = 100
_TOKEN_DIGITS = 6


def _code(text: Any) -> str:
    text = str(text or "")
    return f"`{text}`" if "`" not in text else text


def _signature(row: dict[str, Any]) -> str:
    return one_line_signature(row.get("name"), row.get("signature"), _SIGNATURE_MAX_CHARS)


def _where(row: dict[str, Any]) -> str:
    line = int(row.get("line") or 0)
    end = int(row.get("end_line") or 0)
    rng = f"{line}-{end}" if end > line else (str(line) if line else "")
    return f"`{row.get('path')}:{rng}`" if rng else f"`{row.get('path')}`"


def _entry(row: dict[str, Any]) -> str:
    return f"- {_where(row)} {row.get('kind') or 'symbol'} {_signature(row)}"


def _site_span(site: dict[str, Any]) -> dict[str, Any] | None:
    lines = site.get("lines") or []
    if not lines:
        return None
    start = int(site.get("line") or lines[0][0])
    return {
        "path": site.get("path"),
        "kind": site.get("kind"),
        "name": site.get("name"),
        "lang": lang_of(str(site.get("path") or "")),
        "start": start,
        "end": int(site.get("end_line") or start),
        "role": "test" if site.get("test") else "",
        "shape": "windows",
        "complete": False,
        "lines": lines,
        "focus": [n for n, _ in lines],
    }


def _test_entry(entry: dict[str, Any]) -> str:
    symbols = []
    for sym in entry.get("symbols") or []:
        line, end = int(sym.get("line") or 0), int(sym.get("end_line") or 0)
        rng = f" (L{line}-{end})" if end > line > 0 else (f" (L{line})" if line else "")
        symbols.append(f"`{sym.get('qualified') or sym.get('name')}`{rng}")
    text = f"- `{entry.get('path')}`" + (f" — {', '.join(symbols)}" if symbols else "")
    return text + (f" · {entry['why']}" if entry.get("why") else "")


def _take_list(budget: _Budget, heading: str | None, lines: list[str], more: int = 0) -> list[str]:
    """``heading`` and as many ``lines`` as fit, the rest counted in a closing line."""
    if not lines:
        return []
    out: list[str] = []
    if heading is not None:
        if not budget.take([heading]):
            return []
        out.append(heading)
    for i, line in enumerate(lines):
        if not budget.take([line]):
            more += len(lines) - i
            break
        out.append(line)
    if more:
        tail = f"⋯ {more} more"
        budget.left -= len(tail) + 1
        out.append(tail)
    return out


def _target_label(payload: dict[str, Any]) -> str:
    target = payload.get("target") or {}
    name = _code(target.get("name") or payload.get("query"))
    return f"{name} ({_where(target)})" if target.get("path") else name


def _body(payload: dict[str, Any], budget: _Budget) -> tuple[str, list[str]]:
    """``(title, lines)`` of the answer below the header."""
    mode = payload.get("mode") or "definition"
    status = payload.get("status")
    if status == "not_found" and mode == "impact":
        return "impact: no indexed symbol in the given lines", [
            "- The ranges match no symbol of an indexed file. Give the pre-change line numbers "
            "(`path:L10-L20`, or the diff's `@@ -a,b` side), or a symbol name."
        ]
    if status == "not_found":
        return f"no indexed symbol matches {_code(payload.get('query'))}", [
            "- Check the spelling, or name it as `Type.member` or `path:line`. Code the index does "
            "not cover (unparsed languages, configs) is not in the graph; search for it as usual."
        ]
    if status == "ambiguous":
        rows = payload.get("candidates") or []
        lines = [f"{_entry(r)} → query `{r.get('path')}:{r.get('line')}`" for r in rows]
        title = f"{_code(payload.get('query'))} names {len(rows) + int(payload.get('candidates_more') or 0)} symbols - ask again with one"
        return title, _take_list(budget, None, lines, int(payload.get("candidates_more") or 0))

    if mode == "definition":
        span = payload.get("definition") or {}
        return f"definition of {_target_label(payload)}", _fit_span(budget, span, required=True)

    if mode == "callees":
        rows = payload.get("callees") or []
        more = int(payload.get("more") or 0)
        title = f"callees of {_target_label(payload)} · {len(rows) + more}"
        lines = [_entry(r) for r in rows] or ["- It calls no indexed symbol."]
        return title, _take_list(budget, None, lines, more)

    if mode in ("callers", "references"):
        sites = payload.get("sites") or []
        more = int(payload.get("more") or 0)
        noun = "callers" if mode == "callers" else "references"
        title = f"{noun} of {_target_label(payload)} · {len(sites) + more}"
        if not sites:
            return title, [
                f"- No indexed symbol {'calls' if mode == 'callers' else 'references'} it."
            ]
        out: list[str] = []
        listed: list[str] = []
        for site in sites:
            span = _site_span(site)
            lines = _fit_span(budget, span) if span else []
            if lines:
                out += lines
            else:
                listed.append(_entry(site))
        out += _take_list(budget, None, listed, more)
        return title, out

    impact = payload.get("impact") or {}
    changed = impact.get("changed") or []
    affected = impact.get("affected") or []
    more = int(impact.get("affected_more") or 0)
    title = (
        f"impact of the change · {len(changed)} changed · {len(affected) + more} dependents "
        f"in {impact.get('affected_files') or 0} files"
    )
    out = _take_list(budget, "## Changed symbols", [_entry(r) for r in changed])
    dependents = [
        f"{_entry(r)} · {r.get('via')} {_code(r.get('of'))}"
        + (" (indirect)" if r.get("depth", 1) > 1 else "")
        for r in affected
    ]
    out += _take_list(
        budget,
        "## Dependents",
        dependents or ["- Nothing indexed calls, references or extends them."],
        more,
    )
    tests = [_test_entry(t) for t in impact.get("tests") or []]
    out += _take_list(
        budget,
        "## Tests to run",
        tests or ["- No indexed test reaches the changed symbols."],
        int(impact.get("tests_more") or 0),
    )
    return title, out


def render_inspect(payload: dict[str, Any], *, max_tokens: int | None = None) -> str:
    """The Markdown answer for an ``inspect_symbol_graph`` ``payload`` within ``max_tokens``."""
    tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
    reserve = 160 + _TOKEN_DIGITS
    budget = _Budget(tokens * CHARS_PER_TOKEN - reserve)
    title, lines = _body(payload, budget)
    body = "\n".join(lines)
    count = 0
    text = ""
    for _ in range(3):
        head = f"# BCE graph · {title} · ~{count} tokens"
        text = f"{head}\n{body}" if body else head
        if estimate_tokens(text) == count:
            break
        count = estimate_tokens(text)
    return text + "\n"
