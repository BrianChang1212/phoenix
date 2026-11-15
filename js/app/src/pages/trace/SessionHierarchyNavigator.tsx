import { css } from "@emotion/react";

import {
  hierarchyCategory,
  hierarchyCategoryStyle,
  type HierarchyNode,
} from "./sessionHierarchy";
import type { HierarchyNavigation } from "./useHierarchyNavigation";

const navigatorCSS = css`
  margin-top: 12px;
  svg {
    display: block;
    width: 100%;
    height: 108px;
    touch-action: none;
  }
  .navigation-controls {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 7px;
    margin: 12px 0;
  }
  output {
    margin-left: auto;
    color: #a7be9c;
    font-size: 11px;
  }
  button[aria-label="放大時間軸"] {
    background: #92ed20;
    color: #071209;
    font-weight: bold;
  }
  button:disabled {
    opacity: 0.35;
    cursor: default;
  }
  [role="slider"] {
    cursor: ew-resize;
    outline: none;
  }
  [role="slider"]:focus rect {
    stroke: #e4f3dc;
  }
  @media (max-width: 650px) {
    output {
      margin-left: 0;
      flex-basis: 100%;
    }
  }
`;

export function SessionHierarchyNavigator({
  navigation,
  nodes,
  observedAt,
}: {
  navigation: HierarchyNavigation;
  nodes: HierarchyNode[];
  observedAt: number;
}) {
  const { view, full } = navigation;
  const total = full.end - full.start;
  const width = view.end - view.start;
  const start = ((view.start - full.start) / total) * 1000;
  const end = ((view.end - full.start) / total) * 1000;
  const layer = Math.min(
    8,
    62 / (Math.max(0, ...nodes.map((node) => node.depth)) + 1)
  );
  return (
    <div css={navigatorCSS}>
      <svg
        ref={navigation.overviewRef}
        {...navigation.overviewEvents}
        viewBox="0 0 1000 108"
        preserveAspectRatio="none"
        aria-label="全程時間軸縮圖"
      >
        <rect
          x="0"
          y="4"
          width="1000"
          height="80"
          rx="4"
          fill="#071209"
          stroke="#24412a"
        />
        {nodes.map((node) => (
          <rect
            key={node.key}
            x={((node.startTime - full.start) / total) * 1000}
            y={12 + node.depth * layer}
            width={Math.max(
              1,
              (((node.endTime ?? Math.max(node.startTime, observedAt)) -
                node.startTime) /
                total) *
                1000
            )}
            height={Math.max(2, layer - 1.5)}
            fill={hierarchyCategoryStyle[hierarchyCategory(node)].color}
            opacity=".85"
          />
        ))}
        <rect
          x="0"
          y="8"
          width={start}
          height="72"
          fill="#030906"
          opacity=".7"
        />
        <rect
          x={end}
          y="8"
          width={1000 - end}
          height="72"
          fill="#030906"
          opacity=".7"
        />
        <rect
          x={start}
          y="7"
          width={Math.max(1, end - start)}
          height="74"
          rx="3"
          fill="#a8df7110"
          stroke="#577c46"
        />
        {(["start", "end"] satisfies ("start" | "end")[]).map((edge) => {
          const position = edge === "start" ? start : end;
          return (
            <g
              key={edge}
              role="slider"
              tabIndex={0}
              data-hierarchy-handle={edge}
              aria-label={
                edge === "start" ? "調整時間範圍起點" : "調整時間範圍終點"
              }
              aria-valuemin={full.start}
              aria-valuemax={full.end}
              aria-valuenow={view[edge]}
              aria-valuetext={
                ((view[edge] - full.start) / 1000).toFixed(3) + "s"
              }
              onKeyDown={(event) => {
                if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
                event.preventDefault();
                event.stopPropagation();
                const delta =
                  total * 0.01 * (event.key === "ArrowRight" ? 1 : -1);
                const minimum = Math.min(20, total);
                navigation.setWindow(
                  edge === "start"
                    ? {
                        start: Math.max(
                          full.start,
                          Math.min(view.end - minimum, view.start + delta)
                        ),
                        end: view.end,
                      }
                    : {
                        start: view.start,
                        end: Math.min(
                          full.end,
                          Math.max(view.start + minimum, view.end + delta)
                        ),
                      }
                );
              }}
            >
              <rect
                x={Math.max(0, Math.min(986, position - 7))}
                y="7"
                width="14"
                height="74"
                rx="3"
                fill="transparent"
              />
              <rect
                x={Math.max(0, Math.min(997, position - 1.5))}
                y="32"
                width="3"
                height="20"
                rx="1.5"
                fill="#a3ed52"
                pointerEvents="none"
              />
            </g>
          );
        })}
        {navigation.brush?.surface === "overview" &&
          navigation.brush.containerWidth > 0 && (
            <rect
              x={
                (navigation.brush.left / navigation.brush.containerWidth) * 1000
              }
              y="5"
              width={
                (navigation.brush.width / navigation.brush.containerWidth) *
                1000
              }
              height="77"
              fill="#b1ec7b33"
              stroke="#b1ec7b"
              pointerEvents="none"
            />
          )}
        <text x="0" y="104" fill="#a7be9c" fontSize="11">
          0s
        </text>
        <text x="1000" y="104" textAnchor="end" fill="#a7be9c" fontSize="11">
          {(total / 1000).toFixed(3)}s
        </text>
      </svg>
      <div className="navigation-controls">
        <button
          type="button"
          aria-label="縮小時間軸"
          disabled={width >= total}
          onClick={() => navigation.zoom(2)}
        >
          −
        </button>
        <button
          type="button"
          aria-label="放大時間軸"
          disabled={width <= Math.min(20, total)}
          onClick={() => navigation.zoom(0.5)}
        >
          ＋
        </button>
        <button
          type="button"
          aria-label="全程時間軸"
          onClick={navigation.reset}
        >
          全程
        </button>
        <output aria-label="可視時間範圍">
          {((view.start - full.start) / 1000).toFixed(3)}s →{" "}
          {((view.end - full.start) / 1000).toFixed(3)}s ·{" "}
          {(total / width).toFixed(1)}×
        </output>
      </div>
    </div>
  );
}
