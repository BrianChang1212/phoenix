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
