import { useCallback, useEffect, useState } from "react";
import Graph from "graphology";
import type { UIRepo, UIStats } from "./api";
import { api, API_BASE } from "./api";
import { buildGraph, mergeNeighbors } from "./graph/buildGraph";
import GraphView from "./graph/GraphView";
import DetailPanel from "./components/DetailPanel";
import Sidebar from "./components/Sidebar";
import StatsBar from "./components/StatsBar";

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

  useEffect(() => {
    api
      .repos()
      .then(setRepos)
      .catch((e) =>
        setError(
          `API'ye ulasilamadi (${API_BASE}). 'cce serve' calisiyor mu? Detay: ${e}`,
        ),
      );
  }, []);

  const loadRepo = useCallback(async (repoId: string) => {
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
  }, []);

  const expandNode = useCallback(
    async (gid: string) => {
      if (!graph) return;
      try {
        const data = await api.neighbors(gid);
        mergeNeighbors(graph, gid, data.nodes, data.edges);
        // New object identity is not needed; Sigma re-renders from graphology events.
        setSelectedId(gid);
      } catch (e) {
        setError(`Komsular genisletilemedi: ${e}`);
      }
    },
    [graph],
  );

  const focusNode = useCallback(
    (gid: string) => {
      setSelectedId(gid);
      // Re-set focusId even for the same node so the camera animation replays.
      setFocusId(null);
      requestAnimationFrame(() => setFocusId(gid));
    },
    [],
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
        onFocusNode={focusNode}
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
          <GraphView
            graph={graph}
            hiddenNodeTypes={hiddenNodeTypes}
            hiddenEdgeTypes={hiddenEdgeTypes}
            selectedId={selectedId}
            focusId={focusId}
            onSelect={setSelectedId}
            onExpand={expandNode}
          />
        </div>
      </main>

      {selectedId && (
        <DetailPanel
          gid={selectedId}
          onClose={() => setSelectedId(null)}
          onFocusNode={focusNode}
        />
      )}
    </div>
  );
}
