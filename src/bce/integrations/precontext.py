"""Render a ``get_context_for_task`` payload as the compact prose block an agent prompt carries.

The block is re-sent on every model turn, so it is short by design: a file list in rank order,
then one line per symbol (``path:line kind name distance``) with a snippet of at most
``snippet_lines`` lines, and only where the snippet says more than the name. This is the exact
format the agent benchmark injected; at the default budget it is about 5k characters
(~1.3k tokens).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from bce.core.agent_mode import normalize_agent_mode, workflow_text
from bce.core.defaults import DEFAULT_MAX_TOKENS, DEFAULT_SNIPPET_LINES

HEADING = "## Code context for this task (bgts-context-engine code graph, computed once)"


def render_context_markdown(
    payload: dict[str, Any],
    *,
    snippet_lines: int = DEFAULT_SNIPPET_LINES,
    max_distance: int | None = None,
    max_chars: int | None = None,
    mode: str | None = None,
) -> str:
    """Markdown for the agent from a ``get_context_for_task`` payload.

    ``max_distance`` drops items farther than that from the anchors; ``max_chars`` truncates at an
    item boundary (Claude Code caps hook context at 10 000 characters). With ``mode`` the block
    ends its header with that agent mode's workflow, so truncation never cuts it.
    """
    cov = payload.get("coverage") or {}
    items = list((payload.get("context") or {}).get("items") or [])
    if max_distance is not None:
        items = [it for it in items if (it.get("graph_distance") or 0) <= max_distance]

    def rel(it: dict[str, Any]) -> str:
        path = str(it.get("file_id") or "")
        repo = str(it.get("repo_id") or "")
        prefix = f"{repo}:"
        return path[len(prefix) :] if repo and path.startswith(prefix) else path

    files: dict[str, int] = {}
    for it in items:
        p = rel(it)
        if p:
            files[p] = files.get(p, 0) + 1

    conf = cov.get("confidence") or "unknown"
    head = [
        HEADING,
        "",
        f"Confidence: {conf}. Files below are verified locations of the symbols the task text refers "
        "to (distance 0 = direct match, 1+ = reached through calls/references/imports). Read them "
        "first instead of searching for them; treat them as candidates to inspect, not as a list of "
        "files to change.",
        "",
        "Files: " + ", ".join(f"`{p}`" + (f" ({n})" if n > 1 else "") for p, n in files.items()),
        "",
    ]
    if any(it.get("tier") == "stub" for it in items):
        head += [
            "Entries marked `stub` name a probably related file without its code; open one only if "
            "the other entries do not cover the task.",
            "",
        ]
    unresolved = [u for u in cov.get("unresolved_identifiers") or [] if u.get("name")]
    if unresolved:
        head += [
            "Not covered: "
            + "; ".join(
                f"`{u['name']}` (defined in "
                + ", ".join(f"`{_strip_repo(f)}`" for f in u.get("defined_in") or [])
                + ")"
                for u in unresolved
            ),
            "",
        ]
    ranked = [str(c.get("path")) for c in payload.get("candidates") or [] if c.get("path")]
    if ranked:
        head += ["Also ranked: " + ", ".join(f"`{p}`" for p in ranked), ""]
    if cov.get("likely_incomplete"):
        head += ["The engine marks this answer as likely incomplete.", ""]
    if mode is not None:
        head += [workflow_text(mode, style="prose"), ""]
    blocks: list[str] = []
    for it in items:
        path = rel(it)
        if it.get("tier") == "stub":
            summary = str(it.get("content") or "")
            if summary.startswith(path + " - "):
                summary = summary[len(path) + 3 :]
            blocks.append(f"- `{path}` stub: {summary}")
            continue
        loc = f"{path}:{it.get('line')}" if it.get("line") else path or str(it.get("symbol_id"))
        lines = [
            f"- `{loc}` {it.get('kind') or 'symbol'} `{it.get('name') or ''}` d{it.get('graph_distance')}"
        ]
        content = str(it.get("content") or "")
        name = str(it.get("name") or "")
        placeholder = content.strip() in (
            "",
            name,
            f"{name} @ {it.get('file_id')}:{it.get('line')}",
        )
        if it.get("detail_level") in ("full", "signature") and not placeholder:
            body = content.splitlines()
            if len(body) > snippet_lines:
                body = body[:snippet_lines] + [
                    f"... (+{len(content.splitlines()) - snippet_lines} lines)"
                ]
            lines.append("  ```")
            lines.extend("  " + b for b in body)
            lines.append("  ```")
        blocks.append("\n".join(lines))

    text = "\n".join(head)
    for block in blocks:
        candidate = text + "\n" + block
        if max_chars is not None and len(candidate) + 1 > max_chars:
            text += "\n- ... (truncated)"
            break
        text = candidate
    return text + "\n"


def _strip_repo(file_id: str) -> str:
    return file_id.split(":", 1)[1] if ":" in file_id else file_id


def resolve_agent_mode(explicit: str | None, project: Path) -> str:
    """The mode the block states: ``explicit``, else the project's editor MCP config (the switch
    the server reads), else ``BCE_AGENT_MODE`` from the environment / ``.env``."""
    from bce.config import get_settings
    from bce.integrations.agents import project_agent_mode

    return normalize_agent_mode(
        explicit or project_agent_mode(project) or get_settings().agent_mode
    )


def precontext_for_task(
    task_text: str,
    *,
    repo_ids: list[str] | None = None,
    max_candidates: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    snippet_lines: int = DEFAULT_SNIPPET_LINES,
    max_chars: int | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Run ``get_context_for_task`` once and render it (with ``mode``'s workflow when given).
    Returns ``text`` plus what was measured.

    Imports the storage layer lazily so the CLI can offer the command without paying for it on
    ``--help``.
    """
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.storage.vector.store import VectorStore
    from bce.tools.layer3 import get_context_for_task

    t0 = time.perf_counter()
    with connection() as conn:
        result = get_context_for_task(
            GraphRepository(GraphClient(conn)),
            task_text=task_text,
            repo_ids=repo_ids or None,
            max_candidates=max_candidates,
            max_tokens=max_tokens,
            store=VectorStore(conn),
        )
    payload = result.get("payload") or {}
    text = render_context_markdown(
        payload, snippet_lines=snippet_lines, max_chars=max_chars, mode=mode
    )
    cov = payload.get("coverage") or {}
    return {
        "text": text,
        "mode": mode,
        "ms": int((time.perf_counter() - t0) * 1000),
        "confidence": cov.get("confidence"),
        "n_items": len((payload.get("context") or {}).get("items") or []),
        "chars": len(text),
        "payload": payload,
    }


#: Claude Code discards hook context longer than this.
CLAUDE_HOOK_MAX_CHARS = 10_000
#: Prompts shorter than this ("continue", "yes", "/compact") are not tasks; skip the lookup.
_MIN_PROMPT_CHARS = 20


def claude_hook_response(
    stdin_json: dict[str, Any], *, repo_ids: list[str] | None, mode: str | None = None
) -> dict[str, Any] | None:
    """``UserPromptSubmit`` hook body for Claude Code, or ``None`` when there is nothing to add.

    The agent mode is ``mode``, else the one the session's project selects in ``.mcp.json`` (the
    hook JSON carries its ``cwd``). Fails open: any error is reported on stderr and the prompt goes
    through without context.
    """
    prompt = str(stdin_json.get("prompt") or "").strip()
    if len(prompt) < _MIN_PROMPT_CHARS or prompt.startswith("/"):
        return None
    try:
        project = Path(str(stdin_json.get("cwd") or Path.cwd()))
        pre = precontext_for_task(
            prompt,
            repo_ids=repo_ids,
            max_chars=CLAUDE_HOOK_MAX_CHARS,
            mode=resolve_agent_mode(mode, project),
        )
    except Exception as exc:  # noqa: BLE001 - a hook must never block the prompt
        print(f"bce precontext: skipped ({exc})", file=sys.stderr)
        return None
    if not pre["n_items"]:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": pre["text"],
        }
    }
