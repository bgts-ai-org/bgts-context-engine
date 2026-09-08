"""Enumerations for the code graph schema (data model, spec section 4).

These cover the base schema plus the additions from the proposed-features document:
- ``NodeLabel.ROUTE`` / ``EdgeLabel.ROUTES_TO`` (feature 1: framework-aware routes)
- ``NodeLabel.DESIGN_NOTE`` / ``EdgeLabel.EXPLAINS`` (feature 5: "why" inference)
- ``Provenance`` (feature 3: provenance labeling, carried by every edge)
"""

from __future__ import annotations

from enum import StrEnum


class NodeLabel(StrEnum):
    REPO = "Repo"
    FILE = "File"
    SYMBOL = "Symbol"
    MODULE = "Module"
    # Feature 1 (framework-aware routes) and feature 5 ("why" inference).
    ROUTE = "Route"
    DESIGN_NOTE = "DesignNote"


class EdgeLabel(StrEnum):
    DEFINED_IN = "DEFINED_IN"        # Symbol -> File
    BELONGS_TO = "BELONGS_TO"        # File -> Repo
    IMPORTS = "IMPORTS"              # File/Module -> File/Module
    CALLS = "CALLS"                  # Symbol -> Symbol (may be cross-repo)
    INHERITS = "INHERITS"            # Symbol -> Symbol
    IMPLEMENTS = "IMPLEMENTS"        # Symbol -> Symbol
    REFERENCES = "REFERENCES"        # Symbol -> Symbol (carries ref_kind)
    # Feature 1: Route -> Symbol (handler). Feature 5: DesignNote -> Symbol.
    ROUTES_TO = "ROUTES_TO"
    EXPLAINS = "EXPLAINS"


class SymbolKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    VARIABLE = "variable"
    CONSTANT = "constant"
    ENUM = "enum"
    TYPE = "type"
    FIELD = "field"
    PROPERTY = "property"
    CONSTRUCTOR = "constructor"


class RefKind(StrEnum):
    """Reference kind on REFERENCES edges - critical for scoring (spec section 6.4).

    ``define``/``write`` outweigh ``read``/``pass``; this is the basis of the
    1000-references-to-one-symbol problem solution.
    """

    DEFINE = "define"
    WRITE = "write"
    READ = "read"
    PASS = "pass"


class Provenance(StrEnum):
    """How an edge entered the graph (feature 3).

    Exact static resolution (``scip``) and AST extraction (``treesitter``) are trusted more than
    synthesized cross-language/route bridges (``heuristic``) in scoring (spec section 6.4).
    """

    SCIP = "scip"
    TREESITTER = "treesitter"
    HEURISTIC = "heuristic"


class DesignNoteKind(StrEnum):
    """Kind of design-rationale note extracted from inline comments (feature 5)."""

    NOTE = "note"
    WHY = "why"
    HACK = "hack"
    TODO = "todo"
    FIXME = "fixme"


class HttpMethod(StrEnum):
    """HTTP method for Route nodes (feature 1)."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "ANY"
