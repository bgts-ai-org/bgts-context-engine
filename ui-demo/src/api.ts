/** Thin client for the CCE `/v1/ui/*` endpoints. */

export const API_BASE: string =
  (import.meta.env.VITE_CCE_API as string | undefined) ?? "http://127.0.0.1:8000";

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

async function getJson<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = new URL(API_BASE + path);
  for (const [key, value] of Object.entries(params ?? {})) {
    url.searchParams.set(key, value);
  }
  const resp = await fetch(url);
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
      node_limit: "20000",
      edge_limit: "100000",
    }),
  stats: (repoId: string) => getJson<UIStats>("/v1/ui/stats", { repo_id: repoId }),
  node: (gid: string) => getJson<UINode>("/v1/ui/node", { gid }),
  neighbors: (gid: string) => getJson<UINeighbors>("/v1/ui/neighbors", { gid }),
  search: (q: string, repoId?: string) =>
    getJson<{ query: string; results: UISearchResult[] }>(
      "/v1/ui/search",
      repoId ? { q, repo_id: repoId } : { q },
    ).then((r) => r.results),
};
