import assert from "node:assert/strict";
import { resolve } from "node:path";

export async function verifyHierarchy({
  page,
  session,
  spans,
  fixture,
  base,
  outputDir,
}) {
  const receipt = {
    source: fixture.source,
    session_id: fixture.sessionId,
    spans: spans.length,
    checks: [],
  };
  const check = (name) => {
    receipt.checks.push(name);
    process.stdout.write(`${name}\n`);
  };
  const external = [];
  const request = (request) => {
    if (new URL(request.url()).origin !== base.origin)
      external.push(request.url());
  };
  page.on("request", request);
  const direct = `${base.origin}/projects/${encodeURIComponent(session.project_id)}/sessions/${encodeURIComponent(session.id)}?sessionView=timeline`;
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto(direct);
  const timeline = page.getByRole("region", {
    name: "階層時間軸",
    exact: true,
  });
  await timeline.waitFor();
  receipt.initial_rows = await timeline.getByRole("treeitem").count();
  const header = await timeline.locator(".hierarchy-muted").first().innerText();
  receipt.operations = Number(header.match(/(\d+) 操作/)?.[1]);
  assert(Number.isInteger(receipt.operations));
  if (fixture.source === "kiro")
    assert(receipt.initial_rows < 40, "Kiro starts compact");
  const button = (name) => timeline.getByRole("button", { name, exact: true });
  const range = () => timeline.getByLabel("可視時間範圍").innerText();
  const plot = timeline.getByLabel("時間軸導覽區", { exact: true });
  const overview = timeline.getByLabel("全程時間軸縮圖", { exact: true });
  const result = timeline.getByRole("region", {
    name: "選取的呼叫結果",
    exact: true,
  });
  assert.equal(await result.count(), 0);
  assert.equal(
    await timeline.getByLabel("用量與輪次分析").getAttribute("open"),
    null
  );
  await button("暫停更新").click();
  check("direct URL and selected-only detail");
  const full = await range();
  await button("放大時間軸").click();
  assert.notEqual(await range(), full);
  await plot.focus();
  await page.keyboard.press("+");
  const zoomed = await range();
  await page.keyboard.press("ArrowRight");
  assert.notEqual(await range(), zoomed);
  await page.keyboard.press("Home");
  assert.equal(await range(), full);
  const track = await timeline
    .locator(".hierarchy-track")
    .first()
    .boundingBox();
  assert(track && track.width > 20);
  await page.mouse.move(track.x + track.width / 2, track.y + track.height / 2);
  await page.mouse.wheel(0, -400);
  await page.waitForTimeout(100);
  assert.notEqual(await range(), full);
  check("mouse wheel, button and keyboard zoom, pan key and Home");
  const drag = async (box, from, to, shift = false) => {
    if (shift) await page.keyboard.down("Shift");
    await page.mouse.move(box.x + box.width * from, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * to, box.y + box.height / 2, {
      steps: 8,
    });
    await page.mouse.up();
    if (shift) await page.keyboard.up("Shift");
  };
  let before = await range();
  await drag(track, 0.6, 0.7);
  assert.notEqual(await range(), before);
  before = await range();
  await drag(track, 0.2, 0.7, true);
  assert.notEqual(await range(), before);
  await button("全程時間軸").click();
  const mini = await overview.boundingBox();
  await drag(mini, 0.2, 0.7);
  assert.notEqual(await range(), full);
  check("plot drag pan, Shift brush and overview brush");
  for (const edge of ["起點", "終點"]) {
    await button("全程時間軸").click();
    const handle = timeline.getByRole("slider", {
      name: `調整時間範圍${edge}`,
      exact: true,
    });
    const box = await handle.locator("rect").first().boundingBox();
    before = await range();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(
      box.x + box.width / 2 + (edge === "起點" ? 40 : -40),
      box.y + box.height / 2,
      { steps: 8 }
    );
    await page.mouse.up();
    assert.notEqual(
      await range(),
      before,
      `${edge} must respond at its actual visible hit target`
    );
    before = await range();
    await handle.focus();
    await page.keyboard.press(edge === "起點" ? "ArrowRight" : "ArrowLeft");
    assert.notEqual(await range(), before);
  }
  check("both overview handles by actual mouse hit and keyboard");
  await button("全程時間軸").click();
  await button("全部展開").click();
  if (fixture.source === "kiro") await button("顯示觀測事件").click();
  const rows = timeline.getByRole("treeitem");
  assert.equal(
    await rows.count(),
    spans.length,
    "Every raw API span is reachable"
  );
  const byId = new Map(
    spans.map((span) => [
      `${span.context.trace_id}:${span.context.span_id}`,
      span,
    ])
  );
  const depth = (span) => {
    const parent = byId.get(`${span.context.trace_id}:${span.parent_id}`);
    return parent ? depth(parent) + 1 : 0;
  };
  receipt.max_depth = Math.max(...spans.map(depth));
  const ordered = await rows.evaluateAll((nodes) =>
    nodes.map((node) => ({
      name: node.getAttribute("aria-label"),
      level: Number(node.getAttribute("aria-level")),
    }))
  );
  const tupleOrder = (left, right) =>
    left.name.localeCompare(right.name) || left.level - right.level;
  assert.deepEqual(
    ordered.sort(tupleOrder),
    spans
      .map((span) => ({ name: span.name, level: depth(span) + 1 }))
      .sort(tupleOrder)
  );
  assert.equal(
    Math.max(...ordered.map((row) => row.level)),
    receipt.max_depth + 1
  );
  check("all paginated raw spans and parent depth");
  const tree = timeline.getByRole("tree", { name: "原始呼叫階層" });
  const scrollTop = await tree.evaluate((element) => {
    element.scrollTop = 100;
    return element.scrollTop;
  });
  const beforeScrollRefresh = await timeline
    .locator("time")
    .getAttribute("datetime");
  await button("恢復更新").click();
  await page.waitForFunction(
    (previous) =>
      document
        .querySelector('section[aria-label="階層時間軸"] time')
        ?.getAttribute("datetime") !== previous,
    beforeScrollRefresh
  );
  await button("暫停更新").click();
  assert.equal(await tree.evaluate((element) => element.scrollTop), scrollTop);
  receipt.retained_scroll_top = scrollTop;
  check("expanded tree scroll survives actual API refresh");
  const shortest = timeline
    .locator(".hierarchy-track button")
    .filter({ visible: true });
  const candidate = await shortest.evaluateAll(
    (nodes) =>
      nodes
        .map((node, index) => ({
          index,
          width: node.getBoundingClientRect().width,
        }))
        .filter((node) => node.width > 0)
        .sort((a, b) => a.width - b.width)[0]
  );
  await shortest.nth(candidate.index).click();
  await result.waitFor();
  assert.equal(await result.locator("details").getAttribute("open"), null);
  await result.locator("summary").click();
  const traceLink = result.getByRole("link", {
    name: "開啟原始 Trace",
    exact: true,
  });
  const traceHref = await traceLink.getAttribute("href");
  const raw = spans.find(
    (span) =>
      span.id ===
      new URL(traceHref, base).searchParams.get("selectedSpanNodeId")
  );
  assert(raw, "Trace link must point at the selected raw API span");
  if (!raw.name.includes("observation")) {
    const elapsed =
      (Date.parse(raw.end_time) - Date.parse(raw.start_time)) / 1000;
    if (Number.isFinite(elapsed))
      assert((await result.innerText()).includes(`${elapsed.toFixed(3)} 秒`));
  }
  check("short span mouse selection and raw Trace identity/time parity");
  await result.locator("summary").click();
  await plot.focus();
  await page.keyboard.press("Escape");
  assert.equal(await result.count(), 0);
  const names = timeline.locator(".hierarchy-name-select");
  await names.first().click();
  const selected = await result.locator("strong").innerText();
  await button("全部收合").click();
  await button("放大時間軸").click();
  const stableRange = await range();
  const stableRows = await rows.count();
  const stableTime = await timeline.locator("time").getAttribute("datetime");
  await page.waitForTimeout(5200);
  assert.equal(
    await timeline.locator("time").getAttribute("datetime"),
    stableTime
  );
  check("pause prevents refresh");
  let injected = false;
  const fail = async (route) => {
    if (
      !injected &&
      [fixture.sessionId, session.id].some((id) =>
        decodeURIComponent(new URL(route.request().url()).pathname).includes(
          `/v1/sessions/${id}`
        )
      )
    ) {
      injected = true;
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"error":"acceptance transient failure"}',
      });
    }
    return route.continue();
  };
  await page.route("**/v1/sessions/**", fail);
  await button("恢復更新").click();
  await timeline.getByRole("alert").filter({ hasText: "更新失敗" }).waitFor();
  assert(injected);
  assert.equal(
    await timeline.locator("time").getAttribute("datetime"),
    stableTime
  );
  assert.equal(await range(), stableRange);
  assert.equal(await result.locator("strong").innerText(), selected);
  await page.unroute("**/v1/sessions/**", fail);
  await page.waitForFunction(
    (previous) =>
      document
        .querySelector('section[aria-label="階層時間軸"] time')
        ?.getAttribute("datetime") !== previous,
    stableTime
  );
  await button("暫停更新").click();
  assert.equal(await range(), stableRange);
  assert.equal(await rows.count(), stableRows);
  assert.equal(await result.locator("strong").innerText(), selected);
  check(
    "503 retains last data and recovers with range, collapse and selection intact"
  );
  await button("全程時間軸").click();
  await page.screenshot({
    path: resolve(outputDir, `${fixture.source}-${spans.length}-desktop.png`),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  receipt.mobile_layout = await timeline.evaluate((element) => ({
    viewport_width: window.innerWidth,
    document_width: document.documentElement.scrollWidth,
    timeline_width: element.getBoundingClientRect().width,
    timeline_scroll_width: element.scrollWidth,
    panel_width: element
      .querySelector(".hierarchy-panel")
      .getBoundingClientRect().width,
  }));
  assert(
    receipt.mobile_layout.timeline_scroll_width <=
      receipt.mobile_layout.timeline_width,
    "Hierarchy must not introduce horizontal overflow"
  );
  assert(await button("放大時間軸").isVisible());
  assert(await button("全部展開").isVisible());
  await button("全部展開").click();
  const deepestRow = timeline
    .locator(`[role="treeitem"][aria-level="${receipt.max_depth + 1}"]`)
    .first();
  const deepestName = deepestRow.locator(".hierarchy-name-select");
  const nameBox = await deepestName.boundingBox();
  receipt.deepest_mobile_name_width = nameBox?.width;
  assert(
    nameBox && nameBox.width >= 24,
    "Deepest mobile name must remain a usable selection target"
  );
  await deepestName.click();
  await deepestName.focus();
  await page.keyboard.press("Enter");
  await result.waitFor();
  await page.screenshot({
    path: resolve(outputDir, `${fixture.source}-${spans.length}-mobile.png`),
    fullPage: true,
  });
  await result.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: resolve(
      outputDir,
      `${fixture.source}-${spans.length}-mobile-result.png`
    ),
    fullPage: true,
  });
  check("390px controls, expansion and selection");
  await page.setViewportSize({ width: 1440, height: 1100 });
  await result.locator("summary").click();
  const response = page.waitForResponse(
    (response) =>
      response.request().postData()?.includes("TraceDetailsQuery") &&
      response.url().includes("graphql")
  );
  await result
    .getByRole("link", { name: "開啟原始 Trace", exact: true })
    .click();
  const trace = await (await response).json();
  assert.equal(trace.errors?.length ?? 0, 0);
  assert(trace.data?.project?.trace);
  await page.goto(direct);
  await timeline.waitFor();
  assert.equal(await result.count(), 0);
  check("original Trace API out/back and direct reload isolation");
  page.off("request", request);
  receipt.external_requests = external;
  assert.deepEqual(external, []);
  check("no external requests");
  return receipt;
}
