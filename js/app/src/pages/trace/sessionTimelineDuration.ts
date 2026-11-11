import type {
  SessionOperation,
  SessionTimelineData,
} from "./sessionTimelineData";

export type DurationKind = SessionOperation["kind"] | "mixed" | "gap";
export type TimelineSegment = {
  startTime: number;
  endTime: number;
  kind: DurationKind;
  operations: SessionOperation[];
  provisional: boolean;
};

export function timelineWindow({
  data,
  turnId,
  observedAt,
}: {
  data: SessionTimelineData;
  turnId: string | null;
  observedAt: number;
}) {
  const operations =
    turnId === null
      ? data.operations
      : data.operations.filter((operation) => operation.turnId === turnId);
  const times = operations.map((operation) => ({
    start: operation.startTime,
    end: operation.endTime ?? Math.max(observedAt, operation.startTime),
  }));
  for (const span of data.spans) {
    if (
      turnId !== null &&
      span.attributes["turn.id"] !== turnId &&
      span.attributes["turn_id"] !== turnId
    )
      continue;
    const start = Date.parse(span.start_time);
    const end =
      span.end_time === null
        ? Math.max(observedAt, start)
        : Date.parse(span.end_time);
    if (Number.isFinite(start) && Number.isFinite(end))
      times.push({ start, end });
  }
  const startTime = times.reduce(
    (value, time) => Math.min(value, time.start),
    Infinity
  );
  const endTime = times.reduce(
    (value, time) => Math.max(value, time.end),
    -Infinity
  );
  return {
    operations,
    startTime: times.length ? startTime : observedAt,
    endTime: times.length ? endTime : observedAt,
  };
}

export function buildTimelineDuration({
  operations,
  startTime,
  endTime,
  observedAt,
}: {
  operations: SessionOperation[];
  startTime: number;
  endTime: number;
  observedAt: number;
}) {
  const boundaries = new Map<
    number,
    { starts: SessionOperation[]; ends: SessionOperation[] }
  >();
  function boundary(time: number) {
    let item = boundaries.get(time);
    if (!item) {
      item = { starts: [], ends: [] };
      boundaries.set(time, item);
    }
    return item;
  }
  boundary(startTime);
  boundary(endTime);
  for (const operation of operations) {
    const start = Math.max(startTime, operation.startTime);
    const end = Math.min(
      endTime,
      operation.endTime ?? Math.max(observedAt, operation.startTime)
    );
    if (end <= start) continue;
    boundary(start).starts.push(operation);
    boundary(end).ends.push(operation);
  }
  const times = [...boundaries.keys()].sort((a, b) => a - b);
  const active = new Map<string, SessionOperation>();
  const segments: TimelineSegment[] = [];
  const totals: Record<DurationKind, number> = {
    model: 0,
    tool: 0,
    other: 0,
    mixed: 0,
    gap: 0,
  };
  for (let index = 0; index < times.length - 1; index++) {
    const time = times[index];
    const next = times[index + 1];
    const event = boundaries.get(time);
    if (event) {
      for (const operation of event.ends) active.delete(operation.id);
      for (const operation of event.starts) active.set(operation.id, operation);
    }
    const current = [...active.values()];
    const kinds = new Set(current.map((operation) => operation.kind));
    const kind: DurationKind =
      kinds.size > 1 ? "mixed" : (current[0]?.kind ?? "gap");
    segments.push({
      startTime: time,
      endTime: next,
      kind,
      operations: current,
      provisional: current.some((operation) => operation.endTime === null),
    });
    totals[kind] += next - time;
  }
  return {
    startTime,
    endTime,
    duration: Math.max(0, endTime - startTime),
    segments,
    totals,
  };
}
