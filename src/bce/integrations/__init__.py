"""Editor / agent integrations: one-command setup for Cursor and Claude Code, and the pre-computed
context block an agent starts its first turn with.

The MCP server alone leaves it to the agent to decide when to ask the graph, and agents ask late
(after a round of grep) or repeatedly. What moved the numbers in the agent benchmark was (a) a rule
telling the agent to start from the graph's answer and treat its ``file:line`` entries as verified
locations, and (b) handing it that answer *before* its first turn. ``bce cursor-init`` and
``bce claude-init`` install the first for everyone; the second is a Claude Code hook
(``bce precontext``), because Cursor's ``beforeSubmitPrompt`` hook cannot add context to a prompt.
"""
