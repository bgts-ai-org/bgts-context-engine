"""The ``get_context_for_task`` answer as the block an agent prompt carries (``bce precontext``).

The block is the compact Markdown answer of :mod:`bce.core.render.compact` - the same text the MCP
tool returns - so an agent reads one format whichever way the context reached it. It is re-sent on
every model turn, so the token budget applies to the whole block, workflow included; a character
cap (Claude Code drops hook context past 10 000 characters) lowers that budget so the block always
fits whole.

The Claude Code hook can compute the block locally (``bce precontext``, which needs the database)
or ask a server for it: ``POST /v1/precontext`` on ``bce serve`` and on ``bce serve-mcp
--transport http`` takes the hook's JSON and answers with the hook's response, so the client needs
only ``bce precontext --server URL`` or ``curl`` (:func:`remote_hook_command`).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from bce.core.agent_mode import normalize_agent_mode
from bce.core.defaults import DEFAULT_MAX_TOKENS
from bce.core.render.compact import CHARS_PER_TOKEN, render_compact


def render_context_markdown(
    payload: dict[str, Any],
    *,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    mode: str | None = None,
) -> str:
    """The compact answer for ``payload`` (with ``mode``'s workflow when given).

    ``max_tokens`` defaults to the budget the answer was assembled with; ``max_chars`` caps it
    further so the whole block, workflow included, stays within that many characters.
    """
    tokens = int(max_tokens or (payload.get("context") or {}).get("budget") or DEFAULT_MAX_TOKENS)
    if max_chars is not None:
        tokens = max(1, min(tokens, max_chars // CHARS_PER_TOKEN))
    return render_compact(payload, max_tokens=tokens, mode=mode)


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
    max_chars: int | None = None,
    mode: str | None = None,
    scope: Any = None,
) -> dict[str, Any]:
    """Run ``get_context_for_task`` once and render it (with ``mode``'s workflow when given).
    Returns ``text`` plus what was measured. ``repo_ids`` may name repositories by id or name;
    ``scope`` is the caller's :class:`~bce.core.auth.scope.ScopeFilter` (None: unrestricted).

    Imports the storage layer lazily so the CLI can offer the command without paying for it on
    ``--help``.
    """
    from bce.storage.graph.client import GraphClient
    from bce.storage.graph.repository import GraphRepository
    from bce.storage.relational.db import connection
    from bce.storage.relational.queries import resolve_repo_ids
    from bce.storage.vector.store import VectorStore
    from bce.tools.layer3 import get_context_for_task

    t0 = time.perf_counter()
    with connection() as conn:
        result = get_context_for_task(
            GraphRepository(GraphClient(conn)),
            task_text=task_text,
            repo_ids=resolve_repo_ids(conn, list(repo_ids)) if repo_ids else None,
            max_candidates=max_candidates,
            max_tokens=max_tokens,
            store=VectorStore(conn),
            scope=scope,
        )
    payload = result.get("payload") or {}
    text = render_context_markdown(payload, max_chars=max_chars, max_tokens=max_tokens, mode=mode)
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
#: The server route that answers a Claude Code ``UserPromptSubmit`` hook.
PRECONTEXT_PATH = "/v1/precontext"
#: How long a remote hook waits for the server (a cold answer embeds and runs the selector).
REMOTE_TIMEOUT_S = 60


def is_task_prompt(stdin_json: dict[str, Any]) -> bool:
    prompt = str(stdin_json.get("prompt") or "").strip()
    return len(prompt) >= _MIN_PROMPT_CHARS and not prompt.startswith("/")


def claude_hook_response(
    stdin_json: dict[str, Any],
    *,
    repo_ids: list[str] | None,
    mode: str | None = None,
    scope: Any = None,
) -> dict[str, Any] | None:
    """``UserPromptSubmit`` hook body for Claude Code, or ``None`` when there is nothing to add.

    The agent mode is ``mode``, else the one the session's project selects in ``.mcp.json`` (the
    hook JSON carries its ``cwd``). Fails open: any error is reported on stderr and the prompt goes
    through without context.
    """
    if not is_task_prompt(stdin_json):
        return None
    prompt = str(stdin_json.get("prompt") or "").strip()
    try:
        project = Path(str(stdin_json.get("cwd") or Path.cwd()))
        pre = precontext_for_task(
            prompt,
            repo_ids=repo_ids,
            max_chars=CLAUDE_HOOK_MAX_CHARS,
            mode=resolve_agent_mode(mode, project),
            scope=scope,
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


def server_hook_response(
    stdin_json: dict[str, Any],
    *,
    repo_ids: list[str] | None,
    mode: str | None = None,
    scope: Any = None,
) -> dict[str, Any]:
    """``POST /v1/precontext``: the hook response, ``{}`` when there is nothing to add.

    The agent mode is the request's, else the server's ``BCE_AGENT_MODE``; it is never read from
    the client's ``cwd``, which names a path on the client's machine. An unknown mode raises.
    """
    from bce.config import get_settings

    resolved = normalize_agent_mode(mode or get_settings().agent_mode)
    body = {k: v for k, v in stdin_json.items() if k != "cwd"}
    return claude_hook_response(body, repo_ids=repo_ids, mode=resolved, scope=scope) or {}


def precontext_url(
    server: str, *, repo_ids: list[str] | None = None, mode: str | None = None
) -> str:
    """The ``POST /v1/precontext`` URL of ``server`` (its base URL) for these repositories."""
    query = [("repo", r) for r in repo_ids or []] + ([("mode", mode)] if mode else [])
    base = server.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base + PRECONTEXT_PATH + (f"?{urllib.parse.urlencode(query)}" if query else "")


def remote_hook_response(
    stdin_json: dict[str, Any],
    *,
    server: str,
    token: str | None = None,
    repo_ids: list[str] | None = None,
    mode: str | None = None,
    timeout: float = REMOTE_TIMEOUT_S,
) -> dict[str, Any] | None:
    """The hook response computed by ``server`` (:data:`PRECONTEXT_PATH`), or ``None``. Fails open
    like :func:`claude_hook_response`."""
    if not is_task_prompt(stdin_json):
        return None
    request = urllib.request.Request(
        precontext_url(server, repo_ids=repo_ids, mode=mode),
        data=json.dumps(stdin_json).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
            body = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 - a hook must never block the prompt
        print(f"bce precontext: skipped ({exc})", file=sys.stderr)
        return None
    return body if isinstance(body, dict) and body.get("hookSpecificOutput") else None


def remote_hook_command(
    server: str, *, repo_ids: list[str] | None = None, token_env: str | None = None
) -> str:
    """A ``curl`` hook command for ``server``: the client needs no ``bce`` install. It prints
    nothing when the server fails (``-f``), so the prompt goes through without context."""
    auth = f' -H "Authorization: Bearer ${{{token_env}}}"' if token_env else ""
    url = precontext_url(server, repo_ids=repo_ids)
    return (
        f"curl -sf -m {REMOTE_TIMEOUT_S} -X POST -H 'Content-Type: application/json'{auth} "
        f"--data-binary @- '{url}' || true"
    )
