import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";

interface Props {
  graph: Graph | null;
  hiddenNodeTypes: Set<string>;
  hiddenEdgeTypes: Set<string>;
  selectedId: string | null;
  focusId: string | null;
  onSelect: (id: string | null) => void;
  onExpand: (id: string) => void;
}

const DIMMED_NODE = "#252b36";
const DIMMED_EDGE = "#1a1f29";

/** Sigma.js canvas: renders the graphology graph and wires hover/click/double-click. */
export default function GraphView({
  graph,
  hiddenNodeTypes,
  hiddenEdgeTypes,
  selectedId,
  focusId,
  onSelect,
  onExpand,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hoveredRef = useRef<string | null>(null);
  const stateRef = useRef({ hiddenNodeTypes, hiddenEdgeTypes, selectedId });
  stateRef.current = { hiddenNodeTypes, hiddenEdgeTypes, selectedId };

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
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId } = stateRef.current;
        const res = { ...data };
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
        const { hiddenNodeTypes, hiddenEdgeTypes, selectedId } = stateRef.current;
        const res = { ...data };
        const [src, dst] = graph.extremities(edge);
        if (
          hiddenEdgeTypes.has(data.edgeType as string) ||
          hiddenNodeTypes.has(graph.getNodeAttribute(src, "nodeType") as string) ||
          hiddenNodeTypes.has(graph.getNodeAttribute(dst, "nodeType") as string)
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
