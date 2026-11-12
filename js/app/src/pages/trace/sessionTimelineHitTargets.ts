import type { TimelineSegment } from "./sessionTimelineDuration";

/** Hit geometry only: original timing and parallel membership stay unchanged. */
export function timelineHitCandidates({
  segments,
  startTime,
  duration,
  width,
  pointerX,
}: {
  segments: TimelineSegment[];
  startTime: number;
  duration: number;
  width: number;
  pointerX: number;
}): TimelineSegment[] {
  if (width <= 0 || duration <= 0) return [];
  const x = Math.max(0, Math.min(width, pointerX));
  const radius = (12 / width) * duration;
  const time = startTime + (x / width) * duration;
  return segments.filter(
    (segment) =>
      segment.startTime <= time + radius && segment.endTime >= time - radius
  );
}
