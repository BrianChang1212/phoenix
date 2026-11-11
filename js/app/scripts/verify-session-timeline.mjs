import assert from "node:assert/strict";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { chromium } from "playwright";

const { values } = parseArgs({
  options: {
    "base-url": { type: "string", default: "http://127.0.0.1:6007" },
    "output-dir": { type: "string" },
    channel: { type: "string", default: "msedge" },
    project: { type: "string" },
    "session-id": { type: "string" },
    source: { type: "string" },
  },
});
const base = new URL(values["base-url"]);
assert(
  base.protocol === "http:" &&
    ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname) &&
    !base.username &&
    !base.password &&
    !base.search &&
    !base.hash,
  "Only a loopback HTTP base URL is supported"
);
assert(values["output-dir"], "--output-dir must name a new evidence directory");
assert(
  Boolean(values.project) === Boolean(values["session-id"]),
  "--project and --session-id must be supplied together"
);
if (values.project)
  assert(
    ["codex", "kiro"].includes(values.source),
    "Custom fixture needs --source codex|kiro"
  );
const outputDir = resolve(values["output-dir"]);
await mkdir(outputDir, { recursive: true });
assert.equal(
  (await readdir(outputDir)).length,
  0,
  "Evidence directory must be empty"
);
const url = (path) =>
  `${base.origin}${base.pathname.replace(/\/$/, "")}${path}`;
const fixtures = values.project
  ? [
      {
        project: values.project,
        sessionId: values["session-id"],
        source: values.source,
      },
    ]
  : [
      {
        project: "default",
        sessionId: "01a1bbd4-398b-7bf2-89d8-d6d071a6d7c9",
        source: "codex",
      },
      {
        project: "kiro-local",
        sessionId: "sess_7c8e63c3-8b60-4a09-8744-6185b1b94d9a",
        source: "kiro",
      },
    ];

async function read(path) {
  const response = await fetch(url(path));
  assert(response.ok, `Local API returned ${response.status}`);
  return response.json();
}

async function findSession(fixture) {
  let cursor = null;
  const seen = new Set();
  do {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const page = await read(
      `/v1/projects/${encodeURIComponent(fixture.project)}/sessions?${query}`
    );
    const session = page.data.find(
      (item) => item.session_id === fixture.sessionId
    );
    if (session) return session;
    cursor = page.next_cursor;
    if (cursor) {
      assert(!seen.has(cursor), "Repeated session cursor");
      seen.add(cursor);
    }
  } while (cursor);
  throw new Error(
    `Requested ${fixture.source} validation fixture was not found`
  );
}

async function readSessionSpans(session) {
  const result = [];
  for (const trace of session.traces) {
    let cursor = null;
    const seen = new Set();
    do {
      const query = new URLSearchParams({
        limit: "1000",
        trace_id: trace.trace_id,
      });
      if (cursor) query.set("cursor", cursor);
      const page = await read(
        `/v1/projects/${encodeURIComponent(session.project_id)}/spans?${query}`
      );
      result.push(...page.data);
      cursor = page.next_cursor;
      if (cursor) {
        assert(!seen.has(cursor), "Repeated span cursor");
        seen.add(cursor);
      }
    } while (cursor);
  }
  return result;
}

let browser;
const report = { status: "running", base_url: base.href, sessions: [] };
try {
  browser = await chromium.launch({ channel: values.channel, headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.name));
  for (const fixture of fixtures) {
    const session = await findSession(fixture);
    const spans = await readSessionSpans(session);
    await page.setViewportSize({ width: 1440, height: 1100 });
    const timeRange = new URLSearchParams({
      timeRangeStart: new Date(
        Date.parse(session.start_time) - 60000
      ).toISOString(),
      timeRangeEnd: new Date(
        Date.parse(session.end_time) + 60000
      ).toISOString(),
    });
    await page.goto(
      url(
        `/projects/${encodeURIComponent(session.project_id)}/sessions?${timeRange}`
      )
    );
    await page
      .getByRole("combobox", { name: "Session source", exact: true })
      .selectOption(fixture.source);
    const sessionRow = page
      .getByRole("row")
      .filter({ hasText: fixture.sessionId })
      .first();
    await sessionRow.waitFor({ state: "visible" });
    await sessionRow.click();
    await page.getByRole("tab", { name: "Timeline", exact: true }).click();
    const timeline = page.getByRole("region", {
      name: "Session 操作時間軸",
      exact: true,
    });
    const detail = timeline.getByRole("region", {
      name: "操作詳細資訊",
      exact: true,
    });
    await detail.waitFor({ state: "visible" });
    await timeline
      .getByRole("button", { name: "暫停更新", exact: true })
      .click();
    const names = timeline.getByRole("button", { name: /^\d+ ·/ });
    const operationCount = await names.count();
    assert(operationCount > 0, "Timeline must expose selectable operations");
    await names.first().click();
    const href = await detail
      .getByRole("link", { name: "開啟原始 Trace", exact: true })
      .getAttribute("href");
    const selectedSpanId = new URL(href, base).searchParams.get(
      "selectedSpanNodeId"
    );
    const rawSpan = spans.find((span) => span.id === selectedSpanId);
    assert(rawSpan, "Selected operation must link to a span in this Session");
    const elapsedSeconds =
      (Date.parse(rawSpan.end_time) - Date.parse(rawSpan.start_time)) / 1000;
    assert(
      rawSpan.end_time,
      "Validation fixture must have completed operations"
    );
    assert.equal(
      (await detail.locator("dd").first().innerText()).trim(),
      `${elapsedSeconds.toFixed(2)} 秒`
    );
    const selectedTitle = await detail
      .getByRole("heading", { level: 3 })
      .innerText();
    const previousUpdate = await timeline
      .locator("time")
      .getAttribute("datetime");
    await timeline
      .getByRole("button", { name: "恢復更新", exact: true })
      .click();
    await page.waitForFunction((previous) => {
      const time = document.querySelector(
        'section[aria-label="Session 操作時間軸"] time'
      );
      return time && time.getAttribute("datetime") !== previous;
    }, previousUpdate);
    assert.equal(
      await detail.getByRole("heading", { level: 3 }).innerText(),
      selectedTitle
    );
    await timeline
      .getByRole("button", { name: "暫停更新", exact: true })
      .click();
    const turnSelect = timeline.getByRole("combobox", {
      name: "輪次",
      exact: true,
    });
    let turnZoomVerified = false;
    if (await turnSelect.count()) {
      const turnValue = await turnSelect
        .locator("option")
        .nth(1)
        .getAttribute("value");
      await turnSelect.selectOption(turnValue);
      assert.equal(await turnSelect.inputValue(), turnValue);
      await turnSelect.selectOption("");
      turnZoomVerified = true;
    }
    const parallelButton = timeline
      .getByRole("button", { name: /^平行操作 ·/ })
      .first();
    let parallelVerified = false;
    if (await parallelButton.count()) {
      await parallelButton.click();
      const choices = timeline.getByRole("navigation", {
        name: "平行操作選擇",
        exact: true,
      });
      await choices.getByRole("button").last().click();
      assert.equal(
        await choices.getByRole("button", { pressed: true }).count(),
        1
      );
      parallelVerified = true;
    }
    await names.first().click();
    await timeline.screenshot({
      path: resolve(outputDir, `${fixture.source}-desktop.png`),
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await timeline.screenshot({
      path: resolve(outputDir, `${fixture.source}-narrow.png`),
    });
    report.sessions.push({
      source: fixture.source,
      spans_read: spans.length,
      operations_visible: operationCount,
      selected_elapsed_seconds: elapsedSeconds,
      raw_span_link_verified: true,
      selected_duration_verified: true,
      pause_resume_preserves_selection: true,
      turn_zoom_verified: turnZoomVerified,
      parallel_verified: parallelVerified,
    });
  }
  assert.equal(
    pageErrors.length,
    0,
    "Page must not raise uncaught JavaScript errors"
  );
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.error = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  await writeFile(
    resolve(outputDir, "verification.json"),
    JSON.stringify(report, null, 2),
    { flag: "wx" }
  );
  process.stdout.write(`${JSON.stringify(report)}\n`);
}
