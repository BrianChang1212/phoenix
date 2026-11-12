import { describe, expect, it } from "vitest";

import { timelineHitCandidates } from "../sessionTimelineHitTargets";
import type { TimelineSegment } from "../sessionTimelineDuration";

const segment = (startTime: number, endTime: number): TimelineSegment => ({
  startTime,
  endTime,
  kind: "tool",
  operations: [],
  provisional: false,
});

describe("timeline hit candidates", () => {
  it("keeps nearby sequential subpixel intervals separately selectable", () => {
    const segments = [segment(487, 721), segment(5296, 6208)];
    const candidates = timelineHitCandidates({
      segments,
      startTime: 0,
      duration: 1834000,
      width: 1000,
      pointerX: 0.3,
    });
    expect(candidates).toEqual(segments);
    expect(candidates[0]).not.toBe(candidates[1]);
  });

  it("makes each original interval reachable without changing its timing", () => {
    const segments = [segment(0, 1), segment(100, 110), segment(999, 1000)];
    for (const target of segments) {
      expect(
        timelineHitCandidates({
          segments,
          startTime: 0,
          duration: 1000,
          width: 650,
          pointerX: ((target.startTime + target.endTime) / 2 / 1000) * 650,
        })
      ).toContain(target);
    }
    expect(
      timelineHitCandidates({
        segments,
        startTime: 0,
        duration: 1000,
        width: 650,
        pointerX: -100,
      })
    ).toEqual([segments[0]]);
    expect(
      timelineHitCandidates({
        segments,
        startTime: 0,
        duration: 1000,
        width: 650,
        pointerX: 1000,
      })
    ).toEqual([segments[2]]);
  });
});
