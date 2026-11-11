import { z } from "zod";

import type {
  SessionOperation,
  SessionTimelineData,
  SessionTimelineSpan,
} from "./sessionTimelineData";

export type TokenCounts = {
  input: number | null;
  output: number | null;
  total: number | null;
  cacheRead: number | null;
};
export type TokenUsage =
  | { kind: "unavailable"; reason: string }
  | {
      kind: "available";
      counts: TokenCounts;
      scope: "model" | "turn" | "session";
      provenance: ("model" | "turn")[];
      sourceSpanIds: string[];
    };

const countSchema = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);

function readCounts(
  attributes: Record<string, unknown>,
  scope: "model" | "turn"
): TokenUsage {
  const prefix = "codex.turn.token_usage.";
  const keys =
    scope === "turn"
      ? {
          input: [prefix + "input_tokens"],
          output: [prefix + "output_tokens"],
          total: [prefix + "total_tokens"],
          cacheRead: [prefix + "cached_input_tokens"],
        }
      : {
          input: ["llm.token_count.prompt", "gen_ai.usage.input_tokens"],
          output: ["llm.token_count.completion", "gen_ai.usage.output_tokens"],
          total: ["llm.token_count.total"],
          cacheRead: [
            "llm.token_count.prompt_details.cache_read",
            "gen_ai.usage.cache_read.input_tokens",
          ],
        };
  const counts: TokenCounts = {
    input: null,
    output: null,
    total: null,
    cacheRead: null,
  };
  for (const field of [
    "input",
    "output",
    "total",
    "cacheRead",
  ] satisfies (keyof TokenCounts)[]) {
    for (const key of keys[field]) {
      const value = attributes[key];
      if (value == null) continue;
      const parsed = countSchema.safeParse(value);
      if (!parsed.success)
        return { kind: "unavailable", reason: "用量欄位不合法" };
      if (counts[field] != null && counts[field] !== parsed.data)
        return { kind: "unavailable", reason: "用量來源互相矛盾" };
      counts[field] = parsed.data;
    }
  }
  if (counts.input != null && counts.output != null) {
    const total = counts.input + counts.output;
    if (
      !Number.isSafeInteger(total) ||
      (counts.total != null && counts.total !== total)
    )
      return { kind: "unavailable", reason: "用量總數不一致" };
    counts.total = total;
  }
  if (
    counts.cacheRead != null &&
    counts.input != null &&
    counts.cacheRead > counts.input
  )
    return { kind: "unavailable", reason: "快取輸入超出輸入用量" };
  if (counts.input == null && counts.output == null && counts.total == null)
    return { kind: "unavailable", reason: "來源未提供用量" };
  return {
    kind: "available",
    counts,
    scope,
    provenance: [scope],
    sourceSpanIds: [],
  };
}

export function getOperationTokenUsage(
  operation: SessionOperation
): TokenUsage {
  if (operation.kind !== "model")
    return { kind: "unavailable", reason: "工具不適用" };
  const usage = readCounts(operation.attributes, "model");
  return usage.kind === "available"
    ? { ...usage, sourceSpanIds: [operation.id] }
    : usage;
}

function spanKey(span: SessionTimelineSpan): string {
  return `${span.context.trace_id}:${span.context.span_id}`;
}

function turnId(
  span: SessionTimelineSpan,
  spans: Map<string, SessionTimelineSpan>
): string | null {
  const visited = new Set<string>();
  let current: SessionTimelineSpan | undefined = span;
  while (current && !visited.has(spanKey(current))) {
    visited.add(spanKey(current));
    for (const key of ["turn.id", "turn_id"]) {
      const value = current.attributes[key];
      if (typeof value === "string" && value.trim()) return value;
    }
    current = current.parent_id
      ? spans.get(`${current.context.trace_id}:${current.parent_id}`)
      : undefined;
  }
  return null;
}

function sumUsages(
  usages: TokenUsage[],
  scope: "turn" | "session"
): TokenUsage {
  const counts: TokenCounts = { input: 0, output: 0, total: 0, cacheRead: 0 };
  const sourceSpanIds: string[] = [];
  const provenance = new Set<"model" | "turn">();
  for (const usage of usages) {
    if (usage.kind === "unavailable") return usage;
    if (usage.counts.total == null)
      return { kind: "unavailable", reason: "用量僅有部分欄位，不提供總數" };
    for (const field of [
      "input",
      "output",
      "total",
      "cacheRead",
    ] satisfies (keyof TokenCounts)[]) {
      const value = usage.counts[field];
      counts[field] =
        counts[field] == null || value == null ? null : counts[field] + value;
      if (counts[field] != null && !Number.isSafeInteger(counts[field]))
        return { kind: "unavailable", reason: "用量總數超出有效範圍" };
    }
    sourceSpanIds.push(...usage.sourceSpanIds);
    usage.provenance.forEach((source) => provenance.add(source));
  }
  return usages.length
    ? {
        kind: "available",
        counts,
        scope,
        provenance: [...provenance],
        sourceSpanIds,
      }
    : { kind: "unavailable", reason: "來源未提供用量" };
}

function latestTurnUsage(snapshots: SessionTimelineSpan[]): TokenUsage {
  const ordered = [...snapshots].sort(
    (a, b) =>
      Date.parse(b.end_time ?? b.start_time) -
      Date.parse(a.end_time ?? a.start_time)
  );
  const latest = ordered[0];
  if (!latest) return { kind: "unavailable", reason: "來源未提供用量" };
  const usage = readCounts(latest.attributes, "turn");
  if (usage.kind === "unavailable") return usage;
  const latestTime = Date.parse(latest.end_time ?? latest.start_time);
  for (const snapshot of ordered.filter(
    (span) => Date.parse(span.end_time ?? span.start_time) === latestTime
  )) {
    const other = readCounts(snapshot.attributes, "turn");
    if (
      other.kind !== "available" ||
      JSON.stringify(usage.counts) !== JSON.stringify(other.counts)
    )
      return { kind: "unavailable", reason: "輪次累計來源互相矛盾" };
  }
  return { ...usage, sourceSpanIds: [latest.id] };
}

export function getSessionTokenUsage({
  data,
  selectedTurnId,
}: {
  data: SessionTimelineData;
  selectedTurnId?: string | null;
}): TokenUsage {
  const spans = new Map(data.spans.map((span) => [spanKey(span), span]));
  const models = data.operations.filter(
    (operation) =>
      operation.kind === "model" &&
      (!selectedTurnId || operation.turnId === selectedTurnId)
  );
  const cumulative = new Map<string, SessionTimelineSpan[]>();
  const observedTurns = new Set(
    data.operations
      .map((operation) => operation.turnId)
      .filter((id): id is string => id != null)
  );
  for (const span of data.spans) {
    const id = turnId(span, spans);
    if (id && span.name === "run_turn") observedTurns.add(id);
    if (
      !id ||
      span.name !== "session_task.turn" ||
      !Object.keys(span.attributes).some((key) =>
        key.startsWith("codex.turn.token_usage.")
      )
    )
      continue;
    observedTurns.add(id);
    const group = cumulative.get(id) ?? [];
    group.push(span);
    cumulative.set(id, group);
  }
  const ids = selectedTurnId ? [selectedTurnId] : [...observedTurns];
  const usages: TokenUsage[] = [];
  for (const id of ids) {
    const snapshots = cumulative.get(id);
    if (snapshots?.length) {
      usages.push(latestTurnUsage(snapshots));
    } else {
      const calls = models.filter((operation) => operation.turnId === id);
      if (!calls.length)
        return {
          kind: "unavailable",
          reason: "部分輪次沒有用量，不提供 Session 總數",
        };
      usages.push(...calls.map(getOperationTokenUsage));
    }
  }
  const remaining = models.filter((operation) => operation.turnId == null);
  usages.push(...remaining.map(getOperationTokenUsage));
  const modelIds = new Set(
    models.map((operation) => `${operation.traceId}:${operation.spanId}`)
  );
  for (const operation of models.filter(
    (model) => !model.turnId || !cumulative.has(model.turnId)
  )) {
    let parent = operation.parentId
      ? spans.get(`${operation.traceId}:${operation.parentId}`)
      : undefined;
    const visited = new Set<string>();
    while (parent && !visited.has(spanKey(parent))) {
      visited.add(spanKey(parent));
      if (modelIds.has(spanKey(parent)))
        return { kind: "unavailable", reason: "巢狀模型用量無法確認去重" };
      parent = parent.parent_id
        ? spans.get(`${parent.context.trace_id}:${parent.parent_id}`)
        : undefined;
    }
  }
  return sumUsages(usages, selectedTurnId ? "turn" : "session");
}
