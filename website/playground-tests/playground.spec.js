const { test, expect } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

// One shared page booted once: the Pyodide cold-start is too slow to repeat per
// test. Tests run serially and each leaves the editor in a known state.
test.describe.configure({ mode: "serial" });

let page;

async function waitReady(p) {
  await p.waitForFunction(() => window.__nfMetroReady === true, null, {
    timeout: 160_000,
  });
}

// Advanced controls live in a collapsed <details>; open it before driving them.
async function openAdvanced() {
  await page.evaluate(() => {
    document.getElementById("advanced").open = true;
  });
}

async function suppressNextPopup() {
  await page.evaluate(() => {
    const open = window.open;
    window.open = () => {
      window.open = open;
      return null;
    };
  });
}

async function replaceEditorText(source) {
  await page.locator(".CodeMirror").click();
  await page.keyboard.press(
    process.platform === "darwin" ? "Meta+A" : "Control+A",
  );
  await page.keyboard.insertText(source);
}

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await page.goto("/harness.html");
  await waitReady(page);
  if (await page.locator("#welcome-modal").isVisible()) {
    await page.locator("#welcome-new").click();
  }
});

test.afterAll(async () => {
  await page.close();
});

test("service worker precaches pyodide.js during install", async () => {
  // By the time Pyodide has fully booted, the SW install has long since completed.
  const cached = await page.evaluate(async () => {
    const keys = await caches.keys();
    const name = keys.find((k) => k.startsWith("nfm-playground-"));
    if (!name) return false;
    const cache = await caches.open(name);
    const entries = await cache.keys();
    return entries.some((req) => req.url.includes("pyodide.js"));
  });
  expect(cached).toBe(true);
});

test("boots and renders the seed map", async () => {
  await expect(page.locator("#preview svg")).toHaveCount(1);
  await expect(page.locator("#preview")).toContainText("Reads");
  await expect(page.locator("#preview")).toContainText("FastQC");
  await expect(page.locator("#preview")).toContainText("BAM");
  await expect(page.locator("#error")).toBeHidden();
});

test("standalone harness fills the browser viewport", async () => {
  const dimensions = await page.evaluate(() => ({
    appHeight: document.querySelector(".nfm-playground").getBoundingClientRect()
      .height,
    viewportHeight: window.innerHeight,
  }));
  expect(dimensions.appHeight).toBeGreaterThanOrEqual(
    dimensions.viewportHeight - 1,
  );
});

test("renderer startup message does not show the obsolete download warning", async () => {
  await expect(page.locator("body")).not.toContainText(
    "First load fetches the WASM runtime",
  );
});

test("live edit re-renders with the new station", async () => {
  const source =
    "%%metro line: main | Main | #c792ea\n" +
    "graph LR\n" +
    "    align[Align] -->|main| brandnew[BrandNew]\n";

  await replaceEditorText(source);

  await expect(page.locator("#preview")).toContainText("BrandNew");
  await expect(page.locator("#error")).toBeHidden();
});

test("animate toggle adds motion elements", async () => {
  const balls = page.locator('#preview circle[style*="offset-path"]');
  await expect(balls).toHaveCount(0);
  await page.locator("#opt-animate").check();
  await expect.poll(async () => balls.count()).toBeGreaterThan(0);
  await page.locator("#opt-animate").uncheck();
  await expect(balls).toHaveCount(0);
});

test("layout options are disclosed in their dedicated control panel", async () => {
  await expect(page.locator("#opt-line-spread")).toBeHidden();
  await openAdvanced();
  await expect(page.locator("#opt-line-spread")).toBeVisible();
});

test("directional toggle re-renders the map", async () => {
  const before = await page.locator("#preview").innerHTML();

  await page.locator("#opt-directional").check();
  await expect
    .poll(async () => page.locator("#preview").innerHTML())
    .not.toBe(before);
  await expect(page.locator("#error")).toBeHidden();

  await page.locator("#opt-directional").uncheck();
});

test("brand dropdown writes the %%metro style directive and re-renders", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  const before = await page.locator("#preview").innerHTML();

  await page.locator("#opt-theme").selectOption("seqera");
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("%%metro style: seqera");
  await expect
    .poll(async () => page.locator("#preview").innerHTML())
    .not.toBe(before);

  await page.locator("#opt-theme").selectOption("nfcore");
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("%%metro style: nfcore");
});

test("brand dropdown syncs from the source style directive", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro style: seqera\n%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  await expect(page.locator("#opt-theme")).toHaveValue("seqera");

  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro style: dark\n%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  await expect(page.locator("#opt-theme")).toHaveValue("nfcore");
});

test("mode dropdown writes the %%metro mode directive and re-renders", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  const before = await page.locator("#preview").innerHTML();

  // Pick the mode the page is not already showing (the default tracks the UI
  // theme), so the render genuinely changes.
  const start = await page.locator("#opt-mode").inputValue();
  const other = start === "dark" ? "light" : "dark";
  await page.locator("#opt-mode").selectOption(other);
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain(`%%metro mode: ${other}`);
  await expect
    .poll(async () => page.locator("#preview").innerHTML())
    .not.toBe(before);
});

test("mode dropdown syncs from the source mode directive", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro mode: light\n%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  await expect(page.locator("#opt-mode")).toHaveValue("light");
});

test("debug toggle adds the debug overlay", async () => {
  // A sectioned map has ports/waypoints for the overlay to draw.
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n" +
        "  subgraph s1 [One]\n    n1[N1]\n  end\n" +
        "  subgraph s2 [Two]\n    n2[N2]\n  end\n" +
        "  n1 -->|a| n2\n",
    ),
  );
  const before = await page.locator("#preview").innerHTML();
  await page.locator("#opt-debug").check();
  await expect
    .poll(async () => page.locator("#preview").innerHTML())
    .not.toBe(before);
  await page.locator("#opt-debug").uncheck();
});

test("layout controls write %%metro directives and sync from source", async () => {
  await openAdvanced();
  const getValue = () => page.evaluate(() => window.__nfMetro.getValue());
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );

  // choice -> writes directive, then "auto" removes it
  await page.locator("#opt-line-spread").selectOption("rails");
  await expect.poll(getValue).toContain("%%metro line_spread: rails");
  await page.locator("#opt-line-spread").selectOption("");
  await expect.poll(getValue).not.toContain("line_spread");

  // bool -> writes true, unchecking removes it
  await page.locator("#opt-center-ports").check();
  await expect.poll(getValue).toContain("%%metro center_ports: true");
  await page.locator("#opt-center-ports").uncheck();
  await expect.poll(getValue).not.toContain("center_ports");

  // number -> writes the value and re-renders
  const fontScale = page.locator("#opt-font-scale");
  await fontScale.fill("1.5");
  await page.evaluate(() => window.syncDirectiveControls());
  await expect(fontScale).toHaveValue("1.5");
  await fontScale.blur();
  await expect.poll(getValue).toContain("%%metro font_scale: 1.5");

  // controls sync FROM a source directive
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line_spread: centered\n%%metro center_ports: true\n" +
        "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  await expect(page.locator("#opt-line-spread")).toHaveValue("centered");
  await expect(page.locator("#opt-center-ports")).toBeChecked();
});

test("snippet button inserts valid boilerplate and still renders", async () => {
  await page.locator("#btn-line").click();
  await expect(page.locator("#error")).toBeHidden();
  expect(await page.locator("#preview svg").count()).toBe(1);
});

test("Nextflow DAG import converts the seeded sample into a metro map", async () => {
  await page.locator("#btn-convert").click();
  await expect(page.locator("#convert-modal")).toBeVisible();
  // Docs link points at the Nextflow import guide.
  await expect(
    page.locator('#convert-modal a[href="../nextflow/"]'),
  ).toHaveCount(1);
  // The box is pre-filled with a sample DAG, like the editor's starter map.
  await expect(page.locator("#convert-text")).toHaveValue(/flowchart/);

  // Convert the seeded sample directly.
  await page.locator("#convert-submit").click();
  await expect(page.locator("#convert-modal")).toBeHidden();
  const value = await page.evaluate(() => window.__nfMetro.getValue());
  expect(value).toContain("%%metro");
  expect(value.toLowerCase()).toContain("fastqc");
  await expect
    .poll(async () => page.locator("#preview [data-line-id]").count())
    .toBeGreaterThan(0);
});

test("line color swatch rewrites the hex in the editor", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #abcdef\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    ),
  );
  await expect(
    page.locator('#line-colors input[type="color"]').first(),
  ).toBeVisible();
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
  const valid =
    "%%metro line: a | A | #f00\n" +
    "graph LR\n" +
    "  stable[Stable Good] -->|a| n2[N2]\n";
  await replaceEditorText(valid);
  await expect(page.locator("#preview")).toContainText("Stable Good");
  await expect(page.locator("#error")).toBeHidden();
  const good = await page.locator("#preview").innerHTML();

  // A self-referential cycle the layout engine rejects.
  await replaceEditorText(valid + "  n2 -->|a| stable\n");
  await expect(page.locator("#error")).toBeVisible();
  await expect(page.locator("#error strong")).toHaveText("Source error");
  // Preview is untouched: the broken edit did not blank it.
  expect(await page.locator("#preview").innerHTML()).toBe(good);
});

// 1x1 red PNG, used as a stand-in logo upload.
const TINY_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4" +
  "nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=";

test("a logo path the playground cannot resolve gets a friendly hint", async () => {
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro logo: does/not/exist.png\n%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    );
  });
  await expect(page.locator("#error")).toContainText("not found");
  await expect(page.locator("#error")).toContainText("+ Logo");
});

test("attaching a logo embeds it as a data URI and renders it", async () => {
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    );
  });

  await page.locator("#btn-logo").click();
  await expect(page.locator("#logo-modal")).toBeVisible();
  await expect(page.locator("#logo-submit")).toBeDisabled();

  await page.locator("#logo-file").setInputFiles({
    name: "logo.png",
    mimeType: "image/png",
    buffer: Buffer.from(TINY_PNG_BASE64, "base64"),
  });
  await expect(page.locator("#logo-preview")).toBeVisible();
  await expect(page.locator("#logo-submit")).toBeEnabled();

  await page.locator("#logo-submit").click();
  await expect(page.locator("#logo-modal")).toBeHidden();
  await expect(page.locator("#error")).toBeHidden();

  const value = await page.evaluate(() => window.__nfMetro.getValue());
  expect(value).toContain("%%metro logo: data:image/png;base64,");
  await expect(page.locator("#preview svg image")).toHaveCount(1);

  // Removing it clears the directive and the embedded image.
  await page.locator("#btn-logo").click();
  await page.locator("#logo-remove").click();
  await expect(page.locator("#preview svg image")).toHaveCount(0);
  expect(await page.evaluate(() => window.__nfMetro.getValue())).not.toContain(
    "%%metro logo:",
  );
});

test("SVG and PNG export produce non-empty downloads", async () => {
  // Restore a valid map after the error test.
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
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

test("zoom controls scale the preview and Fit resets", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  zn1[ZN1] -->|a| zn2[ZN2]\n",
    ),
  );
  await expect(
    page.locator('#preview [data-station-id="zn1"]').first(),
  ).toBeVisible();

  const svgWidth = () =>
    page
      .locator("#preview svg")
      .evaluate((s) => s.getBoundingClientRect().width);
  const fitWidth = await svgWidth();

  await page.locator("#zoom-in").click();
  await expect.poll(svgWidth).toBeGreaterThan(fitWidth + 1);
  await expect(page.locator("#preview")).toHaveClass(/zoomed/);

  await page.locator("#zoom-fit").click();
  await expect(page.locator("#preview")).not.toHaveClass(/zoomed/);
  await expect.poll(svgWidth).toBeLessThanOrEqual(fitWidth + 1);
});

test("example dropdown loads a chosen example and renders it", async () => {
  const select = page.locator("#example-select");
  // Manifest populated the dropdown beyond the placeholder + starter.
  expect(await select.locator("option").count()).toBeGreaterThan(2);
  // Entries are grouped into multiple <optgroup>s.
  expect(await select.locator("optgroup").count()).toBeGreaterThan(1);

  await select.selectOption("rnaseq_auto");
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("graph");
  await expect
    .poll(async () => page.locator("#preview [data-line-id]").count())
    .toBeGreaterThan(0);
  // Action menu resets to its placeholder after loading.
  await expect(select).toHaveValue("");

  // The starter entry is always available even without the manifest.
  await select.selectOption("__seed__");
  await expect
    .poll(async () => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("Example Pipeline");
});

test("bug report builds a prefilled GitHub issue with the map and explanation", async () => {
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro line: q | Q | #abc\ngraph LR\n  uniquenode[Unique] -->|q| other[Other]\n",
    );
  });
  await suppressNextPopup();

  await page.locator("#btn-report").click();
  await expect(page.locator("#report-modal")).toBeVisible();
  // The explanation is mandatory: submit stays disabled until it's filled.
  await expect(page.locator("#report-submit")).toBeDisabled();

  await page
    .locator("#report-text")
    .fill("Edge renders backwards from uniquenode");
  await expect(page.locator("#report-submit")).toBeEnabled();
  await page.locator("#report-submit").click();

  await expect(page.locator("#report-modal")).toBeHidden();
  await expect
    .poll(() => page.evaluate(() => window.__nfMetroLastIssueUrl))
    .toEqual(expect.stringContaining("github.com"));
  const issueUrl = await page.evaluate(() => window.__nfMetroLastIssueUrl);
  const u = new URL(issueUrl);
  expect(u.host).toBe("github.com");
  expect(u.pathname).toBe("/seqeralabs/nf-metro/issues/new");
  expect(u.searchParams.get("labels")).toBe("playground");
  const body = u.searchParams.get("body");
  expect(body).toContain("Edge renders backwards from uniquenode");
  expect(body).toContain("uniquenode[Unique]");
  expect(body).toContain("#mmd-gz=");
});

test("bug report URL stays within GitHub's request limit", async () => {
  const source = fs.readFileSync(
    path.join(
      __dirname,
      "../../examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd",
    ),
    "utf8",
  );
  await page.evaluate((value) => {
    window.__nfMetro.setValue(value);
    window.__nfMetroLastIssueUrl = null;
  }, source);
  await suppressNextPopup();

  await page.locator("#btn-report").click();
  await page.locator("#report-text").fill("Genomeassembler map renders badly");
  await page.locator("#report-submit").click();

  await expect
    .poll(() => page.evaluate(() => window.__nfMetroLastIssueUrl))
    .toEqual(expect.stringContaining("github.com"));
  const issueUrl = await page.evaluate(() => window.__nfMetroLastIssueUrl);
  expect(issueUrl.length).toBeLessThanOrEqual(7500);
  expect(new URL(issueUrl).searchParams.get("body")).toContain("#mmd-gz=");
});

test("share link round-trips the editor content", async () => {
  const source = await page.evaluate(() => {
    const v = "%%metro line: z | Z | #0af\ngraph LR\n  s1[S1] -->|z| s2[S2]\n";
    window.__nfMetro.setValue(v);
    return v;
  });
  await page.locator("#btn-share").click();
  await page.waitForFunction(() => location.hash.includes("mmd-gz="));
  const url = await page.evaluate(() => location.href);
  expect(url).toContain("#mmd-gz=");

  await page.goto(url);
  await waitReady(page);
  const restored = await page.evaluate(() => window.__nfMetro.getValue());
  expect(restored).toBe(source);
});

test("share link round-trips an attached logo", async () => {
  await page.evaluate(() => {
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\ngraph LR\n  n1[N1] -->|a| n2[N2]\n",
    );
  });
  await page.locator("#btn-logo").click();
  await page.locator("#logo-file").setInputFiles({
    name: "logo.png",
    mimeType: "image/png",
    buffer: Buffer.from(TINY_PNG_BASE64, "base64"),
  });
  await page.locator("#logo-submit").click();
  await expect(page.locator("#preview svg image")).toHaveCount(1);
  const source = await page.evaluate(() => window.__nfMetro.getValue());

  await page.locator("#btn-share").click();
  await page.waitForFunction(() => location.hash.includes("mmd-gz="));
  const url = await page.evaluate(() => location.href);

  // A full navigation to the shared URL starts from no prior editor state, so
  // it stands in for the recipient opening the link cold: the logo must
  // render with no network/disk access since the data URI travels entirely
  // inside the link itself.
  await page.goto(url);
  await waitReady(page);
  expect(await page.evaluate(() => window.__nfMetro.getValue())).toBe(source);
  await expect(page.locator("#preview svg image")).toHaveCount(1);
});

// A two-section map used by the graphical-editing tests below.
const EDIT_MAP =
  "%%metro line: a | A | #f00\n" +
  "%%metro line: b | B | #0af\n" +
  "graph LR\n" +
  "  subgraph s1 [One]\n" +
  "    n1[N1]\n" +
  "    n2[N2]\n" +
  "    n1 -->|a| n2\n" +
  "  end\n" +
  "  subgraph s2 [Two]\n" +
  "    n3[N3]\n" +
  "  end\n" +
  "  n2 -->|a| n3\n";

const getValue = () => page.evaluate(() => window.__nfMetro.getValue());

async function loadEditMap() {
  await page.evaluate((m) => {
    window.__nfMetro.setMode("select");
    window.__nfMetro.setValue(m);
  }, EDIT_MAP);
  await expect(
    page.locator('#preview [data-station-id="n1"]').first(),
  ).toBeVisible();
}

test("edit-mode buttons toggle and update the hint", async () => {
  await loadEditMap();
  await page.locator('.mode-btn[data-mode="add-edge"]').click();
  await expect(page.locator('.mode-btn[data-mode="add-edge"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.locator('.mode-btn[data-mode="select"]')).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await expect(page.locator("#edit-hint")).toContainText(/source station/i);
  await page.locator('.mode-btn[data-mode="select"]').click();
  await expect(page.locator('.mode-btn[data-mode="add-edge"]')).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

test("clicking a station selects it and shows the property panel", async () => {
  await loadEditMap();
  await expect(page.locator("#prop-panel")).toBeHidden();
  await page.locator('#preview [data-station-id="n1"]').first().click();
  await expect(page.locator("#prop-panel")).toBeVisible();
  await expect(page.locator("#prop-kind")).toHaveText("station");
  await expect(page.locator("#prop-body")).toContainText("id: n1");
  await page.locator("#prop-close").click();
  await expect(page.locator("#prop-panel")).toBeHidden();
});

test("add-station writes a node into the chosen section", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.addStationToSection("s2"));
  expect(await getValue()).toMatch(
    /subgraph s2 \[Two\][\s\S]*node1\[New node\][\s\S]*end/,
  );
  await expect(
    page.locator('#preview [data-station-id="node1"]').first(),
  ).toBeVisible();
  await expect(page.locator("#prop-body")).toContainText("id: node1");
});

test("connect inserts an edge between two stations", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.connect("n1", "n3", "b"));
  expect(await getValue()).toContain("n1 -->|b| n3");
});

test("add-section appends a new subgraph block and renders it", async () => {
  await loadEditMap();
  await page.locator("#btn-add-section").click();
  expect(await getValue()).toMatch(/subgraph section1 \[New Section\]/);
  await expect(
    page.locator('#preview [data-section-id="section1"]').first(),
  ).toBeVisible();
});

test("rename station rewrites its label and keeps the id", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.renameStation("n1", "Renamed"));
  expect(await getValue()).toContain("n1[Renamed]");
});

test("reassign edge line rewrites the line token", async () => {
  await loadEditMap();
  await page.evaluate(() => {
    const edge = window.__nfMetro
      .parseEdges()
      .find((e) => e.src === "n1" && e.tgt === "n2");
    window.__nfMetro.reassignEdgeLine(edge.lineNo, "b");
  });
  expect(await getValue()).toContain("n1 -->|b| n2");
});

test("section grid directive is written then cleared", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.setSectionGrid("s1", 1, 0));
  expect(await getValue()).toContain("%%metro grid: s1 | 1,0");
  await page.evaluate(() => window.__nfMetro.setSectionGrid("s1", null));
  expect(await getValue()).not.toContain("grid: s1");
});

test("delete edge removes its line", async () => {
  await loadEditMap();
  await page.evaluate(() => {
    const edge = window.__nfMetro
      .parseEdges()
      .find((e) => e.src === "n2" && e.tgt === "n3");
    window.__nfMetro.deleteEdge(edge.lineNo);
  });
  expect(await getValue()).not.toContain("n2 -->|a| n3");
});

test("delete station removes its declaration and incident edges", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.deleteStation("n2"));
  const v = await getValue();
  expect(v).not.toContain("n2[N2]");
  expect(v).not.toContain("-->|a| n2");
  expect(v).not.toContain("n2 -->|a| n3");
  await expect(page.locator("#error")).toBeHidden();
});

test("delete section removes the block and its inter-section edges", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.deleteSection("s2"));
  const v = await getValue();
  expect(v).not.toContain("subgraph s2");
  expect(v).not.toContain("n3[N3]");
  expect(v).not.toContain("n2 -->|a| n3");
});

test("clicking an in-section edge selects it as an edge", async () => {
  await loadEditMap();
  // The first route element is the n1->n2 in-section edge, whose endpoints sit
  // on both stations, so it resolves to a specific edge rather than the line. A
  // positional click at the stroke centre matches a real click on a thin route
  // (element-level .click() is flaky on near-zero-height SVG paths).
  const box = await page
    .locator('#preview [data-line-id="a"]')
    .first()
    .boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator("#prop-panel")).toBeVisible();
  await expect(page.locator("#prop-kind")).toHaveText("edge");
  await expect(page.locator("#prop-body")).toContainText("n1");
  await expect(page.locator("#prop-body")).toContainText("n2");
});

test("splitting an edge inserts a station between its endpoints", async () => {
  await loadEditMap();
  await page.evaluate(() => window.__nfMetro.splitEdge("n1", "n2", "a"));
  const v = await getValue();
  expect(v).toContain("n1 -->|a| node1");
  expect(v).toContain("node1 -->|a| n2");
  expect(v).toContain("node1[New node]");
  expect(v).not.toContain("n1 -->|a| n2");
  await expect(
    page.locator('#preview [data-station-id="node1"]').first(),
  ).toBeVisible();
});

test("splitting a multi-line edge keeps every line on both halves", async () => {
  await page.evaluate(() =>
    window.__nfMetro.setValue(
      "%%metro line: a | A | #f00\n%%metro line: b | B | #0af\n" +
        "graph LR\n  subgraph s [S]\n    x[X]\n    y[Y]\n    x -->|a,b| y\n  end\n",
    ),
  );
  await expect(
    page.locator('#preview [data-station-id="x"]').first(),
  ).toBeVisible();
  await page.evaluate(() => window.__nfMetro.splitEdge("x", "y", "a"));
  const v = await getValue();
  expect(v).toContain("x -->|a,b| node1");
  expect(v).toContain("node1 -->|a,b| y");
});

test("the line panel offers add and delete on each edge", async () => {
  await loadEditMap();
  // Clicking an inter-section route selects the line; its edge list carries a
  // splice (+) and delete (x) control per edge.
  await page.evaluate(() => window.__nfMetro.select({ kind: "line", id: "a" }));
  await expect(page.locator("#prop-kind")).toHaveText("line");
  await expect(page.locator(".prop-edge button.add").first()).toBeVisible();
  await expect(page.locator(".prop-edge button.del").first()).toBeVisible();
  await page.locator(".prop-edge button.add").first().click();
  expect(await getValue()).toContain("node1");
});

test("rendering runs off the main browser thread", async () => {
  const elapsed = await page.evaluate(async () => {
    const started = performance.now();
    window.__nfMetro.render();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return performance.now() - started;
  });
  expect(elapsed).toBeLessThan(100);
});

test("the example picker exposes a curated set", async () => {
  const options = page.locator("#example-select option");
  await expect.poll(async () => options.count()).toBeGreaterThan(2);
  expect(await options.count()).toBeLessThanOrEqual(12);
});

test("opening a metro source file replaces the document", async () => {
  const source =
    "%%metro title: Opened File\n%%metro line: a | A | #f00\n" +
    "graph LR\n  one[One] -->|a| two[Two]\n";
  await page.locator("#file-open").setInputFiles({
    name: "opened.mmd",
    mimeType: "text/plain",
    buffer: Buffer.from(source),
  });
  await expect
    .poll(() => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("Opened File");
  await expect(
    page.locator('#preview [data-station-id="one"]').first(),
  ).toBeVisible();
});

test("a local draft survives a page reload", async () => {
  const source =
    "%%metro title: Recovered Draft\n%%metro line: a | A | #f00\n" +
    "graph LR\n  saved[Saved] -->|a| restored[Restored]\n";
  await page.evaluate((value) => window.__nfMetro.setValue(value), source);
  await expect
    .poll(() =>
      page.evaluate(
        () => localStorage.getItem("nf-metro-playground-draft-v2") || "",
      ),
    )
    .toContain("Recovered Draft");
  await page.evaluate(() => history.replaceState(null, "", location.pathname));
  await page.reload();
  await waitReady(page);
  await expect
    .poll(() => page.evaluate(() => window.__nfMetro.getValue()))
    .toContain("Recovered Draft");
  await expect(
    page.locator('#preview [data-station-id="saved"]').first(),
  ).toBeVisible();
});

test("replaced documents remain available as recent maps", async () => {
  await page.evaluate(() => document.getElementById("btn-new").click());
  await expect
    .poll(async () => page.locator("#recent-select option").count())
    .toBeGreaterThan(1);
});

test("source completion exposes directives and graph snippets", async () => {
  await page.evaluate(() => window.__nfMetro.complete());
  await expect(page.locator(".CodeMirror-hints")).toBeVisible();
  await expect(page.locator(".CodeMirror-hint").first()).toContainText("metro");
  await page.keyboard.press("Escape");
});

test("the command palette filters and runs keyboard-first actions", async () => {
  await page.keyboard.press("Control+k");
  await expect(page.locator("#command-modal")).toBeVisible();
  await page.locator("#command-search").fill("layout controls");
  await expect(page.locator(".command-item")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(page.locator("#command-modal")).toBeHidden();
});

test("mobile switches between full-height source and preview views", async () => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.mobile-view[data-view="source"]').click();
  await expect(page.locator("#editor-pane")).toBeVisible();
  await expect(page.locator("#preview-pane")).toBeHidden();
  await page.locator('.mobile-view[data-view="preview"]').click();
  await expect(page.locator("#editor-pane")).toBeHidden();
  await expect(page.locator("#preview-pane")).toBeVisible();
  const sizes = await page.evaluate(() => [
    innerWidth,
    document.body.scrollWidth,
  ]);
  expect(sizes[1]).toBeLessThanOrEqual(sizes[0]);
  await page.setViewportSize({ width: 1280, height: 720 });
});
