import type { PlaybackStep } from "../trace/tracePlayback";

interface Props {
  steps: PlaybackStep[];
  index: number;
  playing: boolean;
  speed: number;
  onSeek: (index: number) => void;
  onTogglePlay: () => void;
  onSpeed: (speed: number) => void;
  onClose: () => void;
}

/** Bottom overlay: play/pause, step back/forward, stage timeline and speed control. */
export default function TracePlayer({
  steps,
  index,
  playing,
  speed,
  onSeek,
  onTogglePlay,
  onSpeed,
  onClose,
}: Props) {
  const step = steps[index];
  if (!step) return null;

  return (
    <div className="trace-player">
      <div className="trace-player-info">
        <strong>{step.title}</strong>
        <span>{step.subtitle}</span>
      </div>

      <div className="trace-player-controls">
        <button
          className="icon-btn"
          onClick={() => onSeek(Math.max(index - 1, 0))}
          disabled={index === 0}
          title="Onceki adim"
        >
          {"|<"}
        </button>
        <button className="icon-btn play-btn" onClick={onTogglePlay} title="Oynat / duraklat">
          {playing ? "||" : ">"}
        </button>
        <button
          className="icon-btn"
          onClick={() => onSeek(Math.min(index + 1, steps.length - 1))}
          disabled={index === steps.length - 1}
          title="Sonraki adim"
        >
          {">|"}
        </button>

        <div className="trace-timeline">
          {steps.map((s, i) => (
            <button
              key={s.id}
              className={`timeline-dot ${i === index ? "active" : ""} ${i < index ? "done" : ""}`}
              title={s.title}
              onClick={() => onSeek(i)}
            />
          ))}
        </div>

        <select
          className="speed-select"
          value={speed}
          onChange={(e) => onSpeed(Number(e.target.value))}
          title="Oynatma hizi"
        >
          <option value={0.5}>0.5x</option>
          <option value={1}>1x</option>
          <option value={2}>2x</option>
        </select>

        <button className="icon-btn" onClick={onClose} title="Analizi kapat">
          x
        </button>
      </div>
    </div>
  );
}
