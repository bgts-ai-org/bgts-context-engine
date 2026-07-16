import Graph from "graphology";
import type { TraceResponse } from "../api";

/** Visual state of one node during a playback step. */
export interface NodeHighlight {
  color: string;
  size: number;
  rank?: number;
  forceLabel?: boolean;
}

/** One animation step: a full highlight map + camera focus targets. */
export interface PlaybackStep {
  id: string;
  title: string;
  subtitle: string;
  highlights: Map<string, NodeHighlight>;
  focusIds: string[];
}

export const SOURCE_COLORS: Record<string, string> = {
  explicit: "#34d399",
  history: "#f59e0b",
  jira: "#f472b6",
  lexical: "#38bdf8",
  semantic: "#a78bfa",
};

const SOURCE_PRIORITY = ["explicit", "history", "jira", "lexical", "semantic"];
const WAVE_COLORS = ["#22d3ee", "#0ea5e9", "#2563eb"];
const SELECTED_COLOR = "#fbbf24";

function anchorColor(sources: string[]): string {
  for (const src of SOURCE_PRIORITY) {
    if (sources.includes(src)) return SOURCE_COLORS[src];
  }
  return "#94a3b8";
}

/** Score -> heat color between cold slate and hot amber. */
function heatColor(t: number): string {
  const lerp = (a: number, b: number) => Math.round(a + (b - a) * t);
  return `rgb(${lerp(72, 251)}, ${lerp(85, 191)}, ${lerp(110, 36)})`;
}

/** Build the ordered animation steps from a pipeline trace. */
export function buildSteps(trace: TraceResponse): PlaybackStep[] {
  const byName = new Map(trace.stages.map((s) => [s.stage, s]));
  const steps: PlaybackStep[] = [];

  const semantic = byName.get("semantic");
  const anchorsStage = byName.get("anchors");
  const expandStage = byName.get("expand");
  const scoreStage = byName.get("score");
  const narrowStage = byName.get("narrow");
  const assembleStage = byName.get("assemble");

  // Step: semantic candidates (only when the stage produced something).
  const semanticIds = semantic?.candidates ?? [];
  if (semanticIds.length > 0) {
    const highlights = new Map<string, NodeHighlight>();
    for (const sid of semanticIds) {
      highlights.set(sid, { color: SOURCE_COLORS.semantic, size: 11, forceLabel: true });
    }
    steps.push({
      id: "semantic",
      title: "Semantik adaylar",
      subtitle: `Embedding aramasi ${semanticIds.length} aday buldu (en dusuk oncelikli capa kaynagi)`,
      highlights,
      focusIds: semanticIds,
    });
  }

  // Step: anchors, colored by strongest source.
  const anchors = anchorsStage?.anchors ?? {};
  const anchorHighlights = new Map<string, NodeHighlight>();
  const sourceCounts = new Map<string, number>();
  for (const [sid, sources] of Object.entries(anchors)) {
    anchorHighlights.set(sid, { color: anchorColor(sources), size: 11, forceLabel: true });
    for (const src of sources) sourceCounts.set(src, (sourceCounts.get(src) ?? 0) + 1);
  }
  const sourceSummary = [...sourceCounts.entries()]
    .map(([src, count]) => `${src}: ${count}`)
    .join(", ");
  steps.push({
    id: "anchors",
    title: "Capalar",
    subtitle: `${anchorHighlights.size} capa atildi (${sourceSummary || "kaynak yok"})`,
    highlights: anchorHighlights,
    focusIds: [...anchorHighlights.keys()],
  });

  // Steps: expansion waves, one per graph distance, cumulative.
  const expandNodes = expandStage?.nodes ?? [];
  const distances = [...new Set(expandNodes.map((n) => n.distance).filter((d) => d > 0))].sort();
  let cumulative = new Map(anchorHighlights);
  for (const dist of distances) {
    const wave = expandNodes.filter((n) => n.distance === dist);
    cumulative = new Map(cumulative);
    for (const node of wave) {
      if (!cumulative.has(node.symbol_id)) {
        cumulative.set(node.symbol_id, {
          color: WAVE_COLORS[Math.min(dist - 1, WAVE_COLORS.length - 1)],
          size: 7,
        });
      }
    }
    steps.push({
      id: `expand-${dist}`,
      title: `Genisleme (${dist}. adim)`,
      subtitle: `${wave.length} yeni dugum ${dist} mesafede kesfedildi (toplam ${cumulative.size})`,
      highlights: cumulative,
      focusIds: [...cumulative.keys()],
    });
  }

  // Step: scoring - heat color + size by normalized score.
  const ranked = scoreStage?.ranked ?? [];
  if (ranked.length > 0) {
    const scores = ranked.map((c) => c.score);
    const min = Math.min(...scores);
    const max = Math.max(...scores);
    const span = max - min || 1;
    const highlights = new Map<string, NodeHighlight>();
    for (const cand of ranked) {
      const t = (cand.score - min) / span;
      highlights.set(cand.symbol_id, {
        color: heatColor(t),
        size: 4 + t * 12,
        forceLabel: false,
      });
    }
    const signalCount = Object.keys(scoreStage?.task_signals ?? {}).length;
    steps.push({
      id: "score",
      title: "Skorlama",
      subtitle: `${ranked.length} aday skorlandi (${signalCount} gorev sinyali); sicak renk = yuksek skor`,
      highlights,
      focusIds: ranked.slice(0, 12).map((c) => c.symbol_id),
    });
  }

  // Step: narrowing to top-N with rank badges.
  const selected = narrowStage?.selected ?? [];
  if (selected.length > 0) {
    const highlights = new Map<string, NodeHighlight>();
    for (const item of selected) {
      highlights.set(item.symbol_id, {
        color: SELECTED_COLOR,
        size: 15,
        rank: item.rank,
        forceLabel: true,
      });
    }
    steps.push({
      id: "narrow",
      title: `Daraltma: ilk ${selected.length}`,
      subtitle: `${ranked.length} aday icinden ilk ${selected.length} secildi`,
      highlights,
      focusIds: selected.map((s) => s.symbol_id),
    });

    const coverage = assembleStage?.coverage as { confidence?: string } | undefined;
    steps.push({
      id: "result",
      title: "Sonuc",
      subtitle: `Baglam paketi hazir - guven: ${coverage?.confidence ?? "?"}, ${
        assembleStage?.context?.included ?? selected.length
      } oge dahil`,
      highlights,
      focusIds: selected.map((s) => s.symbol_id),
    });
  }

  return steps;
}

/**
 * Add trace symbols missing from the loaded graph as small "ghost" nodes so every stage is
 * visible. Ghosts are placed near their file node when it exists, otherwise near the graph center.
 */
export function ensureTraceNodes(graph: Graph, trace: TraceResponse): number {
  let cx = 0;
  let cy = 0;
  if (graph.order > 0) {
    graph.forEachNode((_, attrs) => {
      cx += attrs.x as number;
      cy += attrs.y as number;
    });
    cx /= graph.order;
    cy /= graph.order;
  }

  let added = 0;
  for (const [sid, meta] of Object.entries(trace.symbols)) {
    if (graph.hasNode(sid)) continue;
    let x = cx;
    let y = cy;
    if (meta.file_id && graph.hasNode(meta.file_id)) {
      x = graph.getNodeAttribute(meta.file_id, "x") as number;
      y = graph.getNodeAttribute(meta.file_id, "y") as number;
    }
    const angle = (added * 137.5 * Math.PI) / 180; // golden-angle spiral avoids overlap
    const radius = 6 + (added % 12);
    graph.addNode(sid, {
      x: x + Math.cos(angle) * radius,
      y: y + Math.sin(angle) * radius,
      size: 3,
      color: "#4b5563",
      label: meta.name ?? sid,
      nodeType: "Symbol",
      ghost: true,
    });
    added += 1;
  }
  return added;
}
