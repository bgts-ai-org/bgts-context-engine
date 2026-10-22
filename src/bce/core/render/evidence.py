"""Evidence spans: the code a context answer shows verbatim, cut to what an edit needs.

The assembler ranks and tiers symbols; this module decides which of them the agent sees as code,
and how much of each:

- Only symbols of the *primary* files are shown as code: the selector's ``full`` tier, or without
  a selector the files of the assembler's full-detail items (at most
  :data:`PRIMARY_FILES_UNTIERED`).
- At most :data:`MAX_CODE_SPANS` functions / methods / variables, best first (the selector's
  symbol score, then the engine's rank). A declaration of at most :data:`WHOLE_MAX_LINES` lines
  is shown whole; a longer one as its signature plus +-:data:`WINDOW_RADIUS`-line windows around
  the lines holding what the task names (identifiers, quoted literals, code fragments), marked
  incomplete.
- Classes and interfaces longer than :data:`CONTAINER_WHOLE_MAX_LINES` are shown as a skeleton of
  the header and the member-level lines (fields, method signatures with their line ranges), at
  most :data:`MAX_SKELETONS`; a shorter one whole.
- A file's module symbol is shown as its outline: the top-level lines and each declaration's
  first line, the bodies omitted (windowed like a long declaration when the outline is long).

Every span carries its file range, whether it is complete, a ``content_hash`` of the stored
declaration text and the commit that text was indexed at, so a client can tell whether what it
holds is current. The text is the search side table's (the whole declaration, up to the
extractor's cap); graphs indexed before that table existed fall back to the node's display
snippet, shown without line numbers.

Deterministic: the same items, texts and task text always give the same spans.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from typing import Any

from bce.core.orchestrator.text import content_tokens, mention_terms

#: The indexer's cap on a declaration's stored text (``_treesitter.SEARCH_TEXT_MAX_CHARS``);
#: a text this long without a recorded ``end_line`` is taken to be cut.
SEARCH_TEXT_MAX_CHARS = 24_000
#: Functions / methods / variables shown as code per answer.
MAX_CODE_SPANS = 3
#: Class / interface skeletons per answer.
MAX_SKELETONS = 2
#: Without the selector's tiers, the files of the first full-detail items that count as primary.
PRIMARY_FILES_UNTIERED = 3
#: A declaration up to this many lines is shown whole.
WHOLE_MAX_LINES = 120
#: Lines kept on each side of a line the task names, in a longer declaration.
WINDOW_RADIUS = 25
#: Window radii tried, widest first, until a windowed span fits :data:`WHOLE_MAX_LINES`.
_RADII = (WINDOW_RADIUS, 12, 6, 2)
#: A window edge moves at most this far to land on a blank line (a statement boundary).
_SNAP_LINES = 5
#: The signature is looked for within the first lines of a declaration.
_HEAD_SCAN_LINES = 8
#: Lines kept from the top of a long declaration in which nothing the task names occurs.
_FALLBACK_LINES = 40
#: Windows kept per declaration (in file order).
_MAX_WINDOWS = 4
#: Member lines of a skeleton.
SKELETON_MAX_MEMBERS = 40
#: A class / interface up to this many lines is shown whole: its skeleton would hide nothing.
CONTAINER_WHOLE_MAX_LINES = 30
#: Shortest task word matched when nothing more specific occurs in a long declaration.
_WORD_MIN_LEN = 5
#: Selector symbol score that means "must be modified".
_EDIT_RELEVANCE = 3.0

CONTAINER_KINDS = frozenset({"class", "interface"})
MODULE_KIND = "module"
#: The line a stored module outline has in place of omitted body lines ``a..b``
#: (``bce.indexing.extractor.module.omitted_marker``).
_OMITTED = re.compile(r"^\u22ef (\d+)-(\d+)$")

_LANGS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".cs": "csharp",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".kt": "kotlin", ".scala": "scala", ".rb": "ruby", ".php": "php", ".swift": "swift",
}  # fmt: skip
_HEAD_END = re.compile(r"(:|\{|=>|\)\s*(->\s*[^{]+)?)\s*(#.*|//.*)?$")
_CLOSING = re.compile(r"^[\]\)}]+[;,)]*\s*$|^end\s*$")
_COMMENT = ("#", "//", "/*", "*", "*/")


def path_of(file_id: str) -> str:
    return file_id.split(":", 1)[1] if ":" in file_id else file_id


def lang_of(path: str) -> str:
    dot = path.rfind(".")
    return _LANGS.get(path[dot:].lower(), "") if dot >= 0 else ""


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def primary_file_ids(assembled: list[dict[str, Any]]) -> list[str]:
    """The files whose symbols are shown as code (see module docstring)."""
    out: list[str] = []
    tiered = any(it.get("tier") for it in assembled)
    for it in assembled:
        fid = str(it.get("file_id") or "")
        if not fid or fid in out:
            continue
        if tiered and it.get("tier") == "full":
            out.append(fid)
        elif not tiered and it.get("detail_level") == "full":
            out.append(fid)
            if len(out) >= PRIMARY_FILES_UNTIERED:
                break
    return out


def role_of(item: dict[str, Any]) -> str:
    """``edit`` for what the task most likely changes, ``dependency`` for what it reads."""
    if float(item.get("symbol_relevance") or 0.0) >= _EDIT_RELEVANCE or item.get("pinned"):
        return "edit"
    if item.get("tier") == "stub":
        return "dependency"
    return "edit" if (item.get("graph_distance") or 0) == 0 else "dependency"


def span_candidates(
    assembled: list[dict[str, Any]], ranked: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ranked items to show as code or skeleton, best first, within the per-answer limits."""
    primary = set(primary_file_ids(assembled))
    by_id = {str(it.get("symbol_id")): it for it in ranked if it.get("symbol_id")}
    order: list[tuple[float, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for pos, it in enumerate(assembled):
        sid = str(it.get("symbol_id") or "")
        if not sid or sid in seen or str(it.get("file_id") or "") not in primary:
            continue
        if it.get("detail_level") == "reference" or it.get("tier") == "stub":
            continue
        seen.add(sid)
        full = {**by_id.get(sid, {}), **{k: v for k, v in it.items() if v is not None}}
        order.append((-float(full.get("symbol_relevance") or 0.0), pos, full))
    order.sort(key=lambda t: (t[0], t[1]))
    code: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    for _, _, it in order:
        if str(it.get("kind") or "") in CONTAINER_KINDS:
            if len(containers) < MAX_SKELETONS:
                containers.append(it)
        elif len(code) < MAX_CODE_SPANS:
            code.append(it)
    keep = {id(it) for it in code + containers}
    return [it for _, _, it in order if id(it) in keep]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _head_end(lines: list[str]) -> int:
    """Index of the last signature line (the first line ending a header, within a few lines)."""
    for i, line in enumerate(lines[:_HEAD_SCAN_LINES]):
        if _HEAD_END.search(line.rstrip()):
            return i
    return 0


def _term_patterns(task_text: str) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """Patterns for what the task names (specific), and for its longer plain words (fallback)."""
    specific: list[re.Pattern[str]] = []
    for kind, value in mention_terms(task_text or ""):
        esc = re.escape(value)
        if kind == "literal":
            specific.append(re.compile(rf"""['"`]{esc}['"`]"""))
        elif kind == "code":
            specific.append(re.compile(esc))
        else:
            specific.append(re.compile(rf"\b{esc}\b"))
    words = [w for w in content_tokens(task_text or "") if len(w) >= _WORD_MIN_LEN]
    fallback = [re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in dict.fromkeys(words)]
    return specific, fallback


def _hits(lines: list[str], patterns: Iterable[re.Pattern[str]]) -> list[int]:
    pats = list(patterns)
    if not pats:
        return []
    return [i for i, line in enumerate(lines) if any(p.search(line) for p in pats)]


def _snap(lines: list[str], a: int, b: int) -> tuple[int, int]:
    for k in range(a, max(a - _SNAP_LINES, 0) - 1, -1):
        if not lines[k].strip():
            a = k + 1
            break
    for k in range(b, min(b + _SNAP_LINES, len(lines) - 1) + 1):
        if not lines[k].strip():
            b = k - 1
            break
    return a, max(a, b)


def _windows(lines: list[str], hits: list[int], radius: int) -> list[tuple[int, int]]:
    head = _head_end(lines)
    spans = [(0, head)]
    for h in hits[: _MAX_WINDOWS * 4]:
        spans.append(_snap(lines, max(0, h - radius), min(len(lines) - 1, h + radius)))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 3:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return [merged[0], *merged[1 : _MAX_WINDOWS + 1]]


def window_indices(lines: list[str], task_text: str) -> tuple[list[int], list[int], bool]:
    """``(shown, focus, complete)`` line indices of a declaration for this task.

    ``focus`` are the signature lines and the lines that hold what the task names; a renderer
    short of budget keeps the lines nearest to them.
    """
    specific, fallback = _term_patterns(task_text)
    hits = _hits(lines, specific) or _hits(lines, fallback)
    head = list(range(_head_end(lines) + 1))
    if len(lines) <= WHOLE_MAX_LINES:
        return list(range(len(lines))), sorted(set(head + hits)), True
    if not hits:
        shown = list(range(min(len(lines), max(_FALLBACK_LINES, len(head)))))
        return shown, head, False
    shown = head
    for radius in _RADII:
        spans = _windows(lines, hits, radius)
        shown = sorted({i for a, b in spans for i in range(a, b + 1)})
        if len(shown) <= WHOLE_MAX_LINES:
            break
    visible = set(shown)
    focus = sorted(set(head) | {h for h in hits if h in visible})
    return shown, focus, False


def skeleton_entries(lines: list[str]) -> tuple[list[tuple[int, int | None]], int, int]:
    """Lines of a class / interface skeleton: ``([(index, last_index_or_None)], n_head, omitted)``.

    The ``n_head`` header lines come first (annotations and a wrapped ``extends`` at the header's
    own indentation included); then every line at the members' indentation that is not a comment,
    a docstring, a decorator, a closing bracket or the end of a wrapped header (``) -> None:``),
    with the index of the member's last line when it spans several. ``omitted`` members are past
    :data:`SKELETON_MAX_MEMBERS`.
    """
    if not lines:
        return [], 0, 0
    base = _indent(lines[0])
    head = _head_end(lines)
    while (
        head + 1 < len(lines)
        and lines[head + 1].strip()
        and _indent(lines[head + 1]) <= base
        and not _CLOSING.match(lines[head + 1].strip())
    ):
        head += 1
    out: list[tuple[int, int | None]] = [(i, None) for i in range(head + 1)]
    body: list[int] = []
    in_doc = in_block = False
    for i in range(head + 1, len(lines)):
        stripped = lines[i].strip()
        if in_block:
            in_block = "*/" not in stripped
            continue
        if in_doc:
            in_doc = not (stripped.endswith('"""') or stripped.endswith("'''"))
            continue
        if stripped.startswith(('"""', "'''")):
            quote = stripped[:3]
            in_doc = not (len(stripped) > 3 and stripped.endswith(quote))
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped:
            body.append(i)
    levels = [_indent(lines[i]) for i in body if _indent(lines[i]) > base]
    if not levels:
        return out, head + 1, 0
    level = min(levels)

    def at_level(i: int) -> bool:
        return _indent(lines[i]) == level

    def closes(i: int) -> bool:
        # a closing line, or the rest of a wrapped header (``) -> None:``, ``}) {``)
        text = lines[i].strip()
        return text.startswith((")", "]", "}")) or bool(_CLOSING.match(text))

    def skipped(i: int) -> bool:
        text = lines[i].strip()
        return closes(i) or text.startswith(_COMMENT) or text.startswith("@")

    members = [i for i in body if at_level(i) and not skipped(i)]
    for n, i in enumerate(members[:SKELETON_MAX_MEMBERS]):
        nxt = members[n + 1] if n + 1 < len(members) else len(lines)
        last = i
        for k in range(nxt - 1, i, -1):
            if not lines[k].strip() or _indent(lines[k]) < level:
                continue
            if at_level(k) and skipped(k) and not closes(k):
                continue  # the next member's decorator / comment
            last = k
            break
        out.append((i, last if last > i else None))
    return out, head + 1, max(0, len(members) - SKELETON_MAX_MEMBERS)


def _first_indent(lines: list[str], col: int) -> str:
    """The whitespace before the declaration's first line, when it can be told.

    The stored text starts at the node's column ``col``. What precedes it is indentation when the
    closing line is indented by exactly ``col`` (brace languages), or when nothing closes the
    declaration and every later line is indented deeper (Python). Otherwise it is a keyword the
    node excludes (``export function``, Go's ``type X struct``) and the first line is shown as it
    is stored: padding it would show whitespace the file does not have, and an edit copied from
    it would not match.
    """
    rest = [line for line in lines[1:] if line.strip()]
    if not rest:
        return ""
    last = lines[-1]
    if _CLOSING.match(last.strip()):
        indent = last[: _indent(last)]
        return indent if len(indent) == col else ""
    if all(_indent(line) > col for line in rest):
        return ("\t" if any(line.startswith("\t") for line in rest) else " ") * col
    return ""


def text_lines(text: str, col: int | None) -> list[str]:
    """A stored declaration text as the file's lines (first-line indentation restored when it can
    be told, see :func:`_first_indent`)."""
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if col:
        lines[0] = _first_indent(lines, int(col)) + lines[0]
    return lines


def outline_lines(lines: list[str], start: int = 1) -> list[tuple[int, str]]:
    """``(file line, text)`` of a stored module outline, its omitted-body markers resolved."""
    out: list[tuple[int, str]] = []
    n = start
    for line in lines:
        marker = _OMITTED.match(line)
        if marker:
            n = int(marker.group(2)) + 1
            continue
        out.append((n, line))
        n += 1
    return out


def span_for(item: dict[str, Any], text: str | None, *, task_text: str = "") -> dict[str, Any]:
    """The evidence span of one symbol (``item``: its graph properties) with stored ``text``."""
    return _span(item, text, task_text)


def _span(item: dict[str, Any], text: str | None, task_text: str) -> dict[str, Any]:
    fid = str(item.get("file_id") or "")
    path = path_of(fid)
    kind = str(item.get("kind") or "symbol")
    start = int(item.get("text_line") or item.get("line") or 1)
    end_line = item.get("end_line")
    base = {
        "symbol_id": item.get("symbol_id"),
        "file_id": fid,
        "path": path,
        "name": item.get("name"),
        "kind": kind,
        "role": role_of(item),
        "lang": lang_of(path),
        "start": start,
        "commit": item.get("indexed_at_commit"),
    }
    if not text:
        sig = f"{item.get('name') or ''}{item.get('signature') or ''}".strip()
        snippet = (
            "" if kind in CONTAINER_KINDS else str(item.get("body") or item.get("content") or "")
        )
        return {
            **base,
            "shape": "snippet",
            "end": int(end_line) if end_line else None,
            "complete": False,
            "truncated": False,
            "content_hash": None,
            "lines": None,
            "focus": [],
            "snippet": snippet.strip() or sig,
        }
    lines = text_lines(text.rstrip("\n"), item.get("col"))
    stored_end = start + len(lines) - 1
    end = int(end_line) if end_line else stored_end
    truncated = stored_end < end or (not end_line and len(text) >= SEARCH_TEXT_MAX_CHARS - 1)
    if kind == MODULE_KIND:
        numbered = outline_lines(lines, start)
        shown, focus, _ = window_indices([t for _, t in numbered], task_text)
        return {
            **base,
            "shape": "outline",
            "end": end,
            "complete": False,
            "truncated": False,
            "content_hash": content_hash(text),
            "lines": [list(numbered[i]) for i in shown],
            "focus": [numbered[i][0] for i in focus],
        }
    if kind in CONTAINER_KINDS and len(lines) > CONTAINER_WHOLE_MAX_LINES:
        entries, n_head, omitted = skeleton_entries(lines)
        return {
            **base,
            "shape": "skeleton",
            "end": end,
            "complete": False,
            "truncated": truncated,
            "content_hash": content_hash(text),
            "lines": [[start + i, lines[i]] for i, _ in entries],
            "ranges": [[start + i, start + last] for i, last in entries if last is not None],
            "focus": [start + i for i, _ in entries[:n_head]],
            "more_members": omitted,
        }
    shown, focus, complete = window_indices(lines, task_text)
    return {
        **base,
        "shape": "whole" if complete else "windows",
        "end": end,
        "complete": complete and not truncated,
        "truncated": truncated,
        "content_hash": content_hash(text),
        "lines": [[start + i, lines[i]] for i in shown],
        "focus": [start + i for i in focus],
    }


def build_evidence(
    assembled: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    *,
    task_text: str,
    bodies: Callable[[list[str]], dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """The answer's evidence spans, best first (see module docstring).

    ``assembled`` are the assembler's items, ``ranked`` the enriched pre-assembly items (they
    carry ``end_line`` / ``col`` / the selector's scores); ``bodies`` maps symbol ids to their
    stored declaration text (:meth:`GraphRepository.symbol_bodies`).
    """
    picks = span_candidates(assembled, ranked)
    if not picks:
        return []
    texts: Any = {}
    if callable(bodies):
        try:
            texts = bodies([str(it.get("symbol_id")) for it in picks])
        except Exception:  # noqa: BLE001 - evidence degrades to snippets, never fails the answer
            texts = {}
    if not isinstance(texts, dict):
        texts = {}
    out = []
    for it in picks:
        text = texts.get(str(it.get("symbol_id")))
        out.append(_span(it, text if isinstance(text, str) else None, task_text))
    return out
