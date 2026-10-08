"""Agent mode: how far an agent relies on the ``get_context_for_task`` answer.

The agent benchmark (``local_bench/agent_mcp_bench``) measured two ways of using the answer:

- ``hint`` ("starting point", the default): the answer is the baseline. The agent adds the files of
  the identifiers the engine reports as uncovered and searches only when the engine itself says the
  answer is likely incomplete.
- ``trust`` ("accept as correct"): the answer is the set of locations. The agent works from its
  files and does not search the tree.

The mode is one setting, ``BCE_AGENT_MODE``. The init commands write it into the editor's MCP server
entry so it can be switched there. Every surface that hands the answer to an agent states the
workflow of the active mode: the MCP tool description, ``payload.workflow`` of each answer, and the
``bce precontext`` block. The editor rules only point at that workflow, so switching the mode needs
no rule rewrite.
"""

from __future__ import annotations

from typing import Any

AGENT_MODE_VAR = "BCE_AGENT_MODE"
HINT = "hint"
TRUST = "trust"
DEFAULT_AGENT_MODE = HINT

LABELS = {HINT: "starting point", TRUST: "accept as correct"}

#: Steps per mode. Placeholders name the answer's parts so one text serves the JSON answer (MCP)
#: and the prose block (``bce precontext``); see :data:`_TERMS`.
_STEPS: dict[str, tuple[str, ...]] = {
    HINT: (
        "Start from {files}, `full` tier first: open those files directly instead of searching "
        "for them, and keep every one of them in view.",
        "For every identifier in {unresolved}, open {defined_in}.",
        "When {complete}, the answer covers every identifier the task names: do not search the "
        "tree for further locations.",
        "Only when {incomplete} and the steps above add nothing, check {candidates}, then run a few "
        "targeted greps for the most specific identifiers in the task. With confidence `low`, "
        "normal search is fine.",
        "Tests, configs and locale files are not in the graph; search for those as usual.",
    ),
    TRUST: (
        "Treat {files} as the set of locations for this task, `full` and `stub` tier alike: open "
        "those files directly and work from them.",
        "Do not search the repository (grep, glob, ls, find, codebase search) to find or verify "
        "code locations, and do not ask the engine again with the same task.",
        "If those files do not cover the task, say so instead of searching.",
        "The only exception is a test, config or locale file the task names explicitly; the graph "
        "does not index those.",
    ),
}

_TERMS: dict[str, dict[str, str]] = {
    "json": {
        "files": "`payload.files`",
        "unresolved": "`coverage.unresolved_identifiers`",
        "defined_in": "its `defined_in` files",
        "complete": (
            "`coverage.unresolved_identifiers` is empty and `coverage.likely_incomplete` is false"
        ),
        "incomplete": "`coverage.likely_incomplete` is true",
        "candidates": "`payload.candidates`",
    },
    "prose": {
        "files": "the files listed here",
        "unresolved": 'the "Not covered" line',
        "defined_in": "the files it names",
        "complete": 'there is no "Not covered" line and the answer is not marked likely incomplete',
        "incomplete": "the answer is marked likely incomplete",
        "candidates": 'the "Also ranked" files',
    },
}


def normalize_agent_mode(value: str | None) -> str:
    """The mode named by ``value``; empty means the default. An unknown name raises, so a typo in
    the MCP config fails at startup instead of silently running the other workflow."""
    mode = (value or "").strip().lower()
    if not mode:
        return DEFAULT_AGENT_MODE
    if mode not in _STEPS:
        raise ValueError(f"{AGENT_MODE_VAR}={value!r} is not a mode; known: {', '.join(_STEPS)}")
    return mode


def workflow_steps(mode: str, *, style: str = "json") -> list[str]:
    terms = _TERMS[style]
    return [step.format(**terms) for step in _STEPS[normalize_agent_mode(mode)]]


def workflow(mode: str) -> dict[str, Any]:
    """``payload.workflow`` of an MCP answer."""
    mode = normalize_agent_mode(mode)
    return {"mode": mode, "label": LABELS[mode], "steps": workflow_steps(mode)}


def workflow_text(mode: str, *, style: str = "json") -> str:
    """The workflow as a numbered list under a heading naming the mode and its switch."""
    mode = normalize_agent_mode(mode)
    head = f"Workflow - {AGENT_MODE_VAR}={mode} ({LABELS[mode]}):"
    steps = workflow_steps(mode, style=style)
    return "\n".join([head, *(f"{i}. {s}" for i, s in enumerate(steps, 1))])
