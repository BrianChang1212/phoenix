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
  name: string
): SessionTimelineSpan {
  return {
    id: `node-${id}`,
    name,
    parent_id: parent,
    context: { span_id: id, trace_id: "kiro-trace" },
    span_kind: "TOOL",
    start_time: "2026-11-15T00:00:00Z",
    end_time: "2026-11-15T00:00:01Z",
    status_code: "UNSET",
    attributes: {},
    events: [],
  };
}

const rootSpan = {
  ...span("window", null, "kiro.observed_tool_window"),
  span_kind: "CHAIN",
};
const tool = {
  ...span("tool", "window", "execute_pwsh"),
  attributes: {
    "kiro.record_kind": "paired tool boundaries",
    "kiro.action_summary": "讀取目前設定",
    "kiro.result": "success",
    "kiro.exit_code": "0",
  },
};
const observation = {
  ...span("observation", "window", "execute_pwsh.observation"),
  attributes: {
    "kiro.record_kind": "event observation; no operation duration",
    "kiro.observation_semantics": "state observed at export",
    "kiro.result": "incomplete",
  },
};
const kiro: SessionTimelineData = {
  id: "kiro-node",
  sessionId: "kiro-session",
  projectId: "project",
  source: "kiro",
  operations: [
    {
      id: tool.id,
      spanId: "tool",
      traceId: "kiro-trace",
      parentId: "window",
      name: tool.name,
      summary: "讀取目前設定",
      startTime: Date.parse(tool.start_time),
      endTime: Date.parse("2026-11-15T00:00:01Z"),
      kind: "tool",
      status: "success",
      source: "kiro",
      turnId: null,
      attributes: tool.attributes,
      events: [],
    },
  ],
  spans: [rootSpan, tool, observation],
};

describe("Kiro session hierarchy", () => {
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
  function render(data = kiro) {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline
            data={data}
            observedAt={Date.parse("2026-11-15T00:00:02Z")}
          />
        </MemoryRouter>
      )
    );
  }
  function click(text: string) {
    const element = [...container.querySelectorAll("button")].find(
      (node) => node.textContent === text
    );
    expect(element, text).toBeDefined();
    act(() => element?.click());
  }

  it("starts collapsed and keeps observation filtering when all branches are expanded", () => {
    render();
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(1);
    click("全部展開");
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(2);
    expect(
      container.querySelector(
        '[role="treeitem"][aria-label="execute_pwsh.observation"]'
      )
    ).toBeNull();
    expect(container.textContent).toContain("1 操作");
    click("顯示觀測事件");
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(3);
    click("隱藏觀測事件");
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(2);
  });

  it.each([
    ["success", "0", "工具回報成功"],
    ["error", "7", "失敗"],
    ["timeout", "-1", "逾時"],
    ["denied", null, "權限遭拒"],
  ])(
    "preserves recorded %s and exit code without turning purpose into a result",
    (result, exitCode, label) => {
      render({
        ...kiro,
        spans: [
          rootSpan,
          {
            ...tool,
            attributes: {
              ...tool.attributes,
              "kiro.result": result,
              "kiro.exit_code": exitCode,
            },
          },
        ],
      });
      click("全部展開");
      act(() =>
        container
          .querySelector<HTMLButtonElement>(
            'button[aria-label="選取 execute_pwsh"]'
          )
          ?.click()
      );
      const selected = container.querySelector('[aria-label="選取的呼叫結果"]');
      const summary = selected?.querySelector(".hierarchy-result");
      expect(summary?.textContent).toContain(label);
      expect(summary?.textContent).toContain(result);
      if (exitCode !== null)
        expect(summary?.textContent).toContain(`exit ${exitCode}`);
      else expect(summary?.textContent).not.toContain("exit");
      expect(summary?.textContent).toContain("讀取目前設定");
      expect(selected?.querySelector<HTMLDetailsElement>("details")?.open).toBe(
        false
      );
    }
  );

  it("labels observations without presenting their interval as operation duration", () => {
    render();
    click("全部展開");
    click("顯示觀測事件");
    act(() =>
      container
        .querySelector<HTMLButtonElement>(
          'button[aria-label="選取 execute_pwsh.observation"]'
        )
        ?.click()
    );
    const summary = container.querySelector(
      '[aria-label="選取的呼叫結果"] .hierarchy-result'
    );
    expect(summary?.textContent).toContain("觀測事件");
    expect(summary?.textContent).toContain("非操作耗時");
    expect(summary?.textContent).not.toContain("1.000 秒");
  });

  it("resets Kiro-only filters and selection when moving between sessions", () => {
    render();
    click("全部展開");
    click("顯示觀測事件");
    act(() =>
      container
        .querySelector<HTMLButtonElement>(
          'button[aria-label="選取 execute_pwsh"]'
        )
        ?.click()
    );
    expect(
      container.querySelector('[aria-label="選取的呼叫結果"]')
    ).not.toBeNull();
    render({
      ...kiro,
      id: "codex-node",
      sessionId: "codex-session",
      source: "codex",
      operations: [],
      spans: [span("codex-root", null, "exec_command")],
    });
    expect(container.textContent).toContain("CODEX");
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBeNull();
    expect(
      [...container.querySelectorAll("button")].some((node) =>
        node.textContent?.includes("觀測事件")
      )
    ).toBe(false);
    expect(
      container.querySelector('[aria-label="選取 execute_pwsh"]')
    ).toBeNull();
    render();
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(1);
    click("全部展開");
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(2);
  });
});
