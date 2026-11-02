## Code context: bgts-context-engine

This repository is indexed in the `{server_name}` MCP server (repository id{repo_plural} {repo_ids}):
a deterministic code graph (symbols, calls, references, imports, types).

{hook_paragraph}
- The answer is Markdown. "Primary edit targets" holds the code the task most likely changes as
  numbered lines (`<line>→<code>`), each block headed `path:start-end · kind name · edit|dependency
  · state`. Those lines are the file's current text with its own line numbers: plan the change
  from them instead of re-reading the file. `partial` blocks show windows of a long declaration
  (`⋯ lines a-b omitted` marks what is left out); `skeleton` blocks show a class's members with
  their line ranges.
- "Other symbols in primary files" and "Related files" are verified locations without code: open
  those files directly; do not glob or grep for something the answer already locates. "Related
  tests" are the existing tests of the edit targets, with their line ranges.
- "Gaps" lists what the engine knows the answer does not cover: names it has no symbol for, files
  it does not index, missing tests. For those, and for anything else the answer does not hold,
  search as usual.
- The engine's answer states the agent mode (`hint` = starting point, `trust` = localize-only, for
  benchmarks; set by `BCE_AGENT_MODE` in `{mcp_config}`) and its numbered steps:
  {workflow_source}. Follow those steps.
- The answer lists places to inspect, not a to-do list. Change only what the task requires; a file
  being listed is not a reason to edit it.
- Never repeat a `get_context_for_task` call with the same text. Call it only for a distinct
  sub-problem the context does not cover, with `repo_ids: {repo_ids_json}`; `task_text` is the
  only required argument; leave `max_candidates`, `max_tokens` and `view` unset, the server's
  defaults are the tuned ones.
