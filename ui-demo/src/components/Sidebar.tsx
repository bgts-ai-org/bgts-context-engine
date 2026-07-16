import { useEffect, useRef, useState, type ReactNode } from "react";
import type { UIRepo, UISearchResult } from "../api";
import { api } from "../api";
import { EDGE_TYPES, NODE_LABELS, edgeColor, nodeColor } from "../theme";

type SidebarTab = "explore" | "analyze" | "filters";

const TABS: { id: SidebarTab; label: string }[] = [
  { id: "explore", label: "Keşfet" },
  { id: "analyze", label: "Analiz" },
  { id: "filters", label: "Filtreler" },
];

interface Props {
  repos: UIRepo[];
  selectedRepo: string | null;
  onSelectRepo: (repoId: string) => void;
  hiddenNodeTypes: Set<string>;
  hiddenEdgeTypes: Set<string>;
  onToggleNodeType: (label: string) => void;
  onToggleEdgeType: (type: string) => void;
  onFocusNode: (gid: string) => void;
  tracePanel?: ReactNode;
}

export default function Sidebar({
  repos,
  selectedRepo,
  onSelectRepo,
  hiddenNodeTypes,
  hiddenEdgeTypes,
  onToggleNodeType,
  onToggleEdgeType,
  onFocusNode,
  tracePanel,
}: Props) {
  const [tab, setTab] = useState<SidebarTab>("explore");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<UISearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const debounceRef = useRef<number>(0);

  useEffect(() => {
    window.clearTimeout(debounceRef.current);
    if (!query.trim() || !selectedRepo) {
      setResults([]);
      return;
    }
    debounceRef.current = window.setTimeout(async () => {
      setSearching(true);
      try {
        setResults(await api.search(query.trim(), selectedRepo));
      } catch {
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => window.clearTimeout(debounceRef.current);
  }, [query, selectedRepo]);

  const hiddenFilterCount = hiddenNodeTypes.size + hiddenEdgeTypes.size;

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-dot" />
        <div>
          <h1>CCE Graph Explorer</h1>
          <p>Kod Graph Görselleştirici</p>
        </div>
      </div>

      <nav className="sidebar-tabs" aria-label="Sidebar sekmeleri">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`sidebar-tab${tab === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "filters" && hiddenFilterCount > 0 && (
              <span className="tab-badge" aria-label={`${hiddenFilterCount} gizli`}>
                {hiddenFilterCount}
              </span>
            )}
          </button>
        ))}
      </nav>

      <div className="sidebar-tab-panel" role="tabpanel">
        {tab === "explore" && (
          <>
            <section>
              <h2>Repo</h2>
              <select
                value={selectedRepo ?? ""}
                onChange={(e) => e.target.value && onSelectRepo(e.target.value)}
              >
                <option value="" disabled>
                  Repo secin...
                </option>
                {repos.map((r) => (
                  <option key={r.repo_id} value={r.repo_id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </section>

            <section>
              <h2>Arama</h2>
              <input
                type="search"
                placeholder="Sembol veya dosya ara..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                disabled={!selectedRepo}
              />
              {searching && <p className="muted">araniyor...</p>}
              {results.length > 0 && (
                <ul className="search-results">
                  {results.map((r) => (
                    <li key={r.id}>
                      <button type="button" onClick={() => onFocusNode(r.id)}>
                        <span className="badge" style={{ background: nodeColor(r.label) }} />
                        <span className="result-name">{r.display}</span>
                        <span className="result-meta">
                          {r.label === "Symbol" ? (r.kind ?? "symbol") : (r.path ?? "")}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}

        {tab === "analyze" && tracePanel}

        {tab === "filters" && (
          <>
            <section>
              <h2>Dugum tipleri</h2>
              <ul className="filter-list">
                {NODE_LABELS.map((label) => (
                  <li key={label}>
                    <label>
                      <input
                        type="checkbox"
                        checked={!hiddenNodeTypes.has(label)}
                        onChange={() => onToggleNodeType(label)}
                      />
                      <span className="badge" style={{ background: nodeColor(label) }} />
                      {label}
                    </label>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h2>Kenar tipleri</h2>
              <ul className="filter-list">
                {EDGE_TYPES.map((type) => (
                  <li key={type}>
                    <label>
                      <input
                        type="checkbox"
                        checked={!hiddenEdgeTypes.has(type)}
                        onChange={() => onToggleEdgeType(type)}
                      />
                      <span className="badge line" style={{ background: edgeColor(type) }} />
                      {type}
                    </label>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </div>

      <footer className="sidebar-footer">
        <p>BilgeAdam Technology & Software</p>
      </footer>
    </aside>
  );
}
