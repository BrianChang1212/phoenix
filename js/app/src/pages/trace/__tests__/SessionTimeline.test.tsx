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

  it("opens individually timed dense segments and keeps keyboard selection available", () => {
    const denseData: SessionTimelineData = {
      ...data,
      operations: [
        {
          ...data.operations[0],
          startTime: 487,
          endTime: 721,
          summary: "First dense tool",
        },
        {
          ...data.operations[1],
          startTime: 5296,
          endTime: 6208,
          summary: "Second dense tool",
        },
        {
          ...data.operations[0],
          id: "tail",
          summary: "Last dense tool",
          startTime: 1833999,
          endTime: 1834000,
        },
      ],
    };
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={denseData} />
        </MemoryRouter>
      )
    );
    const axis = container.querySelector<HTMLButtonElement>(
      '[aria-label="選擇時間軸區段"]'
    );
    expect(axis).not.toBeNull();
    if (!axis) return;
    vi.spyOn(axis, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 1000, 48)
    );
    act(() =>
      axis.dispatchEvent(
        new MouseEvent("click", { bubbles: true, detail: 1, clientX: 1 })
      )
    );
    const chooser = container.querySelector('[aria-label="密集區段選擇"]');
    expect(chooser?.textContent).toContain("First dense tool");
    expect(chooser?.textContent).toContain("Second dense tool");
    expect(chooser?.textContent).not.toContain("平行操作");
    const second = [...(chooser?.querySelectorAll("button") ?? [])].find(
      (button) => button.textContent?.includes("Second dense tool")
    );
    act(() => second?.click());
    expect(
      container.querySelector('[aria-label="操作詳細資訊"]')?.textContent
    ).toContain("Second dense tool");
    act(() =>
      axis.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })
      )
    );
    expect(axis.getAttribute("aria-describedby")).toBeTruthy();
    act(() =>
      axis.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Home", bubbles: true })
      )
    );
    expect(
      container.querySelector('[aria-label="操作詳細資訊"]')?.textContent
    ).toContain("First dense tool");
    act(() =>
      axis.dispatchEvent(
        new KeyboardEvent("keydown", { key: "End", bubbles: true })
      )
    );
    expect(
      container.querySelector('[aria-label="操作詳細資訊"]')?.textContent
    ).toContain("Last dense tool");
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

  it("zooms into reliable raw turns even when no operations were extracted", () => {
    const envelopeData: SessionTimelineData = {
      ...data,
      source: "codex",
      operations: [],
      spans: [
        {
          id: "first-turn",
          name: "run_turn",
          context: { span_id: "first-turn", trace_id: "trace" },
          parent_id: null,
          span_kind: "UNKNOWN",
          start_time: "2026-11-11T00:00:00Z",
          end_time: "2026-11-11T00:00:01Z",
          status_code: "UNSET",
          attributes: { "turn.id": "turn-one" },
          events: [],
        },
        {
          id: "second-turn",
          name: "session_task.turn",
          context: { span_id: "second-turn", trace_id: "trace" },
          parent_id: null,
          span_kind: "UNKNOWN",
          start_time: "2026-11-11T00:00:03Z",
          end_time: "2026-11-11T00:00:05Z",
          status_code: "UNSET",
          attributes: {
            turn_id: "turn-two",
            "codex.turn.token_usage.input_tokens": 100,
            "codex.turn.token_usage.output_tokens": 20,
          },
          events: [],
        },
      ],
    };
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={envelopeData} />
        </MemoryRouter>
      )
    );
    const select =
      container.querySelector<HTMLSelectElement>('[aria-label="輪次"]');
    expect(select).not.toBeNull();
    expect(select?.options.length).toBe(3);
    act(() => {
      if (select) {
        select.value = "turn-two";
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    expect(container.textContent).toContain("輪次可觀測時間");
    expect(container.textContent).toContain("2.00 秒");
    expect(container.textContent).toContain("沒有操作資料");
    expect(
      container.querySelector('[aria-label="輪次 Token"]')?.textContent
    ).toContain("120");
    const overview = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "返回總覽"
    );
    act(() => overview?.click());
    expect(container.textContent).toContain("5.00 秒");
  });

  it("selects parallel work without replacing complete operation duration and zooms into a reliable turn", () => {
    const parallelData: SessionTimelineData = {
      ...data,
      operations: [
        {
          ...data.operations[0],
          id: "tool",
          turnId: "turn-one",
          startTime: 0,
          endTime: 4000,
        },
        {
          ...data.operations[1],
          id: "model",
          name: "model",
          summary: "模型分析",
          kind: "model",
          status: "success",
          turnId: "turn-one",
          startTime: 2000,
          endTime: 6000,
        },
        {
          ...data.operations[1],
          id: "last",
          summary: "第二輪檢查",
          turnId: "turn-two",
          startTime: 8000,
          endTime: 10000,
        },
      ],
    };
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline data={parallelData} />
        </MemoryRouter>
      )
    );
    const parallel = container.querySelector<HTMLButtonElement>(
      '[aria-label="選擇時間軸區段"]'
    );
    expect(parallel).not.toBeNull();
    if (parallel) {
      vi.spyOn(parallel, "getBoundingClientRect").mockReturnValue(
        new DOMRect(0, 0, 600, 48)
      );
      act(() =>
        parallel.dispatchEvent(
          new MouseEvent("click", { bubbles: true, detail: 1, clientX: 300 })
        )
      );
    }
    const model = Array.from(
      container.querySelectorAll<HTMLButtonElement>(
        '[aria-label="平行操作選擇"] button'
      )
    ).find((button) => button.textContent?.includes("模型分析"));
    act(() => model?.click());
    const details = container.querySelector('[aria-label="操作詳細資訊"]');
    expect(details?.textContent).toContain("模型分析");
    expect(details?.textContent).toContain("4.00 秒");
    const select =
      container.querySelector<HTMLSelectElement>('[aria-label="輪次"]');
    expect(select).not.toBeNull();
    act(() => {
      if (select) {
        select.value = "turn-two";
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    expect(
      container.querySelector('[aria-label="單行時間軸"]')?.textContent
    ).not.toContain("模型分析");
    expect(
      container
        .querySelector('[aria-label="耗時分布"]')
        ?.getAttribute("aria-label")
    ).toBe("耗時分布");
    expect(container.textContent).toContain("2.00 秒");
    const overview = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button")
    ).find((button) => button.textContent === "返回總覽");
    act(() => overview?.click());
    expect(select?.value).toBe("");
  });
});
