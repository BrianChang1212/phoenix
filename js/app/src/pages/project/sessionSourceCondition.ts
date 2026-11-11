export const SESSION_SOURCE_PARAM = "sessionSource";

const codexCondition = 'any(s.name == "run_turn" for s in spans)';
const kiroCondition =
  'any(s.name == "kiro.observed_tool_window" or s.name == "kiro.session" for s in spans)';
const sourceConditions = {
  codex: codexCondition,
  kiro: kiroCondition,
  unknown: `not (${codexCondition}) and not (${kiroCondition})`,
};

export type SessionSourcePreset = "all" | keyof typeof sourceConditions;

export function readSessionSource(
  params: URLSearchParams
): SessionSourcePreset {
  const value = params.get(SESSION_SOURCE_PARAM);
  return value === "codex" || value === "kiro" || value === "unknown"
    ? value
    : "all";
}

export function withSessionSourceCondition({
  condition,
  source,
}: {
  condition: string;
  source: SessionSourcePreset;
}): string {
  if (source === "all") return condition;
  return condition.trim()
    ? `(${condition}) and (${sourceConditions[source]})`
    : sourceConditions[source];
}
