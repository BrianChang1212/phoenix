import { useEffect, useState } from "react";

import {
  loadSessionTimeline,
  type SessionTimelineData,
} from "./sessionTimelineData";

type Snapshot = { data: SessionTimelineData; observedAt: number };
type TimelineLoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; snapshot: Snapshot; refreshError: string | null };

const REFRESH_INTERVAL_MS = 5000;

export function useSessionTimelineRefresh({
  sessionId,
}: {
  sessionId: string;
}) {
  const [state, setState] = useState<TimelineLoadState>({ kind: "loading" });
  const [mode, setMode] = useState<"live" | "paused">("live");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (mode === "paused") return undefined;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function refresh() {
      try {
        const data = await loadSessionTimeline({
          sessionId,
          signal: controller.signal,
        });
        if (active) {
          setState({
            kind: "ready",
            snapshot: { data, observedAt: Date.now() },
            refreshError: null,
          });
        }
      } catch (error: unknown) {
        if (active) {
          const message = error instanceof Error ? error.message : "讀取失敗";
          setState((previous) =>
            previous.kind === "ready"
              ? { ...previous, refreshError: message }
              : { kind: "error", message }
          );
        }
      }
      if (active) timer = setTimeout(refresh, REFRESH_INTERVAL_MS);
    }
    void refresh();
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [sessionId, mode, attempt]);

  return {
    state,
    mode,
    toggleMode: () =>
      setMode((previous) => (previous === "live" ? "paused" : "live")),
    retry: () => setAttempt((previous) => previous + 1),
  };
}
