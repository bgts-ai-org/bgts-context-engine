import { useCallback, useEffect, useRef, useState } from "react";
import Graph from "graphology";
import type { TraceResponse, UIRepo, UIStats } from "./api";
import { api, API_BASE } from "./api";
import { buildGraph, mergeNeighbors } from "./graph/buildGraph";
import GraphView from "./graph/GraphView";
import DetailPanel from "./components/DetailPanel";
import Sidebar from "./components/Sidebar";
import StatsBar from "./components/StatsBar";
import TracePanel from "./components/TracePanel";
import TracePlayer from "./components/TracePlayer";
import TraceResults from "./components/TraceResults";
import { buildSteps, ensureTraceNodes, type PlaybackStep } from "./trace/tracePlayback";

const STEP_DURATION_MS = 2600;

export default function App() {
  const [repos, setRepos] = useState<UIRepo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [stats, setStats] = useState<UIStats | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [hiddenNodeTypes, setHiddenNodeTypes] = useState<Set<string>>(new Set());
  const [hiddenEdgeTypes, setHiddenEdgeTypes] = useState<Set<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  const [trace, setTrace] = useState<TraceResponse | null>(null);
  const [traceSteps, setTraceSteps] = useState<PlaybackStep[]>([]);
  const [traceIndex, setTraceIndex] = useState(0);
  const [tracePlaying, setTracePlaying] = useState(false);
  const [traceSpeed, setTraceSpeed] = useState(1);
  const [traceRunning, setTraceRunning] = useState(false);
  const traceTimerRef = useRef<number>(0);

  useEffect(() => {
    api
      .repos()
      .then(setRepos)
      .catch((e) =>
        setError(
          `API'ye ulasilamadi (${API_BASE}). 'bce serve' calisiyor mu? Detay: ${e}`,
        ),
      );
  }, []);

  const closeTrace = useCallback(() => {
    window.clearTimeout(traceTimerRef.current);
    setTrace(null);
    setTraceSteps([]);
    setTraceIndex(0);
    setTracePlaying(false);
  }, []);

  const loadRepo = useCallback(
    async (repoId: string) => {
      closeTrace();
      setSelectedRepo(repoId);
      setSelectedId(null);
      setFocusId(null);
      setGraph(null);
      setStats(null);
      setLoading(true);
      setError(null);
      try {
        const [graphData, statsData] = await Promise.all([
          api.graph(repoId),
          api.stats(repoId),
        ]);
        setGraph(buildGraph(graphData.nodes, graphData.edges));
        setTruncated(graphData.truncated);
        setStats(statsData);
      } catch (e) {
        setError(`Graph yuklenemedi: ${e}`);
      } finally {
        setLoading(false);
      }
    },
    [closeTrace],
  );

  const runTrace = useCallback(
    async (taskText: string, maxCandidates: number, maxTokens: number) => {
      if (!selectedRepo || !graph) return;
      closeTrace();
      setSelectedId(null);
      setTraceRunning(true);
      setError(null);
      try {
        const result = await api.contextTrace(
          taskText,
          selectedRepo,
          maxCandidates,
          maxTokens,
        );
        const steps = buildSteps(result);
        if (steps.length === 0) {
          setError("Pipeline hicbir aday uretmedi; farkli bir gorev metni deneyin.");
          return;
        }
        ensureTraceNodes(graph, result);
        setTrace(result);
        setTraceSteps(steps);
        setTraceIndex(0);
        setTracePlaying(true);
      } catch (e) {
        setError(`Analiz calistirilamadi: ${e}`);
      } finally {
        setTraceRunning(false);
      }
    },
    [selectedRepo, graph, closeTrace],
  );

  // Auto-advance playback; pauses at the last step.
  useEffect(() => {
    window.clearTimeout(traceTimerRef.current);
    if (!tracePlaying || traceSteps.length === 0) return;
    if (traceIndex >= traceSteps.length - 1) {
      setTracePlaying(false);
      return;
    }
    traceTimerRef.current = window.setTimeout(
      () => setTraceIndex((i) => Math.min(i + 1, traceSteps.length - 1)),
      STEP_DURATION_MS / traceSpeed,
    );
    return () => window.clearTimeout(traceTimerRef.current);
  }, [tracePlaying, traceIndex, traceSteps, traceSpeed]);

  const seekTrace = useCallback((index: number) => {
    window.clearTimeout(traceTimerRef.current);
    setTraceIndex(index);
  }, []);

  const togglePlay = useCallback(() => {
    setTracePlaying((p) => {
      // Replay from the start when play is pressed at the end.
      if (!p && traceIndex >= traceSteps.length - 1) {
        setTraceIndex(0);
      }
      return !p;
    });
  }, [traceIndex, traceSteps.length]);

  const expandNode = useCallback(
    async (gid: string) => {
      if (!graph) return;
      try {
        const data = await api.neighbors(gid);
        mergeNeighbors(graph, gid, data.nodes, data.edges);
        setSelectedId(gid);
      } catch (e) {
        setError(`Komsular genisletilemedi: ${e}`);
      }
    },
    [graph],
  );

  const focusNode = useCallback((gid: string) => {
    setFocusId(null);
    requestAnimationFrame(() => setFocusId(gid));
  }, []);

  const focusAndSelect = useCallback(
    (gid: string) => {
      setSelectedId(gid);
      focusNode(gid);
    },
    [focusNode],
  );

  const toggleNodeType = useCallback((label: string) => {
    setHiddenNodeTypes((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }, []);

  const toggleEdgeType = useCallback((type: string) => {
    setHiddenEdgeTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }, []);

  const currentStep = trace && traceSteps.length > 0 ? traceSteps[traceIndex] : null;

  return (
    <div className="app">
      <Sidebar
        repos={repos}
        selectedRepo={selectedRepo}
        onSelectRepo={loadRepo}
        hiddenNodeTypes={hiddenNodeTypes}
        hiddenEdgeTypes={hiddenEdgeTypes}
        onToggleNodeType={toggleNodeType}
        onToggleEdgeType={toggleEdgeType}
        onFocusNode={focusAndSelect}
        tracePanel={
          <TracePanel
            repoName={
              repos.find((r) => r.repo_id === selectedRepo)?.name ?? selectedRepo
            }
            disabled={!selectedRepo || !graph}
            running={traceRunning}
            onRun={runTrace}
          />
        }
      />

      <main className="main">
        <StatsBar stats={stats} truncated={truncated} loading={loading} />
        <div className="graph-wrap">
          {error && <div className="toast error">{error}</div>}
          {!graph && !loading && !error && (
            <div className="empty-state">
              <h2>Kod graph'inizi kesfedin</h2>
              <p>
                Soldan indexlenmis bir repo secin; dosyalar, semboller ve aralarindaki
                cagri/iliski kenarlari interaktif olarak cizilir.
              </p>
            </div>
          )}
          {loading && (
            <div className="empty-state">
              <div className="spinner" />
              <p>Graph yukleniyor ve yerlesim hesaplaniyor...</p>
            </div>
          )}
          {traceRunning && !loading && (
            <div className="search-status" aria-live="polite">
              <span className="search-status-dot" />
              Bağlam aranıyor...
            </div>
          )}
          <GraphView
            graph={graph}
            hiddenNodeTypes={hiddenNodeTypes}
            hiddenEdgeTypes={hiddenEdgeTypes}
            selectedId={selectedId}
            focusId={focusId}
            traceStep={currentStep}
            searching={traceRunning}
            onSelect={setSelectedId}
            onExpand={expandNode}
          />
          {currentStep && (
            <TracePlayer
              steps={traceSteps}
              index={traceIndex}
              playing={tracePlaying}
              speed={traceSpeed}
              onSeek={seekTrace}
              onTogglePlay={togglePlay}
              onSpeed={setTraceSpeed}
              onClose={closeTrace}
            />
          )}
        </div>
      </main>

      {selectedId ? (
        <DetailPanel
          gid={selectedId}
          onClose={() => setSelectedId(null)}
          onFocusNode={focusAndSelect}
        />
      ) : (
        trace &&
        currentStep && (
          <TraceResults trace={trace} step={currentStep} onFocusNode={focusAndSelect} />
        )
      )}
    </div>
  );
}
