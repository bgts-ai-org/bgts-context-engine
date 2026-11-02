"""Agent-facing renderings of a ``get_context_for_task`` answer.

:mod:`bce.core.render.evidence` picks the code an answer shows (numbered spans, windowed around
what the task names, classes as skeletons); :mod:`bce.core.render.compact` turns the payload into
the Markdown an agent reads, with the token budget applied to that final text. Every surface that
hands the answer to an agent (MCP, ``bce precontext``, the Claude Code hook, REST
``?view=compact``) uses the same renderer.
"""

from bce.core.render.compact import VIEWS, normalize_view, render_compact
from bce.core.render.evidence import build_evidence

__all__ = ["VIEWS", "build_evidence", "normalize_view", "render_compact"]
