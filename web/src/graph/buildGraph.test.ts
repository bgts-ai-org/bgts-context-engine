import Graph from "graphology";
import { describe, expect, it } from "vitest";

import type { UIEdge, UINode } from "../api";
import { buildGraph, mergeNeighbors } from "./buildGraph";

const node = (id: string, label = "Symbol", display = ""): UINode => ({
  id,
  label,
  display,
  properties: {},
});

const edge = (id: string, source: string, target: string, type = "CALLS"): UIEdge => ({
  id,
  source,
  target,
  type,
  properties: {},
});

describe("buildGraph", () => {
  it("adds every node and edge from the API payload", () => {
    const graph = buildGraph(
      [node("a"), node("b")],
      [edge("a->b", "a", "b")],
    );

    expect(graph.order).toBe(2);
    expect(graph.size).toBe(1);
    expect(graph.hasEdge("a->b")).toBe(true);
  });

  it("drops edges whose endpoints are outside the fetched node window", () => {
    const graph = buildGraph([node("a")], [edge("a->missing", "a", "missing")]);

    expect(graph.order).toBe(1);
    expect(graph.size).toBe(0);
  });

  it("ignores duplicate node and edge ids", () => {
    const graph = buildGraph(
      [node("a"), node("a"), node("b")],
      [edge("a->b", "a", "b"), edge("a->b", "a", "b")],
    );

    expect(graph.order).toBe(2);
    expect(graph.size).toBe(1);
  });

  it("skips nodes with a blank id", () => {
    expect(buildGraph([node(""), node("a")], []).order).toBe(1);
  });

  it("falls back to the id when the API sends no display caption", () => {
    const graph = buildGraph([node("a", "Symbol", ""), node("b", "Symbol", "nice")], []);

    expect(graph.getNodeAttribute("a", "label")).toBe("a");
    expect(graph.getNodeAttribute("b", "label")).toBe("nice");
  });

  it("sizes nodes by degree and caps the busiest ones", () => {
    const hub = "hub";
    const spokes = Array.from({ length: 400 }, (_, i) => `s${i}`);
    const graph = buildGraph(
      [node(hub), ...spokes.map((id) => node(id))],
      spokes.map((id) => edge(`${hub}->${id}`, hub, id)),
    );

    expect(graph.getNodeAttribute(hub, "size")).toBe(22);
    expect(graph.getNodeAttribute(hub, "size")).toBeGreaterThan(
      graph.getNodeAttribute("s0", "size") as number,
    );
  });

  it("assigns finite layout coordinates", () => {
    const graph = buildGraph(
      [node("a"), node("b"), node("c")],
      [edge("a->b", "a", "b"), edge("b->c", "b", "c")],
    );

    graph.forEachNode((_, attrs) => {
      expect(Number.isFinite(attrs.x)).toBe(true);
      expect(Number.isFinite(attrs.y)).toBe(true);
    });
  });

  it("handles an empty payload", () => {
    const graph = buildGraph([], []);
    expect(graph.order).toBe(0);
    expect(graph.size).toBe(0);
  });
});

describe("mergeNeighbors", () => {
  function seeded(): Graph {
    const graph = new Graph({ multi: true, type: "directed" });
    graph.addNode("origin", { x: 10, y: 20, size: 5, label: "origin", nodeType: "Symbol" });
    return graph;
  }

  it("returns only the newly added node ids", () => {
    const graph = seeded();
    const added = mergeNeighbors(graph, "origin", [node("origin"), node("new")], []);

    expect(added).toEqual(["new"]);
    expect(graph.order).toBe(2);
  });

  it("places new nodes on a ring around the origin", () => {
    const graph = seeded();
    mergeNeighbors(graph, "origin", [node("n1"), node("n2"), node("n3")], []);

    for (const id of ["n1", "n2", "n3"]) {
      const dx = (graph.getNodeAttribute(id, "x") as number) - 10;
      const dy = (graph.getNodeAttribute(id, "y") as number) - 20;
      expect(Math.hypot(dx, dy)).toBeCloseTo(18, 5);
    }
  });

  it("adds edges once both endpoints are present", () => {
    const graph = seeded();
    mergeNeighbors(graph, "origin", [node("new")], [edge("origin->new", "origin", "new")]);

    expect(graph.hasEdge("origin->new")).toBe(true);
  });

  it("does not duplicate an edge already in the graph", () => {
    const graph = seeded();
    mergeNeighbors(graph, "origin", [node("new")], [edge("origin->new", "origin", "new")]);
    mergeNeighbors(graph, "origin", [node("new")], [edge("origin->new", "origin", "new")]);

    expect(graph.size).toBe(1);
  });

  it("gives CALLS edges more visual weight than structural ones", () => {
    const graph = seeded();
    mergeNeighbors(
      graph,
      "origin",
      [node("a"), node("b")],
      [edge("e1", "origin", "a", "CALLS"), edge("e2", "origin", "b", "DEFINED_IN")],
    );

    expect(graph.getEdgeAttribute("e1", "size")).toBeGreaterThan(
      graph.getEdgeAttribute("e2", "size") as number,
    );
  });
});
