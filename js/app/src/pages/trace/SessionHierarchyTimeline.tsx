import { css } from "@emotion/react";
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";

import {
  buildSessionHierarchy,
  hierarchyCategory,
  hierarchyCategoryStyle,
  visibleHierarchyRows,
  type HierarchyNode,
} from "./sessionHierarchy";
import { SessionHierarchyNavigator } from "./SessionHierarchyNavigator";
import type { SessionTimelineData } from "./sessionTimelineData";
import { useHierarchyNavigation } from "./useHierarchyNavigation";

const hierarchyCSS = css`
  --hierarchy-accent: #92ed20;
  min-height: 100%;
  padding: 24px;
  color: #e4f3dc;
  background-color: #050d07;
  background-image:
    linear-gradient(#16321a30 1px, transparent 1px),
    linear-gradient(90deg, #16321a30 1px, transparent 1px);
  background-size: 32px 32px;
  font:
    13px/1.6 Consolas,
    "Cascadia Mono",
    "Microsoft JhengHei",
    monospace;
  box-sizing: border-box;
  overflow: auto;
  h2 {
    font-size: 22px;
    margin: 0 0 5px;
  }
  button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    border: 1px solid #345b36;
    border-radius: 5px;
    background: #152b18;
    padding: 7px 11px;
  }
  button:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--hierarchy-accent);
    outline-offset: 2px;
  }
  .hierarchy-muted {
    color: #a7be9c;
    font-size: 11px;
  }
  .hierarchy-controls,
  .hierarchy-legend,
  .hierarchy-result {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .hierarchy-controls {
    margin: 16px 0 12px;
  }
  .hierarchy-controls button:first-child {
    color: var(--hierarchy-accent);
  }
  .hierarchy-panel {
    position: relative;
    touch-action: none;
    border: 1px solid #284c2c;
    border-radius: 6px;
    background: #08140a;
    overflow: hidden;
  }
  .hierarchy-panel:focus-visible {
    outline: 2px solid var(--hierarchy-accent);
    outline-offset: 2px;
  }
  .hierarchy-ruler,
  .hierarchy-row {
    display: grid;
    grid-template-columns: minmax(160px, 30%) minmax(0, 1fr);
    align-items: center;
  }
  .hierarchy-ruler {
    background: #102513;
    padding: 9px 12px;
    color: #a7be9c;
    font-size: 11px;
  }
  .hierarchy-ticks {
    display: flex;
    justify-content: space-between;
  }
  .hierarchy-scroll {
    max-height: 520px;
    overflow: auto;
    scrollbar-color: #345b36 #08140a;
  }
  .hierarchy-row {
    min-height: 34px;
    padding: 0 12px;
  }
  .hierarchy-row:nth-child(even) {
    background: #0c1b0e;
  }
  .hierarchy-row[aria-selected="true"] {
    background: #193a16;
  }
  .hierarchy-name {
    display: flex;
    align-items: center;
    min-width: 0;
    gap: 3px;
  }
  .hierarchy-name button {
    padding: 3px;
    border: 0;
    background: transparent;
    min-width: 22px;
    text-align: left;
  }
  .hierarchy-name .hierarchy-name-select {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .hierarchy-track {
    position: relative;
    height: 34px;
    border-left: 1px solid #2d5831;
    background-image: linear-gradient(90deg, #1c3821 1px, transparent 1px);
    background-size: 25% 100%;
    overflow: hidden;
  }
  .hierarchy-track button {
    position: absolute;
    top: 8px;
    height: 18px;
    min-width: 2px;
    padding: 0;
    border: 0;
    border-radius: 3px;
    color: #071209;
    overflow: hidden;
    text-align: left;
    font-size: 11px;
  }
  .hierarchy-track button[aria-pressed="true"] {
    outline: 1px solid #e4f3dc;
    outline-offset: 3px;
  }
  .hierarchy-track button.error {
    box-shadow: 0 0 0 2px #fc8e83;
  }
  .hierarchy-track button span {
    padding: 0 5px;
    white-space: nowrap;
  }
  .hierarchy-boundary {
    color: #ffc14a;
    margin: 6px 0;
    font-size: 11px;
  }
  .hierarchy-legend {
    padding: 10px 12px;
    border-top: 1px solid #284c2c;
    font-size: 11px;
  }
  .hierarchy-legend i {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 5px;
    border-radius: 2px;
  }
  .hierarchy-detail {
    border: 1px solid #284c2c;
    border-radius: 5px;
    margin-top: 12px;
    padding: 12px 16px;
    background: #0b1c0d;
  }
  .hierarchy-result {
    font-size: 12px;
  }
  .hierarchy-result strong {
    overflow-wrap: anywhere;
  }
  .hierarchy-result button {
    margin-left: auto;
    padding: 0 5px;
    background: none;
    border: 0;
  }
  .hierarchy-result .hierarchy-status {
    color: #ffc14a;
  }
  .hierarchy-detail details {
    border-top: 1px solid #284c2c;
    padding-top: 7px;
    margin-top: 10px;
  }
  summary {
    cursor: pointer;
  }
  pre {
    overflow: auto;
    max-height: 220px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: inherit;
    font-size: 11px;
  }
  a {
    color: #92ed20;
  }
  .hierarchy-analysis {
    border-top: 1px solid #284c2c;
    margin-top: 18px;
    padding-top: 12px;
  }
  @media (max-width: 650px) {
    padding: 16px 12px;
    .hierarchy-ruler,
    .hierarchy-row {
      grid-template-columns: minmax(115px, 40%) minmax(0, 1fr);
      padding-left: 6px;
      padding-right: 6px;
    }
    .hierarchy-name {
      font-size: 11px;
    }
  }
`;

function duration(node: HierarchyNode, observedAt: number) {
  return Math.max(
    0,
    (node.endTime ?? Math.max(node.startTime, observedAt)) - node.startTime
  );
}

function status(node: HierarchyNode) {
  if (node.endTime === null) return "進行中 · 暫時耗時";
  if (node.span.status_code === "ERROR") return "ERROR · 失敗";
  if (node.span.status_code === "OK") return "OK · 成功";
  return "UNSET · 未知";
}

export function SessionHierarchyTimeline({
  data,
  observedAt,
  refreshControls,
  analysis,
}: {
  data: SessionTimelineData;
  observedAt: number;
  refreshControls?: ReactNode;
  analysis: ReactNode;
}) {
  const hierarchy = useMemo(() => buildSessionHierarchy(data), [data]);
  const [expanded, setExpanded] = useState(
    () =>
      new Set(
        hierarchy.nodes.filter((node) => node.depth < 2).map((node) => node.key)
      )
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [rawOpen, setRawOpen] = useState(false);
  const rows = visibleHierarchyRows(hierarchy, expanded);
  const selected = hierarchy.nodes.find((node) => node.key === selectedKey);
  const startTime = hierarchy.nodes.length
    ? Math.min(...hierarchy.nodes.map((node) => node.startTime))
    : observedAt;
  const endTime = hierarchy.nodes.length
    ? Math.max(
        ...hierarchy.nodes.map(
          (node) => node.endTime ?? Math.max(node.startTime, observedAt)
        )
      )
    : observedAt;
  const total = Math.max(1, endTime - startTime);
  const navigation = useHierarchyNavigation(
    { start: startTime, end: startTime + total },
    () => setSelectedKey(null)
  );
  const viewport = navigation.view;
  const viewportTotal = viewport.end - viewport.start;
  const missing = hierarchy.nodes.filter(
    (node) => node.parentState === "missing"
  ).length;
  function select(node: HierarchyNode) {
    setSelectedKey(node.key);
    setRawOpen(false);
  }
  function toggle(node: HierarchyNode) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(node.key)) next.delete(node.key);
      else next.add(node.key);
      return next;
    });
  }
  return (
    <section css={hierarchyCSS} aria-label="階層時間軸">
      <h2>階層時間軸</h2>
      <div className="hierarchy-muted">
        {data.source.toUpperCase()} · {hierarchy.nodes.length} spans ·
        Y：parent_id 父子階層
      </div>
      {refreshControls}
      {missing > 0 && (
        <p className="hierarchy-boundary">上層未錄入 · {missing} 個分支</p>
      )}
      {hierarchy.invalidCount > 0 && (
        <p role="alert">
          資料關係異常 · {hierarchy.invalidCount}{" "}
          個節點；未將異常父子關係當成正常呼叫鏈。
        </p>
      )}
      <SessionHierarchyNavigator
        navigation={navigation}
        nodes={hierarchy.nodes}
        observedAt={observedAt}
      />
      <div className="hierarchy-controls">
        <button
          type="button"
          onClick={() =>
            setExpanded(new Set(hierarchy.nodes.map((node) => node.key)))
          }
        >
          全部展開
        </button>
        <button type="button" onClick={() => setExpanded(new Set())}>
          全部收合
        </button>
        <span className="hierarchy-muted">
          X：真實起訖時間；內部呼叫不等於業務步驟
        </span>
      </div>
      <div
        className="hierarchy-panel"
        ref={navigation.plotRef}
        tabIndex={0}
        aria-label="時間軸導覽區"
        onKeyDown={navigation.keyDown}
        {...navigation.plotEvents}
      >
        {navigation.brush?.surface === "plot" && (
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              zIndex: 2,
              pointerEvents: "none",
              top: 0,
              bottom: 0,
              left: navigation.brush.left,
              width: navigation.brush.width,
              background: "#b1ec7b22",
              border: "1px solid #b1ec7b",
            }}
          />
        )}
        <div className="hierarchy-ruler">
          <span>父子呼叫</span>
          <div className="hierarchy-ticks">
            {[0, 1, 2, 3, 4].map((tick) => (
              <span key={tick}>
                {(
                  (viewport.start - startTime + (viewportTotal * tick) / 4) /
                  1000
                ).toFixed(3)}
                s
              </span>
            ))}
          </div>
        </div>
        <div className="hierarchy-scroll" role="tree" aria-label="原始呼叫階層">
          {rows.map((node) => {
            const category = hierarchyCategoryStyle[hierarchyCategory(node)];
            const name = node.operation?.summary ?? node.span.name;
            const hasChildren = node.children.length > 0;
            const nodeEnd =
              node.endTime ?? Math.max(node.startTime, observedAt);
            const left = Math.max(viewport.start, node.startTime);
            const right = Math.min(viewport.end, nodeEnd);
            const width = Math.max(0, ((right - left) / viewportTotal) * 100);
            return (
              <div
                key={node.key}
                className="hierarchy-row"
                role="treeitem"
                aria-label={node.span.name}
                aria-level={node.depth + 1}
                aria-expanded={hasChildren ? expanded.has(node.key) : undefined}
                aria-selected={selectedKey === node.key}
              >
                <div
                  className="hierarchy-name"
                  style={{ paddingLeft: Math.min(node.depth * 13, 130) }}
                >
                  {hasChildren ? (
                    <button
                      type="button"
                      aria-label={`${expanded.has(node.key) ? "收合" : "展開"} ${node.span.name}`}
                      aria-expanded={expanded.has(node.key)}
                      onClick={() => toggle(node)}
                    >
                      {expanded.has(node.key) ? "▾" : "▸"}
                    </button>
                  ) : (
                    <span aria-hidden="true">·</span>
                  )}
                  <button
                    className="hierarchy-name-select"
                    type="button"
                    aria-label={`選取 ${node.span.name}`}
                    title={name}
                    onClick={() => select(node)}
                  >
                    {node.parentState === "missing" ? "↳ " : ""}
                    {name}
                  </button>
                </div>
                <div className="hierarchy-track">
                  {right >= left && (
                    <button
                      type="button"
                      aria-label={`時間條 ${node.span.name}`}
                      aria-pressed={selectedKey === node.key}
                      title={`${name} · ${category.label} · ${status(node)}`}
                      className={
                        node.span.status_code === "ERROR" ? "error" : undefined
                      }
                      style={{
                        left: `${((left - viewport.start) / viewportTotal) * 100}%`,
                        width: `${width}%`,
                        background: category.color,
                      }}
                      onClick={() => select(node)}
                    >
                      {width > 20 && <span>{name}</span>}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
          {rows.length === 0 && <p role="status">沒有已錄入的呼叫節點</p>}
        </div>
        <div className="hierarchy-legend">
          <span>
            {rows.length} / {hierarchy.nodes.length} spans
          </span>
          {Object.values(hierarchyCategoryStyle).map((category) => (
            <span key={category.label}>
              <i style={{ background: category.color }} />
              {category.label}
            </span>
          ))}
          <span>錯誤：紅框及文字；狀態點選查看</span>
        </div>
      </div>
      {selected && (
        <section
          className="hierarchy-detail"
          aria-label="選取的呼叫結果"
          aria-live="polite"
        >
          <div className="hierarchy-result">
            <strong>{selected.operation?.summary ?? selected.span.name}</strong>
            <span className="hierarchy-status">{status(selected)}</span>
            <span>{(duration(selected, observedAt) / 1000).toFixed(3)} 秒</span>
            <button
              type="button"
              aria-label="關閉呼叫結果"
              onClick={() => setSelectedKey(null)}
            >
              ×
            </button>
          </div>
          <details
            open={rawOpen}
            onToggle={(event) => setRawOpen(event.currentTarget.open)}
          >
            <summary>查看原始資料</summary>
            <p>
              {selected.span.start_time} → {selected.span.end_time ?? "進行中"}
            </p>
            <Link
              to={`/projects/${encodeURIComponent(data.projectId)}/traces/${encodeURIComponent(selected.span.context.trace_id)}?selectedSpanNodeId=${encodeURIComponent(selected.span.id)}`}
            >
              開啟原始 Trace
            </Link>
            <pre>
              {JSON.stringify(
                {
                  spanId: selected.span.context.span_id,
                  parentId: selected.span.parent_id ?? null,
                  traceId: selected.span.context.trace_id,
                  status: selected.span.status_code,
                  attributes: selected.span.attributes,
                },
                null,
                2
              )}
            </pre>
          </details>
        </section>
      )}
      <details className="hierarchy-analysis" aria-label="用量與輪次分析">
        <summary>用量與輪次分析</summary>
        {analysis}
      </details>
    </section>
  );
}
