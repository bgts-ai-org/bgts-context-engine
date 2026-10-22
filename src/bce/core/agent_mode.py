"""Agent mode: how far an agent relies on the ``get_context_for_task`` answer.

- ``hint`` ("starting point", the default): the answer comes first. The agent designs the change
  from the code it shows, reads only the ranges it leaves out, opens the files of the identifiers
  it reports as uncovered, and searches as usual for whatever else it needs - the answer lists the
  gaps the engine knows of, but an agent is never told not to search.
- ``trust`` ("localize-only, for benchmarks"): the answer is the set of locations. The agent works
  from its files and does not search the tree. This measures the engine's localisation alone; it
  is not a mode to work in.

The mode is one setting, ``BCE_AGENT_MODE``. The init commands write it into the editor's MCP server
entry so it can be switched there. Every surface that hands the answer to an agent states the
workflow of the active mode: the MCP tool description, the answer's "Workflow" section (or
``payload.workflow`` of the JSON view), and the ``bce precontext`` block. The editor rules only
point at that workflow, so switching the mode needs no rule rewrite.
"""

from __future__ import annotations

from typing import Any

AGENT_MODE_VAR = "BCE_AGENT_MODE"
HINT = "hint"
TRUST = "trust"
DEFAULT_AGENT_MODE = HINT

LABELS = {HINT: "starting point", TRUST: "localize-only, for benchmarks"}

#: Steps per mode. Placeholders name the answer's parts so one text serves the JSON answer
#: (MCP ``view=debug``) and the compact Markdown answer (MCP default, ``bce precontext``); see
#: :data:`_TERMS`.
_STEPS: dict[str, tuple[str, ...]] = {
    HINT: (
        "Start from {evidence}: {evidence_use}. Open {related} directly instead of searching for "
        "them.",
        "Read a file only for lines {omitted}, or where your editor needs a read before an edit - "
        "and then only that range (offset/limit), not the whole file.",
        "{unresolved_step} If anything else you need is not in the answer, search as usual (grep, "
        "glob); {gaps} lists what the engine knows the answer lacks.",
        "{tests} are the existing tests of the edit targets. Configs and locale files are not "
        "indexed; search for those as usual.",
    ),
    TRUST: (
        "Treat {files} as the set of locations for this task, {all_tiers}: work from {evidence} "
        "and open those files directly.",
        "Do not search the repository (grep, glob, ls, find, codebase search) to find or verify "
        "code locations, and do not ask the engine again with the same task.",
        "If those files do not cover the task, say so instead of searching.",
        "The only exception is a config or locale file the task names explicitly; the graph "
        "does not index those.",
    ),
}

_TERMS: dict[str, dict[str, str]] = {
    "json": {
        "files": "`payload.files`",
        "all_tiers": "`full` and `stub` tier alike",
        "evidence": "`payload.evidence`",
        "evidence_use": (
            "the code of the primary edit targets with their file ranges, as the files hold it "
            "now - design the change from it"
        ),
        "related": "the other `payload.files`",
        "omitted": "`payload.evidence` does not show (spans with `complete: false`)",
        "unresolved_step": (
            "For every identifier in `coverage.unresolved_identifiers`, open its `defined_in` "
            "files."
        ),
        "gaps": "`coverage.gaps`",
        "tests": "`payload.tests`",
    },
    "compact": {
        "files": "the files of the answer",
        "all_tiers": "primary and related alike",
        "evidence": '"Primary edit targets"',
        "evidence_use": (
            "the numbered code is the files' current text, with their line numbers - design the "
            "change from it"
        ),
        "related": 'the "Related files"',
        "omitted": "the answer leaves out (`⋯ lines a-b omitted`, partial or skeleton spans)",
        "unresolved_step": 'For every name "Gaps" places in a file, open that file.',
        "gaps": '"Gaps"',
        "tests": '"Related tests"',
    },
    "locate": {
        "files": "the files of the answer",
        "all_tiers": "primary and related alike",
        "evidence": '"Primary edit targets"',
        "evidence_use": (
            "each entry is a declaration and its line range - read those ranges and design the "
            "change from them"
        ),
        "related": 'the "Related files"',
        "omitted": "outside the listed ranges that the change needs",
        "unresolved_step": 'For every name "Gaps" places in a file, open that file.',
        "gaps": '"Gaps"',
        "tests": '"Related tests"',
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
