import { useSearchParams } from "react-router";

import {
  readSessionSource,
  SESSION_SOURCE_PARAM,
} from "./sessionSourceCondition";

export function SessionSourceFilter() {
  const [searchParams, setSearchParams] = useSearchParams();
  return (
    <label
      style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}
    >
      Source
      <select
        aria-label="Session source"
        value={readSessionSource(searchParams)}
        title="依已觀測 instrumentation span 名稱篩選；未知不代表沒有來源。"
        style={{
          color: "inherit",
          background: "var(--global-background-color-100)",
          border: "1px solid var(--global-border-color-default)",
          borderRadius: 8,
          padding: 8,
        }}
        onChange={(event) => {
          const source = event.target.value;
          setSearchParams(
            (previous) => {
              const next = new URLSearchParams(previous);
              if (source === "all") next.delete(SESSION_SOURCE_PARAM);
              else next.set(SESSION_SOURCE_PARAM, source);
              return next;
            },
            { replace: true }
          );
        }}
      >
        <option value="all">All</option>
        <option value="codex">Codex</option>
        <option value="kiro">Kiro</option>
        <option value="unknown">Unknown</option>
      </select>
    </label>
  );
}
