/**
 * Vite plugin that resolves `<path>.mmd?metro` imports to the map's inline SVG,
 * rendered at build time via the `nf-metro` CLI. The <Metro> component imports
 * maps through this plugin (`import.meta.glob(..., { query: '?metro' })`) so the
 * SVG arrives as a normal module string: an inlined `<svg>` whose `light-dark()`
 * chrome follows the page's color-scheme, and whose elements (e.g. an embedded
 * legend logo `<image>`) survive the production HTML build, which a runtime
 * `set:html` string does not.
 *
 * Query flags select the render mode: `?metro&debug` adds the layout overlay,
 * `?metro&nextflow` converts a Nextflow DAG first.
 *
 * Rendered SVGs are cached by SHA-256 of the (mode, source) pair under
 * website/.metro-cache/ (git-ignored), so re-builds only shell out on the first
 * encounter of each unique map.
 *
 * At the start of a production build the plugin pre-warms the cache by invoking
 * `nf-metro render-many` once with all corpus maps, amortising Python startup
 * across the full set rather than spawning one process per map.
 *
 * Requires `nf-metro` on PATH (activate the nf-core micromamba env).
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import {
  OG_GALLERY_MAP,
  OG_PIPELINES_MAP,
  OG_DEFAULT_MAP,
} from "./og-targets.mjs";

// Paths are anchored to process.cwd() rather than import.meta.url: `npm run
// {dev,build}` always runs with cwd = website/ (see scripts/serve_docs.sh and
// .github/workflows/docs.yml), whereas import.meta.url is only stable when
// this module loads through Vite's plugin container (the `<Metro>` embed
// path). A page endpoint that imports renderMetroFile directly (e.g. OG image
// generation) gets this module rolled into a relocated prerender chunk, which
// would silently break any __dirname-relative path.
const CACHE_DIR = join(process.cwd(), ".metro-cache");
mkdirSync(CACHE_DIR, { recursive: true });

// Repo root (one level above the Astro project). nf-metro resolves a map's
// relative asset paths (e.g. `%%metro logo: examples/...png`) against the
// working directory, so renders must run from here or the logo is dropped.
export const REPO_ROOT = join(process.cwd(), "..");

// Part of the cache key, so a released layout change invalidates cached SVGs
// instead of silently serving stale renders for unchanged sources. (An editable
// install whose code changed without a version bump still needs the cache
// cleared - `serve_docs.sh --rebuild` does that.)
let NF_METRO_VERSION = "unknown";
try {
  NF_METRO_VERSION = execFileSync("nf-metro", ["--version"], {
    cwd: REPO_ROOT,
  })
    .toString()
    .trim();
} catch {
  /* nf-metro absent at init; renderMetroFile reports it clearly on first use */
}

/** drawsvg emits an XML prolog; strip it before inlining into HTML. */
const XML_PROLOG_RE = /^<\?xml.*?\?>\s*/;

/**
 * Recursively collect .mmd files under `dir`.
 * @param {string} dir
 * @param {boolean} recursive  When false, only top-level files are returned.
 * @returns {string[]} Absolute paths.
 */
function findMmdFiles(dir, recursive = true) {
  const results = [];
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return results;
  }
  for (const entry of entries) {
    if (entry.isFile() && entry.name.endsWith(".mmd")) {
      results.push(join(dir, entry.name));
    } else if (recursive && entry.isDirectory()) {
      results.push(...findMmdFiles(join(dir, entry.name), true));
    }
  }
  return results;
}

/**
 * Compute the cache key for a given (mode, source) pair.
 * Must stay in sync with the hash in renderMetroFile.
 * @param {string} source  Raw .mmd source text.
 * @param {string} mode    Render mode string: "", "d", "n", "c" (chrome-less), combinations thereof.
 * @returns {string} 16-character hex hash.
 */
function cacheHash(source, mode) {
  return createHash("sha256")
    .update(`${NF_METRO_VERSION}\n${mode}\n${source}`)
    .digest("hex")
    .slice(0, 16);
}

/**
 * Collect the `.mmd` source paths that need a chrome-less ("baked colour")
 * render for OG image generation: every pipelines content-collection entry
 * (website/src/content/pipelines.json, written by `scripts/build_gallery.py`
 * before the Astro build runs - each pipeline gets its own OG image), plus
 * the fixed maps `og-targets.mjs` names for the non-per-entry OG routes
 * (gallery index, pipelines index, site-wide default). Gallery entries don't
 * get individual OG images (177 internal layout-regression fixtures aren't
 * pages anyone links to externally), so gallery.json isn't consulted here.
 *
 * Returns just the fixed maps if pipelines.json hasn't been generated yet
 * (e.g. a raw `astro dev` without that step) - og-image.mjs falls back to
 * rendering on demand in that case.
 * @returns {string[]} Absolute `.mmd` paths.
 */
function findOgSourceFiles() {
  const contentDir = join(process.cwd(), "src/content");
  const fixedMaps = [OG_GALLERY_MAP, OG_PIPELINES_MAP, OG_DEFAULT_MAP];
  const files = new Set(fixedMaps.map((p) => join(REPO_ROOT, p)));
  try {
    const entries = JSON.parse(
      readFileSync(join(contentDir, "pipelines.json"), "utf-8"),
    );
    for (const entry of entries) {
      files.add(join(REPO_ROOT, entry.src));
    }
  } catch (e) {
    if (e.code !== "ENOENT") {
      console.warn(`nf-metro: failed to read pipelines.json for OG pre-warm: ${e.message}`);
    }
  }
  return [...files];
}

/**
 * Pre-warm the .metro-cache by rendering all corpus maps in a single
 * `nf-metro render-many` call.  Maps whose cache entry already exists are
 * skipped.  Called once at the start of each production build; individual
 * load() calls then hit the warm cache without spawning a new process.
 *
 * Mirror of Metro.astro's import.meta.glob patterns:
 *   examples/**\/*.mmd          → plain mode
 *   examples/*.mmd              → also debug mode
 *   tests/fixtures/*.mmd        → plain mode (top-level only)
 *   tests/fixtures/nextflow/**  → nextflow mode
 *
 * Also renders a chrome-less ("baked colour") copy of every OG-image source
 * map (see findOgSourceFiles), which og-image.mjs rasterizes into OG preview
 * images - batching them here amortises Python startup the same way the
 * plain renders do.
 */
function prewarmMetroCache() {
  if (NF_METRO_VERSION === "unknown") {
    console.warn(
      "nf-metro: not on PATH; skipping build-time cache pre-warm. " +
        "Individual renders will still run on demand.",
    );
    return;
  }

  /** @type {Array<{input:string, output:string, debug:boolean, from_nextflow:boolean, no_self_color_scheme:boolean, no_chrome_css:boolean, no_manifest:boolean}>} */
  const jobs = [];

  function addJob(file, mode) {
    const source = readFileSync(file, "utf-8");
    const hash = cacheHash(source, mode);
    const cacheFile = join(CACHE_DIR, `${hash}.svg`);
    if (!existsSync(cacheFile)) {
      jobs.push({
        input: file,
        output: cacheFile,
        debug: mode.includes("d"),
        from_nextflow: mode.includes("n"),
        no_self_color_scheme: true,
        no_chrome_css: mode.includes("c"),
        layout_options: { manifest: false },
      });
    }
  }

  // examples/**/*.mmd → plain; examples/*.mmd → also debug
  const examplesDir = join(REPO_ROOT, "examples");
  for (const file of findMmdFiles(examplesDir, true)) {
    addJob(file, "");
    if (dirname(file) === examplesDir) {
      addJob(file, "d");
    }
  }

  // tests/fixtures/*.mmd (top-level only) → plain
  const fixturesDir = join(REPO_ROOT, "tests", "fixtures");
  for (const file of findMmdFiles(fixturesDir, false)) {
    addJob(file, "");
  }

  // tests/fixtures/nextflow/**/*.mmd → nextflow
  for (const file of findMmdFiles(join(fixturesDir, "nextflow"), true)) {
    addJob(file, "n");
  }

  // OG image sources → chrome-less, for OG image rasterization
  for (const file of findOgSourceFiles()) {
    addJob(file, "c");
  }

  if (jobs.length === 0) {
    console.log("nf-metro: cache already warm, skipping pre-warm.");
    return;
  }

  console.log(`nf-metro: pre-warming cache — rendering ${jobs.length} map(s)...`);
  const manifestPath = join(tmpdir(), "metro-batch.json");
  writeFileSync(manifestPath, JSON.stringify(jobs), "utf-8");
  try {
    execFileSync("nf-metro", ["render-many", manifestPath], {
      cwd: REPO_ROOT,
      stdio: "inherit",
    });
  } catch (err) {
    console.warn(
      "nf-metro render-many failed; individual renders will fall back " +
        `to per-file mode.\n${err.message}`,
    );
  } finally {
    try {
      unlinkSync(manifestPath);
    } catch {
      /* best-effort */
    }
  }
}

/**
 * Render a committed `.mmd` file to inline SVG markup.
 * @param {string} file  Absolute path to the source `.mmd`.
 * @param {{ debug?: boolean, fromNextflow?: boolean, chromeCss?: boolean }} [opts]
 *   `chromeCss: false` bakes concrete colours instead of the `--nfm-*` custom
 *   properties `<Metro>` relies on for live host recoloring - use this for
 *   standalone raster export (e.g. OG images), never for inline embeds.
 * @returns {string} inline `<svg>` markup
 */
export function renderMetroFile(
  file,
  { debug = false, fromNextflow = false, chromeCss = true } = {},
) {
  const source = readFileSync(file, "utf-8");
  const mode = `${debug ? "d" : ""}${fromNextflow ? "n" : ""}${chromeCss ? "" : "c"}`;
  const hash = cacheHash(source, mode);
  const cacheFile = join(CACHE_DIR, `${hash}.svg`);

  try {
    return readFileSync(cacheFile, "utf-8").replace(XML_PROLOG_RE, "");
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }

  const tmpOutput = join(tmpdir(), `metro-${hash}.svg`);
  // Render the real file (not a temp copy) from the repo root so the map's
  // relative asset paths resolve.
  const args = [
    "render",
    file,
    "-o",
    tmpOutput,
    "--no-self-color-scheme",
    "--no-manifest",
  ];
  if (debug) args.push("--debug");
  if (fromNextflow) args.push("--from-nextflow");
  if (!chromeCss) args.push("--no-chrome-css");

  try {
    execFileSync("nf-metro", args, {
      cwd: REPO_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (err) {
    const stderr = err.stderr?.toString().trim() || "";
    throw new Error(
      `nf-metro render failed for ${file}:\n${stderr}\n\n` +
        `Make sure 'nf-metro' is on PATH (activate the nf-core micromamba env).`,
    );
  }

  const svg = readFileSync(tmpOutput, "utf-8").replace(XML_PROLOG_RE, "");
  try {
    unlinkSync(tmpOutput);
  } catch {
    /* best-effort cleanup */
  }
  writeFileSync(cacheFile, svg, "utf-8");
  return svg;
}

/** @returns {import('vite').Plugin} */
export function metroVitePlugin() {
  return {
    name: "nf-metro-svg",
    buildStart() {
      prewarmMetroCache();
    },
    load(id) {
      const [file, query = ""] = id.split("?");
      if (!file.endsWith(".mmd")) return;
      const params = new URLSearchParams(query);
      if (!params.has("metro")) return;
      const svg = renderMetroFile(file, {
        debug: params.has("debug"),
        fromNextflow: params.has("nextflow"),
      });
      return { code: `export default ${JSON.stringify(svg)};`, map: null };
    },
  };
}
