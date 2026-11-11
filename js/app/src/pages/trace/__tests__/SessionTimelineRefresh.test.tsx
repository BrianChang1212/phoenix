import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionTimelineView } from "../SessionTimeline";
import {
  loadSessionTimeline,
  type SessionTimelineData,
} from "../sessionTimelineData";

vi.mock("../sessionTimelineData", () => ({ loadSessionTimeline: vi.fn() }));

const data: SessionTimelineData = {
  id: "node-session",
  sessionId: "session-1",
  projectId: "project-1",
  source: "codex",
  spans: [],
  operations: [
    {
      id: "operation-1",
      spanId: "span-1",
      traceId: "trace-1",
      parentId: null,
      name: "exec_command",
      summary: "檢查設定",
      startTime: 1000,
      endTime: 2000,
      kind: "tool",
      status: "success",
      source: "codex",
      turnId: "turn-one",
      attributes: {},
      events: [],
    },
  ],
};

describe("SessionTimeline live updates", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-11T10:00:00Z"));
    vi.mocked(loadSessionTimeline).mockReset();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });
  const button = (label: string) =>
    Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
      (element) => element.textContent === label
    );
  async function mount(sessionId = "session-1") {
    await act(async () =>
      root.render(
        <MemoryRouter>
          <SessionTimelineView sessionId={sessionId} />
        </MemoryRouter>
      )
    );
  }

  it("pauses automatic updates and refreshes immediately when resumed", async () => {
    vi.mocked(loadSessionTimeline).mockResolvedValue(data);
    await mount();
    expect(button("暫停更新")).toBeDefined();
    await act(async () => button("暫停更新")?.click());
    await act(async () => vi.advanceTimersByTimeAsync(20000));
    expect(loadSessionTimeline).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("已暫停");
    expect(container.querySelector("time")?.dateTime).toBe(
      "2026-11-11T10:00:00.000Z"
    );
    await act(async () => button("恢復更新")?.click());
    expect(loadSessionTimeline).toHaveBeenCalledTimes(2);
  });

  it("retains selection, filters, turn zoom and scroll while showing completed and new operations", async () => {
    const first: SessionTimelineData = {
      ...data,
      operations: [
        data.operations[0],
        {
          ...data.operations[0],
          id: "selected",
          spanId: "selected-span",
          summary: "檢查測試",
          startTime: 2000,
          endTime: null,
          status: "running",
        },
      ],
    };
    const updated: SessionTimelineData = {
      ...first,
      operations: [
        first.operations[0],
        {
          ...first.operations[1],
          endTime: 4000,
          status: "success",
        },
        {
          ...first.operations[0],
          id: "new",
          spanId: "new-span",
          summary: "檢查新增",
          startTime: 4000,
          endTime: 5000,
        },
      ],
    };
    vi.mocked(loadSessionTimeline)
      .mockResolvedValueOnce(first)
      .mockResolvedValue(updated);
    await mount();
    const search = container.querySelector<HTMLInputElement>(
      '[aria-label="搜尋操作"]'
    );
    const source = container.querySelector<HTMLSelectElement>(
      '[aria-label="操作來源"]'
    );
    const turn = container.querySelector<HTMLSelectElement>(
      '[aria-label="輪次"]'
    );
    await act(async () => {
      if (search) {
        Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value"
        )?.set?.call(search, "檢查");
        search.dispatchEvent(new Event("input", { bubbles: true }));
      }
      if (source) {
        source.value = "codex";
        source.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (turn) {
        turn.value = "turn-one";
        turn.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    const selected = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button")
    ).find((el) => el.textContent?.includes("檢查測試"));
    await act(async () => selected?.click());
    const scroll = container.querySelector<HTMLElement>(
      '[aria-label="時間軸捲動區"]'
    );
    expect(scroll).not.toBeNull();
    if (scroll) scroll.scrollLeft = 120;
    selected?.focus();
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(search?.value).toBe("檢查");
    expect(source?.value).toBe("codex");
    expect(turn?.value).toBe("turn-one");
    expect(container.querySelector('[aria-label="時間軸捲動區"]')).toBe(scroll);
    expect(scroll?.scrollLeft).toBe(120);
    expect(document.activeElement).toBe(selected);
    const details = container.querySelector('[aria-label="操作詳細資訊"]');
    expect(details?.querySelector("h3")?.textContent).toBe("檢查測試");
    expect(details?.textContent).toContain("成功");
    expect(details?.textContent).toContain("2.00 秒");
    expect(
      Array.from(container.querySelectorAll("button")).filter((el) =>
        el.textContent?.includes("檢查新增")
      )
    ).toHaveLength(1);
  });

  it("keeps the last snapshot on refresh failure and clears stale status after retry", async () => {
    vi.mocked(loadSessionTimeline)
      .mockResolvedValueOnce(data)
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValue(data);
    await mount();
    const selection = container.querySelector('[aria-label="操作詳細資訊"]');
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(container.querySelector('[aria-label="操作詳細資訊"]')).toBe(
      selection
    );
    expect(selection?.textContent).toContain("檢查設定");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "可能過期"
    );
    expect(container.querySelector("time")?.dateTime).toBe(
      "2026-11-11T10:00:00.000Z"
    );
    await act(async () => button("重試")?.click());
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("time")?.dateTime).toBe(
      "2026-11-11T10:00:05.000Z"
    );
  });

  it("keeps the operation selected through a timeline block when newly received work overlaps it", async () => {
    const selectedOperation = {
      ...data.operations[0],
      id: "selected",
      summary: "檢查測試",
      startTime: 2000,
      endTime: 4000,
    };
    const first: SessionTimelineData = {
      ...data,
      operations: [data.operations[0], selectedOperation],
    };
    const updated: SessionTimelineData = {
      ...data,
      operations: [
        data.operations[0],
        {
          ...data.operations[0],
          id: "late",
          summary: "延遲收到的操作",
          startTime: 1500,
          endTime: 3000,
        },
        selectedOperation,
      ],
    };
    vi.mocked(loadSessionTimeline)
      .mockResolvedValueOnce(first)
      .mockResolvedValue(updated);
    await mount();
    const block = container.querySelector<HTMLButtonElement>(
      '[aria-label="檢查測試 · +1–3 秒"]'
    );
    expect(block).not.toBeNull();
    await act(async () => block?.click());
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(container.querySelector("h3")?.textContent).toBe("檢查測試");
  });

  it("does not overlap pending requests and cancels work when unmounted", async () => {
    let finish: ((value: SessionTimelineData) => void) | undefined;
    vi.mocked(loadSessionTimeline)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve;
        })
      )
      .mockResolvedValue(data);
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(20000));
    expect(loadSessionTimeline).toHaveBeenCalledTimes(1);
    await act(async () => finish?.(data));
    await act(async () => vi.advanceTimersByTimeAsync(4999));
    expect(loadSessionTimeline).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(loadSessionTimeline).toHaveBeenCalledTimes(2);
    const signal = vi.mocked(loadSessionTimeline).mock.calls[1][0].signal;
    await act(async () => root.render(null));
    expect(signal?.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(20000));
    expect(loadSessionTimeline).toHaveBeenCalledTimes(2);
  });

  it("discards an in-flight refresh when paused and only applies a resumed response", async () => {
    let finish: ((value: SessionTimelineData) => void) | undefined;
    const newer: SessionTimelineData = {
      ...data,
      operations: [{ ...data.operations[0], summary: "更新後設定" }],
    };
    vi.mocked(loadSessionTimeline)
      .mockResolvedValueOnce(data)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve;
        })
      )
      .mockResolvedValue(newer);
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    const signal = vi.mocked(loadSessionTimeline).mock.calls[1][0].signal;
    await act(async () => button("暫停更新")?.click());
    expect(signal?.aborted).toBe(true);
    await act(async () => finish?.(newer));
    expect(container.querySelector("h3")?.textContent).toBe("檢查設定");
    expect(container.querySelector("time")?.dateTime).toBe(
      "2026-11-11T10:00:00.000Z"
    );
    await act(async () => button("恢復更新")?.click());
    expect(container.querySelector("h3")?.textContent).toBe("更新後設定");
    expect(loadSessionTimeline).toHaveBeenCalledTimes(3);
  });

  it("ignores an older Session response after switching to another Session", async () => {
    let finish: ((value: SessionTimelineData) => void) | undefined;
    const other: SessionTimelineData = {
      ...data,
      sessionId: "session-2",
      operations: [{ ...data.operations[0], summary: "第二個 Session" }],
    };
    vi.mocked(loadSessionTimeline)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve;
        })
      )
      .mockResolvedValueOnce(other);
    await mount();
    const signal = vi.mocked(loadSessionTimeline).mock.calls[0][0].signal;
    await mount("session-2");
    expect(signal?.aborted).toBe(true);
    await act(async () => finish?.(data));
    expect(container.querySelector("h3")?.textContent).toBe("第二個 Session");
    expect(container.textContent).not.toContain("檢查設定");
    expect(loadSessionTimeline).toHaveBeenLastCalledWith(
      expect.objectContaining({ sessionId: "session-2" })
    );
  });

  it("lets the user retry an initial load failure without claiming an empty Session", async () => {
    vi.mocked(loadSessionTimeline)
      .mockRejectedValueOnce(new Error("load failed"))
      .mockResolvedValueOnce(data);
    await mount();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "load failed"
    );
    expect(container.textContent).not.toContain("沒有操作資料");
    await act(async () => button("重試")?.click());
    expect(container.querySelector("h3")?.textContent).toBe("檢查設定");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});
