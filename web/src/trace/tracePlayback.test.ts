import Graph from "graphology";
import { describe, expect, it } from "vitest";

import type { TraceResponse, TraceStage } from "../api";
import { translate, type TranslateFn } from "../i18n";
import { SOURCE_COLORS, buildSteps, ensureTraceNodes } from "./tracePlayback";

const t: TranslateFn = (key, vars) => translate("en", key, vars);

const ANCHOR = "py::app::login#1";
const CALLER = "py::app::handler#1";
const CALLEE = "py::app::verify#1";

function trace(stages: TraceStage[], symbols: string[] = [ANCHOR]): TraceResponse {
  return {
    task_text: "fix login timeout",
    repo_ids: ["demo"],
    max_candidates: 2,
    stages,
    symbols: Object.fromEntries(
      symbols.map((sid) => [
        sid,
        { name: sid.split("::").pop() ?? sid, kind: "function", file_id: "demo:app.py", line: 1 },
      ]),
    ),
  };
}

const anchorsStage = (anchors: Record<string, string[]>): TraceStage => ({
  stage: "anchors",
  duration_ms: 1,
  anchors,
});

describe("buildSteps", () => {
  it("always emits the anchors step, even with no anchors", () => {
    const steps = buildSteps(trace([anchorsStage({})]), t);
    expect(steps.map((s) => s.id)).toEqual(["anchors"]);
    expect(steps[0].subtitle).toBe("0 anchors placed (no sources)");
  });

  it("skips the semantic step when the stage produced nothing", () => {
    const steps = buildSteps(
      trace([{ stage: "semantic", duration_ms: 1, candidates: [] }, anchorsStage({})]),
      t,
    );
    expect(steps.map((s) => s.id)).not.toContain("semantic");
  });

  it("emits the semantic step ahead of anchors when candidates exist", () => {
    const steps = buildSteps(
      trace([
        { stage: "semantic", duration_ms: 1, candidates: [CALLEE] },
        anchorsStage({ [ANCHOR]: ["explicit"] }),
      ]),
      t,
    );
    expect(steps.map((s) => s.id)).toEqual(["semantic", "anchors"]);
    expect(steps[0].highlights.get(CALLEE)?.color).toBe(SOURCE_COLORS.semantic);
  });

  it("colors an anchor by its highest-priority source", () => {
    const steps = buildSteps(
      trace([anchorsStage({ [ANCHOR]: ["semantic", "explicit"] })]),
      t,
    );
    expect(steps[0].highlights.get(ANCHOR)?.color).toBe(SOURCE_COLORS.explicit);
  });

  it("emits one cumulative expansion step per distance", () => {
    const steps = buildSteps(
      trace([
        anchorsStage({ [ANCHOR]: ["explicit"] }),
        {
          stage: "expand",
          duration_ms: 1,
          nodes: [
            { symbol_id: ANCHOR, distance: 0, is_anchor: true, ref_kind: null, provenance: null },
            { symbol_id: CALLER, distance: 1, is_anchor: false, ref_kind: null, provenance: null },
            { symbol_id: CALLEE, distance: 2, is_anchor: false, ref_kind: null, provenance: null },
          ],
        },
      ]),
      t,
    );

    expect(steps.map((s) => s.id)).toEqual(["anchors", "expand-1", "expand-2"]);
    // Distance 0 is the anchor itself and must not open its own wave.
    expect(steps[1].highlights.size).toBe(2);
    expect(steps[2].highlights.size).toBe(3);
    expect(steps[2].subtitle).toBe("1 new nodes discovered at distance 2 (3 in total)");
  });

  it("scales scoring highlights across the observed score range", () => {
    const steps = buildSteps(
      trace([
        anchorsStage({ [ANCHOR]: ["explicit"] }),
        {
          stage: "score",
          duration_ms: 1,
          task_signals: { [ANCHOR]: 1 },
          ranked: [
            {
              symbol_id: ANCHOR,
              repo_id: "demo",
              score: 0.9,
              graph_distance: 0,
              is_anchor: true,
              features: {},
            },
            {
              symbol_id: CALLER,
              repo_id: "demo",
              score: 0.1,
              graph_distance: 1,
              is_anchor: false,
              features: {},
            },
          ],
        },
      ]),
      t,
    );

    const score = steps.find((s) => s.id === "score");
    expect(score?.highlights.get(ANCHOR)?.size).toBeGreaterThan(
      score?.highlights.get(CALLER)?.size ?? 0,
    );
  });

  it("does not divide by zero when every candidate scores the same", () => {
    const identical = [ANCHOR, CALLER].map((sid) => ({
      symbol_id: sid,
      repo_id: "demo",
      score: 0.5,
      graph_distance: 0,
      is_anchor: false,
      features: {},
    }));
    const steps = buildSteps(
      trace([anchorsStage({}), { stage: "score", duration_ms: 1, ranked: identical }]),
      t,
    );

    const score = steps.find((s) => s.id === "score");
    for (const sid of [ANCHOR, CALLER]) {
      expect(Number.isFinite(score?.highlights.get(sid)?.size)).toBe(true);
    }
  });

  it("appends narrowing and result steps carrying rank badges", () => {
    const steps = buildSteps(
      trace([
        anchorsStage({ [ANCHOR]: ["explicit"] }),
        {
          stage: "narrow",
          duration_ms: 1,
          selected: [
            { symbol_id: ANCHOR, score: 0.9, rank: 1 },
            { symbol_id: CALLER, score: 0.4, rank: 2 },
          ],
        },
        {
          stage: "assemble",
          duration_ms: 1,
          context: { included: 2, total: 2 },
          coverage: { confidence: "high" },
        },
      ]),
      t,
    );

    expect(steps.map((s) => s.id)).toEqual(["anchors", "narrow", "result"]);
    expect(steps[1].highlights.get(CALLER)?.rank).toBe(2);
    expect(steps[2].subtitle).toBe("Context pack ready - confidence high, 2 items included");
  });

  it("omits narrowing when nothing was selected", () => {
    const steps = buildSteps(
      trace([anchorsStage({}), { stage: "narrow", duration_ms: 1, selected: [] }]),
      t,
    );
    expect(steps.map((s) => s.id)).toEqual(["anchors"]);
  });
});

describe("ensureTraceNodes", () => {
  it("adds ghost nodes for trace symbols missing from the graph", () => {
    const graph = new Graph();
    const added = ensureTraceNodes(graph, trace([anchorsStage({})], [ANCHOR, CALLER]));

    expect(added).toBe(2);
    expect(graph.getNodeAttribute(ANCHOR, "ghost")).toBe(true);
    expect(graph.getNodeAttribute(ANCHOR, "label")).toBe("login#1");
  });

  it("leaves existing nodes untouched", () => {
    const graph = new Graph();
    graph.addNode(ANCHOR, { x: 5, y: 5, size: 10, label: "real" });

    expect(ensureTraceNodes(graph, trace([anchorsStage({})], [ANCHOR]))).toBe(0);
    expect(graph.getNodeAttribute(ANCHOR, "label")).toBe("real");
  });

  it("places a ghost next to its file node when that file is loaded", () => {
    const graph = new Graph();
    graph.addNode("demo:app.py", { x: 100, y: 200, size: 5 });

    ensureTraceNodes(graph, trace([anchorsStage({})], [ANCHOR]));

    // Offset onto a small spiral around the file, so within a few units of it.
    expect(graph.getNodeAttribute(ANCHOR, "x")).toBeCloseTo(106, 0);
    expect(graph.getNodeAttribute(ANCHOR, "y")).toBeCloseTo(200, 0);
  });

  it("produces finite coordinates for an empty graph", () => {
    const graph = new Graph();
    ensureTraceNodes(graph, trace([anchorsStage({})], [ANCHOR]));

    expect(Number.isFinite(graph.getNodeAttribute(ANCHOR, "x"))).toBe(true);
    expect(Number.isFinite(graph.getNodeAttribute(ANCHOR, "y"))).toBe(true);
  });
});
