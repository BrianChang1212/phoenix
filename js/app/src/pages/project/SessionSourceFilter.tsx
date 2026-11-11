import { useSessionFilters } from "./SessionFiltersContext";

const codexCondition = 'any(s.name == "run_turn" for s in spans)';
const kiroCondition =
  'any(s.name == "kiro.observed_tool_window" or s.name == "kiro.session" for s in spans)';
const sourceConditions = {
  codex: codexCondition,
  kiro: kiroCondition,
  unknown: `not (${codexCondition}) and not (${kiroCondition})`,
};

export function SessionSourceFilter() {
  const { filterCondition, setFilterCondition } = useSessionFilters();
  const current = Object.entries(sourceConditions).find(
    ([, condition]) =>
      filterCondition === condition ||
      filterCondition.endsWith(` and (${condition})`)
  );
  return (
    <label
      style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}
    >
      Source
      <select
        aria-label="Session source"
        value={current?.[0] ?? "all"}
        title="依已觀測 instrumentation span 名稱篩選；未知不代表沒有來源。"
        style={{
          color: "inherit",
          background: "var(--global-background-color-100)",
          border: "1px solid var(--global-border-color-default)",
          borderRadius: 8,
          padding: 8,
        }}
        onChange={(event) => {
          let base = filterCondition;
          if (current) {
            base =
              base === current[1]
                ? ""
                : base.slice(1, -(` and (${current[1]})`.length + 1));
          }
          const next = Object.entries(sourceConditions).find(
            ([key]) => key === event.target.value
          )?.[1];
          setFilterCondition(
            next ? (base ? `(${base}) and (${next})` : next) : base
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
