import { css } from "@emotion/react";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import {
  loadSessionTimeline,
  type SessionOperation,
  type SessionTimelineData,
} from "./sessionTimelineData";
import { OperationTokenUsage, SessionTokenSummary } from "./SessionTokenUsage";

const timelineCSS = css`
  padding: 24px;
  overflow: auto;
  background: #101010;
  color: #f5f5f7;
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --timeline-model: #b6df7c;
  --timeline-tool: #76b900;
  h2 {
    font-size: 20px;
    margin: 0 0 16px;
  }
  button,
  input,
  select {
    font: inherit;
    border: 1px solid #444;
    border-radius: 12px;
    padding: 10px 12px;
    color: inherit;
    background: #252525;
  }
  button {
    cursor: pointer;
  }
  button:focus-visible,
  input:focus-visible,
  select:focus-visible {
    outline: 3px solid #b6df7c;
    outline-offset: 3px;
  }
  .timeline-controls {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .timeline-scroll {
    overflow-x: auto;
    padding: 5px;
  }
  .timeline-axis {
    position: relative;
    height: 70px;
    min-width: 650px;
    margin: 20px 0;
  }
  .timeline-bar {
    position: absolute;
    top: 0;
    height: 48px;
    padding: 0;
    min-width: 2px;
    border: 2px solid #101010;
    border-radius: 8px;
    color: #101010;
    background: var(--timeline-tool);
  }
  .timeline-bar.model {
    background: var(--timeline-model);
  }
  .timeline-bar.error,
  .timeline-bar.timeout {
    background: #ff947d;
  }
  [aria-pressed="true"] {
    outline: 2px solid #d7f6b1;
    outline-offset: 1px;
  }
  .timeline-names {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;
    margin: 20px 0;
  }
  .timeline-name {
    text-align: left;
    overflow-wrap: anywhere;
    white-space: normal;
  }
  .timeline-name small {
    display: block;
    color: #b5b5b5;
    margin-top: 5px;
  }
  .timeline-detail {
    padding: 24px;
    border-radius: 20px;
    border: 1px solid #3a482c;
    background: #1a1a1a;
  }
  .timeline-detail h3 {
    margin: 0 0 16px;
    overflow-wrap: anywhere;
  }
  .timeline-detail dl {
    display: flex;
    gap: 24px;
    flex-wrap: wrap;
  }
  .timeline-detail dt {
    font-size: 12px;
    color: #aaa;
  }
  .timeline-detail dd {
    margin: 4px 0;
  }
  .timeline-detail a {
    color: #b6df7c;
  }
  .timeline-detail pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 300px;
    overflow: auto;
    font-size: 12px;
  }
  .timeline-muted {
    color: #b5b5b5;
    font-size: 13px;
  }
`;

export const operationStatusLabels: Record<SessionOperation["status"], string> =
  {
    success: "成功",
    error: "失敗",
    timeout: "逾時",
    running: "進行中",
    unknown: "未知",
  };

export function SessionTimeline({ data }: { data: SessionTimelineData }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [observedAt] = useState(() => Date.now());
  const rows = data.operations.filter(
    (operation) =>
      (source === "all" || source === operation.source) &&
      `${operation.name} ${operation.summary ?? ""}`
        .toLowerCase()
        .includes(search.toLowerCase())
  );
  const selected =
    rows.find((operation) => operation.id === selectedId) ?? rows[0];
  const start = Math.min(
    ...data.operations.map((operation) => operation.startTime)
  );
  const end = Math.max(
    ...data.operations.map((operation) => operation.endTime ?? observedAt)
  );
  const duration = Math.max(1, end - start);

  return (
    <section css={timelineCSS} aria-label="Session 操作時間軸">
      <h2>操作時間軸</h2>
      <SessionTokenSummary data={data} />
      <div className="timeline-controls">
        <input
          aria-label="搜尋操作"
          placeholder="搜尋操作"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="操作來源"
          value={source}
          onChange={(event) => setSource(event.target.value)}
        >
          <option value="all">全部來源</option>
          <option value="codex">Codex</option>
          <option value="kiro">Kiro</option>
          <option value="unknown">未知來源</option>
        </select>
        <span className="timeline-muted">{rows.length} 項操作</span>
      </div>
      {rows.length === 0 ? (
        <p role="status">沒有操作資料</p>
      ) : (
        <>
          <div className="timeline-scroll">
            <div className="timeline-axis" aria-label="單行時間軸">
              {rows.map((operation, index) => (
                <button
                  key={operation.id}
                  type="button"
                  className={`timeline-bar ${operation.kind} ${operation.status}`}
                  style={{
                    left: `${((operation.startTime - start) / duration) * 100}%`,
                    width: `${(Math.max(0, (operation.endTime ?? end) - operation.startTime) / duration) * 100}%`,
                  }}
                  title={operation.summary ?? operation.name}
                  aria-label={`${index + 1} · ${operation.summary ?? operation.name}`}
                  aria-pressed={selected?.id === operation.id}
                  onClick={() => setSelectedId(operation.id)}
                >
                  {index + 1}
                </button>
              ))}
            </div>
          </div>
          <div className="timeline-names">
            {rows.map((operation, index) => (
              <button
                key={operation.id}
                type="button"
                className="timeline-name"
                aria-pressed={selected?.id === operation.id}
                onClick={() => setSelectedId(operation.id)}
              >
                {index + 1} · {operation.summary ?? operation.name}
                <small>
                  {((operation.endTime ?? end) - operation.startTime) / 1000} 秒
                  · {operationStatusLabels[operation.status]}
                </small>
              </button>
            ))}
          </div>
          {selected && (
            <section
              className="timeline-detail"
              aria-label="操作詳細資訊"
              aria-live="polite"
            >
              <h3>{selected.summary ?? selected.name}</h3>
              <OperationTokenUsage operation={selected} />
              {!selected.summary && (
                <p className="timeline-muted">摘要不可用 · {selected.name}</p>
              )}
              <dl>
                <div>
                  <dt>耗時</dt>
                  <dd>
                    {(
                      ((selected.endTime ?? end) - selected.startTime) /
                      1000
                    ).toFixed(2)}{" "}
                    秒{selected.endTime == null ? "（暫時值）" : ""}
                  </dd>
                </div>
                <div>
                  <dt>狀態</dt>
                  <dd>{operationStatusLabels[selected.status]}</dd>
                </div>
                <div>
                  <dt>來源</dt>
                  <dd>{selected.source}</dd>
                </div>
              </dl>
              <p className="timeline-muted">
                {new Date(selected.startTime).toISOString()} →{" "}
                {selected.endTime == null
                  ? "進行中"
                  : new Date(selected.endTime).toISOString()}
              </p>
              <Link
                to={`/projects/${encodeURIComponent(data.projectId)}/traces/${encodeURIComponent(selected.traceId)}?selectedSpanNodeId=${encodeURIComponent(selected.id)}`}
              >
                開啟原始 Trace
              </Link>
              <details>
                <summary>技術細節</summary>
                <pre>
                  {JSON.stringify(
                    {
                      spanId: selected.spanId,
                      traceId: selected.traceId,
                      attributes: selected.attributes,
                    },
                    null,
                    2
                  )}
                </pre>
              </details>
            </section>
          )}
        </>
      )}
    </section>
  );
}

export function SessionTimelineView({ sessionId }: { sessionId: string }) {
  return <SessionTimelineLoader key={sessionId} sessionId={sessionId} />;
}

function SessionTimelineLoader({ sessionId }: { sessionId: string }) {
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "loaded"; data: SessionTimelineData }
    | { kind: "error"; message: string }
  >({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    loadSessionTimeline({ sessionId, signal: controller.signal }).then(
      (data) => {
        if (!controller.signal.aborted) setState({ kind: "loaded", data });
      },
      (error: unknown) => {
        if (!controller.signal.aborted)
          setState({
            kind: "error",
            message: error instanceof Error ? error.message : "讀取失敗",
          });
      }
    );
    return () => controller.abort();
  }, [sessionId, attempt]);
  if (state.kind === "loaded")
    return <SessionTimeline key={sessionId} data={state.data} />;
  return (
    <section css={timelineCSS}>
      <p role={state.kind === "error" ? "alert" : "status"}>
        {state.kind === "loading" ? "讀取操作資料…" : state.message}
      </p>
      {state.kind === "error" && (
        <button
          type="button"
          onClick={() => {
            setState({ kind: "loading" });
            setAttempt((value) => value + 1);
          }}
        >
          重試
        </button>
      )}
    </section>
  );
}
