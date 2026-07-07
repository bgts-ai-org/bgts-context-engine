import Graph from "graphology";
import { circular } from "graphology-layout";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { UIEdge, UINode } from "../api";
import { edgeColor, nodeColor } from "../theme";

export interface NodeAttributes {
  x: number;
  y: number;
  size: number;
  color: string;
  label: string;
  nodeType: string;
  hidden?: boolean;
}

/** Build a graphology graph from API nodes/edges and run a ForceAtlas2 layout. */
export function buildGraph(nodes: UINode[], edges: UIEdge[]): Graph {
  const graph = new Graph({ multi: true, type: "directed" });

  for (const node of nodes) {
    if (!node.id || graph.hasNode(node.id)) continue;
    graph.addNode(node.id, {
      x: 0,
      y: 0,
      size: 3,
      color: nodeColor(node.label),
      label: node.display || node.id,
      nodeType: node.label,
    });
  }

  for (const edge of edges) {
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.id)) continue;
    graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
      color: edgeColor(edge.type),
      edgeType: edge.type,
      size: edge.type === "CALLS" ? 1.2 : 0.6,
    });
  }

  // Size nodes by degree (sqrt scale keeps god-nodes from dwarfing everything).
  graph.forEachNode((id) => {
    const degree = graph.degree(id);
    graph.setNodeAttribute(id, "size", Math.min(3 + Math.sqrt(degree) * 1.6, 22));
  });

  circular.assign(graph, { scale: 100 });
  const settings = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, {
    iterations: graph.order > 2000 ? 120 : 300,
    settings: { ...settings, gravity: 1.2, scalingRatio: 12, slowDown: 6 },
  });

  return graph;
}

/** Add expanded neighbor nodes/edges near an origin node, then locally relax them. */
export function mergeNeighbors(
  graph: Graph,
  originId: string,
  nodes: UINode[],
  edges: UIEdge[],
): string[] {
  const originX = graph.getNodeAttribute(originId, "x") as number;
  const originY = graph.getNodeAttribute(originId, "y") as number;
  const added: string[] = [];

  nodes.forEach((node, i) => {
    if (!node.id || graph.hasNode(node.id)) return;
    const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1);
    graph.addNode(node.id, {
      x: originX + Math.cos(angle) * 18,
      y: originY + Math.sin(angle) * 18,
      size: 4,
      color: nodeColor(node.label),
      label: node.display || node.id,
      nodeType: node.label,
    });
    added.push(node.id);
  });

  for (const edge of edges) {
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.id)) continue;
    graph.addDirectedEdgeWithKey(edge.id, edge.source, edge.target, {
      color: edgeColor(edge.type),
      edgeType: edge.type,
      size: edge.type === "CALLS" ? 1.2 : 0.6,
    });
  }

  return added;
}
