import type {
  SessionOperation,
  SessionTimelineData,
  SessionTimelineSpan,
} from "./sessionTimelineData";

export type HierarchyCategory = "tool" | "model" | "flow" | "storage" | "other";
export type HierarchyNode = {
  key: string;
  span: SessionTimelineSpan;
  operation: SessionOperation | null;
  parentKey: string | null;
  parentState: "root" | "linked" | "missing" | "invalid";
  children: HierarchyNode[];
  depth: number;
  startTime: number;
  endTime: number | null;
};
export type SessionHierarchy = {
  nodes: HierarchyNode[];
  roots: HierarchyNode[];
  invalidCount: number;
};

export const hierarchyCategoryStyle: Record<
  HierarchyCategory,
  { label: string; color: string }
> = {
  tool: { label: "工具", color: "#28bfff" },
  model: { label: "模型", color: "#c17aff" },
  flow: { label: "流程", color: "#10efb6" },
  storage: { label: "寫入", color: "#ffc14a" },
  other: { label: "觀測／其他", color: "#8ba7b0" },
};

export function hierarchyKey(traceId: string, spanId: string) {
  return JSON.stringify([traceId, spanId]);
}

export function hierarchyCategory(node: HierarchyNode): HierarchyCategory {
  const name = node.span.name;
  if (/persist_rollout|append_items/.test(name)) return "storage";
  if (
    node.span.span_kind === "LLM" ||
    /sampling|handle_responses|model_request/.test(name)
  )
    return "model";
  if (/observation|guardian|lock|tool_blocking/.test(name)) return "other";
  if (
    node.span.span_kind === "TOOL" ||
    /exec_command|dispatch_tool|handle_tool_call|invoke_tool|collect_output/.test(
      name
    )
  )
    return "tool";
  if (/^exec$|code_mode|process|open_session|realtime_conversation/.test(name))
    return "flow";
  return "other";
}

export function buildSessionHierarchy(
  data: SessionTimelineData
): SessionHierarchy {
  const operations = new Map(
    data.operations.map((operation) => [
      hierarchyKey(operation.traceId, operation.spanId),
      operation,
    ])
  );
  const nodes = new Map<string, HierarchyNode>();
  let invalidCount = 0;
  for (const span of data.spans) {
    const startTime = Date.parse(span.start_time);
    const endTime = span.end_time === null ? null : Date.parse(span.end_time);
    if (
      !Number.isFinite(startTime) ||
      (endTime !== null && (!Number.isFinite(endTime) || endTime < startTime))
    ) {
      invalidCount++;
      continue;
    }
    const key = hierarchyKey(span.context.trace_id, span.context.span_id);
    nodes.set(key, {
      key,
      span,
      operation: operations.get(key) ?? null,
      parentKey: span.parent_id
        ? hierarchyKey(span.context.trace_id, span.parent_id)
        : null,
      parentState: "root",
      children: [],
      depth: 0,
      startTime,
      endTime,
    });
  }
  const invalid = new Set<string>();
  for (const node of nodes.values()) {
    const seen = new Set<string>();
    let current: HierarchyNode | undefined = node;
    while (current) {
      if (seen.has(current.key)) {
        for (const key of seen) invalid.add(key);
        break;
      }
      seen.add(current.key);
      current = current.parentKey ? nodes.get(current.parentKey) : undefined;
    }
  }
  const roots: HierarchyNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parentKey ? nodes.get(node.parentKey) : undefined;
    if (invalid.has(node.key)) {
      node.parentState = "invalid";
      roots.push(node);
    } else if (parent) {
      node.parentState = "linked";
      parent.children.push(node);
    } else {
      node.parentState = node.parentKey ? "missing" : "root";
      roots.push(node);
    }
  }
  const order = (a: HierarchyNode, b: HierarchyNode) =>
    a.startTime - b.startTime || a.key.localeCompare(b.key);
  roots.sort(order);
  for (const node of nodes.values()) node.children.sort(order);
  const pending = roots.map((node) => ({ node, depth: 0 }));
  for (let index = 0; index < pending.length; index++) {
    const item = pending[index];
    item.node.depth = item.depth;
    for (const node of item.node.children)
      pending.push({ node, depth: item.depth + 1 });
  }
  return {
    nodes: Array.from(nodes.values()),
    roots,
    invalidCount: invalidCount + invalid.size,
  };
}

export function visibleHierarchyRows(
  hierarchy: SessionHierarchy,
  expanded: ReadonlySet<string>
) {
  const rows: HierarchyNode[] = [];
  const pending = [...hierarchy.roots].reverse();
  while (pending.length) {
    const node = pending.pop();
    if (!node) continue;
    rows.push(node);
    if (expanded.has(node.key)) pending.push(...[...node.children].reverse());
  }
  return rows;
}
