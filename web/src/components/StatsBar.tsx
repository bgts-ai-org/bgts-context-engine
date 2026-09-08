import type { UIStats } from "../api";
import { useTranslation } from "../i18n";
import { langColor, nodeColor } from "../theme";

interface Props {
  stats: UIStats | null;
  truncated: boolean;
  loading: boolean;
}

const formatCount = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

/** Short labels for the legend. */
const LANG_ABBR: Record<string, string> = {
  csharp: "CS",
  "c#": "CS",
  python: "PY",
  typescript: "TS",
  javascript: "JS",
  java: "JV",
  go: "GO",
  golang: "GO",
  rust: "RS",
  ruby: "RB",
  php: "PHP",
  cpp: "C++",
  "c++": "C++",
  c: "C",
  kotlin: "KT",
  swift: "SW",
  scala: "SC",
  shell: "SH",
  bash: "SH",
  html: "HTML",
  css: "CSS",
  sql: "SQL",
  markdown: "MD",
  md: "MD",
  json: "JSON",
  yaml: "YML",
  xml: "XML",
  vue: "VUE",
  svelte: "SV",
  dart: "DT",
  powershell: "PS",
  dockerfile: "DK",
};

const langAbbr = (lang: string): string => {
  const key = lang.trim().toLowerCase();
  if (LANG_ABBR[key]) return LANG_ABBR[key];
  const compact = key.replace(/[^a-z0-9+#]/g, "");
  return (compact.slice(0, 2) || "?").toUpperCase();
};

const formatPct = (count: number, total: number): string => {
  const pct = (count / total) * 100;
  if (pct >= 10) return `${pct.toFixed(0)}%`;
  if (pct >= 1) return `${pct.toFixed(1)}%`;
  return `<1%`;
};

export default function StatsBar({ stats, truncated, loading }: Props) {
  const { t } = useTranslation();

  if (loading) {
    return (
      <div className="stats-bar">
        <span className="muted">{t("stats.loading")}</span>
      </div>
    );
  }
  if (!stats) {
    return (
      <div className="stats-bar">
        <span className="muted">{t("stats.selectRepo")}</span>
      </div>
    );
  }

  const languages = Object.entries(stats.languages).sort((a, b) => b[1] - a[1]);
  const langTotal = languages.reduce((acc, [, count]) => acc + count, 0);

  return (
    <div className="stats-bar">
      <div className="stat">
        <span className="stat-value">{formatCount(stats.total_nodes)}</span>
        <span className="stat-label">{t("stats.nodes")}</span>
      </div>
      <div className="stat">
        <span className="stat-value">{formatCount(stats.total_edges)}</span>
        <span className="stat-label">{t("stats.edges")}</span>
      </div>

      <div className="divider" />

      {Object.entries(stats.node_counts)
        .filter(([, count]) => count > 0)
        .map(([label, count]) => (
          <div className="stat small" key={label}>
            <span className="badge" style={{ background: nodeColor(label) }} />
            <span className="stat-label">
              {label} <strong>{formatCount(count)}</strong>
            </span>
          </div>
        ))}

      {languages.length > 0 && langTotal > 0 && (
        <>
          <div className="divider" />
          <div className="lang-block">
            <div
              className="lang-strip"
              role="img"
              aria-label={languages
                .map(([l, c]) => `${l} ${formatPct(c, langTotal)}`)
                .join(", ")}
            >
              {languages.map(([lang, count]) => (
                <span
                  key={lang}
                  className="lang-seg"
                  style={{
                    flexGrow: count,
                    flexBasis: 0,
                    background: langColor(lang),
                  }}
                  title={`${lang}: ${formatPct(count, langTotal)}`}
                />
              ))}
            </div>
            <ul className="lang-legend">
              {languages.map(([lang, count]) => (
                <li key={lang} title={`${lang}: ${count}`}>
                  <span className="lang-dot" style={{ background: langColor(lang) }} />
                  <span className="lang-abbr">{langAbbr(lang)}</span>
                  <span className="lang-pct">{formatPct(count, langTotal)}</span>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}

      {truncated && <span className="warn">{t("stats.truncated")}</span>}
    </div>
  );
}
