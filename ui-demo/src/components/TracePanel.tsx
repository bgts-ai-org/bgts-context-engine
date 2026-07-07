import { useState } from "react";

interface Props {
  disabled: boolean;
  running: boolean;
  onRun: (taskText: string, maxCandidates: number) => void;
}

/** Task input for the pipeline trace: text + top-N, fires the analysis. */
export default function TracePanel({ disabled, running, onRun }: Props) {
  const [taskText, setTaskText] = useState("");
  const [maxCandidates, setMaxCandidates] = useState(8);

  const canRun = !disabled && !running && taskText.trim().length > 0;

  return (
    <section>
      <h2>Gorev analizi</h2>
      <textarea
        className="task-input"
        rows={3}
        placeholder="Gorev metni girin, orn: fix login timeout in meeting webhook..."
        value={taskText}
        onChange={(e) => setTaskText(e.target.value)}
        disabled={disabled}
      />
      <div className="task-controls">
        <label className="task-topn">
          Top-N
          <select
            value={maxCandidates}
            onChange={(e) => setMaxCandidates(Number(e.target.value))}
            disabled={disabled}
          >
            {[4, 8, 12, 16].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <button
          className="primary-btn"
          disabled={!canRun}
          onClick={() => onRun(taskText.trim(), maxCandidates)}
        >
          {running ? "Calisiyor..." : "Analiz Et"}
        </button>
      </div>
      {disabled && <p className="muted">Once bir repo secin.</p>}
    </section>
  );
}
