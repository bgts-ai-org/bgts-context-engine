"""Request defaults shared by every surface (CLI, REST, MCP, agent integrations).

Kept in a module with no heavy imports so the CLI can read them without loading the storage layer
(``bce languages`` and ``bce cursor-init`` need no database).
"""

#: Default answer length (K) of ``get_context_for_task`` / ``suggest_change_sites``. 20 is where the
#: retrieval benchmarks scored best for both fitted profiles (PR replay recall@K keeps rising to 20
#: and flattens after), and the agent benchmark ran with it.
DEFAULT_MAX_CANDIDATES = 20

#: Default token budget of the assembled context. An agent re-sends the context on every model
#: turn, so it is kept short: at 1500 the agent benchmark carried ~1.3k tokens of graph context per
#: turn and still halved its search output; the earlier 4000 travelled as ~4k tokens per turn for
#: no further gain. Callers that want longer snippets raise it per request.
DEFAULT_MAX_TOKENS = 1500

#: Snippet lines kept per item when the context is rendered as prose for an agent prompt.
DEFAULT_SNIPPET_LINES = 12
