/** Shared color coding for node labels and edge types. */

export const NODE_COLORS: Record<string, string> = {
  Repo: "#f59e0b",
  File: "#38bdf8",
  Symbol: "#34d399",
  Module: "#a78bfa",
  Route: "#f472b6",
  DesignNote: "#94a3b8",
};

export const EDGE_COLORS: Record<string, string> = {
  DEFINED_IN: "#26547c",
  BELONGS_TO: "#4a4437",
  IMPORTS: "#5b4a8a",
  CALLS: "#1f6f54",
  INHERITS: "#8a3a5c",
  IMPLEMENTS: "#8a6d3a",
  REFERENCES: "#3a5f8a",
  ROUTES_TO: "#8a3a76",
  EXPLAINS: "#55606e",
};

export const NODE_LABELS = Object.keys(NODE_COLORS);
export const EDGE_TYPES = Object.keys(EDGE_COLORS);

export const nodeColor = (label: string): string => NODE_COLORS[label] ?? "#6b7280";
export const edgeColor = (type: string): string => EDGE_COLORS[type] ?? "#374151";
