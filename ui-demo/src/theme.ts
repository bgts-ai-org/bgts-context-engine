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

/**
 * Official GitHub Linguist language colors
 * (https://github.com/github-linguist/linguist/blob/master/lib/linguist/languages.yml).
 * Keys are lowercase API / display aliases used by CCE.
 */
export const LANG_COLORS: Record<string, string> = {
  // Core languages indexed by CCE
  csharp: "#7355dd",
  "c#": "#7355dd",
  python: "#3572A5",
  typescript: "#3178c6",
  javascript: "#f1e05a",
  java: "#b07219",
  go: "#00ADD8",
  golang: "#00ADD8",

  // Common companion languages
  rust: "#dea584",
  ruby: "#701516",
  php: "#4F5D95",
  kotlin: "#A97BFF",
  swift: "#F05138",
  scala: "#c22d40",
  dart: "#00B4AB",
  lua: "#000080",
  r: "#198CE7",
  perl: "#0298c3",
  elixir: "#6e4a7e",
  erlang: "#B83998",
  haskell: "#5e5086",
  clojure: "#db5855",
  groovy: "#4298b8",
  zig: "#ec915c",
  nim: "#ffc200",
  crystal: "#000100",
  ocaml: "#ef7a08",
  elm: "#60B5CC",
  solidity: "#AA6746",
  fortran: "#4d41b1",
  cuda: "#3A4E3A",
  assembly: "#6E4C13",
  webassembly: "#04133b",
  wasm: "#04133b",

  // C family
  c: "#555555",
  cpp: "#f34b7d",
  "c++": "#f34b7d",
  "objective-c": "#438eff",
  objc: "#438eff",
  "f#": "#b845fc",
  fsharp: "#b845fc",

  // Web / markup
  html: "#e34c26",
  css: "#663399",
  scss: "#c6538c",
  sass: "#a53b70",
  less: "#1d365d",
  vue: "#41b883",
  svelte: "#ff3e00",
  astro: "#ff5a03",
  tsx: "#3178c6",
  jsx: "#f1e05a",
  graphql: "#e10098",
  markdown: "#083fa1",
  md: "#083fa1",
  json: "#292929",
  yaml: "#cb171e",
  yml: "#cb171e",
  xml: "#0060ac",
  sql: "#e38c00",
  plsql: "#e38c00",
  plpgsql: "#336790",

  // Shell / ops
  shell: "#89e051",
  bash: "#89e051",
  sh: "#89e051",
  powershell: "#012456",
  batchfile: "#C1F12E",
  dockerfile: "#384d54",
  docker: "#384d54",
  makefile: "#427819",
  make: "#427819",
  hcl: "#844FBA",
  terraform: "#844FBA",

  // Other
  coffeescript: "#244776",
  tex: "#3D6117",
  "vim script": "#199f4b",
  vim: "#199f4b",
  "emacs lisp": "#c065db",
  elisp: "#c065db",
};

const UNKNOWN_LANG_COLOR = "#8b949e";

export const langColor = (lang: string): string => {
  const key = lang.trim().toLowerCase();
  return LANG_COLORS[key] ?? UNKNOWN_LANG_COLOR;
};
