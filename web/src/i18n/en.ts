/**
 * English catalog. This file is the source of truth for the available keys: every other
 * locale is typed as `Record<TranslationKey, string>`, so a missing or misspelled key is a
 * compile error rather than a blank label at runtime.
 *
 * Placeholders use `{name}` and are substituted by `translate()`.
 */
export const en = {
  "app.title": "BCE Graph Explorer",
  "app.subtitle": "Code graph visualiser",
  "app.company": "BGTS",
  "app.language": "Language",

  "nav.tabsLabel": "Sidebar tabs",
  "nav.explore": "Explore",
  "nav.analyze": "Analyze",
  "nav.filters": "Filters",
  "nav.hiddenCount": "{count} hidden",

  "repo.heading": "Repository",
  "repo.placeholder": "Select a repository...",

  "search.heading": "Search",
  "search.placeholder": "Search symbols or files...",
  "search.searching": "searching...",
  "search.failed": "Search failed: {details}",
  "search.noResults": "No matches.",

  "filters.nodeTypes": "Node types",
  "filters.edgeTypes": "Edge types",

  "stats.loading": "loading graph...",
  "stats.selectRepo": "Select a repository on the left to begin.",
  "stats.nodes": "nodes",
  "stats.edges": "edges",
  "stats.truncated": "graph truncated by limit",

  "empty.title": "Explore your code graph",
  "empty.body":
    "Pick an indexed repository on the left. Files, symbols and the call and reference edges between them are drawn interactively.",
  "empty.loadingGraph": "Loading the graph and computing the layout...",
  "empty.searchingContext": "Searching for context...",

  "trace.repoLabel": "Repo",
  "trace.selectRepoFirst": "Select a repository first.",
  "trace.heading": "Task analysis",
  "trace.taskPlaceholder": "Describe a task, for example: fix login timeout in meeting webhook...",
  "trace.topN": "Top-N",
  "trace.maxTokens": "max_tokens",
  "trace.run": "Analyze",
  "trace.running": "Running...",

  "player.prev": "Previous step",
  "player.playPause": "Play / pause",
  "player.next": "Next step",
  "player.speed": "Playback speed",
  "player.close": "Close analysis",

  "results.candidatesChip": "CANDIDATES",
  "results.topChip": "TOP {count}",
  "results.confidence": "confidence",
  "results.anchors": "anchors",
  "results.candidates": "candidates",
  "results.selectedHeading": "Selected candidates",
  "results.scoreHeading": "Score ranking (top {count})",
  "results.waiting": "Scored candidates appear here as the animation progresses.",

  "detail.close": "Close",
  "detail.docstring": "Docstring",
  "detail.note": "Note",
  "detail.code": "Code",
  "detail.connections": "Connections ({count})",

  "error.apiUnreachable": "Could not reach the API. Is `bce serve` running? Details: {details}",
  "error.graphLoad": "Could not load the graph: {details}",
  "error.traceRun": "Could not run the analysis: {details}",
  "error.noCandidates": "The pipeline produced no candidates. Try a different task description.",
  "error.expandNeighbors": "Could not expand neighbours: {details}",

  "steps.semantic.title": "Semantic candidates",
  "steps.semantic.subtitle":
    "Embedding search found {count} candidates (the lowest-priority anchor source)",
  "steps.anchors.title": "Anchors",
  "steps.anchors.subtitle": "{count} anchors placed ({sources})",
  "steps.anchors.noSources": "no sources",
  "steps.expand.title": "Expansion (step {distance})",
  "steps.expand.subtitle":
    "{count} new nodes discovered at distance {distance} ({total} in total)",
  "steps.score.title": "Scoring",
  "steps.score.subtitle":
    "{count} candidates scored using {signals} task signals; a warmer colour means a higher score",
  "steps.narrow.title": "Narrowing: top {count}",
  "steps.narrow.subtitle": "Top {count} selected out of {total} candidates",
  "steps.result.title": "Result",
  "steps.result.subtitle": "Context pack ready - confidence {confidence}, {included} items included",
} as const;

export type TranslationKey = keyof typeof en;
