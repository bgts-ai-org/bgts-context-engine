import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import type { PlaybackStep } from "../trace/tracePlayback";

interface Props {
  graph: Graph | null;
  hiddenNodeTypes: Set<string>;
  hiddenEdgeTypes: Set<string>;
  selectedId: string | null;
  focusId: string | null;
  traceStep: PlaybackStep | null;
  /** While true, walk random edges as a "searching" animation. */
  searching?: boolean;
  onSelect: (id: string | null) => void;
  onExpand: (id: string) => void;
}

const DIMMED_NODE = "#252b36";
const DIMMED_EDGE = "#1a1f29";
const TRACE_DIMMED_NODE = "#1c212c";
const TRACE_EDGE = "#3d4658";

const SEARCH_DIMMED_NODE = "#1a1f2a";
const SEARCH_HEAD = "#67e8f9";
const SEARCH_TRAIL = ["#22d3ee", "#0ea5e9", "#2563eb", "#1e3a5f"];
const SEARCH_EDGE = "#38bdf8";
const SEARCH_STEP_MS = 220;
const SEARCH_TRAIL_LEN = 10;

interface SearchFrame {
  current: string;
  trail: string[];
  edgeKeys: Set<string>;
}

function isVisibleNode(
  graph: Graph,
  node: string,
  hiddenNodeTypes: Set<string>,
): boolean {
  if (!graph.hasNode(node)) return false;
  if (graph.getNodeAttribute(node, "ghost")) return false;
  const nodeType = graph.getNodeAttribute(node, "nodeType") as string;
  return !hiddenNodeTypes.has(nodeType);
}

function visibleNeighbors(
  graph: Graph,
  node: string,
  hiddenNodeTypes: Set<string>,
  hiddenEdgeTypes: Set<string>,
): string[] {
  const out: string[] = [];
  graph.forEachEdge(node, (_edge, attrs, src, dst) => {
    if (hiddenEdgeTypes.has(attrs.edgeType as string)) return;
    const other = src === node ? dst : src;
    if (isVisibleNode(graph, other, hiddenNodeTypes)) out.push(other);
  });
  return out;
}

function pickRandomStart(
  graph: Graph,
  hiddenNodeTypes: Set<string>,
): string | null {
  const candidates: string[] = [];
  const preferred: string[] = [];
  graph.forEachNode((node, attrs) => {
    if (!isVisibleNode(graph, node, hiddenNodeTypes)) return;
    candidates.push(node);
    if (attrs.nodeType === "Symbol" || attrs.nodeType === "File") {
      preferred.push(node);
    }
  });
  const pool = preferred.length > 0 ? preferred : candidates;
  if (pool.length === 0) return null;
  return pool[Math.floor(Math.random() * pool.length)] ?? null;
}

function nextHop(
  graph: Graph,
  current: string,
  recent: Set<string>,
  hiddenNodeTypes: Set<string>,
  hiddenEdgeTypes: Set<string>,
): string {
  const neighbors = visibleNeighbors(graph, current, hiddenNodeTypes, hiddenEdgeTypes);
  if (neighbors.length === 0) {
    return pickRandomStart(graph, hiddenNodeTypes) ?? current;
  }
  const fresh = neighbors.filter((n) => !recent.has(n));
  const pool = fresh.length > 0 ? fresh : neighbors;
  return pool[Math.floor(Math.random() * pool.length)] ?? current;
}

function edgeBetween(graph: Graph, a: string, b: string): string | null {
  const forward = graph.edges(a, b);
  if (forward[0]) return forward[0]!;
  const backward = graph.edges(b, a);
  return backward[0] ?? null;
}

/** Sigma.js canvas: renders the graphology graph and wires hover/click/double-click. */
export default function GraphView({
  graph,
  hiddenNodeTypes,
  hiddenEdgeTypes,
  selectedId,
  focusId,
  traceStep,
  searching = false,
  onSelect,
  onExpand,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hoveredRef = useRef<string | null>(null);
  const searchRef = useRef<SearchFrame | null>(null);
  const stateRef = useRef({
    hiddenNodeTypes,
    hiddenEdgeTypes,
    selectedId,
    traceStep,
    searching,
  });
  stateRef.current = { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep, searching };

  useEffect(() => {
    if (!containerRef.current || !graph) return;

    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelRenderedSizeThreshold: 7,
      labelFont: "Inter, sans-serif",
      labelSize: 11,
      labelColor: { color: "#cbd5e1" },
      defaultEdgeType: "arrow",
      zIndex: true,
      minCameraRatio: 0.02,
      maxCameraRatio: 20,
      nodeReducer: (node, data) => {
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep, searching } =
          stateRef.current;
        const res = { ...data };

        // Trace playback mode overrides normal filtering/hover behaviour entirely.
        if (traceStep) {
          const hl = traceStep.highlights.get(node);
          if (hl) {
            res.color = hl.color;
            res.size = hl.size;
            res.zIndex = 3;
            res.forceLabel = hl.forceLabel ?? false;
            if (hl.rank !== undefined) {
              res.label = `#${hl.rank}  ${data.label ?? node}`;
              res.highlighted = true;
            }
          } else if (data.ghost) {
            res.hidden = true;
          } else {
            res.color = TRACE_DIMMED_NODE;
            res.label = "";
            res.size = Math.min((data.size as number) ?? 3, 4);
            res.zIndex = 0;
          }
          return res;
        }

        // Searching walk: highlight the active path, dim everything else.
        if (searching && searchRef.current) {
          const { current, trail } = searchRef.current;
          if (data.ghost) {
            res.hidden = true;
            return res;
          }
          if (hiddenNodeTypes.has(data.nodeType as string)) {
            res.hidden = true;
            return res;
          }
          const trailIdx = trail.indexOf(node);
          if (node === current) {
            res.color = SEARCH_HEAD;
            res.size = Math.max((data.size as number) ?? 5, 14);
            res.zIndex = 4;
            res.forceLabel = true;
            res.highlighted = true;
          } else if (trailIdx >= 0) {
            const age = trail.length - 1 - trailIdx;
            res.color = SEARCH_TRAIL[Math.min(age, SEARCH_TRAIL.length - 1)] ?? SEARCH_TRAIL[0];
            res.size = Math.max(((data.size as number) ?? 4) * (1 - age * 0.08), 5);
            res.zIndex = 2;
            res.label = age < 3 ? (data.label as string) : "";
          } else {
            res.color = SEARCH_DIMMED_NODE;
            res.label = "";
            res.size = Math.min((data.size as number) ?? 3, 3.5);
            res.zIndex = 0;
          }
          return res;
        }

        if (data.ghost) {
          res.hidden = true;
          return res;
        }
        if (hiddenNodeTypes.has(data.nodeType as string)) {
          res.hidden = true;
          return res;
        }
        const hovered = hoveredRef.current;
        const focus = hovered ?? selectedId;
        if (focus) {
          const isNeighbor =
            node === focus ||
            graph.someEdge(focus, (_e, attrs, src, dst) => {
              if (hiddenEdgeTypes.has(attrs.edgeType as string)) return false;
              return src === node || dst === node;
            });
          if (!isNeighbor) {
            res.color = DIMMED_NODE;
            res.label = "";
          } else {
            res.highlighted = node === focus;
            res.zIndex = 2;
          }
        }
        return res;
      },
      edgeReducer: (edge, data) => {
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep, searching } =
          stateRef.current;
        const res = { ...data };
        const [src, dst] = graph.extremities(edge);

        if (traceStep) {
          const bothLit = traceStep.highlights.has(src) && traceStep.highlights.has(dst);
          if (bothLit) {
            res.color = TRACE_EDGE;
            res.zIndex = 1;
          } else {
            res.hidden = true;
          }
          return res;
        }

        if (searching && searchRef.current) {
          if (
            hiddenEdgeTypes.has(data.edgeType as string) ||
            hiddenNodeTypes.has(graph.getNodeAttribute(src, "nodeType") as string) ||
            hiddenNodeTypes.has(graph.getNodeAttribute(dst, "nodeType") as string) ||
            graph.getNodeAttribute(src, "ghost") ||
            graph.getNodeAttribute(dst, "ghost")
          ) {
            res.hidden = true;
            return res;
          }
          if (searchRef.current.edgeKeys.has(edge)) {
            res.color = SEARCH_EDGE;
            res.size = Math.max((data.size as number) ?? 1, 2.2);
            res.zIndex = 2;
          } else {
            res.hidden = true;
          }
          return res;
        }

        if (
          hiddenEdgeTypes.has(data.edgeType as string) ||
          hiddenNodeTypes.has(graph.getNodeAttribute(src, "nodeType") as string) ||
          hiddenNodeTypes.has(graph.getNodeAttribute(dst, "nodeType") as string) ||
          graph.getNodeAttribute(src, "ghost") ||
          graph.getNodeAttribute(dst, "ghost")
        ) {
          res.hidden = true;
          return res;
        }
        const focus = hoveredRef.current ?? selectedId;
        if (focus && src !== focus && dst !== focus) {
          res.color = DIMMED_EDGE;
        }
        return res;
      },
    });

    sigma.on("enterNode", ({ node }) => {
      hoveredRef.current = node;
      sigma.refresh({ skipIndexation: true });
    });
    sigma.on("leaveNode", () => {
      hoveredRef.current = null;
      sigma.refresh({ skipIndexation: true });
    });
    sigma.on("clickNode", ({ node }) => onSelect(node));
    sigma.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      onExpand(node);
    });
    sigma.on("clickStage", () => onSelect(null));

    sigmaRef.current = sigma;
    return () => {
      sigma.kill();
      sigmaRef.current = null;
    };
  }, [graph, onSelect, onExpand]);

  // Filter/selection changes only need a re-render, not a rebuild.
  useEffect(() => {
    sigmaRef.current?.refresh({ skipIndexation: true });
  }, [hiddenNodeTypes, hiddenEdgeTypes, selectedId]);

  // Random edge-walk while the pipeline is running.
  useEffect(() => {
    const sigma = sigmaRef.current;
    if (!sigma || !graph || !searching || traceStep) {
      searchRef.current = null;
      if (sigma && !traceStep) sigma.refresh({ skipIndexation: true });
      return;
    }

    const start = pickRandomStart(graph, hiddenNodeTypes);
    if (!start) return;

    searchRef.current = { current: start, trail: [start], edgeKeys: new Set() };
    sigma.refresh({ skipIndexation: true });

    // Keep a wide overview so the walk is visible across the whole graph.
    sigma.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1 }, { duration: 350 });

    const timer = window.setInterval(() => {
      const frame = searchRef.current;
      if (!frame) return;

      const recent = new Set(frame.trail.slice(-6));
      const next = nextHop(
        graph,
        frame.current,
        recent,
        hiddenNodeTypes,
        hiddenEdgeTypes,
      );

      const trail = [...frame.trail, next].slice(-SEARCH_TRAIL_LEN);
      const edgeKeys = new Set<string>();
      for (let i = 1; i < trail.length; i++) {
        const key = edgeBetween(graph, trail[i - 1]!, trail[i]!);
        if (key) edgeKeys.add(key);
      }

      searchRef.current = { current: next, trail, edgeKeys };
      sigma.refresh({ skipIndexation: true });
    }, SEARCH_STEP_MS);

    return () => {
      window.clearInterval(timer);
      searchRef.current = null;
    };
  }, [searching, traceStep, graph, hiddenNodeTypes, hiddenEdgeTypes]);

  // Trace step changes: re-render highlights and fly the camera to fit the step's focus nodes.
  useEffect(() => {
    const sigma = sigmaRef.current;
    if (!sigma) return;
    sigma.refresh({ skipIndexation: true });
    if (!traceStep || !graph) return;

    const points = traceStep.focusIds
      .filter((id) => graph.hasNode(id))
      .map((id) => sigma.getNodeDisplayData(id))
      .filter((p): p is NonNullable<typeof p> => p != null);
    if (points.length === 0) return;

    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const spread = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    // Display data lives in the framed space where the whole graph spans ~1.0.
    const ratio = Math.min(Math.max(spread * 1.6 + 0.05, 0.06), 1.3);
    sigma.getCamera().animate({ x: cx, y: cy, ratio }, { duration: 550 });
  }, [traceStep, graph]);

  // Fly the camera to a node when a search result is picked.
  useEffect(() => {
    const sigma = sigmaRef.current;
    if (!sigma || !focusId || !graph?.hasNode(focusId)) return;
    const pos = sigma.getNodeDisplayData(focusId);
    if (pos) {
      sigma.getCamera().animate({ x: pos.x, y: pos.y, ratio: 0.08 }, { duration: 600 });
    }
  }, [focusId, graph]);

  return <div ref={containerRef} className="graph-canvas" />;
}
