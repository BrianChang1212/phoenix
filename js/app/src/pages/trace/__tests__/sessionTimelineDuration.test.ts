import { describe, expect, it } from "vitest";

import type {
  SessionOperation,
  SessionTimelineData,
} from "../sessionTimelineData";
import {
  buildTimelineDuration,
  timelineWindow,
} from "../sessionTimelineDuration";

function operation(
  id: string,
  kind: SessionOperation["kind"],
  startTime: number,
  endTime: number | null
): SessionOperation {
  return {
    id,
    spanId: id,
    traceId: "trace",
    parentId: null,
    name: id,
    summary: null,
    startTime,
    endTime,
    kind,
    status: endTime === null ? "running" : "unknown",
    source: "codex",
    turnId: null,
    attributes: {},
    events: [],
  };
}

describe("session elapsed time", () => {
  it("counts overlap once and leaves mixed work and missing intervals explicit", () => {
    const timeline = buildTimelineDuration({
      operations: [
        operation("model", "model", 0, 4000),
        operation("tool", "tool", 2000, 6000),
      ],
      startTime: 0,
      endTime: 8000,
      observedAt: 8000,
    });
    expect(timeline.totals).toEqual({
      model: 2000,
      tool: 2000,
      other: 0,
      mixed: 2000,
      gap: 2000,
    });
    expect(
      timeline.segments.map((s) => [
        s.startTime,
        s.endTime,
        s.kind,
        s.operations.length,
      ])
    ).toEqual([
      [0, 2000, "model", 1],
      [2000, 4000, "mixed", 2],
      [4000, 6000, "tool", 1],
      [6000, 8000, "gap", 0],
    ]);
    expect(timeline.duration).toBe(8000);
  });

  it("counts same-kind parallel work once and exposes each operation", () => {
    const result = buildTimelineDuration({
      operations: [
        operation("first", "tool", 0, 4000),
        operation("second", "tool", 0, 2000),
      ],
      startTime: 0,
      endTime: 4000,
      observedAt: 4000,
    });
    expect(result.totals.tool).toBe(4000);
    expect(result.totals.mixed).toBe(0);
    expect(result.segments[0].operations.map((item) => item.id)).toEqual([
      "first",
      "second",
    ]);
  });

  it("marks unfinished intervals provisional and retains zero-duration operations outside the strip", () => {
    const result = buildTimelineDuration({
      operations: [
        operation("running", "tool", 1000, null),
        operation("instant", "model", 2000, 2000),
      ],
      startTime: 0,
      endTime: 4000,
      observedAt: 4000,
    });
    expect(result.totals).toEqual({
      tool: 3000,
      model: 0,
      mixed: 0,
      other: 0,
      gap: 1000,
    });
    expect(result.segments[1].provisional).toBe(true);
    expect(result.segments[1].operations[0].endTime).toBeNull();
  });

  it("uses the explicit turn envelope rather than inventing a turn from a trace", () => {
    const first = {
      ...operation("tool", "tool", 2000, 4000),
      turnId: "turn-one",
    };
    const data: SessionTimelineData = {
      id: "s",
      sessionId: "s",
      projectId: "p",
      source: "codex",
      operations: [first],
      spans: [
        {
          id: "turn",
          name: "run_turn",
          context: { span_id: "turn", trace_id: "trace" },
          span_kind: "CHAIN",
          start_time: "1970-01-01T00:00:01Z",
          end_time: "1970-01-01T00:00:06Z",
          status_code: "UNSET",
          attributes: { "turn.id": "turn-one" },
          events: [],
        },
      ],
    };
    const window = timelineWindow({
      data,
      turnId: "turn-one",
      observedAt: 10000,
    });
    expect(window.startTime).toBe(1000);
    expect(window.endTime).toBe(6000);
    expect(
      timelineWindow({ data, turnId: "trace", observedAt: 10000 }).operations
    ).toEqual([]);
  });
});
