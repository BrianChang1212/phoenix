import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionTimeline } from "../SessionTimeline";
import type { SessionTimelineData } from "../sessionTimelineData";

const data: SessionTimelineData = {
  id: "session-node",
  sessionId: "session-navigation",
  projectId: "project",
  source: "codex",
  operations: [],
  spans: [
    {
      id: "root-node",
      name: "exec",
      context: { span_id: "root", trace_id: "trace" },
      parent_id: null,
      span_kind: "UNKNOWN",
      start_time: "2026-11-15T00:00:00Z",
      end_time: "2026-11-15T00:00:10Z",
      status_code: "UNSET",
      attributes: {},
      events: [],
    },
    {
      id: "child-node",
      name: "exec_command",
      context: { span_id: "child", trace_id: "trace" },
      parent_id: "root",
      span_kind: "TOOL",
      start_time: "2026-11-15T00:00:02Z",
      end_time: "2026-11-15T00:00:04Z",
      status_code: "OK",
      attributes: {},
      events: [],
    },
  ],
};

describe("Hierarchy timeline navigation", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    act(() =>
      root.render(
        <MemoryRouter>
          <SessionTimeline
            data={data}
            observedAt={Date.parse("2026-11-15T00:00:10Z")}
          />
        </MemoryRouter>
      )
    );
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });
  const button = (label: string) =>
    container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  const range = () =>
    container.querySelector('[aria-label="可視時間範圍"]')?.textContent;

  it("consumes Escape while clearing detail so the enclosing drawer stays open", () => {
    const outerEscape = vi.fn();
    document.addEventListener("keydown", outerEscape);
    act(() => button("選取 exec_command")?.click());
    const plot = container.querySelector('[aria-label="時間軸導覽區"]');
    act(() =>
      plot?.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
          cancelable: true,
        })
      )
    );
    expect(container.querySelector('[aria-label="選取的呼叫結果"]')).toBeNull();
    expect(outerEscape).not.toHaveBeenCalled();
    document.removeEventListener("keydown", outerEscape);
  });

  it("zooms the real visible range and resets it without clearing selected detail", () => {
    expect(range()).toContain("0.000s → 10.000s");
    act(() => button("選取 exec_command")?.click());
    act(() => button("放大時間軸")?.click());
    expect(range()).toContain("2.500s → 7.500s");
    expect(
      container.querySelector('[aria-label="選取的呼叫結果"]')
    ).not.toBeNull();
    act(() => button("全程時間軸")?.click());
    expect(range()).toContain("0.000s → 10.000s");
    expect(
      container.querySelector('[aria-label="選取的呼叫結果"]')
    ).not.toBeNull();
  });

  it("zooms at the pointer and supports keyboard navigation without stealing input keys", () => {
    const plot = container.querySelector<HTMLElement>(
      '[aria-label="時間軸導覽區"]'
    );
    expect(plot).not.toBeNull();
    if (!plot) throw new Error("Missing timeline navigation");
    const track = plot.querySelector<HTMLElement>(".hierarchy-track");
    if (!track) throw new Error("Missing timeline track");
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue(
      new DOMRect(300, 0, 700, 100)
    );
    act(() =>
      plot.dispatchEvent(
        new WheelEvent("wheel", {
          deltaY: -Math.log(2) / 0.002,
          clientX: 650,
          bubbles: true,
          cancelable: true,
        })
      )
    );
    expect(range()).toContain("2.500s → 7.500s");
    act(() =>
      plot.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })
      )
    );
    expect(range()).toContain("3.500s → 8.500s");
    const input = document.createElement("input");
    plot.append(input);
    act(() =>
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "-", bubbles: true })
      )
    );
    expect(range()).toContain("3.500s → 8.500s");
    input.remove();
    act(() =>
      plot.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Home", bubbles: true })
      )
    );
    expect(range()).toContain("0.000s → 10.000s");
  });

  it("brushes, pans and resizes the same time range through actual pointer handlers", () => {
    const plot = container.querySelector<HTMLElement>(
      '[aria-label="時間軸導覽區"]'
    );
    const overview = container.querySelector<SVGSVGElement>(
      '[aria-label="全程時間軸縮圖"]'
    );
    const track = plot?.querySelector<HTMLElement>(".hierarchy-track");
    if (!plot || !overview || !track)
      throw new Error("Missing navigation surface");
    vi.spyOn(plot, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 1000, 100)
    );
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue(
      new DOMRect(300, 0, 700, 34)
    );
    vi.spyOn(overview, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 1000, 108)
    );
    function pointer(
      target: Element,
      type: string,
      x: number,
      shiftKey = false
    ) {
      const event = new MouseEvent(type, {
        clientX: x,
        bubbles: true,
        shiftKey,
        button: 0,
      });
      Object.defineProperty(event, "pointerId", { value: 1 });
      act(() => target.dispatchEvent(event));
    }
    pointer(plot, "pointerdown", 475, true);
    pointer(plot, "pointermove", 825, true);
    pointer(plot, "pointerup", 825, true);
    expect(range()).toContain("2.500s → 7.500s");
    pointer(plot, "pointerdown", 650);
    pointer(plot, "pointermove", 580);
    pointer(plot, "pointerup", 580);
    expect(range()).toContain("3.000s → 8.000s");
    pointer(overview, "pointerdown", 200);
    pointer(overview, "pointermove", 600);
    pointer(overview, "pointerup", 600);
    expect(range()).toContain("2.000s → 6.000s");
    const startHandle = overview.querySelector(
      '[aria-label="調整時間範圍起點"]'
    );
    if (!startHandle) throw new Error("Missing start handle");
    pointer(startHandle, "pointerdown", 200);
    pointer(overview, "pointermove", 300);
    pointer(overview, "pointerup", 300);
    expect(range()).toContain("3.000s → 6.000s");
    const keyboardHandle = overview.querySelector(
      '[aria-label="調整時間範圍起點"]'
    );
    act(() =>
      keyboardHandle?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })
      )
    );
    expect(range()).toContain("2.900s → 6.000s");
  });
});
