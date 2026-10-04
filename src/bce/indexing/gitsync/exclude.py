"""File exclusion for indexing: built-in vendored/minified patterns, ``.bceignore`` and config.

Minified bundles and vendored builds parse into thousands of meaningless symbols (``e``, ``on``,
``update``) whose snippets are one enormous line, so they crowd real code out of a context pack and
blow its token budget. Three sources decide what is skipped, evaluated as one ordered gitignore
pattern list in which the last matching pattern wins:

1. :data:`DEFAULT_EXCLUDES` - ``*.min.js`` and friends;
2. ``.bceignore`` at the repository root (gitignore syntax, committed with the code);
3. extra patterns from configuration (``BCE_INDEX_EXCLUDE``) and then the command line
   (``--exclude``).

A path no pattern matches is still skipped when its content looks minified
(:func:`looks_minified`: the mean line length is above a threshold). A path re-included by a
negated pattern (``!vendor/keep.min.js``) bypasses that check, so the heuristic always has an
override. Every decision depends only on the path, the file bytes and the configured patterns, so
the same commit with the same configuration always yields the same file set.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Name of the repo-root ignore file.
IGNORE_FILE = ".bceignore"

#: Built-in patterns, evaluated before ``.bceignore`` so a negation there can re-include them.
DEFAULT_EXCLUDES: tuple[str, ...] = ("*.min.js", "*.min.mjs", "*.min.cjs", "*.bundle.js")

#: Default minification threshold: mean bytes per line above which a file is skipped. Hand-written
#: code averages 30-60; minified bundles run into the thousands.
DEFAULT_MINIFIED_LINE_LENGTH = 300

#: Files smaller than this are never treated as minified (a short one-liner is not a bundle).
_MINIFIED_MIN_BYTES = 1024


class Match(enum.Enum):
    """Outcome of the pattern list for one path."""

    NONE = "none"  # no pattern matched: the content heuristic decides
    EXCLUDED = "excluded"
    INCLUDED = "included"  # re-included by a negated pattern: never skipped


@dataclass(frozen=True, slots=True)
class _Pattern:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


def _segment_regex(segment: str) -> str:
    """Translate one path segment of a gitignore glob (``*``, ``?``, ``[...]``, ``\\x``)."""
    out: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(segment[i]))
        elif c == "[":
            j = i + 1
            if j < n and segment[j] in "!^":
                j += 1
            if j < n and segment[j] == "]":
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:  # unterminated class: a literal bracket
                out.append(re.escape(c))
            else:
                body = segment[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _compile(line: str) -> _Pattern | None:
    """Compile one gitignore line; ``None`` for blanks and comments."""
    line = line.rstrip("\r\n")
    if not line.strip() or line.startswith("#"):
        return None
    # Trailing whitespace is insignificant unless escaped.
    stripped = line.rstrip(" \t")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += line[len(stripped)]
    line = stripped
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    # A slash at the start or in the middle anchors the pattern to the repository root.
    anchored = "/" in line
    line = line.lstrip("/")
    segments = line.split("/")
    parts: list[str] = []
    for idx, seg in enumerate(segments):
        last = idx == len(segments) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:.*/)?")
        else:
            parts.append(_segment_regex(seg) + ("" if last else "/"))
    body = "".join(parts)
    if not anchored:
        body = "(?:.*/)?" + body
    return _Pattern(regex=re.compile(f"^{body}$"), negated=negated, dir_only=dir_only)


def looks_minified(source: bytes, max_avg_line_length: int = DEFAULT_MINIFIED_LINE_LENGTH) -> bool:
    """True when ``source`` reads as minified/generated: mean line length above the threshold.

    ``max_avg_line_length <= 0`` disables the check. Pure function of the bytes (deterministic).
    """
    if max_avg_line_length <= 0 or len(source) < _MINIFIED_MIN_BYTES:
        return False
    lines = source.count(b"\n") + (0 if source.endswith(b"\n") else 1)
    return len(source) / max(lines, 1) > max_avg_line_length


@dataclass(frozen=True, slots=True)
class ExcludeRules:
    """Ordered gitignore patterns plus the minification threshold for one repository."""

    patterns: tuple[str, ...] = DEFAULT_EXCLUDES
    max_avg_line_length: int = DEFAULT_MINIFIED_LINE_LENGTH
    _compiled: tuple[_Pattern, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        compiled = tuple(p for p in (_compile(line) for line in self.patterns) if p is not None)
        object.__setattr__(self, "_compiled", compiled)

    def _last_match(self, path: str, is_dir: bool) -> _Pattern | None:
        hit: _Pattern | None = None
        for pattern in self._compiled:
            if pattern.dir_only and not is_dir:
                continue
            if pattern.regex.match(path):
                hit = pattern
        return hit

    def match(self, rel_path: str) -> Match:
        """Evaluate the patterns for a repo-relative POSIX file path (gitignore semantics).

        As in git, a file inside an excluded directory cannot be re-included by a negation; only
        the directory itself can be (``!vendor/``).
        """
        parts = rel_path.split("/")
        for depth in range(1, len(parts)):
            hit = self._last_match("/".join(parts[:depth]), is_dir=True)
            if hit is not None and not hit.negated:
                return Match.EXCLUDED
        hit = self._last_match(rel_path, is_dir=False)
        if hit is None:
            return Match.NONE
        return Match.INCLUDED if hit.negated else Match.EXCLUDED

    def path_excluded(self, rel_path: str) -> bool:
        return self.match(rel_path) is Match.EXCLUDED

    def excluded(self, rel_path: str, source: bytes) -> bool:
        """Full decision for a file: the patterns first, then the minification heuristic."""
        decision = self.match(rel_path)
        if decision is Match.NONE:
            return looks_minified(source, self.max_avg_line_length)
        return decision is Match.EXCLUDED


def parse_patterns(raw: str) -> tuple[str, ...]:
    """Split a comma- or newline-separated pattern list (``BCE_INDEX_EXCLUDE``)."""
    return tuple(p.strip() for p in re.split(r"[,\n]", raw) if p.strip())


def ignore_file_patterns(text: str | None) -> tuple[str, ...]:
    """The lines of a ``.bceignore`` file (``None`` = no file)."""
    return tuple(text.splitlines()) if text else ()


def read_ignore_file(root: str | Path) -> str | None:
    """Contents of ``<root>/.bceignore``, or ``None`` when it does not exist."""
    path = Path(root) / IGNORE_FILE
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


def build_rules(
    ignore_text: str | None,
    extra: Iterable[str] = (),
    max_avg_line_length: int = DEFAULT_MINIFIED_LINE_LENGTH,
) -> ExcludeRules:
    """Assemble the rules in precedence order: defaults, ``.bceignore``, then ``extra``."""
    patterns: Sequence[str] = (*DEFAULT_EXCLUDES, *ignore_file_patterns(ignore_text), *extra)
    return ExcludeRules(patterns=tuple(patterns), max_avg_line_length=max_avg_line_length)


def load_rules(
    root: str | Path,
    extra: Iterable[str] = (),
    max_avg_line_length: int = DEFAULT_MINIFIED_LINE_LENGTH,
) -> ExcludeRules:
    """Rules for the working tree at ``root`` (reads its ``.bceignore``)."""
    return build_rules(read_ignore_file(root), extra, max_avg_line_length)
