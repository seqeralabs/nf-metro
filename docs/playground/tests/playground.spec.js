const { test, expect } = require("@playwright/test");

// One shared page booted once: the Pyodide cold-start is too slow to repeat per
// test. Tests run serially and each leaves the editor in a known state.
test.describe.configure({ mode: "serial" });

let page;

async function waitReady(p) {
  await p.waitForFunction(() => window.__nfMetroReady === true, null, {
    timeout: 160_000,
  });
}

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await page.goto("/index.html");
  await waitReady(page);
});

test.afterAll(async () => {
  await page.close();
});

test("boots and renders the seed map", async () => {
  await expect(page.locator("#preview svg")).toHaveCount(1);
  expect(await page.locator("#preview [data-line-id]").count()).toBeGreaterThan(0);
  expect(await page.locator('#preview [data-station-id="reads"]').count()).toBeGreaterThan(0);
  await expect(page.locator("#error")).toBeHidden();
});

test("live edit re-renders with the new station", async () => {
  await expect(page.locator('#preview [data-station-id="brandnew"]')).toHaveCount(0);
  await page.evaluate(() => {
    const v = window.__nfMetro.getValue() + "\n    align -->|main| brandnew[BrandNew]\n";
    window.__nfMetro.setValue(v);
  });
  await expect(page.locator('#preview [data-station-id="brandnew"]').first()).toBeVisible();
});

test("animate toggle adds motion elements", async () => {
  await expect(page.locator("#preview animateMotion")).toHaveCount(0);
  await page.locator("#opt-animate").check();
  await expect
    .poll(async () => page.locator("#preview animateMotion").count())
    .toBeGreaterThan(0);
  await page.locator("#opt-animate").uncheck();
  await expect(page.locator("#preview animateMotion")).toHaveCount(0);
});

test("directional toggle adds chevron markers", async () => {
  await expect(page.locator('#preview [class*="metro-direction"]')).toHaveCount(0);
  await page.locator("#opt-directional").check();
  expect(
    await page.locator('#preview [class*="metro-direction"]').count()
  ).toBeGreaterThan(0);
  await page.locator("#opt-directional").uncheck();
});

test("theme switch changes the rendered output", async () => {
  const before = await page.locator("#preview").innerHTML();
  await page.locator("#opt-theme").selectOption("light");
  await expect
    .poll(async () => page.locator("#preview").innerHTML())
    .not.toBe(before);
  await page.locator("#opt-theme").selectOption("nfcore");
});

test("snippet button inserts valid boilerplate and still renders", async () => {
  await page.locator("#btn-line").click();
  await expect(page.locator("#error")).toBeHidden();
  expect(await page.locator("#preview svg").count()).toBe(1);
});

test("line color swatch rewrites the hex in the editor", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #abcdef\ngraph LR\n  n1[N1] -->|a| n2[N2]\n"
    )
  );
  await expect(page.locator('#line-colors input[type="color"]').first()).toBeVisible();
  await page.evaluate(() => {
    const input = document.querySelector('#line-colors input[type="color"]');
    input.value = "#123456";
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("#123456");
});

test("syntax error surfaces inline and keeps the last good render", async () => {
  const good = await page.locator("#preview").innerHTML();
  await page.evaluate(() => {
    // A self-referential cycle the layout engine rejects.
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n  n2 -->|a| n1\n"
    );
  });
  await expect(page.locator("#error")).toBeVisible();
  // Preview is untouched: the broken edit did not blank it.
  expect(await page.locator("#preview").innerHTML()).toBe(good);
});

test("SVG and PNG export produce non-empty downloads", async () => {
  // Restore a valid map after the error test.
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n"
    );
  });
  await expect(page.locator("#preview svg")).toHaveCount(1);

  const [svg] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#btn-svg").click(),
  ]);
  expect(svg.suggestedFilename()).toMatch(/\.svg$/);

  const [png] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#btn-png").click(),
  ]);
  expect(png.suggestedFilename()).toMatch(/\.png$/);
  const fs = require("fs");
  const stat = fs.statSync(await png.path());
  expect(stat.size).toBeGreaterThan(0);
});

test("share link round-trips the editor content", async () => {
  const source = await page.evaluate(() => {
    const v = "%%metro line: z | Z | #0af\ngraph LR\n  s1[S1] -->|z| s2[S2]\n";
    window.__nfMetro.setValue(v);
    return v;
  });
  await page.locator("#btn-share").click();
  const url = await page.evaluate(() => location.href);
  expect(url).toContain("#mmd=");

  await page.goto(url);
  await waitReady(page);
  const restored = await page.evaluate(() => window.__nfMetro.getValue());
  expect(restored).toBe(source);
});
