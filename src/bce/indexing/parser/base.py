"""The ``LanguageProvider`` contract (abstraction-first multi-language design).

Every supported language implements this interface. The pipeline only ever talks to the abstract
contract, so new languages (Java, C#, Go, Swift, Kotlin, ObjC...) plug in without touching the
extractor, scorer, or tools.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from bce.domain.models import GraphFragment


@dataclass(slots=True)
class ParseContext:
    """Everything a provider needs to attach extracted nodes/edges to the right file/repo."""

    repo_id: str
    file_id: str
    path: str
    language: str
    source: bytes
    indexed_at_commit: str = "WORKDIR"
    # Logical package/module path (e.g. dotted module for Python). Used in symbol_id and Module.
    package: str | None = None


class LanguageProvider(abc.ABC):
    """Parse a source file and extract graph nodes/edges deterministically."""

    #: Human-readable language identifier, used in ``symbol_id`` (e.g. "python", "javascript").
    language: str = "abstract"

    #: Line-comment markers for this language (used by the DesignNote extractor, feature 5).
    line_comment_markers: tuple[str, ...] = ("#",)

    @abc.abstractmethod
    def extensions(self) -> tuple[str, ...]:
        """File extensions (lowercase, with leading dot) handled by this provider."""

    @abc.abstractmethod
    def parse(self, source: bytes) -> Any:
        """Parse raw bytes into a language-specific AST (tree-sitter Tree)."""

    @abc.abstractmethod
    def extract(self, tree: Any, ctx: ParseContext) -> GraphFragment:
        """Extract Symbol/File nodes and CALLS/IMPORTS/INHERITS/REFERENCES edges from the AST.

        Implementations MUST be deterministic: the same source + context always yields the same
        fragment (modulo the stable de-dup/sort applied by :meth:`GraphFragment.deduped`).
        """

    def extract_routes(
        self, tree: Any, ctx: ParseContext, symbol_lines: dict[int, str]
    ) -> GraphFragment:
        """Feature 1: extract Route nodes + ROUTES_TO edges (framework-aware).

        ``symbol_lines`` maps a 1-based definition line to its ``symbol_id`` (from :meth:`extract`),
        so a route decorator/registration can bind to its handler symbol. Default is a no-op;
        language providers that know web frameworks override this. Must be deterministic.
        """
        return GraphFragment()

    def derive_package(self, ctx: ParseContext) -> str | None:
        """Best-effort logical package/namespace for a file. Override per language.

        Default: derive a dotted path from the repo-relative file path.
        """
        path = ctx.path.replace("\\", "/").lstrip("./")
        for ext in self.extensions():
            if path.endswith(ext):
                path = path[: -len(ext)]
                break
        return path.strip("/").replace("/", ".") or None
