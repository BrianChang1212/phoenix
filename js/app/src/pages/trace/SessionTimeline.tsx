import { css } from "@emotion/react";
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router";

import type {
  SessionOperation,
  SessionTimelineData,
} from "./sessionTimelineData";
import {
  buildTimelineDuration,
  timelineWindow,
  type DurationKind,
  type TimelineSegment,
} from "./sessionTimelineDuration";
import { OperationTokenUsage, SessionTokenSummary } from "./SessionTokenUsage";
import { useSessionTimelineRefresh } from "./useSessionTimelineRefresh";
import { timelineHitCandidates } from "./sessionTimelineHitTargets";

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
    height: 48px;
    min-width: 650px;
    margin: 12px 0;
  }
  .timeline-bar {
    position: absolute;
    top: 0;
    height: 48px;
    padding: 0;
    min-width: 0;
    overflow: hidden;
    box-sizing: border-box;
    border: 0;
    pointer-events: none;
    border-radius: 8px;
    color: #101010;
    background: var(--timeline-tool);
  }
  .timeline-hit-surface {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 48px;
    padding: 0;
    border: 0;
    background: transparent;
  }
  .timeline-segment-options {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 8px 0;
  }
  .timeline-segment-options button {
    flex-shrink: 0;
  }
  .timeline-bar.model {
    background: var(--timeline-model);
  }
  .timeline-bar.gap {
    background: #424242;
    color: #ddd;
  }
  .timeline-bar.other {
    background: #a8adb5;
  }
  .timeline-bar.mixed {
    background: #d6b96a;
  }
  .timeline-bar.parallel {
    background-image: repeating-linear-gradient(
      135deg,
      #ffffff22 0,
      #ffffff22 6px,
      transparent 6px,
      transparent 12px
    );
  }
  .timeline-overview {
    display: flex;
    gap: 24px;
    align-items: center;
    padding: 20px;
    border-radius: 22px;
    background: #1a1a1a;
    margin-bottom: 24px;
  }
  .timeline-overview > svg {
    flex-shrink: 0;
  }
  .timeline-total {
    display: block;
    font-size: 30px;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .timeline-legend {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 12px;
    color: #ccc;
    margin-top: 12px;
  }
  .timeline-legend i {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 4px;
    border-radius: 50%;
  }
  .timeline-ruler {
    display: flex;
    justify-content: space-between;
    min-width: 650px;
    font-size: 12px;
    color: #aaa;
  }
  .timeline-name {
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .timeline-name-icon {
    display: grid;
    place-items: center;
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    border-radius: 9px;
    color: #101010;
    background: var(--timeline-tool);
  }
  .timeline-name-icon.model {
    background: var(--timeline-model);
  }
  .timeline-name-icon.error,
  .timeline-name-icon.timeout {
    background: #ff947d;
  }
  .timeline-parallel-options {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  @media (max-width: 600px) {
    .timeline-overview {
      padding: 16px;
      gap: 12px;
    }
    .timeline-total {
      font-size: 24px;
    }
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
    max-height: 280px;
    overflow-y: auto;
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

const durationColors: Record<DurationKind, string> = {
  model: "#b6df7c",
  tool: "#76b900",
  other: "#a8adb5",
  mixed: "#d6b96a",
  gap: "#424242",
};
const durationLabels: Record<DurationKind, string> = {
  model: "模型",
  tool: "工具",
  other: "其他",
  mixed: "混合／平行",
  gap: "未觀測",
};
function OperationIcon({ kind }: { kind: DurationKind }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === "model" ? (
        <>
          <rect x="7" y="7" width="10" height="10" rx="2" />
          <path d="M9 3v4m6-4v4M9 17v4m6-4v4M3 9h4m-4 6h4m10-6h4m-4 6h4" />
        </>
      ) : kind === "mixed" ? (
        <path d="M7 3v18m10-18v18M4 6l3-3 3 3m4 12 3 3 3-3" />
      ) : kind === "gap" ? (
        <path d="M9 5v14m6-14v14" />
      ) : (
        <path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16" />
      )}
    </svg>
  );
}

type TimelineSelection =
  | { kind: "operation"; id: string }
  | { kind: "segment"; startTime: number }
  | null;

function resolveTimelineSelection({
  selection,
  segments,
  rows,
  focusedParallelId,
}: {
  selection: TimelineSelection;
  segments: TimelineSegment[];
  rows: SessionOperation[];
  focusedParallelId: string | null;
}) {
  const selectedSegment =
    selection?.kind === "segment"
      ? segments.find((segment) => segment.startTime === selection.startTime)
      : undefined;
  const visibleIds = new Set(rows.map((operation) => operation.id));
  const segmentRows = selectedSegment?.operations.filter((operation) =>
    visibleIds.has(operation.id)
  );
  const selected =
    selection?.kind === "operation"
      ? rows.find((operation) => operation.id === selection.id) ?? rows[0]
      : selectedSegment
      ? segmentRows?.[0]
      : rows[0];
  return {
    selectedSegment,
    segmentRows,
    displayedOperation:
      segmentRows?.find((operation) => operation.id === focusedParallelId) ??
      selected,
  };
}

function useObservationTime(snapshotObservedAt?: number) {
  const [initialObservedAt] = useState(() => Date.now());
  return snapshotObservedAt ?? initialObservedAt;
}

export function SessionTimeline({
  data,
  observedAt: snapshotObservedAt,
  refreshControls,
}: {
  data: SessionTimelineData;
  observedAt?: number;
  refreshControls?: ReactNode;
}) {
  const [selection, setSelection] = useState<TimelineSelection>(null);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [candidateTimes, setCandidateTimes] = useState<number[]>([]);
  const axisHelpId = useId();
  const observedAt = useObservationTime(snapshotObservedAt);
  const rawTurnIds = data.spans.flatMap((span) => {
    if (span.name !== "run_turn" && span.name !== "session_task.turn")
      return [];
    const id = [span.attributes["turn.id"], span.attributes["turn_id"]].find(
      (value): value is string =>
        typeof value === "string" && value.trim().length > 0
    );
    return id ? [id] : [];
  });
  const turns = [
    ...new Set(
      data.operations
        .flatMap((operation) => (operation.turnId ? [operation.turnId] : []))
        .concat(rawTurnIds)
    ),
  ];
  const effectiveTurnId =
    turns.find((turnId) => turnId === selectedTurnId) ?? null;
  const window = timelineWindow({ data, turnId: effectiveTurnId, observedAt });
  const timeline = buildTimelineDuration({ ...window, observedAt });
  const rows = window.operations.filter(
    (operation) =>
      (source === "all" || source === operation.source) &&
      `${operation.name} ${operation.summary ?? ""}`
        .toLowerCase()
        .includes(search.toLowerCase())
  );
  const visibleIds = new Set(rows.map((operation) => operation.id));
  const provisional = window.operations.some(
    (operation) => operation.endTime === null
  );
  const relativeSeconds = (time: number) =>
    Number(((time - timeline.startTime) / 1000).toFixed(3));
  const [focusedParallelId, setFocusedParallelId] = useState<string | null>(
    null
  );
  const { selectedSegment, segmentRows, displayedOperation } =
    resolveTimelineSelection({
      selection,
      segments: timeline.segments,
      rows,
      focusedParallelId,
    });
  const durationParts = Object.entries(timeline.totals);
  const candidates = timeline.segments.filter((segment) =>
    candidateTimes.includes(segment.startTime)
  );
  function selectSegment(segment: TimelineSegment) {
    setSelection({ kind: "segment", startTime: segment.startTime });
    setFocusedParallelId(
      segment.operations.find((operation) => visibleIds.has(operation.id))
        ?.id ?? null
    );
  }
  function segmentLabel(segment: TimelineSegment) {
    const operation = segment.operations.find((item) =>
      visibleIds.has(item.id)
    );
    return `${
      segment.operations.length > 1
        ? "平行操作"
        : segment.kind === "gap"
        ? "未觀測區間"
        : operation?.summary ?? operation?.name ?? "已篩除操作"
    } · +${relativeSeconds(segment.startTime)}–${relativeSeconds(
      segment.endTime
    )} 秒`;
  }
  let offset = 0;

  return (
    <section css={timelineCSS} aria-label="Session 操作時間軸">
      <h2>操作時間軸</h2>
      {refreshControls}
      <SessionTokenSummary data={data} turnId={effectiveTurnId} />
      <div className="timeline-overview">
        <svg
          viewBox="0 0 110 110"
          width="110"
          height="110"
          role="img"
          aria-label="耗時分布"
        >
          <circle
            cx="55"
            cy="55"
            r="43"
            fill="none"
            stroke="#303030"
            strokeWidth="10"
          />
          {durationParts.map(([kind, value]) => {
            const color = Object.entries(durationColors).find(
              ([key]) => key === kind
            )?.[1];
            const length =
              timeline.duration > 0 ? (value / timeline.duration) * 270.177 : 0;
            const arcOffset = offset;
            offset += length;
            return value > 0 ? (
              <circle
                key={kind}
                cx="55"
                cy="55"
                r="43"
                fill="none"
                stroke={color}
                strokeWidth="10"
                strokeDasharray={`${length} ${270.177 - length}`}
                strokeDashoffset={-arcOffset}
                transform="rotate(-90 55 55)"
              />
            ) : null;
          })}
          <text x="55" y="60" textAnchor="middle" fill="#f5f5f7" fontSize="18">
            {(timeline.duration / 1000).toFixed(1)}s
          </text>
        </svg>
        <div>
          <strong className="timeline-total">
            {(timeline.duration / 1000).toFixed(2)} 秒
          </strong>
          <span className="timeline-muted">
            {effectiveTurnId ? "輪次可觀測時間" : "Session 可觀測時間"}
            {provisional ? " · 暫時值" : ""}
          </span>
          <div className="timeline-legend">
            {durationParts
              .filter(([, value]) => value > 0)
              .map(([kind, value]) => (
                <span key={kind}>
                  <i
                    style={{
                      background: Object.entries(durationColors).find(
                        ([key]) => key === kind
                      )?.[1],
                    }}
                  />
                  {
                    Object.entries(durationLabels).find(
                      ([key]) => key === kind
                    )?.[1]
                  }{" "}
                  {(value / 1000).toFixed(2)}s
                </span>
              ))}
          </div>
        </div>
      </div>
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
        {turns.length ? (
          <select
            aria-label="輪次"
            value={effectiveTurnId ?? ""}
            onChange={(event) => {
              setSelectedTurnId(event.target.value || null);
              setSelection(null);
            }}
          >
            <option value="">整段 Session</option>
            {turns.map((turnId, index) => (
              <option key={turnId} value={turnId}>
                輪次 {index + 1} · {turnId}
              </option>
            ))}
          </select>
        ) : (
          <span className="timeline-muted">輪次：無資料</span>
        )}
        {effectiveTurnId && (
          <button
            type="button"
            onClick={() => {
              setSelectedTurnId(null);
              setSelection(null);
            }}
          >
            返回總覽
          </button>
        )}
        <span className="timeline-muted">{rows.length} 項操作</span>
      </div>
      {rows.length === 0 ? (
        <p role="status">沒有操作資料</p>
      ) : (
        <>
          <div
            className="timeline-scroll"
            role="region"
            aria-label="時間軸捲動區"
          >
            <div className="timeline-ruler">
              {[0, 1, 2, 3, 4].map((tick) => (
                <span key={tick}>
                  +{Number(((timeline.duration * tick) / 4000).toFixed(2))}s
                </span>
              ))}
            </div>
            <div className="timeline-axis" aria-label="單行時間軸">
              {timeline.segments.map((segment) => {
                const filtered = segment.operations.filter((operation) =>
                  visibleIds.has(operation.id)
                );
                const isParallel = segment.operations.length > 1;
                const isError = segment.operations.some(
                  (operation) =>
                    operation.status === "error" ||
                    operation.status === "timeout"
                );
                const label = `${
                  isParallel
                    ? "平行操作"
                    : segment.kind === "gap"
                    ? "未觀測區間"
                    : filtered[0]?.summary ?? filtered[0]?.name ?? "已篩除操作"
                } · +${relativeSeconds(segment.startTime)}–${relativeSeconds(
                  segment.endTime
                )} 秒`;
                return (
                  <span
                    key={segment.startTime}
                    className={`timeline-bar ${segment.kind} ${
                      isParallel ? "parallel" : ""
                    } ${isError ? "error" : ""}`}
                    style={{
                      left: `${
                        timeline.duration > 0
                          ? ((segment.startTime - timeline.startTime) /
                              timeline.duration) *
                            100
                          : 0
                      }%`,
                      width: `${
                        timeline.duration > 0
                          ? ((segment.endTime - segment.startTime) /
                              timeline.duration) *
                            100
                          : 0
                      }%`,
                      opacity:
                        segment.operations.length > 0 && !filtered.length
                          ? 0.25
                          : 1,
                    }}
                    title={label}
                    aria-label={label}
                    aria-hidden="true"
                  >
                    <OperationIcon kind={isParallel ? "mixed" : segment.kind} />
                  </span>
                );
              })}
              <button
                type="button"
                className="timeline-hit-surface"
                aria-label="選擇時間軸區段"
                aria-describedby={axisHelpId}
                onClick={(event) => {
                  if (event.detail === 0) {
                    const segment = selectedSegment ?? timeline.segments[0];
                    if (segment) {
                      setCandidateTimes([segment.startTime]);
                      selectSegment(segment);
                    }
                    return;
                  }
                  const bounds = event.currentTarget.getBoundingClientRect();
                  const nearby = timelineHitCandidates({
                    segments: timeline.segments,
                    startTime: timeline.startTime,
                    duration: timeline.duration,
                    width: bounds.width,
                    pointerX: event.clientX - bounds.left,
                  });
                  setCandidateTimes(nearby.map((segment) => segment.startTime));
                  if (nearby.length === 1) selectSegment(nearby[0]);
                }}
                onKeyDown={(event) => {
                  if (
                    ![
                      "ArrowLeft",
                      "ArrowRight",
                      "Home",
                      "End",
                      "Enter",
                    ].includes(event.key)
                  )
                    return;
                  event.preventDefault();
                  const current = timeline.segments.findIndex(
                    (segment) => segment === selectedSegment
                  );
                  const index =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                      ? timeline.segments.length - 1
                      : event.key === "ArrowLeft"
                      ? Math.max(0, current - 1)
                      : event.key === "ArrowRight"
                      ? Math.min(timeline.segments.length - 1, current + 1)
                      : Math.max(0, current);
                  const segment = timeline.segments[index];
                  if (segment) {
                    setCandidateTimes([segment.startTime]);
                    selectSegment(segment);
                  }
                }}
              />
            </div>
          </div>
          <p id={axisHelpId} className="timeline-muted" aria-live="polite">
            點選時間軸選擇附近區段；左右方向鍵逐段選擇。
            {selectedSegment ? ` ${segmentLabel(selectedSegment)}` : ""}
          </p>
          {candidates.length > 0 && (
            <nav className="timeline-segment-options" aria-label="密集區段選擇">
              {candidates.map((segment) => (
                <button
                  key={segment.startTime}
                  type="button"
                  aria-pressed={selectedSegment === segment}
                  onClick={() => selectSegment(segment)}
                >
                  {segmentLabel(segment)}
                </button>
              ))}
            </nav>
          )}
          <div className="timeline-names">
            {rows.map((operation, index) => (
              <button
                key={operation.id}
                type="button"
                className="timeline-name"
                aria-pressed={displayedOperation?.id === operation.id}
                onClick={() => {
                  setSelection({ kind: "operation", id: operation.id });
                  setFocusedParallelId(null);
                }}
              >
                <span
                  className={`timeline-name-icon ${operation.kind} ${operation.status}`}
                >
                  <OperationIcon kind={operation.kind} />
                </span>
                <span>
                  {index + 1} · {operation.summary ?? operation.name}
                  <small>
                    {((operation.endTime ??
                      Math.max(observedAt, operation.startTime)) -
                      operation.startTime) /
                      1000}{" "}
                    秒 · {operationStatusLabels[operation.status]}
                    {operation.endTime === null ? " · 暫時值" : ""}
                  </small>
                </span>
              </button>
            ))}
          </div>
          {selectedSegment && selectedSegment.operations.length > 1 && (
            <nav
              className="timeline-parallel-options"
              aria-label="平行操作選擇"
            >
              {segmentRows?.map((operation) => (
                <button
                  type="button"
                  key={operation.id}
                  aria-pressed={displayedOperation?.id === operation.id}
                  onClick={() => setFocusedParallelId(operation.id)}
                >
                  {operation.summary ?? operation.name}
                </button>
              ))}
            </nav>
          )}
          {displayedOperation ? (
            <section
              className="timeline-detail"
              aria-label="操作詳細資訊"
              aria-live="polite"
            >
              <h3>{displayedOperation.summary ?? displayedOperation.name}</h3>
              <OperationTokenUsage operation={displayedOperation} />
              {!displayedOperation.summary && (
                <p className="timeline-muted">
                  摘要不可用 · {displayedOperation.name}
                </p>
              )}
              {selectedSegment && (
                <p className="timeline-muted">
                  選取區間 +{relativeSeconds(selectedSegment.startTime)}–
                  {relativeSeconds(selectedSegment.endTime)} 秒 ·
                  下方為操作完整耗時
                </p>
              )}
              <dl>
                <div>
                  <dt>耗時</dt>
                  <dd>
                    {(
                      ((displayedOperation.endTime ??
                        Math.max(observedAt, displayedOperation.startTime)) -
                        displayedOperation.startTime) /
                      1000
                    ).toFixed(2)}{" "}
                    秒{displayedOperation.endTime === null ? "（暫時值）" : ""}
                  </dd>
                </div>
                <div>
                  <dt>狀態</dt>
                  <dd>{operationStatusLabels[displayedOperation.status]}</dd>
                </div>
                <div>
                  <dt>來源</dt>
                  <dd>{displayedOperation.source}</dd>
                </div>
              </dl>
              <p className="timeline-muted">
                {new Date(displayedOperation.startTime).toISOString()} →{" "}
                {displayedOperation.endTime === null
                  ? "進行中"
                  : new Date(displayedOperation.endTime).toISOString()}
              </p>
              <Link
                to={`/projects/${encodeURIComponent(
                  data.projectId
                )}/traces/${encodeURIComponent(
                  displayedOperation.traceId
                )}?selectedSpanNodeId=${encodeURIComponent(
                  displayedOperation.id
                )}`}
              >
                開啟原始 Trace
              </Link>
              <details>
                <summary>技術細節</summary>
                <pre>
                  {JSON.stringify(
                    {
                      spanId: displayedOperation.spanId,
                      traceId: displayedOperation.traceId,
                      attributes: displayedOperation.attributes,
                    },
                    null,
                    2
                  )}
                </pre>
              </details>
            </section>
          ) : selectedSegment ? (
            <section className="timeline-detail" aria-label="區間詳細資訊">
              <h3>
                {selectedSegment.kind === "gap" ? "未觀測區間" : "已篩除操作"}
              </h3>
              <p>
                {(
                  (selectedSegment.endTime - selectedSegment.startTime) /
                  1000
                ).toFixed(2)}{" "}
                秒
              </p>
              <span className="timeline-muted">
                沒有可顯示的操作；不推定為模型運算。
              </span>
            </section>
          ) : null}
        </>
      )}
    </section>
  );
}

export function SessionTimelineView({ sessionId }: { sessionId: string }) {
  return <SessionTimelineLoader key={sessionId} sessionId={sessionId} />;
}

function SessionTimelineLoader({ sessionId }: { sessionId: string }) {
  const { state, mode, toggleMode, retry } = useSessionTimelineRefresh({
    sessionId,
  });
  const controls = (
    <>
      <div className="timeline-controls" aria-label="時間軸更新控制">
        <button type="button" onClick={toggleMode}>
          {mode === "live" ? "暫停更新" : "恢復更新"}
        </button>
        <span className="timeline-muted">
          {mode === "live" ? "自動更新 · 每 5 秒" : "已暫停"}
        </span>
        {state.kind === "ready" && (
          <time
            className="timeline-muted"
            dateTime={new Date(state.snapshot.observedAt).toISOString()}
          >
            上次更新 {new Date(state.snapshot.observedAt).toLocaleTimeString()}
          </time>
        )}
      </div>
      {state.kind === "ready" && state.refreshError !== null && (
        <p role="alert">
          更新失敗，保留上次資料（可能過期）：{state.refreshError}{" "}
          <button type="button" onClick={retry} disabled={mode === "paused"}>
            重試
          </button>
        </p>
      )}
    </>
  );
  if (state.kind === "ready") {
    return (
      <SessionTimeline
        data={state.snapshot.data}
        observedAt={state.snapshot.observedAt}
        refreshControls={controls}
      />
    );
  }
  return (
    <section css={timelineCSS}>
      {controls}
      <p role={state.kind === "error" ? "alert" : "status"}>
        {state.kind === "loading" ? "讀取操作資料…" : state.message}
      </p>
      {state.kind === "error" && (
        <button type="button" onClick={retry} disabled={mode === "paused"}>
          重試
        </button>
      )}
    </section>
  );
}
