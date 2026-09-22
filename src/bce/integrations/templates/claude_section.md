## Code context: bgts-context-engine

This repository is indexed in the `{server_name}` MCP server (repository id{repo_plural} {repo_ids}):
a deterministic code graph (symbols, calls, references, imports, types).

{hook_paragraph}
- Each item carries `file_id` (`<repo>:<path>`), `line`, `name`, `kind` and a short `content`
  snippet. These are verified locations: open those files directly; do not glob or grep for
  something the result already locates. Search only for what it does not cover - locale files,
  configs and tests are not in the graph.
- The items are places to inspect, not a to-do list. Change only what the task requires; a file
  being listed is not a reason to edit it.
- Never repeat a `get_context_for_task` call with the same text. Call it only for a distinct
  sub-problem the context does not cover, with `repo_ids: {repo_ids_json}`; `task_text` is the
  only required argument and the defaults (`max_candidates` 20, `max_tokens` 1500) are the tuned
  ones. If `coverage.confidence` is `low`, prefer normal search over another call.
