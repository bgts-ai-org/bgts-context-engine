import type { TraceResponse, TraceRankedCandidate } from "../api";
import { useTranslation } from "../i18n";
import type { PlaybackStep } from "../trace/tracePlayback";

interface Props {
  trace: TraceResponse;
  step: PlaybackStep;
  onFocusNode: (gid: string) => void;
}

const SHOW_RANKED_FROM = new Set(["score", "narrow", "result"]);

/** Right panel during trace playback: ranked candidates, top-N once narrowing happened. */
export default function TraceResults({ trace, step, onFocusNode }: Props) {
  const { t } = useTranslation();
  const byName = new Map(trace.stages.map((s) => [s.stage, s]));
  const ranked = byName.get("score")?.ranked ?? [];
  const selected = byName.get("narrow")?.selected ?? [];
  const coverage = byName.get("assemble")?.coverage as
    | { confidence?: string; anchor_count?: number; candidate_count?: number }
    | undefined;

  const showRanked = SHOW_RANKED_FROM.has(step.id) || step.id.startsWith("expand");
  const isFinal = step.id === "narrow" || step.id === "result";
  const selectedIds = new Set(selected.map((s) => s.symbol_id));

  const list: TraceRankedCandidate[] = isFinal
    ? ranked.filter((c) => selectedIds.has(c.symbol_id))
    : ranked.slice(0, 20);

  return (
    <aside className="detail-panel">
      <header>
        <div>
          <span className="chip" style={{ borderColor: "#fbbf24" }}>
            {isFinal
              ? t("results.topChip", { count: selected.length })
              : t("results.candidatesChip")}
          </span>
          <h2>{t("trace.heading")}</h2>
        </div>
      </header>

      <div className="detail-body">
        <p className="docstring">{trace.task_text}</p>

        {isFinal && coverage && (
          <dl className="info-grid">
            <div>
              <dt>{t("results.confidence")}</dt>
              <dd>{coverage.confidence}</dd>
            </div>
            <div>
              <dt>{t("results.anchors")}</dt>
              <dd>{coverage.anchor_count}</dd>
            </div>
            <div>
              <dt>{t("results.candidates")}</dt>
              <dd>{coverage.candidate_count}</dd>
            </div>
          </dl>
        )}

        {showRanked && list.length > 0 && (
          <section>
            <h3>
              {isFinal
                ? t("results.selectedHeading")
                : t("results.scoreHeading", { count: list.length })}
            </h3>
            <ul className="ranked-list">
              {list.map((cand, i) => {
                const meta = trace.symbols[cand.symbol_id];
                const rank = isFinal
                  ? selected.find((s) => s.symbol_id === cand.symbol_id)?.rank ?? i + 1
                  : i + 1;
                return (
                  <li key={cand.symbol_id}>
                    <button onClick={() => onFocusNode(cand.symbol_id)} title={cand.symbol_id}>
                      <span className={`rank-badge ${isFinal ? "final" : ""}`}>{rank}</span>
                      <span className="ranked-info">
                        <span className="result-name">{meta?.name ?? cand.symbol_id}</span>
                        <span className="ranked-file">{meta?.file_id ?? ""}</span>
                      </span>
                      <span className="ranked-score">{cand.score.toFixed(2)}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {!showRanked && <p className="muted">{t("results.waiting")}</p>}
      </div>
    </aside>
  );
}
