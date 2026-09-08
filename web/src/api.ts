/** Thin client for the BCE `/v1/ui/*` endpoints. */

/**
 * Empty by default so requests stay relative: the production bundle is served by the API
 * itself under `/ui`, and the dev server proxies `/v1` to the API (see vite.config.ts).
 * Set VITE_BCE_API to target an API on a different origin.
 */
export const API_BASE: string = (import.meta.env.VITE_BCE_API as string | undefined) ?? "";

/** Upper bounds for a single whole-repo graph fetch; override via VITE_BCE_* if needed. */
export const GRAPH_NODE_LIMIT: string = (import.meta.env.VITE_BCE_NODE_LIMIT as string) ?? "20000";
export const GRAPH_EDGE_LIMIT: string = (import.meta.env.VITE_BCE_EDGE_LIMIT as string) ?? "100000";

export interface UIRepo {
  repo_id: string;
  name: string;
  default_branch: string | null;
  remote_url: string | null;
  last_indexed_commit: string | null;
  created_at: string | null;
}

export interface UINode {
  id: string;
  label: string;
  display: string;
  properties: Record<string, unknown>;
}

export interface UIEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface UIGraph {
  repo_id: string;
  nodes: UINode[];
  edges: UIEdge[];
  truncated: boolean;
}

export interface UIStats {
  repo_id: string;
  node_counts: Record<string, number>;
  edge_counts: Record<string, number>;
  total_nodes: number;
  total_edges: number;
  languages: Record<string, number>;
}

export interface UISearchResult {
  id: string;
  label: string;
  display: string;
  kind?: string | null;
  file_id?: string | null;
  line?: number | null;
  path?: string | null;
  language?: string | null;
}

export interface UINeighbors {
  gid: string;
  nodes: UINode[];
  edges: UIEdge[];
}

export interface TraceExpandNode {
  symbol_id: string;
  distance: number;
  is_anchor: boolean;
  ref_kind: string | null;
  provenance: string | null;
}

export interface TraceRankedCandidate {
  symbol_id: string;
  repo_id: string | null;
  score: number;
  graph_distance: number;
  is_anchor: boolean;
  features: Record<string, number>;
}

export interface TraceSelected {
  symbol_id: string;
  score: number;
  rank: number;
}

export interface TraceStage {
  stage: "semantic" | "anchors" | "expand" | "score" | "narrow" | "assemble";
  duration_ms: number;
  candidates?: string[];
  anchors?: Record<string, string[]>;
  nodes?: TraceExpandNode[];
  ranked?: TraceRankedCandidate[];
  task_signals?: Record<string, number>;
  selected?: TraceSelected[];
  context?: { included: number; total: number };
  coverage?: Record<string, unknown>;
  error?: string;
}

export interface TraceSymbolMeta {
  name: string | null;
  kind: string | null;
  file_id: string | null;
  line: number | null;
}

export interface TraceResponse {
  task_text: string;
  repo_ids: string[] | null;
  max_candidates: number;
  stages: TraceStage[];
  symbols: Record<string, TraceSymbolMeta>;
}

async function getJson<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(API_BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(params ?? {})) {
    url.searchParams.set(key, value);
  }
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}: ${await resp.text()}`);
  }
  return (await resp.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}: ${await resp.text()}`);
  }
  return (await resp.json()) as T;
}

export const api = {
  repos: () => getJson<{ repos: UIRepo[] }>("/v1/ui/repos").then((r) => r.repos),
  graph: (repoId: string) =>
    getJson<UIGraph>("/v1/ui/graph", {
      repo_id: repoId,
      node_limit: GRAPH_NODE_LIMIT,
      edge_limit: GRAPH_EDGE_LIMIT,
    }),
  stats: (repoId: string) => getJson<UIStats>("/v1/ui/stats", { repo_id: repoId }),
  node: (gid: string) => getJson<UINode>("/v1/ui/node", { gid }),
  neighbors: (gid: string) => getJson<UINeighbors>("/v1/ui/neighbors", { gid }),
  search: (q: string, repoId?: string) =>
    getJson<{ query: string; results: UISearchResult[] }>(
      "/v1/ui/search",
      repoId ? { q, repo_id: repoId } : { q },
    ).then((r) => r.results),
  contextTrace: (
    taskText: string,
    repoId: string,
    maxCandidates: number,
    maxTokens: number,
  ) =>
    postJson<TraceResponse>("/v1/ui/context-trace", {
      task_text: taskText,
      repo_ids: [repoId],
      max_candidates: maxCandidates,
      max_tokens: maxTokens,
    }),
};
