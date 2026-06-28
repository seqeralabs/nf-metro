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
 * Requires `nf-metro` on PATH (activate the nf-core micromamba env).
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CACHE_DIR = join(__dirname, "../../.metro-cache");
mkdirSync(CACHE_DIR, { recursive: true });

// Repo root (two levels above the Astro project). nf-metro resolves a map's
// relative asset paths (e.g. `%%metro logo: examples/...png`) against the
// working directory, so renders must run from here or the logo is dropped.
const REPO_ROOT = join(__dirname, "../../..");

/** drawsvg emits an XML prolog; strip it before inlining into HTML. */
const XML_PROLOG_RE = /^<\?xml.*?\?>\s*/;

/**
 * Render a committed `.mmd` file to inline SVG markup.
 * @param {string} file  Absolute path to the source `.mmd`.
 * @param {{ debug?: boolean, fromNextflow?: boolean }} [opts]
 * @returns {string} inline `<svg>` markup
 */
export function renderMetroFile(file, { debug = false, fromNextflow = false } = {}) {
  const source = readFileSync(file, "utf-8");
  const mode = `${debug ? "d" : ""}${fromNextflow ? "n" : ""}`;
  const hash = createHash("sha256")
    .update(`${mode}\n${source}`)
    .digest("hex")
    .slice(0, 16);
  const cacheFile = join(CACHE_DIR, `${hash}.svg`);

  try {
    return readFileSync(cacheFile, "utf-8");
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
