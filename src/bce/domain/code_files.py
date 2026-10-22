"""Source-code file extensions and the programming language each one is written in.

Used where the engine has to know what a repository's *code* is, independently of which parsers
are installed: the index census (``bce doctor``: how much of the code the index covers) and the
``coverage.gaps`` check of the paths a task names. Documentation, configuration and data files
(``.md``, ``.json``, ``.yml``, ``.lock``) are not code the graph is meant to hold.
"""

from __future__ import annotations

CODE_LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".vue": "vue", ".svelte": "svelte",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".cs": "csharp", ".vb": "visualbasic", ".fs": "fsharp",
    ".go": "go", ".rs": "rust",
    ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".hh": "cpp", ".hxx": "cpp",
    ".m": "objc", ".mm": "objc", ".swift": "swift",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".pm": "perl", ".lua": "lua", ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell",
    ".clj": "clojure", ".cljs": "clojure", ".elm": "elm", ".ml": "ocaml", ".jl": "julia",
    ".r": "r", ".zig": "zig", ".nim": "nim", ".sol": "solidity",
}  # fmt: skip

CODE_EXTENSIONS = frozenset(CODE_LANGUAGES)


def extension_of(path: str) -> str:
    """Lower-cased extension of the file name (``.py``), or ``""``."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""
