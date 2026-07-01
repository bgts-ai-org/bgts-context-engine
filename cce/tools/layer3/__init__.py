"""Layer 3 - task-aware orchestration tools (spec section 7, highest value).

``get_context_for_task``, ``expand_blast_radius``, ``suggest_change_sites``, ``select_repos``,
``assemble_context``. Each returns a language-neutral payload plus a coverage object (section 8) and
a localized message (P1 i18n boundary).
"""

from cce.tools.layer3.orchestration import (
    assemble_context,
    expand_blast_radius,
    get_context_for_task,
    select_repos,
    suggest_change_sites,
)

__all__ = [
    "get_context_for_task",
    "expand_blast_radius",
    "suggest_change_sites",
    "select_repos",
    "assemble_context",
]
