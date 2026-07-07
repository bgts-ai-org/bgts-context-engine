import { useEffect, useRef, useState } from "react";
import type { UIRepo, UISearchResult } from "../api";
import { api } from "../api";
import { EDGE_TYPES, NODE_LABELS, edgeColor, nodeColor } from "../theme";

interface Props {
  repos: UIRepo[];
  selectedRepo: string | null;
  onSelectRepo: (repoId: string) => void;
  hiddenNodeTypes: Set<string>;
  hiddenEdgeTypes: Set<string>;
  onToggleNodeType: (label: string) => void;
  onToggleEdgeType: (type: string) => void;
  onFocusNode: (gid: string) => void;
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
}: Props) {
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

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-dot" />
        <div>
          <h1>CCE Graph Explorer</h1>
          <p>kod graph gorsellestirici</p>
        </div>
      </div>

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
                <button onClick={() => onFocusNode(r.id)}>
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

      <footer className="sidebar-footer">
        <p>Tek tik: detay - Cift tik: komsulari genislet</p>
      </footer>
    </aside>
  );
}
