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
 * `?metro&nextflow` converts a Nextflow DAG first, `?metro&animate` adds the
 * `--animate` balls (still SVG - the CSS drives their motion).
 *
 * Rendered SVGs are cached by SHA-256 of the (mode, source) pair under
 * website/.metro-cache/ (git-ignored), so re-builds only shell out on the first
 * encounter of each unique map.
 *
 * At the start of a production build the plugin pre-warms the cache by invoking
 * `nf-metro render-many` once with all corpus maps, amortising Python startup
 * across the full set rather than spawning one process per map.
 *
 * `renderMetroBinary` (below) is a separate, simpler path for the raster and
 * looping-video formats (png, gif, webp, mp4, webm): unlike an SVG, their
 * bytes can't be inlined into the page, so they're written once under the
 * git-ignored `website/public/_generated/metro/` and referenced by URL. It is
 * called directly from `<Metro>`'s frontmatter rather than through this
 * plugin's `load()` hook - a `.astro` component's frontmatter already runs
 * server-side at build (and per-request in dev), so there's no need to route
 * a URL-returning call through Vite's module graph the way the inlined-SVG
 * string needs to be, to survive the production HTML build.
 *
 * Requires `nf-metro` on PATH (activate the nf-core micromamba env).
 */

import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  statSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
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

// Public output for renderMetroBinary: a normal static file under this
// directory is served (and copied into the production build) like any other
// public/ asset, so a <Metro format="gif" .../> URL needs no plugin support.
const GENERATED_DIR = join(process.cwd(), "public", "_generated", "metro");
mkdirSync(GENERATED_DIR, { recursive: true });

// Repo root (one level above the Astro project): where the map corpus lives,
// and the working directory the nf-metro CLI is invoked from.
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
 * Collect files ending in `suffix` under `dir`, recursing by default.
 * @param {string} dir
 * @param {boolean} recursive  When false, only top-level files are returned.
 * @returns {string[]} Absolute paths.
 */
function findFiles(dir, recursive = true, suffix = ".mmd") {
  const results = [];
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return results;
  }
  for (const entry of entries) {
    if (entry.isFile() && entry.name.endsWith(suffix)) {
      results.push(join(dir, entry.name));
    } else if (recursive && entry.isDirectory()) {
      results.push(...findFiles(join(dir, entry.name), true, suffix));
    }
  }
  return results;
}

/**
 * DAG formats `nf-metro render` converts on the way in, keyed by the
 * `inputFormat` the render helpers take. `letter` joins the cache mode
 * string; `ext` is the source extension the Vite plugin loads; `lang` is the
 * source's highlighting language; `lines` is the `nf-metro convert --lines`
 * mode the format uses by default; `fixtures` lists the directory under
 * tests/fixtures/ and the suffix whose files the build pre-warms.
 */
export const INPUT_FORMATS = {
  nextflow: {
    letter: "n",
    ext: ".mmd",
    lang: "metro",
    lines: "sections",
    fixtures: { dir: "nextflow", suffix: ".mmd", recursive: true },
  },
  snakemake: {
    letter: "s",
    ext: ".dot",
    lang: "dot",
    lines: "graph",
    fixtures: { dir: "snakemake", suffix: ".rulegraph.dot", recursive: false },
  },
};

/**
 * Compute the cache key for a given (mode, source) pair.
 * Must stay in sync with the hash in renderMetroFile.
 * @param {string} source  Raw .mmd source text.
 * @param {string} mode    Render mode string: "", "d" (debug), "n"/"s" (Nextflow/Snakemake
 *   input), "G"/"S" (graph/section lines), "c" (chrome-less), "a" (animate), combinations thereof.
 * @returns {string} 16-character hex hash.
 */
function cacheHash(source, mode) {
  return createHash("sha256")
    .update(`${NF_METRO_VERSION}\n${mode}\n${source}`)
    .digest("hex")
    .slice(0, 16);
}

/**
 * Per-map SVG class prefix. Inline SVG `<style>` rules are document-global, so
 * maps sharing the default `nf-metro-*` class names restyle one another
 * whenever their stylesheets differ (themes, modes). Deriving the prefix from
 * the cache hash keeps it unique per map and stable across rebuilds.
 * @param {string} hash  Cache hash of the map.
 * @returns {string}
 */
function classPrefix(hash) {
  return `m${hash.slice(0, 8)}`;
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
 *   examples/**\/*.mmd                  → plain mode
 *   examples/*.mmd, examples/guide/*.mmd → also debug, animate
 *   tests/fixtures/*.mmd                → plain mode (top-level only)
 *   tests/fixtures/nextflow/**          → nextflow mode
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

  /** @type {Array<{input:string, output:string, debug:boolean, from?:string, no_self_color_scheme:boolean, no_chrome_css:boolean, svg_class_prefix:string, layout_options:{manifest:boolean, animate?:boolean}}>} */
  const jobs = [];

  function addJob(file, mode) {
    const source = readFileSync(file, "utf-8");
    const hash = cacheHash(source, mode);
    const cacheFile = join(CACHE_DIR, `${hash}.svg`);
    if (!existsSync(cacheFile)) {
      const layoutOptions = { manifest: false };
      if (mode.includes("a")) layoutOptions.animate = true;
      jobs.push({
        input: file,
        output: cacheFile,
        debug: mode.includes("d"),
        from: Object.entries(INPUT_FORMATS).find(([, f]) => mode.includes(f.letter))?.[0],
        no_self_color_scheme: true,
        no_chrome_css: mode.includes("c"),
        svg_class_prefix: classPrefix(hash),
        layout_options: layoutOptions,
      });
    }
  }

  // examples/**/*.mmd → plain; examples/ and examples/guide/ → also debug, animate
  const examplesDir = join(REPO_ROOT, "examples");
  const examplesGuideDir = join(examplesDir, "guide");
  for (const file of findFiles(examplesDir, true)) {
    addJob(file, "");
    if (dirname(file) === examplesDir || dirname(file) === examplesGuideDir) {
      addJob(file, "d");
      addJob(file, "a");
    }
  }

  // tests/fixtures/*.mmd (top-level only) → plain
  const fixturesDir = join(REPO_ROOT, "tests", "fixtures");
  for (const file of findFiles(fixturesDir, false)) {
    addJob(file, "");
  }

  // DAG-export fixtures → converted with their format's flag
  for (const { letter, fixtures } of Object.values(INPUT_FORMATS)) {
    for (const file of findFiles(join(fixturesDir, fixtures.dir), fixtures.recursive, fixtures.suffix)) {
      addJob(file, letter);
    }
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
 * @param {{ debug?: boolean, inputFormat?: keyof typeof INPUT_FORMATS | null, lines?: "graph" | "sections" | null, chromeCss?: boolean, animate?: boolean }} [opts]
 *   `chromeCss: false` bakes concrete colours instead of the `--nfm-*` custom
 *   properties `<Metro>` relies on for live host recoloring - use this for
 *   standalone raster export (e.g. OG images), never for inline embeds.
 *   `animate: true` adds the `--animate` balls (still plain SVG; their motion
 *   is CSS, not baked frames - see `renderMetroBinary` for a looping export).
 *   `lines` (with `inputFormat`) converts with `nf-metro convert --lines` first,
 *   then renders the converted map.
 * @returns {string} inline `<svg>` markup
 */
export function renderMetroFile(
  file,
  { debug = false, inputFormat = null, lines = null, chromeCss = true, animate = false } = {},
) {
  const source = readFileSync(file, "utf-8");
  const converter = inputFormat ? INPUT_FORMATS[inputFormat] : null;
  const linesLetter = { graph: "G", sections: "S" }[lines] ?? "";
  const mode = `${debug ? "d" : ""}${converter?.letter ?? ""}${linesLetter}${chromeCss ? "" : "c"}${animate ? "a" : ""}`;
  const hash = cacheHash(source, mode);
  const cacheFile = join(CACHE_DIR, `${hash}.svg`);

  try {
    return readFileSync(cacheFile, "utf-8").replace(XML_PROLOG_RE, "");
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }

  const tmpOutput = join(tmpdir(), `metro-${hash}.svg`);
  const converted = lines ? join(tmpdir(), `metro-${hash}.mmd`) : null;
  let svg;
  try {
    if (converted) {
      const stderr = runNfMetro(
        ["convert", file, "--from", inputFormat, "--lines", lines, "-o", converted],
        file,
      );
      // Matches the warning nf-metro convert prints when a map of several
      // sections keeps its section lines, which would render the same picture
      // as the format's default embed.
      if (stderr.includes("only in a one-section map")) {
        throw Object.assign(
          new Error(`<Metro lines="${lines}">: ${file} has several sections:\n${stderr}`),
          { onImport: true },
        );
      }
    }
    // Render the real file rather than a temp copy: the CLI resolves a map's
    // asset paths against the directory the file itself sits in. A converted
    // DAG has no assets, so its temp copy is safe to render.
    const args = [
      "render",
      converted ?? file,
      "-o",
      tmpOutput,
      "--no-self-color-scheme",
      "--no-manifest",
      "--svg-class-prefix",
      classPrefix(hash),
    ];
    if (debug) args.push("--debug");
    if (converter && !converted) args.push("--from", inputFormat);
    if (!chromeCss) args.push("--no-chrome-css");
    if (animate) args.push("--animate");
    runNfMetro(args, file);
    svg = readFileSync(tmpOutput, "utf-8").replace(XML_PROLOG_RE, "");
  } finally {
    for (const tmp of [tmpOutput, converted]) if (tmp) rmSync(tmp, { force: true });
  }
  writeFileSync(cacheFile, svg, "utf-8");
  return svg;
}

/**
 * Run the nf-metro CLI from the repo root, turning a failure into an error
 * that names the source and quotes nf-metro's stderr.
 * @param {string[]} args
 * @param {string} file  The source the run is for, as named in the error.
 * @returns {string} what nf-metro wrote to stderr (warnings) on success
 */
function runNfMetro(args, file) {
  const { status, stderr, error } = spawnSync("nf-metro", args, {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (error || status !== 0) {
    throw new Error(
      `nf-metro ${args[0]} failed for ${file}:\n${(stderr ?? "").trim()}\n\n` +
        `Make sure 'nf-metro' is on PATH (activate the nf-core micromamba env).`,
    );
  }
  return stderr ?? "";
}

/** Output formats `renderMetroBinary` can produce. */
export const BINARY_FORMATS = /** @type {const} */ (["png", "gif", "webp", "mp4", "webm"]);

/**
 * Render a committed `.mmd` file to a raster or looping-video format and
 * publish it as a plain static file under `website/public/_generated/metro/`
 * (git-ignored; present by the time a production build copies `public/` into
 * its output). Content-addressed like `renderMetroFile`'s SVG cache, so a
 * rebuild only re-invokes the CLI for a (source, options) pair it hasn't
 * produced before.
 * @param {string} file  Absolute path to the source `.mmd`.
 * @param {{
 *   format: "png"|"gif"|"webp"|"mp4"|"webm",
 *   debug?: boolean,
 *   inputFormat?: keyof typeof INPUT_FORMATS | null,
 *   animate?: boolean,
 *   duration?: number,
 *   fps?: number,
 *   scale?: number,
 *   rasterWidth?: number,
 * }} opts
 * @returns {{ path: string, bytes: number }} `path` is relative to the site
 *   root (no leading `base` - callers prefix it, e.g. with `site.ts`'s `base`).
 */
export function renderMetroBinary(file, opts) {
  const {
    format,
    debug = false,
    inputFormat = null,
    animate = false,
    duration,
    fps,
    scale,
    rasterWidth,
  } = opts;
  const source = readFileSync(file, "utf-8");
  const converter = inputFormat ? INPUT_FORMATS[inputFormat] : null;
  const key = [
    NF_METRO_VERSION,
    format,
    debug ? "d" : "",
    converter?.letter ?? "",
    animate ? "a" : "",
    duration ?? "",
    fps ?? "",
    scale ?? "",
    rasterWidth ?? "",
    source,
  ].join(" ");
  const hash = createHash("sha256").update(key).digest("hex").slice(0, 16);
  const fileName = `${hash}.${format}`;
  const outPath = join(GENERATED_DIR, fileName);

  if (!existsSync(outPath)) {
    // Render the real file rather than a temp copy: the CLI resolves a map's
    // asset paths against the directory the file itself sits in.
    const args = [
      "render",
      file,
      "-o",
      outPath,
      "--format",
      format,
      "--no-self-color-scheme",
      "--no-manifest",
    ];
    if (debug) args.push("--debug");
    if (converter) args.push("--from", inputFormat);
    if (animate) args.push("--animate");
    if (duration != null) args.push("--duration", String(duration));
    if (fps != null) args.push("--fps", String(fps));
    if (scale != null) args.push("--scale", String(scale));
    if (rasterWidth != null) args.push("--raster-width", String(rasterWidth));

    runNfMetro(args, `${file} (format=${format})`);
  }

  return { path: `_generated/metro/${fileName}`, bytes: statSync(outPath).size };
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
      const params = new URLSearchParams(query);
      if (!params.has("metro")) return;
      const inputFormat = Object.keys(INPUT_FORMATS).find((f) => params.has(f)) ?? null;
      if (!file.endsWith(inputFormat ? INPUT_FORMATS[inputFormat].ext : ".mmd")) return;
      let svg;
      try {
        svg = renderMetroFile(file, {
          debug: params.has("debug"),
          animate: params.has("animate"),
          inputFormat,
          lines: params.get("lines"),
        });
      } catch (err) {
        // The build loads every file a glob matches, embedded or not, so a
        // map that cannot take its lines mode fails only when a page imports it.
        if (!err.onImport) throw err;
        return { code: `throw new Error(${JSON.stringify(err.message)});`, map: null };
      }
      return { code: `export default ${JSON.stringify(svg)};`, map: null };
    },
  };
}

/**
 * Astro integration copying GENERATED_DIR into the build output at
 * astro:build:done. Astro's own public/ -> dist/ copy runs before page
 * rendering, so files renderMetroBinary writes during that render (this is
 * the only time it's called) never reach dist/ on their own.
 * @returns {import('astro').AstroIntegration}
 */
export function metroGeneratedAssetsIntegration() {
  return {
    name: "nf-metro-copy-generated",
    hooks: {
      "astro:build:done": ({ dir }) => {
        if (!existsSync(GENERATED_DIR)) return;
        const dest = fileURLToPath(new URL("_generated/metro/", dir));
        mkdirSync(dest, { recursive: true });
        cpSync(GENERATED_DIR, dest, { recursive: true });
      },
    },
  };
}
