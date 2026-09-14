#!/usr/bin/env node
// Records the docs/live.mdx demo clip (website/public/assets/live_demo.mp4).
//
// Lives in this package (rather than scripts/) to reuse its @playwright/test
// devDependency and already-cached Chromium download - it is not itself an
// e2e test.
//
// Usage (from the repo root, with an nf-metro + Nextflow env active - see
// the `live-demo-video` skill):
//   node website/playground-tests/record-live-demo.js <output-dir>
//
// Produces <output-dir>/*.webm. Convert that to the committed mp4 with
// scripts/convert_demo_video.sh (see the skill for why: Playwright only
// records webm).
const { chromium } = require("@playwright/test");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const PORT = 8790;
const SERVE_URL = `http://localhost:${PORT}/`;
const STATE_URL = `http://localhost:${PORT}/state`;
const MAP = "examples/live/pipeline.mmd";
const WORKFLOW = "examples/live/workflow/main.nf";
const WORKFLOW_CONFIG = "examples/live/workflow/nextflow.config";
// Padding added to the measured content box so `.stage`'s `overflow: auto`
// never grows a scrollbar over a viewport sized to the exact content size.
const SIZE_PAD = 4;

const OUT_DIR = process.argv[2];
const REPO_ROOT = process.argv[3] || process.cwd();
if (!OUT_DIR) {
  console.error("usage: record-live-demo.js <output-dir> [repo-root]");
  process.exit(1);
}

function fetchState() {
  return new Promise((resolve, reject) => {
    http
      .get(STATE_URL, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForServer(deadline) {
  while (Date.now() < deadline) {
    try {
      await fetchState();
      return true;
    } catch (e) {
      await sleep(300);
    }
  }
  return false;
}

// The page has no single wrapper around its header + map card - both are
// direct <body> children, and a plain block box always stretches to fill the
// viewport, so measuring any of *their* rects just reports back whatever
// viewport we already chose. The header's own two flex items (#run,
// .controls) and the map's .wrap div are each sized to their own content
// instead (no flex-grow, no stretch), so those are what actually tell us how
// small the viewport can be before the header wraps or the map card clips -
// i.e. the content's true footprint, independent of viewport size.
async function measureContentSize(browser) {
  const context = await browser.newContext({ colorScheme: "dark" });
  const page = await context.newPage();
  try {
    await page.goto(SERVE_URL, { waitUntil: "load" });
    await page.waitForSelector(".wrap > svg");
    return await page.evaluate(() => {
      // #run's text is "Run: <name> · <state>" - both pieces change once the
      // real run starts, and Nextflow's auto-generated <adjective>_<surname>
      // run names run considerably longer than the "waiting for events"
      // placeholder shown before that. Swap in a deliberately long name and
      // the longest state word ("complete") before measuring, so sizing to
      // this idle snapshot doesn't leave the header too narrow for what it
      // actually displays a few seconds later - it would visibly reflow
      // (the flex item shrinks and its text wraps) once the run starts.
      document.getElementById("run-name").textContent =
        "affectionate_chandrasekhar";
      document.getElementById("run-state").textContent = "complete";

      const px = (v) => parseFloat(v) || 0;
      const header = document.querySelector("header");
      const headerStyle = getComputedStyle(header);
      const run = document.getElementById("run");
      const controls = document.querySelector(".controls");
      const stage = document.querySelector(".stage");
      const stageStyle = getComputedStyle(stage);
      const wrap = document.querySelector(".wrap");

      const headerContentWidth =
        px(headerStyle.paddingLeft) +
        run.getBoundingClientRect().width +
        px(headerStyle.columnGap) +
        controls.getBoundingClientRect().width +
        px(headerStyle.paddingRight);
      const stageContentWidth =
        px(stageStyle.paddingLeft) +
        wrap.getBoundingClientRect().width +
        px(stageStyle.paddingRight);

      return {
        width: Math.ceil(Math.max(headerContentWidth, stageContentWidth)),
        height: Math.ceil(
          header.getBoundingClientRect().height +
            px(stageStyle.paddingTop) +
            wrap.getBoundingClientRect().height +
            px(stageStyle.paddingBottom),
        ),
      };
    });
  } finally {
    await context.close();
  }
}

(async () => {
  const server = spawn("nf-metro", ["serve", MAP, "--port", String(PORT)], {
    cwd: REPO_ROOT,
    stdio: "inherit",
  });

  try {
    if (!(await waitForServer(Date.now() + 15_000))) {
      throw new Error("nf-metro serve did not come up in time");
    }

    const browser = await chromium.launch();
    try {
      const measured = await measureContentSize(browser);
      const size = {
        width: measured.width + SIZE_PAD,
        height: measured.height + SIZE_PAD,
      };

      const context = await browser.newContext({
        viewport: size,
        // nf-metro serve bakes one concrete light/dark mode into the SVG
        // rather than shipping light-dark() for it, independent of the page
        // chrome (which does follow prefers-color-scheme) - force dark so
        // the two match instead of pairing a dark toolbar with a light map
        // card.
        colorScheme: "dark",
        recordVideo: { dir: OUT_DIR, size },
      });
      try {
        const page = await context.newPage();
        // The page holds an open SSE connection to /stream, so
        // "networkidle" never fires - wait for the map SVG instead.
        await page.goto(SERVE_URL, { waitUntil: "load" });
        await page.waitForSelector(".wrap > svg");

        // Let the idle map sit on screen for a beat before the run starts.
        await sleep(1500);

        const nextflow = spawn(
          "nextflow",
          [
            "run",
            WORKFLOW,
            "-c",
            WORKFLOW_CONFIG,
            "-with-weblog",
            `http://localhost:${PORT}/events`,
          ],
          { cwd: REPO_ROOT, stdio: "inherit" },
        );
        try {
          const deadline = Date.now() + 120_000;
          let finished = false;
          while (Date.now() < deadline) {
            await sleep(1000);
            try {
              const state = await fetchState();
              // The state schema calls this "complete", not "completed".
              if (
                state.run &&
                (state.run.state === "complete" || state.run.state === "error")
              ) {
                finished = true;
                break;
              }
            } catch (e) {
              // server between requests - keep polling
            }
          }
          if (!finished) {
            console.error("Timed out waiting for the run to finish");
          }

          // Hold on the finished state for a couple of seconds before cutting.
          await sleep(2500);
        } finally {
          nextflow.kill();
        }
      } finally {
        await context.close();
      }
    } finally {
      await browser.close();
    }
  } finally {
    server.kill();
  }

  console.log(`Recorded to ${path.resolve(OUT_DIR)}`);
})();
