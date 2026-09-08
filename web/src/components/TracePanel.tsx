import { useEffect, useRef, useState } from "react";

const TOP_N_OPTIONS = [4, 8, 12, 16];
const MAX_TOKEN_OPTIONS = [2000, 4000, 8000, 16000];

interface Props {
  repoName: string | null;
  disabled: boolean;
  running: boolean;
  onRun: (taskText: string, maxCandidates: number, maxTokens: number) => void;
}

/** Task input for the pipeline trace: text + params, fires the analysis. */
export default function TracePanel({ repoName, disabled, running, onRun }: Props) {
  const [taskText, setTaskText] = useState("");
  const [maxCandidates, setMaxCandidates] = useState(8);
  const [maxTokens, setMaxTokens] = useState(4000);
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
            <span className="trace-repo-label">Repo</span>
            <span className="trace-repo-name">{repoName}</span>
          </>
        ) : (
          <span className="muted">Once bir repo secin.</span>
        )}
      </div>

      <h2>Gorev analizi</h2>
      <textarea
        ref={textareaRef}
        className="task-input"
        rows={3}
        placeholder="Gorev metni girin, orn: fix login timeout in meeting webhook..."
        value={taskText}
        onChange={(e) => setTaskText(e.target.value)}
        disabled={disabled}
      />

      <div className="task-param">
        <span className="task-param-label">Top-N</span>
        <div className="task-param-options" role="group" aria-label="Top-N">
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
        <span className="task-param-label">max_tokens</span>
        <div className="task-param-options" role="group" aria-label="max_tokens">
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
        {running ? "Calisiyor..." : "Analiz Et"}
      </button>
    </section>
  );
}
