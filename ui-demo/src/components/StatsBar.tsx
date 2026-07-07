import type { UIStats } from "../api";
import { nodeColor } from "../theme";

interface Props {
  stats: UIStats | null;
  truncated: boolean;
  loading: boolean;
}

const formatCount = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

export default function StatsBar({ stats, truncated, loading }: Props) {
  if (loading) {
    return (
      <div className="stats-bar">
        <span className="muted">graph yukleniyor...</span>
      </div>
    );
  }
  if (!stats) {
    return (
      <div className="stats-bar">
        <span className="muted">Baslamak icin soldan bir repo secin.</span>
      </div>
    );
  }

  const languages = Object.entries(stats.languages);
  const langTotal = languages.reduce((acc, [, count]) => acc + count, 0);

  return (
    <div className="stats-bar">
      <div className="stat">
        <span className="stat-value">{formatCount(stats.total_nodes)}</span>
        <span className="stat-label">dugum</span>
      </div>
      <div className="stat">
        <span className="stat-value">{formatCount(stats.total_edges)}</span>
        <span className="stat-label">kenar</span>
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

      {languages.length > 0 && (
        <>
          <div className="divider" />
          <div className="lang-strip" title={languages.map(([l, c]) => `${l}: ${c}`).join(", ")}>
            {languages.map(([lang, count], i) => (
              <span
                key={lang}
                className="lang-seg"
                style={{
                  width: `${Math.max((count / langTotal) * 100, 4)}%`,
                  filter: `hue-rotate(${i * 40}deg)`,
                }}
              >
                {lang}
              </span>
            ))}
          </div>
        </>
      )}

      {truncated && <span className="warn">graph limit nedeniyle kirpildi</span>}
    </div>
  );
}
