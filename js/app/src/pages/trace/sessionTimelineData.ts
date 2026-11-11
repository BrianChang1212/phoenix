import { z } from "zod";

import { authFetch } from "@phoenix/authFetch";
import { BASE_URL } from "@phoenix/config";

export type SessionSource = "codex" | "kiro" | "unknown";
export type SessionOperation = {
  id: string;
  spanId: string;
  traceId: string;
  parentId: string | null;
  name: string;
  summary: string | null;
  startTime: number;
  endTime: number | null;
  kind: "tool" | "model" | "other";
  status: "success" | "error" | "timeout" | "running" | "unknown";
  source: SessionSource;
  turnId: string | null;
  attributes: Record<string, unknown>;
  events: { name: string; attributes: Record<string, unknown> }[];
};
export type SessionTimelineData = {
  id: string;
  sessionId: string;
  projectId: string;
  source: SessionSource;
  operations: SessionOperation[];
  spans: SessionTimelineSpan[];
};

const attributesSchema = z.record(z.string(), z.unknown());
const spanSchema = z.object({
  id: z.string(),
  name: z.string(),
  context: z.object({ span_id: z.string(), trace_id: z.string() }),
  parent_id: z.string().nullable().optional(),
  span_kind: z.string(),
  start_time: z.iso.datetime({ offset: true }),
  end_time: z.iso.datetime({ offset: true }).nullable(),
  status_code: z.string(),
  attributes: attributesSchema,
  events: z
    .array(z.object({ name: z.string(), attributes: attributesSchema }))
    .default([]),
});
export type SessionTimelineSpan = z.infer<typeof spanSchema>;
const spansPageSchema = z.object({
  data: z.array(spanSchema),
  next_cursor: z.string().nullable(),
});
const sessionSchema = z.object({
  data: z.object({
    id: z.string(),
    session_id: z.string(),
    project_id: z.string(),
    traces: z.array(z.object({ trace_id: z.string() })),
  }),
});

function stringAttribute(
  attributes: Record<string, unknown>,
  key: string
): string | null {
  const value = attributes[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function spanSource(span: SessionTimelineSpan): SessionSource {
  const a = span.attributes;
  if (
    Object.keys(a).some((key) => key.startsWith("kiro.")) ||
    span.name === "kiro.session" ||
    span.name === "kiro.observed_tool_window"
  )
    return "kiro";
  if (
    Object.keys(a).some((key) => key.startsWith("codex.")) ||
    span.name === "code_mode.broker.invoke_tool" ||
    (span.name === "run_turn" && a["conversation.id"] != null)
  )
    return "codex";
  return "unknown";
}

function operationStatus(
  span: SessionTimelineSpan
): SessionOperation["status"] {
  if (span.end_time == null) return "running";
  const result = span.attributes["kiro.result"];
  if (result === "timeout") return "timeout";
  if (result === "error" || result === "denied" || span.status_code === "ERROR")
    return "error";
  if (result === "success" || span.status_code === "OK") return "success";
  return "unknown";
}

function normalizeOperation(
  span: SessionTimelineSpan
): SessionOperation | null {
  const a = span.attributes;
  if (
    a["kiro.record_kind"] === "event observation; no operation duration" ||
    stringAttribute(a, "kiro.observation_semantics") !== null
  )
    return null;
  const isTool =
    span.span_kind === "TOOL" ||
    a["codex.tool.phase"] === "dispatch" ||
    span.name === "code_mode.broker.invoke_tool";
  const isModel =
    span.span_kind === "LLM" ||
    (span.name === "handle_responses" &&
      (a["llm.token_count.total"] != null ||
        a["gen_ai.usage.input_tokens"] != null));
  // Session envelopes and transport/internal spans are not user operations.
  if (!isTool && !isModel) return null;
  const endTime = span.end_time == null ? null : Date.parse(span.end_time);
  const startTime = Date.parse(span.start_time);
  if (endTime != null && endTime < startTime)
    throw new Error("操作時間資料不合法");
  return {
    id: span.id || `${span.context.trace_id}:${span.context.span_id}`,
    spanId: span.context.span_id,
    traceId: span.context.trace_id,
    parentId: span.parent_id ?? null,
    name: stringAttribute(a, "tool_name") ?? span.name,
    summary: stringAttribute(a, "kiro.action_summary"),
    startTime,
    endTime,
    kind: isTool ? "tool" : "model",
    status: operationStatus(span),
    source: spanSource(span),
    turnId: stringAttribute(a, "turn.id") ?? stringAttribute(a, "turn_id"),
    attributes: a,
    events: span.events,
  };
}

async function readJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const response = await authFetch(url, { signal });
  if (!response.ok) throw new Error(`讀取失敗 (${response.status})`);
  return response.json();
}

function inheritOperationContext(
  operation: SessionOperation,
  spans: Map<string, SessionTimelineSpan>
) {
  const ancestors = new Set<string>();
  let parentId = operation.parentId;
  while (parentId && !ancestors.has(parentId)) {
    ancestors.add(parentId);
    const parent = spans.get(`${operation.traceId}:${parentId}`);
    if (!parent) break;
    if (operation.source === "unknown") operation.source = spanSource(parent);
    operation.turnId ??=
      stringAttribute(parent.attributes, "turn.id") ??
      stringAttribute(parent.attributes, "turn_id");
    parentId = parent.parent_id ?? null;
  }
}

export async function loadSessionTimeline({
  sessionId,
  signal,
}: {
  sessionId: string;
  signal?: AbortSignal;
}): Promise<SessionTimelineData> {
  const { data: session } = sessionSchema.parse(
    await readJson(
      `${BASE_URL}/v1/sessions/${encodeURIComponent(sessionId)}`,
      signal
    )
  );
  const spans = new Map<string, SessionTimelineSpan>();
  for (const trace of session.traces) {
    let cursor: string | null = null;
    const seen = new Set<string>();
    do {
      const params = new URLSearchParams({
        limit: "1000",
        trace_id: trace.trace_id,
      });
      if (cursor) params.set("cursor", cursor);
      const page = spansPageSchema.parse(
        await readJson(
          `${BASE_URL}/v1/projects/${encodeURIComponent(session.project_id)}/spans?${params}`,
          signal
        )
      );
      for (const span of page.data) {
        if (span.context.trace_id !== trace.trace_id)
          throw new Error("Trace 資料不一致");
        spans.set(`${span.context.trace_id}:${span.context.span_id}`, span);
      }
      cursor = page.next_cursor;
      if (cursor) {
        if (seen.has(cursor)) throw new Error("分頁游標重複，資料尚未完整");
        seen.add(cursor);
      }
    } while (cursor);
  }
  const operations: SessionOperation[] = [];
  for (const span of spans.values()) {
    const operation = normalizeOperation(span);
    if (!operation) continue;
    inheritOperationContext(operation, spans);
    operations.push(operation);
  }
  const sources = new Set(
    operations
      .map((operation) => operation.source)
      .filter((source) => source !== "unknown")
  );
  const source: SessionSource =
    sources.size === 1
      ? (sources.values().next().value ?? "unknown")
      : "unknown";
  return {
    id: session.id,
    sessionId: session.session_id,
    projectId: session.project_id,
    source,
    operations: operations.sort((a, b) => a.startTime - b.startTime),
    spans: Array.from(spans.values()),
  };
}
