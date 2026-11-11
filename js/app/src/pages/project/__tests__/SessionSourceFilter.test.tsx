import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  SessionFiltersProvider,
  useSessionFilters,
} from "../SessionFiltersContext";
import { SessionSourceFilter } from "../SessionSourceFilter";

describe("Session source presets", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function Page() {
    const { filterCondition, setFilterCondition } = useSessionFilters();
    const location = useLocation();
    return (
      <>
        <SessionSourceFilter />
        <button
          onClick={() =>
            setFilterCondition(`${filterCondition} and num_traces < 10`)
          }
        >
          Append condition
        </button>
        <output data-condition>{filterCondition}</output>
        <output data-search>{location.search}</output>
      </>
    );
  }
  async function renderPage(condition: string, source?: string) {
    const params = new URLSearchParams({ sessionFilterCondition: condition });
    if (source) params.set("sessionSource", source);
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={[`/sessions?${params}`]}>
          <SessionFiltersProvider>
            <Page />
          </SessionFiltersProvider>
        </MemoryRouter>
      )
    );
  }
  async function selectSource(value: string) {
    const select = container.querySelector("select");
    if (!select) throw new Error("Source filter not rendered");
    await act(async () => {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("does not truncate manual DSL that ends with a known source predicate", async () => {
    const condition =
      'latency_ms > 1000 and (any(s.name == "run_turn" for s in spans))';
    await renderPage(condition, "codex");
    await selectSource("all");
    expect(container.querySelector("[data-condition]")?.textContent).toBe(
      condition
    );
    expect(
      new URLSearchParams(
        container.querySelector("[data-search]")?.textContent ?? ""
      ).get("sessionFilterCondition")
    ).toBe(condition);
    expect(container.querySelector("[data-search]")?.textContent).not.toContain(
      "sessionSource"
    );
  });

  it("keeps presets separate from manually edited conditions", async () => {
    await renderPage("num_traces >= 5");
    await selectSource("codex");
    expect(container.querySelector("[data-condition]")?.textContent).toBe(
      "num_traces >= 5"
    );
    expect(container.querySelector("[data-search]")?.textContent).toContain(
      "sessionSource=codex"
    );
    await act(async () => container.querySelector("button")?.click());
    await selectSource("kiro");
    expect(container.querySelector("[data-condition]")?.textContent).toBe(
      "num_traces >= 5 and num_traces < 10"
    );
    expect(container.querySelector("[data-search]")?.textContent).toContain(
      "sessionSource=kiro"
    );
    await selectSource("all");
    expect(container.querySelector("[data-condition]")?.textContent).toBe(
      "num_traces >= 5 and num_traces < 10"
    );
  });
});
