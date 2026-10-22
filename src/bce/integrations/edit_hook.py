"""Claude Code ``PostToolUse`` hook: flag files edited in the session as newer than the index.

The engine answers from the index, so once the agent edits a file the line numbers and code that
BCE answers show for it are those of the previous text. After the first edit of each file in a
session the hook adds one line of context saying so; later edits of the same file add nothing.

Edits are recorded in ``<project>/.bce/session_edits`` (``session_id<TAB>path`` lines). The hook
needs no database and imports nothing heavy, and it fails open: a malformed payload or an
unwritable directory only means no warning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EDITS_FILE = Path(".bce") / "session_edits"
#: Tools whose ``tool_input.file_path`` (or ``notebook_path``) was written.
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
MATCHER = "|".join(EDIT_TOOLS)
HOOK_MARKER = "bce edit-hook"


def _relative(path: str, project: Path) -> str:
    try:
        return Path(path).resolve().relative_to(project.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def edit_hook_response(stdin_json: dict[str, Any]) -> dict[str, Any] | None:
    """The hook response for one tool call, or ``None`` (not an edit, or not the first one)."""
    if stdin_json.get("tool_name") not in EDIT_TOOLS:
        return None
    tool_input = stdin_json.get("tool_input") or {}
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not path:
        return None
    project = Path(str(stdin_json.get("cwd") or Path.cwd()))
    session = str(stdin_json.get("session_id") or "")
    rel = _relative(path, project)
    record = project / EDITS_FILE
    try:
        seen = record.read_text(encoding="utf-8").splitlines() if record.is_file() else []
        line = f"{session}\t{rel}"
        if line in seen:
            return None
        record.parent.mkdir(parents=True, exist_ok=True)
        with record.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"bce edit-hook: skipped ({exc})", file=sys.stderr)
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"BCE: `{rel}` now differs from the index. Line numbers and code that BCE answers "
                "show for this file are from before your edit; read the file for its current "
                "text. Once the change is committed, `bce reindex` brings the index up to date."
            ),
        }
    }


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(f"bce edit-hook: stdin is not JSON ({exc})", file=sys.stderr)
        return
    response = edit_hook_response(payload) if isinstance(payload, dict) else None
    if response is not None:
        print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
