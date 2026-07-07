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
  onSelect: (id: string | null) => void;
  onExpand: (id: string) => void;
}

const DIMMED_NODE = "#252b36";
const DIMMED_EDGE = "#1a1f29";
const TRACE_DIMMED_NODE = "#1c212c";
const TRACE_EDGE = "#3d4658";

/** Sigma.js canvas: renders the graphology graph and wires hover/click/double-click. */
export default function GraphView({
  graph,
  hiddenNodeTypes,
  hiddenEdgeTypes,
  selectedId,
  focusId,
  traceStep,
  onSelect,
  onExpand,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hoveredRef = useRef<string | null>(null);
  const stateRef = useRef({ hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep });
  stateRef.current = { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep };

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
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep } = stateRef.current;
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
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId, traceStep } = stateRef.current;
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
