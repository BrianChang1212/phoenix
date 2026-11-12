import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ProjectProvider } from "@phoenix/contexts/ProjectContext";

import { SideNavContent } from "../Layout";
import { ProjectIndexPage } from "../project/ProjectIndexPage";

describe("Tracing navigation", () => {
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
    localStorage.clear();
  });

  it.each([true, false])(
    "only offers Tracing and Settings with expanded=%s",
    async (isExpanded) => {
      await act(async () => {
        root.render(
          <MemoryRouter>
            <SideNavContent isExpanded={isExpanded} />
          </MemoryRouter>
        );
      });
      const navigationLinks = Array.from(
        container.querySelectorAll("nav ul a")
      ).map((link) => ({
        label: link.textContent,
        href: link.getAttribute("href"),
      }));
      expect(navigationLinks).toEqual([
        { label: "Tracing", href: "/projects" },
        { label: "Settings", href: "/settings" },
      ]);
      expect(container.querySelectorAll("nav ul button")).toHaveLength(0);
    }
  );

  it("opens Sessions from a project root while preserving URL filters", async () => {
    function Destination() {
      const location = useLocation();
      return (
        <output>{location.pathname + location.search + location.hash}</output>
      );
    }
    await act(async () => {
      root.render(
        <MemoryRouter
          initialEntries={["/projects/project-1?timeRangeKey=7d#details"]}
        >
          <ProjectProvider projectId="project-1">
            <Routes>
              <Route path="/projects/:projectId">
                <Route index element={<ProjectIndexPage />} />
                <Route path="sessions" element={<Destination />} />
                <Route path="spans" element={<Destination />} />
              </Route>
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      );
    });
    expect(container.querySelector("output")?.textContent).toBe(
      "/projects/project-1/sessions?timeRangeKey=7d#details"
    );
  });
});
