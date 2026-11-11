import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type {
  SessionTimelineData,
  SessionTimelineSpan,
} from "../sessionTimelineData";
import { OperationTokenUsage, SessionTokenSummary } from "../SessionTokenUsage";

function model(
  id: string,
  attributes: Record<string, unknown>
): SessionTimelineSpan {
  return {
    id,
    name: "handle_responses",
    context: { span_id: id, trace_id: "trace" },
    parent_id: null,
    span_kind: "LLM",
    start_time: "2026-11-11T00:00:00Z",
    end_time: "2026-11-11T00:00:01Z",
    status_code: "UNSET",
    attributes,
    events: [],
  };
}
function data(spans: SessionTimelineSpan[]): SessionTimelineData {
  return {
    id: "session",
    sessionId: "session",
    projectId: "project",
    source: "codex",
    spans,
    operations: spans
      .filter((s) => s.span_kind === "LLM")
      .map((s) => ({
        id: s.id,
        spanId: s.context.span_id,
        traceId: s.context.trace_id,
        parentId: s.parent_id ?? null,
        name: s.name,
        summary: null,
        startTime: 0,
        endTime: 1000,
        kind: "model",
        status: "unknown",
        source: "codex",
        turnId: null,
        attributes: s.attributes,
        events: s.events,
      })),
  };
}

describe("Session Token usage", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("shows input and output once when both semantic aliases exist", () => {
    act(() =>
      root.render(
        <SessionTokenSummary
          data={data([
            model("one", {
              "llm.token_count.prompt": 100,
              "llm.token_count.completion": 20,
              "llm.token_count.total": 120,
              "gen_ai.usage.input_tokens": 100,
              "gen_ai.usage.output_tokens": 20,
              "llm.token_count.prompt_details.cache_read": 60,
            }),
          ])}
        />
      )
    );
    expect(container.textContent).toContain("120");
    expect(container.textContent).toContain("輸入 100");
    expect(container.textContent).toContain("輸出 20");
    expect(container.textContent).toContain("快取 60");
    expect(
      container.querySelector('[role="img"]')?.getAttribute("aria-label")
    ).toContain("輸入 100");
  });

  it("does not choose between conflicting cumulative snapshots at the same time", () => {
    const snapshots = [100, 200].map((count, i) => ({
      ...model(`turn-${i}`, {
        "turn.id": "turn-1",
        "codex.turn.token_usage.input_tokens": count,
        "codex.turn.token_usage.output_tokens": 20,
      }),
      name: "session_task.turn",
      span_kind: "UNKNOWN",
    }));
    act(() => root.render(<SessionTokenSummary data={data(snapshots)} />));
    expect(container.textContent).toContain("無資料");
    expect(container.textContent).toContain("輪次累計來源互相矛盾");
  });

  it("uses the turn cumulative once and keeps the selected model's own usage", () => {
    const call = model("call", {
      "turn.id": "turn-1",
      "llm.token_count.prompt": 100,
      "llm.token_count.completion": 20,
    });
    const cumulative = {
      ...model("cumulative", {
        "turn.id": "turn-1",
        "codex.turn.token_usage.input_tokens": 300,
        "codex.turn.token_usage.output_tokens": 40,
        "codex.turn.token_usage.cached_input_tokens": 200,
      }),
      name: "session_task.turn",
      span_kind: "UNKNOWN",
    };
    const session = data([call, cumulative]);
    session.operations = session.operations.map((operation) => ({
      ...operation,
      turnId: "turn-1",
    }));
    act(() =>
      root.render(<SessionTokenSummary data={session} turnId="turn-1" />)
    );
    expect(container.textContent).toContain("輪次 Token340");
    expect(container.textContent).toContain("來源層級：輪次累計");
    expect(container.textContent).not.toContain("460");
    const operation = session.operations[0];
    if (!operation) throw new Error("fixture requires a model");
    act(() => root.render(<OperationTokenUsage operation={operation} />));
    expect(container.textContent).toContain("操作 Token120");
  });

  it("distinguishes absent Kiro usage, genuine zero, and a tool's inapplicability", () => {
    act(() =>
      root.render(
        <SessionTokenSummary data={{ ...data([]), source: "kiro" }} />
      )
    );
    expect(container.textContent).toContain("無資料");
    expect(container.querySelector('[role="img"]')).toBeNull();
    const session = data([
      model("zero", {
        "llm.token_count.prompt": 0,
        "llm.token_count.completion": 0,
      }),
    ]);
    act(() => root.render(<SessionTokenSummary data={session} />));
    expect(container.textContent).toContain("Session Token0");
    expect(container.textContent).not.toContain("無資料");
    const modelOperation = session.operations[0];
    if (!modelOperation) throw new Error("fixture requires a model");
    act(() =>
      root.render(
        <OperationTokenUsage operation={{ ...modelOperation, kind: "tool" }} />
      )
    );
    expect(container.textContent).toContain("不適用");
  });

  it("withholds the session total when one observed model has no usage", () => {
    const session = data([
      model("known", {
        "llm.token_count.prompt": 100,
        "llm.token_count.completion": 20,
      }),
      model("missing", {}),
    ]);
    act(() => root.render(<SessionTokenSummary data={session} />));
    expect(container.textContent).toContain("無資料");
    expect(container.textContent).not.toContain("120");
  });

  it("does not infer an input/output ratio from a total alone", () => {
    const session = data([model("total", { "llm.token_count.total": 120 })]);
    act(() => root.render(<SessionTokenSummary data={session} />));
    expect(container.textContent).toContain("120");
    expect(container.textContent).toContain("輸入 無資料");
    expect(container.querySelector('[role="img"]')).toBeNull();
  });

  it("rejects inconsistent aliases rather than choosing a plausible count", () => {
    act(() =>
      root.render(
        <SessionTokenSummary
          data={data([
            model("bad", {
              "llm.token_count.prompt": 100,
              "gen_ai.usage.input_tokens": 200,
              "llm.token_count.completion": 20,
            }),
          ])}
        />
      )
    );
    expect(container.textContent).toContain("無資料");
    expect(container.textContent).toContain("用量來源互相矛盾");
  });
});
