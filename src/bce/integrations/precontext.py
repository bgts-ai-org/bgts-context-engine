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
from typing import Any

from bce.core.defaults import DEFAULT_MAX_CANDIDATES, DEFAULT_MAX_TOKENS, DEFAULT_SNIPPET_LINES

HEADING = "## Code context for this task (bgts-context-engine code graph, computed once)"


def render_context_markdown(
    payload: dict[str, Any],
    *,
    snippet_lines: int = DEFAULT_SNIPPET_LINES,
    max_distance: int | None = None,
    max_chars: int | None = None,
) -> str:
    """Markdown for the agent from a ``get_context_for_task`` payload.

    ``max_distance`` drops items farther than that from the anchors; ``max_chars`` truncates at an
    item boundary (Claude Code caps hook context at 10 000 characters).
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
    blocks: list[str] = []
    for it in items:
        path = rel(it)
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


def precontext_for_task(
    task_text: str,
    *,
    repo_ids: list[str] | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    snippet_lines: int = DEFAULT_SNIPPET_LINES,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Run ``get_context_for_task`` once and render it. Returns ``text`` plus what was measured.

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
    text = render_context_markdown(payload, snippet_lines=snippet_lines, max_chars=max_chars)
    cov = payload.get("coverage") or {}
    return {
        "text": text,
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
    stdin_json: dict[str, Any], *, repo_ids: list[str] | None
) -> dict[str, Any] | None:
    """``UserPromptSubmit`` hook body for Claude Code, or ``None`` when there is nothing to add.

    Fails open: any error is reported on stderr and the prompt goes through without context.
    """
    prompt = str(stdin_json.get("prompt") or "").strip()
    if len(prompt) < _MIN_PROMPT_CHARS or prompt.startswith("/"):
        return None
    try:
        pre = precontext_for_task(prompt, repo_ids=repo_ids, max_chars=CLAUDE_HOOK_MAX_CHARS)
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
