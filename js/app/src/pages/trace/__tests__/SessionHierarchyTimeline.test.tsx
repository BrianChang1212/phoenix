import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SessionTimeline } from "../SessionTimeline";
import type {
  SessionTimelineData,
  SessionTimelineSpan,
} from "../sessionTimelineData";

function span(
  id: string,
  parent: string | null,
  name: string,
  trace = "trace-a"
): SessionTimelineSpan {
  return {
    id: `node-${trace}-${id}`,
    name,
    parent_id: parent,
    context: { span_id: id, trace_id: trace },
    span_kind: "UNKNOWN",
    start_time: "2026-11-15T00:00:00Z",
    end_time: "2026-11-15T00:00:02Z",
    status_code: "UNSET",
    attributes: {},
    events: [],
  };
}

const data: SessionTimelineData = {
  id: "session-node",
  sessionId: "session-1",
  projectId: "project-1",
  source: "codex",
  operations: [],
  spans: [
    span("child", "root", "dispatch_tool_call_with_state"),
    span("root", "outside-capture", "exec"),
    span("leaf", "child", "unified_exec.exec_command"),
  ],
};

describe("Session hierarchy timeline", () => {
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
  function render(value = data) {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline
            data={value}
            observedAt={Date.parse("2026-11-15T00:00:03Z")}
          />
        </MemoryRouter>
      )
    );
  }
  function button(label: string) {
    return container.querySelector<HTMLButtonElement>(
      `button[aria-label="${label}"]`
    );
  }

  it("shows recorded parent depth and reveals only the selected result", () => {
    render();
    expect(container.querySelector('[aria-label="階層時間軸"]')).not.toBeNull();
    expect(
      container
        .querySelector('[role="treeitem"][aria-label="exec"]')
        ?.getAttribute("aria-level")
    ).toBe("1");
    expect(
      container
        .querySelector(
          '[role="treeitem"][aria-label="dispatch_tool_call_with_state"]'
        )
        ?.getAttribute("aria-level")
    ).toBe("2");
    expect(
      container
        .querySelector(
          '[role="treeitem"][aria-label="unified_exec.exec_command"]'
        )
        ?.getAttribute("aria-level")
    ).toBe("3");
    expect(container.textContent).toContain("上層未錄入");
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBeNull();
    const analysis = container.querySelector<HTMLDetailsElement>(
      '[aria-label="用量與輪次分析"]'
    );
    expect(analysis?.open).toBe(false);
    act(() => button("選取 unified_exec.exec_command")?.click());
    const selected = container.querySelector('[aria-label="選取的呼叫結果"]');
    expect(selected?.textContent).toContain("UNSET");
    expect(selected?.textContent).not.toContain("成功");
    expect(selected?.querySelector<HTMLDetailsElement>("details")?.open).toBe(
      false
    );
    expect(selected?.querySelector("a")?.getAttribute("href")).toContain(
      "trace-a?selectedSpanNodeId=node-trace-a-leaf"
    );
  });

  it("collapses recorded descendants without hiding another trace with the same span id", () => {
    render({
      ...data,
      spans: [...data.spans, span("root", null, "other trace", "trace-b")],
    });
    act(() => button("收合 exec")?.click());
    expect(button("選取 dispatch_tool_call_with_state")).toBeNull();
    expect(button("選取 other trace")).not.toBeNull();
    act(() => button("展開 exec")?.click());
    expect(button("選取 dispatch_tool_call_with_state")).not.toBeNull();
    const collapseAll = [...container.querySelectorAll("button")].find(
      (el) => el.textContent === "全部收合"
    );
    act(() => collapseAll?.click());
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(2);
  });

  it("reports cyclic parents instead of presenting them as a normal call chain", () => {
    render({
      ...data,
      spans: [
        span("a", "b", "cycle a"),
        span("b", "a", "cycle b"),
        span("valid", null, "valid root"),
      ],
    });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "資料關係異常"
    );
    expect(
      container
        .querySelector('[role="treeitem"][aria-label="cycle a"]')
        ?.getAttribute("aria-level")
    ).toBe("1");
    expect(button("選取 valid root")).not.toBeNull();
  });

  it("clears a removed selection and explains why without selecting a replacement", () => {
    render();
    act(() => button("選取 unified_exec.exec_command")?.click());
    render({
      ...data,
      spans: data.spans.filter((span) => span.context.span_id !== "leaf"),
    });
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBeNull();
    expect(
      container.querySelector('[aria-label="選取狀態"]')?.textContent
    ).toContain("選取的呼叫已不在最新資料中");
    expect(
      container.querySelector('[role="treeitem"][aria-selected="true"]')
    ).toBeNull();
    render();
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBeNull();
    act(() => button("選取 exec")?.click());
    expect(container.querySelector('[aria-label="選取狀態"]')).toBeNull();
  });
});
