import { useEffect, useMemo, useState } from "react";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import java from "highlight.js/lib/languages/java";
import csharp from "highlight.js/lib/languages/csharp";
import go from "highlight.js/lib/languages/go";
import "highlight.js/styles/atom-one-dark.css";
import type { UINeighbors, UINode } from "../api";
import { api } from "../api";
import { edgeColor, nodeColor } from "../theme";

hljs.registerLanguage("python", python);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("java", java);
hljs.registerLanguage("csharp", csharp);
hljs.registerLanguage("go", go);

interface Props {
  gid: string;
  onClose: () => void;
  onFocusNode: (gid: string) => void;
}

const LANG_BY_ID_PREFIX: Record<string, string> = {
  py: "python",
  js: "javascript",
  ts: "typescript",
  java: "java",
  cs: "csharp",
  go: "go",
};

function guessLanguage(node: UINode): string | null {
  const lang = node.properties.language;
  if (typeof lang === "string" && lang) return LANG_BY_ID_PREFIX[lang] ?? lang;
  const prefix = node.id.split("::", 1)[0];
  return LANG_BY_ID_PREFIX[prefix] ?? null;
}

export default function DetailPanel({ gid, onClose, onFocusNode }: Props) {
  const [node, setNode] = useState<UINode | null>(null);
  const [neighbors, setNeighbors] = useState<UINeighbors | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setNode(null);
    setNeighbors(null);
    setError(null);
    api
      .node(gid)
      .then((n) => !cancelled && setNode(n))
      .catch((e) => !cancelled && setError(String(e)));
    api
      .neighbors(gid)
      .then((n) => !cancelled && setNeighbors(n))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [gid]);

  const body = typeof node?.properties.body === "string" ? node.properties.body : null;
  const highlighted = useMemo(() => {
    if (!body || !node) return null;
    const lang = guessLanguage(node);
    try {
      return lang
        ? hljs.highlight(body, { language: lang }).value
        : hljs.highlightAuto(body).value;
    } catch {
      return null;
    }
  }, [body, node]);

  const infoRows: [string, unknown][] = node
    ? (
        [
          ["kind", node.properties.kind],
          ["signature", node.properties.signature],
          ["file", node.properties.file_id ?? node.properties.path],
          ["line", node.properties.line],
          ["namespace", node.properties.namespace],
          ["visibility", node.properties.visibility],
          ["language", node.properties.language],
          ["http", node.properties.http_method],
          ["route", node.properties.path_pattern],
          ["commit", node.properties.indexed_at_commit],
        ] as [string, unknown][]
      ).filter(([, v]) => v !== undefined && v !== null && v !== "")
    : [];

  return (
    <aside className="detail-panel">
      <header>
        <div>
          {node && (
            <span className="chip" style={{ borderColor: nodeColor(node.label) }}>
              {node.label}
            </span>
          )}
          <h2 title={gid}>{node?.display ?? "..."}</h2>
        </div>
        <button className="icon-btn" onClick={onClose} title="Kapat">
          x
        </button>
      </header>

      {error && <p className="error">{error}</p>}

      {node && (
        <div className="detail-body">
          <dl className="info-grid">
            {infoRows.map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd className={key === "signature" || key === "file" ? "mono" : undefined}>
                  {String(value)}
                </dd>
              </div>
            ))}
          </dl>

          {typeof node.properties.docstring === "string" && node.properties.docstring && (
            <section>
              <h3>Docstring</h3>
              <p className="docstring">{node.properties.docstring}</p>
            </section>
          )}

          {typeof node.properties.text === "string" && node.properties.text && (
            <section>
              <h3>Not</h3>
              <p className="docstring">{node.properties.text}</p>
            </section>
          )}

          {body && (
            <section>
              <h3>Kod</h3>
              <pre className="code-block">
                {highlighted ? (
                  <code dangerouslySetInnerHTML={{ __html: highlighted }} />
                ) : (
                  <code>{body}</code>
                )}
              </pre>
            </section>
          )}

          {neighbors && neighbors.edges.length > 0 && (
            <section>
              <h3>Baglantilar ({neighbors.edges.length})</h3>
              <ul className="neighbor-list">
                {neighbors.edges.slice(0, 60).map((edge) => {
                  const isOut = edge.source === gid;
                  const otherId = isOut ? edge.target : edge.source;
                  const other = neighbors.nodes.find((n) => n.id === otherId);
                  return (
                    <li key={edge.id}>
                      <button onClick={() => onFocusNode(otherId)} title={otherId}>
                        <span
                          className="edge-chip"
                          style={{ background: edgeColor(edge.type) }}
                        >
                          {isOut ? "->" : "<-"} {edge.type}
                        </span>
                        <span className="result-name">{other?.display ?? otherId}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>
      )}
    </aside>
  );
}
