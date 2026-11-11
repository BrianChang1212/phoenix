import { afterEach, expect, it, vi } from "vitest";

import { loadSessionTimeline } from "../sessionTimelineData";

vi.mock("@phoenix/config", () => ({ BASE_URL: "http://phoenix.test" }));
vi.mock("@phoenix/authFetch", () => ({
  authFetch: (...args: Parameters<typeof fetch>) => fetch(...args),
}));
afterEach(() => vi.unstubAllGlobals());

it("loads every page and inherits source and turn without treating transport success as command success", async () => {
  const base = {
    id: "root-node",
    name: "run_turn",
    context: { span_id: "root", trace_id: "trace" },
    span_kind: "UNKNOWN",
    parent_id: null,
    start_time: "2026-11-11T00:00:00Z",
    end_time: "2026-11-11T00:00:03Z",
    status_code: "UNSET",
    events: [],
  };
  const parent = {
    ...base,
    attributes: {
      "conversation.id": "session",
      "turn.id": "turn",
      "codex.turn.token_usage.input_tokens": 10,
    },
  };
  const model = {
    ...base,
    id: "model-node",
    name: "handle_responses",
    context: { span_id: "model", trace_id: "trace" },
    parent_id: "root",
    attributes: { "llm.token_count.total": 10 },
  };
  const tool = {
    ...base,
    id: "tool-node",
    name: "exec_command",
    context: { span_id: "tool", trace_id: "trace" },
    parent_id: "root",
    attributes: { "codex.tool.phase": "dispatch" },
    events: [{ name: "tool-result", attributes: { success: true } }],
  };
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      return Response.json(
        url.includes("/v1/sessions/")
          ? {
              data: {
                id: "s",
                session_id: "session",
                project_id: "p",
                traces: [{ trace_id: "trace" }],
              },
            }
          : url.includes("cursor=next")
            ? { data: [model, tool], next_cursor: null }
            : { data: [parent], next_cursor: "next" }
      );
    })
  );
  const data = await loadSessionTimeline({ sessionId: "s" });
  expect(calls).toHaveLength(3);
  expect(data.operations).toHaveLength(2);
  expect(data.operations.map((operation) => operation.source)).toEqual([
    "codex",
    "codex",
  ]);
  expect(data.operations.map((operation) => operation.turnId)).toEqual([
    "turn",
    "turn",
  ]);
  expect(data.operations[1]?.status).toBe("unknown");
  expect(data.spans).toHaveLength(3);
});

it("keeps paired Kiro operations without counting their observation markers as extra actions", async () => {
  const base = {
    id: "window-node",
    name: "kiro.observed_tool_window",
    context: { span_id: "window", trace_id: "kiro-trace" },
    parent_id: null,
    span_kind: "CHAIN",
    start_time: "2026-11-11T00:00:00Z",
    end_time: "2026-11-11T00:00:10Z",
    status_code: "UNSET",
    attributes: { "session.id": "kiro-session" },
    events: [],
  };
  const operations = ["success", "error", "timeout"].map((result, index) => ({
    ...base,
    id: `tool-${index}`,
    name: "execute_pwsh",
    context: { span_id: `tool-${index}`, trace_id: "kiro-trace" },
    parent_id: "window",
    span_kind: "TOOL",
    end_time: index === 0 ? base.start_time : base.end_time,
    attributes: {
      "session.id": "kiro-session",
      "kiro.record_kind": "paired tool boundaries",
      "kiro.result": result,
    },
  }));
  const markers = operations.map((operation, index) => ({
    ...operation,
    id: `observation-${index}`,
    name: "execute_pwsh.observation",
    context: { span_id: `observation-${index}`, trace_id: "kiro-trace" },
    end_time: base.start_time,
    attributes: {
      "session.id": "kiro-session",
      ...(index !== 1
        ? { "kiro.record_kind": "event observation; no operation duration" }
        : {}),
      ...(index !== 0
        ? { "kiro.observation_semantics": "historical point observation" }
        : {}),
    },
  }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      Response.json(
        url.includes("/v1/sessions/")
          ? {
              data: {
                id: "kiro-node",
                session_id: "kiro-session",
                project_id: "kiro-project",
                traces: [{ trace_id: "kiro-trace" }],
              },
            }
          : { data: [base, ...operations, ...markers], next_cursor: null }
      )
    )
  );

  const data = await loadSessionTimeline({ sessionId: "kiro-node" });

  expect(data.operations).toHaveLength(3);
  expect(data.operations.map((operation) => operation.status)).toEqual([
    "success",
    "error",
    "timeout",
  ]);
  expect(data.operations[0]?.endTime).toBe(data.operations[0]?.startTime);
  expect(data.spans).toHaveLength(7);
});
