import { css } from "@emotion/react";

import type {
  SessionOperation,
  SessionTimelineData,
} from "./sessionTimelineData";
import {
  getOperationTokenUsage,
  getSessionTokenUsage,
  type TokenUsage,
} from "./sessionTokenUsageData";

const usageCSS = css`
  padding: 18px 20px;
  margin: 16px 0;
  border-radius: 20px;
  background: #202020;
  .token-heading {
    color: #b5b5b5;
    font-size: 13px;
    margin-bottom: 8px;
  }
  .token-total {
    font-size: 28px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .token-labels {
    font-size: 13px;
    color: #c7c7c7;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 8px;
  }
  .token-ratio {
    height: 12px;
    background: #414141;
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    margin: 12px 0;
  }
  .token-ratio span {
    height: 100%;
  }
  details {
    margin-top: 12px;
    font-size: 12px;
    color: #b5b5b5;
  }
  summary {
    cursor: pointer;
  }
  .token-sources {
    overflow-wrap: anywhere;
  }
`;
const format = (value: number) => value.toLocaleString("en-US");

function TokenUsageCard({
  title,
  usage,
}: {
  title: string;
  usage: TokenUsage;
}) {
  if (usage.kind === "unavailable")
    return (
      <section css={usageCSS} aria-label={title}>
        <div className="token-heading">{title}</div>
        <strong>{usage.reason === "工具不適用" ? "不適用" : "無資料"}</strong>
        <div className="token-labels">{usage.reason}</div>
      </section>
    );
  const { counts } = usage;
  const hasRatio =
    counts.input != null &&
    counts.output != null &&
    counts.total != null &&
    counts.total > 0;
  return (
    <section css={usageCSS} aria-label={title}>
      <div className="token-heading">{title}</div>
      <div className="token-total">
        {counts.total == null ? "部分資料" : format(counts.total)}
      </div>
      {hasRatio && (
        <div
          className="token-ratio"
          role="img"
          aria-label={`輸入 ${counts.input}、輸出 ${counts.output} tokens`}
        >
          <span
            style={{
              width: `${((counts.input ?? 0) / (counts.total ?? 1)) * 100}%`,
              background: "#b6df7c",
            }}
          />
          <span style={{ flex: 1, background: "#76b900" }} />
        </div>
      )}
      <div className="token-labels">
        <span>
          輸入 {counts.input == null ? "無資料" : format(counts.input)}
        </span>
        <span>
          輸出 {counts.output == null ? "無資料" : format(counts.output)}
        </span>
        {counts.cacheRead != null && (
          <span>快取 {format(counts.cacheRead)}（已含輸入）</span>
        )}
      </div>
      <details>
        <summary>用量來源</summary>
        <div>
          {usage.scope === "model"
            ? "單次模型呼叫"
            : usage.scope === "turn"
              ? "輪次用量"
              : "已觀測 Session 用量"}{" "}
          · 輸入＋輸出；快取不重複加總
        </div>
        <div className="token-sources">{usage.sourceSpanIds.join(" · ")}</div>
      </details>
    </section>
  );
}

export function SessionTokenSummary({
  data,
  turnId,
}: {
  data: SessionTimelineData;
  turnId?: string | null;
}) {
  return (
    <TokenUsageCard
      title={turnId ? "輪次 Token" : "Session Token"}
      usage={getSessionTokenUsage({ data, selectedTurnId: turnId })}
    />
  );
}

export function OperationTokenUsage({
  operation,
}: {
  operation: SessionOperation;
  data?: SessionTimelineData;
}) {
  return (
    <TokenUsageCard
      title="操作 Token"
      usage={getOperationTokenUsage(operation)}
    />
  );
}
