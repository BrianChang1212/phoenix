import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionTimeline } from "../SessionTimeline";
import type { SessionTimelineData } from "../sessionTimelineData";

vi.mock("../sessionTimelineData", () => ({ loadSessionTimeline: vi.fn() }));

const data: SessionTimelineData = {
  id: "session-node",
  sessionId: "session-1",
  projectId: "project-node",
  source: "kiro",
  spans: [],
  operations: [
    {
      id: "span-node-1",
      spanId: "span-1",
      traceId: "trace-1",
      parentId: null,
      name: "execute_pwsh",
      summary: "讀取專案設定與檢查完整的設定描述",
      startTime: 1000,
      endTime: 2000,
      kind: "tool",
      status: "unknown",
      source: "kiro",
      turnId: null,
      attributes: {},
      events: [],
    },
    {
      id: "span-node-2",
      spanId: "span-2",
      traceId: "trace-1",
      parentId: null,
      name: "execute_pwsh",
      summary: "檢查失敗的命令",
      startTime: 2000,
      endTime: 4000,
      kind: "tool",
      status: "error",
      source: "kiro",
      turnId: null,
      attributes: { "kiro.exit_code": 7 },
      events: [],
    },
  ],
};

describe("SessionTimeline", () => {
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

  it("selects a complete operation description and links to its original trace", () => {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={data} />
        </MemoryRouter>
      )
    );
    const button = Array.from(container.querySelectorAll("button")).find((el) =>
      el.textContent?.includes("檢查失敗的命令")
    );
    expect(button).toBeDefined();
    act(() => button?.click());
    const details = container.querySelector('[aria-label="操作詳細資訊"]');
    expect(details?.textContent).toContain("失敗");
    expect(details?.textContent).toContain("2.00 秒");
    expect(details?.querySelector("a")?.getAttribute("href")).toContain(
      "trace-1"
    );
  });

  it("does not present an unset tool result as success", () => {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={data} />
        </MemoryRouter>
      )
    );
    const details = container.querySelector('[aria-label="操作詳細資訊"]');
    expect(details?.textContent).toContain("未知");
    expect(details?.textContent).not.toContain("成功");
    expect(container.textContent).toContain("讀取專案設定與檢查完整的設定描述");
  });

  it("shows an explicit empty state", () => {
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={{ ...data, operations: [] }} />
        </MemoryRouter>
      )
    );
    expect(container.textContent).toContain("沒有操作資料");
  });
});
