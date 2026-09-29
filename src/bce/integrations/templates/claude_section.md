## Code context: bgts-context-engine

This repository is indexed in the `{server_name}` MCP server (repository id{repo_plural} {repo_ids}):
a deterministic code graph (symbols, calls, references, imports, types).

{hook_paragraph}
- Each item carries `file_id` (`<repo>:<path>`), `line`, `name`, `kind` and a short `content`
  snippet. These are verified locations: open those files directly; do not glob or grep for
  something the result already locates. Search only for what it does not cover - locale files,
  configs and tests are not in the graph.
- When items carry `tier`, read the `full` ones first: they are the files the task most likely
  edits. A `stub` item is one line (`path - N candidate symbol(s): ...`) for a probably related
  file; open it only if the `full` files do not cover the task.
- The items are places to inspect, not a to-do list. Change only what the task requires; a file
  being listed is not a reason to edit it.
- Never repeat a `get_context_for_task` call with the same text. Call it only for a distinct
  sub-problem the context does not cover, with `repo_ids: {repo_ids_json}`; `task_text` is the
  only required argument; leave `max_candidates` and `max_tokens` unset, the server's defaults are
  the tuned ones. If `coverage.confidence` is `low`, prefer normal search over another call.
