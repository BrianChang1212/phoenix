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
  function render(
    value = data,
    observedAt = Date.parse("2026-11-15T00:00:03Z")
  ) {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={value} observedAt={observedAt} />
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

  it.each(["選取", "時間條"])(
    "restores timeline keyboard focus when a focused %s node disappears",
    (control) => {
      render();
      const focused = button(`${control} unified_exec.exec_command`);
      expect(focused).not.toBeNull();
      act(() => focused?.focus());
      expect(document.activeElement).toBe(focused);
      render({
        ...data,
        spans: data.spans.filter((span) => span.context.span_id !== "leaf"),
      });
      const navigation = container.querySelector<HTMLElement>(
        '[aria-label="時間軸導覽區"]'
      );
      expect(document.activeElement).toBe(navigation);
      const range = container.querySelector('[aria-label="可視時間範圍"]');
      const full = range?.textContent;
      act(() =>
        document.activeElement?.dispatchEvent(
          new KeyboardEvent("keydown", { key: "+", bubbles: true })
        )
      );
      expect(range?.textContent).not.toBe(full);
    }
  );

  it("does not steal focus from another control when a previously focused node disappears", () => {
    render();
    act(() => button("選取 unified_exec.exec_command")?.focus());
    const other = document.createElement("button");
    document.body.append(other);
    try {
      act(() => other.focus());
      render({
        ...data,
        spans: data.spans.filter((span) => span.context.span_id !== "leaf"),
      });
      expect(document.activeElement).toBe(other);
    } finally {
      other.remove();
    }
  });

  it("replaces provisional raw-span duration and status when the operation completes", () => {
    const running: SessionTimelineData = {
      ...data,
      spans: data.spans.map((span) =>
        span.context.span_id === "child" ? { ...span, end_time: null } : span
      ),
    };
    render(running);
    act(() => button("選取 dispatch_tool_call_with_state")?.click());
    const result = container.querySelector('[aria-label="選取的呼叫結果"]');
    expect(result?.textContent).toContain("進行中 · 暫時耗時");
    expect(result?.textContent).toContain("3.000 秒");
    render(
      {
        ...running,
        spans: running.spans.map((span) =>
          span.context.span_id === "child"
            ? {
                ...span,
                end_time: "2026-11-15T00:00:04Z",
                status_code: "OK",
              }
            : span
        ),
      },
      Date.parse("2026-11-15T00:00:06Z")
    );
    expect(result?.textContent).toContain("OK · 成功");
    expect(result?.textContent).toContain("4.000 秒");
    expect(result?.textContent).not.toContain("暫時耗時");
  });

  it("keeps the zoomed window, selection, collapsed branch and scroll when raw spans extend the full range", () => {
    render();
    act(() => button("選取 dispatch_tool_call_with_state")?.click());
    act(() => button("收合 dispatch_tool_call_with_state")?.click());
    act(() => button("放大時間軸")?.click());
    const range = container.querySelector('[aria-label="可視時間範圍"]');
    const visibleWindow = range?.textContent?.split("·")[0];
    const tree = container.querySelector<HTMLElement>(
      '[aria-label="原始呼叫階層"]'
    );
    if (tree) tree.scrollTop = 80;
    const result = container.querySelector('[aria-label="選取的呼叫結果"]');
    render({
      ...data,
      spans: [
        ...data.spans,
        {
          ...span("later", null, "later call"),
          start_time: "2026-11-15T00:00:12Z",
          end_time: "2026-11-15T00:00:20Z",
        },
      ],
    });
    expect(range?.textContent?.split("·")[0]).toBe(visibleWindow);
    expect(
      container.querySelector('[aria-label="全程時間軸縮圖"]')?.textContent
    ).toContain("20.000s");
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBe(
      result
    );
    expect(result?.textContent).toContain("dispatch_tool_call_with_state");
    expect(button("展開 dispatch_tool_call_with_state")).not.toBeNull();
    expect(button("選取 unified_exec.exec_command")).toBeNull();
    expect(button("選取 later call")).not.toBeNull();
    expect(button("時間條 later call")).toBeNull();
    expect(tree?.scrollTop).toBe(80);
  });
});
