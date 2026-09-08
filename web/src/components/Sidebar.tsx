import { useEffect, useRef, useState, type ReactNode } from "react";
import type { UIRepo, UISearchResult } from "../api";
import { api } from "../api";
import { LOCALES, LOCALE_NAMES, isLocale, useTranslation, type TranslationKey } from "../i18n";
import { EDGE_TYPES, NODE_LABELS, edgeColor, nodeColor } from "../theme";

type SidebarTab = "explore" | "analyze" | "filters";

const TABS: { id: SidebarTab; labelKey: TranslationKey }[] = [
  { id: "explore", labelKey: "nav.explore" },
  { id: "analyze", labelKey: "nav.analyze" },
  { id: "filters", labelKey: "nav.filters" },
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
  const { t, locale, setLocale } = useTranslation();
  const [tab, setTab] = useState<SidebarTab>("explore");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<UISearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const debounceRef = useRef<number>(0);

  useEffect(() => {
    window.clearTimeout(debounceRef.current);
    if (!query.trim() || !selectedRepo) {
      setResults([]);
      setSearchError(null);
      return;
    }
    debounceRef.current = window.setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        setResults(await api.search(query.trim(), selectedRepo));
      } catch (e) {
        // Surfaced rather than swallowed: an empty result list and a failed request look
        // identical to the user otherwise.
        setResults([]);
        setSearchError(t("search.failed", { details: String(e) }));
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => window.clearTimeout(debounceRef.current);
  }, [query, selectedRepo, t]);

  const hiddenFilterCount = hiddenNodeTypes.size + hiddenEdgeTypes.size;

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-dot" />
        <div>
          <h1>{t("app.title")}</h1>
          <p>{t("app.subtitle")}</p>
        </div>
      </div>

      <nav className="sidebar-tabs" aria-label={t("nav.tabsLabel")}>
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            className={`sidebar-tab${tab === entry.id ? " active" : ""}`}
            onClick={() => setTab(entry.id)}
          >
            {t(entry.labelKey)}
            {entry.id === "filters" && hiddenFilterCount > 0 && (
              <span
                className="tab-badge"
                aria-label={t("nav.hiddenCount", { count: hiddenFilterCount })}
              >
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
              <h2>{t("repo.heading")}</h2>
              <select
                value={selectedRepo ?? ""}
                onChange={(e) => e.target.value && onSelectRepo(e.target.value)}
              >
                <option value="" disabled>
                  {t("repo.placeholder")}
                </option>
                {repos.map((r) => (
                  <option key={r.repo_id} value={r.repo_id}>
                    {r.name}
                  </option>
                ))}
              </select>
            </section>

            <section>
              <h2>{t("search.heading")}</h2>
              <input
                type="search"
                placeholder={t("search.placeholder")}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                disabled={!selectedRepo}
              />
              {searching && <p className="muted">{t("search.searching")}</p>}
              {searchError && (
                <p className="error" role="alert">
                  {searchError}
                </p>
              )}
              {!searching && !searchError && query.trim() && results.length === 0 && (
                <p className="muted">{t("search.noResults")}</p>
              )}
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
              <h2>{t("filters.nodeTypes")}</h2>
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
              <h2>{t("filters.edgeTypes")}</h2>
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
        <label className="locale-picker">
          <span className="visually-hidden">{t("app.language")}</span>
          <select
            value={locale}
            onChange={(e) => isLocale(e.target.value) && setLocale(e.target.value)}
            aria-label={t("app.language")}
          >
            {LOCALES.map((code) => (
              <option key={code} value={code}>
                {LOCALE_NAMES[code]}
              </option>
            ))}
          </select>
        </label>
        <p>{t("app.company")}</p>
      </footer>
    </aside>
  );
}
