import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";

import { SessionFiltersContext } from "../SessionFiltersContext";
import { SessionSourceFilter } from "../SessionSourceFilter";

it("filters by instrumentation source while preserving the user's condition", () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  function TestPage() {
    const [filterCondition, setFilterCondition] = useState("latency_ms > 1000");
    return (
      <SessionFiltersContext.Provider
        value={{
          filterCondition,
          setFilterCondition,
          appendFilterCondition: setFilterCondition,
        }}
      >
        <SessionSourceFilter />
        <output>{filterCondition}</output>
      </SessionFiltersContext.Provider>
    );
  }
  act(() => root.render(<TestPage />));
  const select = container.querySelector("select");
  if (!select) throw new Error("Source filter not rendered");
  act(() => {
    select.value = "codex";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelector("output")?.textContent).toContain(
    "latency_ms > 1000"
  );
  expect(container.querySelector("output")?.textContent).toContain(
    's.name == "run_turn"'
  );
  act(() => {
    select.value = "kiro";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelector("output")?.textContent).toContain(
    's.name == "kiro.session"'
  );
  expect(container.querySelector("output")?.textContent).not.toContain(
    's.name == "run_turn"'
  );
  act(() => {
    select.value = "all";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelector("output")?.textContent).toBe(
    "latency_ms > 1000"
  );
  act(() => root.unmount());
});
