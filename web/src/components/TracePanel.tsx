import { useEffect, useRef, useState } from "react";
import { useTranslation } from "../i18n";

/** Preset choices for the two pipeline knobs; overridable via VITE_BCE_* at build time. */
const parsePresets = (raw: string | undefined, fallback: number[]): number[] => {
  const parsed = (raw ?? "")
    .split(",")
    .map((part) => Number(part.trim()))
    .filter((n) => Number.isFinite(n) && n > 0);
  return parsed.length > 0 ? parsed : fallback;
};

const TOP_N_OPTIONS = parsePresets(
  import.meta.env.VITE_BCE_TOP_N_PRESETS as string | undefined,
  [4, 8, 12, 16],
);
const MAX_TOKEN_OPTIONS = parsePresets(
  import.meta.env.VITE_BCE_TOKEN_PRESETS as string | undefined,
  [2000, 4000, 8000, 16000],
);

interface Props {
  repoName: string | null;
  disabled: boolean;
  running: boolean;
  onRun: (taskText: string, maxCandidates: number, maxTokens: number) => void;
}

/** Task input for the pipeline trace: text + params, fires the analysis. */
export default function TracePanel({ repoName, disabled, running, onRun }: Props) {
  const { t } = useTranslation();
  const [taskText, setTaskText] = useState("");
  const [maxCandidates, setMaxCandidates] = useState(
    () => TOP_N_OPTIONS[Math.min(1, TOP_N_OPTIONS.length - 1)],
  );
  const [maxTokens, setMaxTokens] = useState(
    () => MAX_TOKEN_OPTIONS[Math.min(1, MAX_TOKEN_OPTIONS.length - 1)],
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [taskText]);

  const canRun = !disabled && !running && taskText.trim().length > 0;

  return (
    <section className="trace-panel">
      <div className="trace-repo" title={repoName ?? undefined}>
        {repoName ? (
          <>
            <span className="trace-repo-label">{t("trace.repoLabel")}</span>
            <span className="trace-repo-name">{repoName}</span>
          </>
        ) : (
          <span className="muted">{t("trace.selectRepoFirst")}</span>
        )}
      </div>

      <h2>{t("trace.heading")}</h2>
      <textarea
        ref={textareaRef}
        className="task-input"
        rows={3}
        placeholder={t("trace.taskPlaceholder")}
        value={taskText}
        onChange={(e) => setTaskText(e.target.value)}
        disabled={disabled}
      />

      <div className="task-param">
        <span className="task-param-label">{t("trace.topN")}</span>
        <div className="task-param-options" role="group" aria-label={t("trace.topN")}>
          {TOP_N_OPTIONS.map((n) => (
            <button
              key={n}
              type="button"
              className={`task-param-option${maxCandidates === n ? " active" : ""}`}
              disabled={disabled}
              onClick={() => setMaxCandidates(n)}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      <div className="task-param">
        <span className="task-param-label">{t("trace.maxTokens")}</span>
        <div className="task-param-options" role="group" aria-label={t("trace.maxTokens")}>
          {MAX_TOKEN_OPTIONS.map((n) => (
            <button
              key={n}
              type="button"
              className={`task-param-option${maxTokens === n ? " active" : ""}`}
              disabled={disabled}
              onClick={() => setMaxTokens(n)}
            >
              {n >= 1000 ? `${n / 1000}k` : n}
            </button>
          ))}
        </div>
      </div>

      <button
        className="primary-btn task-run-btn"
        disabled={!canRun}
        onClick={() => onRun(taskText.trim(), maxCandidates, maxTokens)}
      >
        {running ? t("trace.running") : t("trace.run")}
      </button>
    </section>
  );
}
