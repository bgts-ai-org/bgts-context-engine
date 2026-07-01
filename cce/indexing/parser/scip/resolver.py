"""SCIP resolver abstraction + a CLI-backed implementation.

A :class:`ScipResolver` answers one deterministic question about a repository:

    "Is there an *exact* (SCIP-confirmed) relationship between these two symbols?"

The extractor uses that to elevate an edge's provenance from ``treesitter`` to ``scip`` (feature 3).
Because SCIP indexing is repo-wide (cross-file), a resolver is built once per repo root and then
queried per edge.

Two implementations ship:

- :class:`NullScipResolver` - always unavailable; confirms nothing. This is the default and keeps
  behaviour identical to a pure tree-sitter build.
- :class:`ScipCliResolver` - runs ``scip-python`` / ``scip-typescript`` if present on PATH, reads the
  emitted ``index.scip``, and confirms relationships found there. If the binary or the ``scip``
  reader library is missing, or indexing fails, it degrades to unavailable (no exceptions leak).

Determinism: SCIP output is a deterministic function of the source at a commit; the confirmation set
is order-independent (a frozenset lookup), so provenance elevation never changes result ordering
beyond the (already deterministic) provenance weight in scoring.
"""

from __future__ import annotations

import abc
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ScipResolution:
    """The confirmed-relationship set extracted from a SCIP index for one repo.

    ``edges`` holds ``(from_moniker, to_moniker)`` pairs where ``scip`` observed a reference/relation.
    ``definitions`` holds monikers that have a definition occurrence (exact symbols). Monikers are
    the SCIP symbol strings; the extractor maps its own symbol ids to monikers via a supplied hook.
    """

    edges: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    definitions: frozenset[str] = field(default_factory=frozenset)

    def confirms(self, from_moniker: str, to_moniker: str) -> bool:
        return (from_moniker, to_moniker) in self.edges

    def has_definition(self, moniker: str) -> bool:
        return moniker in self.definitions


class ScipResolver(abc.ABC):
    """Repo-level exact-resolution provider (optional, feature 3)."""

    language: str = "abstract"

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this resolver can produce a SCIP index for the given environment."""

    @abc.abstractmethod
    def resolve(self, repo_root: str | Path) -> ScipResolution | None:
        """Produce the confirmed-relationship set for a repo, or ``None`` if unavailable."""


class NullScipResolver(ScipResolver):
    """Default: SCIP disabled. Confirms nothing; extraction stays pure tree-sitter."""

    language = "null"

    def available(self) -> bool:
        return False

    def resolve(self, repo_root: str | Path) -> ScipResolution | None:
        return None


#: SCIP indexer binaries by ecosystem. Presence on PATH gates availability.
_SCIP_BINARIES = {
    "python": "scip-python",
    "typescript": "scip-typescript",
    "javascript": "scip-typescript",
}


class ScipCliResolver(ScipResolver):
    """Runs an ecosystem's SCIP indexer binary if present and reads ``index.scip``.

    ``language`` selects the binary. Everything is best-effort: any failure (missing binary, missing
    reader library, non-zero exit) yields ``None`` so the pipeline falls back to tree-sitter.
    """

    def __init__(self, language: str, *, binary: str | None = None, timeout_s: int = 600) -> None:
        self.language = language
        self._binary = binary or _SCIP_BINARIES.get(language, "")
        self._timeout_s = timeout_s

    def available(self) -> bool:
        return bool(self._binary) and shutil.which(self._binary) is not None

    def resolve(self, repo_root: str | Path) -> ScipResolution | None:
        if not self.available():
            return None
        root = Path(repo_root).resolve()
        index_path = root / "index.scip"
        try:
            subprocess.run(
                [self._binary, "index", "--output", str(index_path)],
                cwd=str(root),
                capture_output=True,
                check=True,
                timeout=self._timeout_s,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None
        if not index_path.is_file():
            return None
        return _read_scip_index(index_path)


def _read_scip_index(index_path: Path) -> ScipResolution | None:
    """Parse a SCIP index into a :class:`ScipResolution`, or ``None`` if the reader is unavailable.

    Uses the optional ``scip`` protobuf bindings. Definitions and reference occurrences are collected
    per document; a reference occurrence inside a symbol's definition range contributes an edge
    (enclosing_symbol -> referenced_symbol). Kept intentionally tolerant of schema variance.
    """
    try:
        from scip_pb2 import Index, SymbolRole  # type: ignore
    except ModuleNotFoundError:
        try:
            from scip.scip_pb2 import Index, SymbolRole  # type: ignore
        except ModuleNotFoundError:
            return None

    try:
        raw = index_path.read_bytes()
        idx = Index()
        idx.ParseFromString(raw)
    except Exception:
        return None

    definition_role = int(getattr(SymbolRole, "Definition", 1))
    definitions: set[str] = set()
    edges: set[tuple[str, str]] = set()

    for doc in idx.documents:
        ranges = []
        for occ in doc.occurrences:
            is_def = bool(occ.symbol_roles & definition_role)
            if is_def:
                definitions.add(occ.symbol)
                ranges.append((_range_tuple(occ.range), occ.symbol))
        # Second pass: attribute each reference to the enclosing definition range (if any).
        for occ in doc.occurrences:
            if occ.symbol_roles & definition_role:
                continue
            enclosing = _enclosing_symbol(_range_tuple(occ.range), ranges)
            if enclosing is not None and enclosing != occ.symbol:
                edges.add((enclosing, occ.symbol))

    return ScipResolution(edges=frozenset(edges), definitions=frozenset(definitions))


def _range_tuple(rng) -> tuple[int, int, int, int]:
    """Normalize a SCIP range (``[startLine, startChar, endLine, endChar]`` or 3-tuple)."""
    vals = list(rng)
    if len(vals) == 3:
        return (vals[0], vals[1], vals[0], vals[2])
    if len(vals) >= 4:
        return (vals[0], vals[1], vals[2], vals[3])
    return (0, 0, 0, 0)


def _enclosing_symbol(
    ref_range: tuple[int, int, int, int],
    def_ranges: list[tuple[tuple[int, int, int, int], str]],
) -> str | None:
    """Smallest definition range that contains the reference start line (deterministic tie-break)."""
    line = ref_range[0]
    best: tuple[int, str] | None = None
    for (sl, _sc, el, _ec), sym in def_ranges:
        if sl <= line <= el:
            span = el - sl
            if best is None or span < best[0] or (span == best[0] and sym < best[1]):
                best = (span, sym)
    return best[1] if best else None


def build_scip_resolver(language: str) -> ScipResolver:
    """Return a CLI resolver for a language if a binary is known, else the null resolver."""
    if language in _SCIP_BINARIES:
        resolver = ScipCliResolver(language)
        if resolver.available():
            return resolver
    return NullScipResolver()
