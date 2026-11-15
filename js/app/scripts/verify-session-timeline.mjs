import assert from "node:assert/strict";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { chromium } from "playwright";

import { verifyHierarchy } from "./verify-session-hierarchy.mjs";

const { values } = parseArgs({
  options: {
    "base-url": { type: "string", default: "http://127.0.0.1:6007" },
    "output-dir": { type: "string" },
    channel: { type: "string", default: "msedge" },
    project: { type: "string" },
    "session-id": { type: "string" },
    source: { type: "string" },
    hierarchy: { type: "boolean", default: false },
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
        sessionId: "01a1c9e2-0f28-7d90-9e11-7f7b0037e2a7",
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

function pairedKiroIntervals(spans) {
  return spans
    .filter(
      (span) =>
        (span.attributes["kiro.record_kind"] ??
          span.attributes.kiro?.record_kind) === "paired tool boundaries"
    )
    .map((span) => [Date.parse(span.start_time), Date.parse(span.end_time)])
    .filter(([start, end]) => Number.isFinite(start) && end > start)
    .sort((left, right) => left[0] - right[0]);
}

function hasPairedOverlap(intervals) {
  let previousEnd = -Infinity;
  for (const [start, end] of intervals) {
    if (start < previousEnd) return true;
    previousEnd = Math.max(previousEnd, end);
  }
  return false;
}

function rawCounts(span) {
  const attributes = span.attributes;
  const turn = span.name === "session_task.turn";
  const prefix = "codex.turn.token_usage.";
  const keys = turn
    ? {
        input: [prefix + "input_tokens"],
        output: [prefix + "output_tokens"],
        total: [prefix + "total_tokens"],
        cache: [prefix + "cached_input_tokens"],
      }
    : {
        input: ["llm.token_count.prompt", "gen_ai.usage.input_tokens"],
        output: ["llm.token_count.completion", "gen_ai.usage.output_tokens"],
        total: ["llm.token_count.total"],
        cache: [
          "llm.token_count.prompt_details.cache_read",
          "gen_ai.usage.cache_read.input_tokens",
        ],
      };
  const counts = {};
  for (const [name, aliases] of Object.entries(keys)) {
    const values = aliases
      .map((key) => attributes[key])
      .filter((value) => value != null);
    if (
      values.some((value) => !Number.isSafeInteger(value) || value < 0) ||
      new Set(values).size > 1
    )
      return null;
    counts[name] = values[0] ?? null;
  }
  if (counts.input != null && counts.output != null) {
    const total = counts.input + counts.output;
    if (
      !Number.isSafeInteger(total) ||
      (counts.total != null && counts.total !== total)
    )
      return null;
    counts.total = total;
  }
  if (
    counts.cache != null &&
    counts.input != null &&
    counts.cache > counts.input
  )
    return null;
  return counts.total == null ? null : counts;
}

async function verifyVisibleUsage(card, spans) {
  if (await card.getByText("無資料", { exact: true }).count()) {
    return { availability: "unavailable", numeric_parity_verified: false };
  }
  const disclosure = card.locator("details");
  await disclosure.locator("summary").click();
  const sourceIds = (await disclosure.locator("div").last().innerText()).split(
    " · "
  );
  assert(
    sourceIds.length > 0 && new Set(sourceIds).size === sourceIds.length,
    "Visible usage provenance must be nonempty and unique"
  );
  const sources = sourceIds.map((id) => spans.find((span) => span.id === id));
  assert(
    sources.every(Boolean),
    "Visible usage source must belong to the selected Session"
  );
  const counts = sources.map(rawCounts);
  assert(
    counts.every(Boolean),
    "Visible usage must be supported by valid raw span counts"
  );
  const sum = (field) =>
    counts.every((count) => count[field] != null)
      ? counts.reduce((value, count) => value + count[field], 0)
      : null;
  const total = sum("total");
  const input = sum("input");
  const output = sum("output");
  const cache = sum("cache");
  await card
    .getByText(total.toLocaleString("en-US"), { exact: true })
    .waitFor();
  await card
    .getByText(
      `輸入 ${input == null ? "無資料" : input.toLocaleString("en-US")}`,
      { exact: true }
    )
    .waitFor();
  await card
    .getByText(
      `輸出 ${output == null ? "無資料" : output.toLocaleString("en-US")}`,
      { exact: true }
    )
    .waitFor();
  if (cache != null)
    await card
      .getByText(`快取 ${cache.toLocaleString("en-US")}（已含輸入）`, {
        exact: true,
      })
      .waitFor();
  await disclosure.locator("summary").click();
  return {
    availability: "available",
    numeric_parity_verified: true,
    source_spans: sources.length,
    input,
    output,
    total,
    cache_input: cache,
  };
}

let browser;
let page;
const pageErrors = [];
const report = { status: "running", base_url: base.href, sessions: [] };
try {
  browser = await chromium.launch({ channel: values.channel, headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
  });
  page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on("pageerror", (error) => pageErrors.push(error.name));
  for (const fixture of fixtures) {
    const session = await findSession(fixture);
    const spans = await readSessionSpans(session);
    if (values.hierarchy) {
      report.sessions.push(
        await verifyHierarchy({
          page,
          session,
          spans,
          fixture,
          base,
          outputDir,
        })
      );
      continue;
    }
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
        `/projects/${encodeURIComponent(
          session.project_id
        )}/sessions?${timeRange}`
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
    await names.first().focus();
    await page.keyboard.press("Enter");
    assert.equal(await names.first().getAttribute("aria-pressed"), "true");
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
    const stableUpdate = await timeline
      .locator("time")
      .getAttribute("datetime");
    const failSelectedSessionOnce = async (route) => {
      const pathname = decodeURIComponent(
        new URL(route.request().url()).pathname
      );
      if (pathname.endsWith(`/v1/sessions/${session.id}`) && !injectedFaults) {
        injectedFaults += 1;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: '{"error":"controlled validation fault"}',
        });
      } else await route.continue();
    };
    let injectedFaults = 0;
    await page.route("**/v1/sessions/**", failSelectedSessionOnce);
    await timeline
      .getByRole("button", { name: "恢復更新", exact: true })
      .click();
    const staleAlert = timeline
      .getByRole("alert")
      .filter({ hasText: "更新失敗，保留上次資料" });
    await staleAlert.waitFor();
    assert.equal(
      await detail.getByRole("heading", { level: 3 }).innerText(),
      selectedTitle
    );
    assert.equal(
      await timeline.locator("time").getAttribute("datetime"),
      stableUpdate
    );
    await timeline
      .getByRole("button", { name: "暫停更新", exact: true })
      .click();
    await page.unroute("**/v1/sessions/**", failSelectedSessionOnce);
    await timeline
      .getByRole("button", { name: "恢復更新", exact: true })
      .click();
    await page.waitForFunction((previous) => {
      const time = document.querySelector(
        'section[aria-label="Session 操作時間軸"] time'
      );
      return time && time.getAttribute("datetime") !== previous;
    }, stableUpdate);
    await staleAlert.waitFor({ state: "hidden" });
    await timeline
      .getByRole("button", { name: "暫停更新", exact: true })
      .click();
    assert.equal(injectedFaults, 1);
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
    const axis = timeline.getByRole("button", {
      name: "選擇時間軸區段",
      exact: true,
    });
    const parallelVisual = timeline.locator(".timeline-bar.parallel").first();
    const pairedIntervals = pairedKiroIntervals(spans);
    assert(
      operationCount >= pairedIntervals.length,
      "Every paired Kiro operation must remain available in the operation list"
    );
    if (hasPairedOverlap(pairedIntervals))
      assert(
        (await parallelVisual.count()) > 0,
        "Overlapping source operations must remain visible as parallel segments"
      );
    let parallelVerified = false;
    let denseAxisPointerVerified = false;
    if (await parallelVisual.count()) {
      await axis.scrollIntoViewIfNeeded();
      const axisBounds = await axis.boundingBox();
      const segmentBounds = await parallelVisual.boundingBox();
      const segmentLabel = await parallelVisual.getAttribute("title");
      assert(axisBounds && segmentBounds && segmentLabel);
      await axis.click({
        position: {
          x: Math.max(
            1,
            Math.min(
              axisBounds.width - 1,
              segmentBounds.x + segmentBounds.width / 2 - axisBounds.x
            )
          ),
          y: axisBounds.height / 2,
        },
      });
      const segmentChoices = timeline.getByRole("navigation", {
        name: "密集區段選擇",
        exact: true,
      });
      await segmentChoices
        .getByRole("button", { name: segmentLabel, exact: true })
        .click();
      denseAxisPointerVerified = true;
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
    let operationListScrollVerified = false;
    if (operationCount > 100) {
      const listGeometry = await timeline
        .locator(".timeline-names")
        .evaluate((element) => ({
          height: element.clientHeight,
          scrollHeight: element.scrollHeight,
        }));
      assert(
        listGeometry.height > 0 &&
          listGeometry.height <= 320 &&
          listGeometry.scrollHeight > listGeometry.height,
        "The complete large operation list must scroll within a bounded viewport"
      );
      const detailDistance = await timeline.evaluate((element) => {
        const track = element.querySelector(".timeline-axis");
        const panel = element.querySelector('[aria-label="操作詳細資訊"]');
        return (
          panel.getBoundingClientRect().top -
          track.getBoundingClientRect().bottom
        );
      });
      assert(
        detailDistance < 1000,
        "Operation details must remain near the axis rather than below every card"
      );
      operationListScrollVerified = true;
    }
    await axis.focus();
    await page.keyboard.press("Home");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Enter");
    assert.equal(
      await timeline
        .getByRole("navigation", { name: "密集區段選擇", exact: true })
        .getByRole("button", { pressed: true })
        .count(),
      1
    );
    await names.first().click();
    const sessionUsage = await verifyVisibleUsage(
      timeline.getByRole("region", { name: "Session Token", exact: true }),
      spans
    );
    let modelUsage = {
      availability: "not-observed",
      numeric_parity_verified: false,
    };
    let screenshotOperation = 0;
    const rawModelUsageCandidates = spans.filter(
      (span) =>
        (span.name === "handle_responses" || span.span_kind === "LLM") &&
        rawCounts(span)
    ).length;
    for (
      let index = 0;
      rawModelUsageCandidates > 0 && index < operationCount;
      index += 1
    ) {
      await names.nth(index).focus();
      await page.keyboard.press("Enter");
      const modelHref = await detail
        .getByRole("link", { name: "開啟原始 Trace", exact: true })
        .getAttribute("href");
      const modelId = new URL(modelHref, base).searchParams.get(
        "selectedSpanNodeId"
      );
      const raw = spans.find((span) => span.id === modelId);
      if (raw && raw.name !== "session_task.turn" && rawCounts(raw)) {
        modelUsage = await verifyVisibleUsage(
          detail.getByRole("region", { name: "操作 Token", exact: true }),
          spans
        );
        assert(
          modelUsage.numeric_parity_verified,
          "A model with valid raw counts must display verifiable Token usage"
        );
        screenshotOperation = index;
        break;
      }
    }
    if (rawModelUsageCandidates)
      assert(
        modelUsage.numeric_parity_verified,
        "Available model Token values must be verified in the timeline"
      );
    await names.nth(screenshotOperation).click();
    await timeline.evaluate((element) => {
      element.scrollTop = 0;
    });
    await timeline.screenshot({
      path: resolve(outputDir, `${fixture.source}-desktop.png`),
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await timeline.evaluate((element) => {
      element.scrollTop = 0;
    });
    await timeline.screenshot({
      path: resolve(outputDir, `${fixture.source}-narrow.png`),
    });
    const returnToTimeline = page.url();
    await page.setViewportSize({ width: 1440, height: 1100 });
    const sessionReport = {
      source: fixture.source,
      spans_read: spans.length,
      operations_visible: operationCount,
      selected_elapsed_seconds: elapsedSeconds,
      raw_span_link_verified: true,
      selected_duration_verified: true,
      pause_resume_preserves_selection: true,
      keyboard_selection_verified: true,
      refresh_failure_retains_snapshot: true,
      controlled_fault: {
        status: 503,
        injected: injectedFaults,
        target: "selected-session-get",
      },
      refresh_recovery_verified: true,
      raw_trace_navigation_verified: false,
      return_to_timeline_verified: false,
      session_token_check: sessionUsage,
      model_token_check: modelUsage,
      raw_model_usage_candidates: rawModelUsageCandidates,
      turn_zoom_verified: turnZoomVerified,
      parallel_verified: parallelVerified,
      dense_axis_pointer_verified: denseAxisPointerVerified,
      axis_keyboard_verified: true,
      operation_list_scroll_verified: operationListScrollVerified,
    };
    report.sessions.push(sessionReport);
    report.current_stage = "original-trace-navigation";
    const traceLink = detail.getByRole("link", {
      name: "開啟原始 Trace",
      exact: true,
    });
    const traceHref = await traceLink.getAttribute("href");
    const traceDataResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/graphql") &&
        response.request().postData()?.includes("TraceDetailsQuery")
    );
    await traceLink.click();
    await page.waitForURL(
      (current) => current.pathname === new URL(traceHref, base).pathname
    );
    const traceData = await (await traceDataResponse).json();
    assert.equal(
      traceData.errors?.length ?? 0,
      0,
      "Original Trace query must resolve without GraphQL errors"
    );
    assert(
      traceData.data?.project?.trace,
      "Original Trace must resolve actual data"
    );
    sessionReport.raw_trace_navigation_verified = true;
    report.current_stage = "return-to-timeline";
    await page.goto(returnToTimeline);
    await page
      .getByRole("region", { name: "Session 操作時間軸", exact: true })
      .waitFor();
    sessionReport.return_to_timeline_verified = true;
  }
  assert.equal(
    pageErrors.length,
    0,
    "Page must not raise uncaught JavaScript errors"
  );
  report.status = "passed";
  report.current_stage = "complete";
} catch (error) {
  report.status = "failed";
  report.error = error instanceof Error ? error.message : String(error);
  if (page && !page.isClosed()) {
    await page.screenshot({
      path: resolve(outputDir, "failure.png"),
      fullPage: true,
    });
    report.failure_page = {
      url: page.url(),
      tabs: await page.getByRole("tab").evaluateAll((elements) =>
        elements.map((element) => ({
          label:
            element.getAttribute("aria-label") ?? element.textContent?.trim(),
          selected: element.getAttribute("aria-selected"),
        }))
      ),
      page_errors: pageErrors,
    };
  }
  process.exitCode = 1;
} finally {
  report.page_errors = pageErrors;
  await browser?.close();
  await writeFile(
    resolve(outputDir, "verification.json"),
    JSON.stringify(report, null, 2),
    { flag: "wx" }
  );
  process.stdout.write(`${JSON.stringify(report)}\n`);
}
