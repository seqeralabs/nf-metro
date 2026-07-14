"use strict";

// Pinned so the install path is reproducible; bump deliberately.
const PYODIDE_VERSION = "v0.27.2";

const REPO = "seqeralabs/nf-metro";

const SEED = `%%metro title: Example Pipeline
%%metro line: qc | QC | #2dd4bf
%%metro line: main | Main | #c792ea

graph LR
    subgraph input [Input]
        reads[Reads]
    end
    subgraph proc [Processing]
        fastqc[FastQC]
        trim[Trim]
        align[Align]
        fastqc -->|qc,main| trim
        trim -->|main| align
    end
    subgraph results [Results]
        bam[BAM]
    end
    reads -->|qc,main| fastqc
    align -->|main| bam
`;

// Seeded into the import dialog so the expected `-with-dag` format is visible
// and Convert works out of the box; users paste over it with their own DAG.
const SAMPLE_NEXTFLOW_DAG = `flowchart TB
    subgraph " "
    v0["Channel.fromPath"]
    end
    subgraph "PIPELINE [PIPELINE]"
    v1(["FASTQC"])
    v2(["TRIM"])
    v3(["ALIGN"])
    v4(["MULTIQC"])
    end
    v0 --> v1
    v1 --> v2
    v2 --> v3
    v1 --> v4
    v3 --> v4
`;

// Glue defined inside the Pyodide runtime: returns a JSON envelope so a render
// error surfaces as data rather than a thrown PythonError to unwind in JS.
//
// Layout cache: parse+compute_layout is expensive (~200-500ms). We cache the
// settled MetroGraph keyed on (normalised mmd + geometry options). Brand, mode,
// debug, animate, and directional are render-only - they don't affect station
// coordinates, so changing them skips layout and only re-runs render_svg.
// The style: and mode: directives are stripped before hashing so toggling brand
// or render mode doesn't bust the geometry cache.
//
// permissive is always forced on (see currentOptions()): a guard failure
// downgrades to a PermissiveGuardWarning and the render proceeds best-effort,
// instead of the whole editor going blank on one bad topology. Warnings are
// collected per-call (never accumulated onto the cached graph) and returned
// alongside the svg so the UI can show both.
const PY_GLUE = `
import hashlib
import json
import re as _re
import warnings
from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.convert import convert_nextflow_dag
from nf_metro.parser.model import PermissiveGuardWarning, split_guard_warnings
from nf_metro.render import render_svg

_cached_graph = None
_cached_key = None
_RENDER_ONLY = frozenset({"animate", "directional"})
_STYLE_RE = _re.compile(r"^\\s*%%metro\\s+(?:style|mode):.*$", _re.MULTILINE)

def nfm_render(mmd, opts_json):
    global _cached_graph, _cached_key
    opts = json.loads(opts_json)
    all_layout = {k: v for k, v in (opts.get("layout_options") or {}).items() if v is not None}

    layout_geom = {k: v for k, v in all_layout.items() if k not in _RENDER_ONLY}
    render_only = {k: v for k, v in all_layout.items() if k in _RENDER_ONLY}

    mmd_norm = _STYLE_RE.sub("", mmd).strip()
    cache_key = hashlib.md5((mmd_norm + "\\x00" + json.dumps(layout_geom, sort_keys=True)).encode()).hexdigest()

    svg = None
    error = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.filterwarnings("always", category=PermissiveGuardWarning)
        try:
            if cache_key != _cached_key:
                graph = prepare_graph(mmd, layout_options=layout_geom)
                _cached_graph = graph
                _cached_key = cache_key
            else:
                graph = _cached_graph

            for k, v in render_only.items():
                setattr(graph, k, bool(v))

            theme_obj = resolve_theme(opts.get("theme") or None, graph, mode=opts.get("mode") or None)
            svg = render_svg(
                graph,
                theme_obj,
                debug=bool(opts.get("debug")),
                responsive=True,
                font_portability="embed",
                self_color_scheme=False,
            )
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

    guard_warnings = [str(w.message) for w in split_guard_warnings(caught)[0]]
    if svg is not None:
        return json.dumps({"ok": True, "svg": svg, "warnings": guard_warnings})
    return json.dumps({"ok": False, "error": error, "warnings": guard_warnings})

def nfm_convert(nextflow_dag):
    try:
        return json.dumps({"ok": True, "mmd": convert_nextflow_dag(nextflow_dag)})
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})
`;

const el = (id) => document.getElementById(id);
let editor = null;
let pyRender = null;
let pyConvert = null;
let lastSvg = "";
let nfMetroVersion = "";
let buildSha = "";
const examples = {};

/* ------------------------------- editor -------------------------------- */

function defineMode() {
  CodeMirror.defineSimpleMode("metro", {
    start: [
      { regex: /%%metro\b.*/, token: "metro-directive" },
      { regex: /%%.*/, token: "comment" },
      { regex: /\b(graph|subgraph|end|LR|RL|TB|BT)\b/, token: "metro-keyword" },
      { regex: /\|[^|]*\|/, token: "metro-line" },
      { regex: /(--+>|<--+|-\.->|==+>)/, token: "metro-arrow" },
    ],
  });
}

function initEditor() {
  defineMode();
  editor = CodeMirror.fromTextArea(el("editor"), {
    mode: "metro",
    lineNumbers: true,
    lineWrapping: false,
    theme: "default",
  });
  editor.setValue(loadFromHash() || SEED);
  loadFromHashGz().then((src) => {
    if (src != null) editor.setValue(src);
  });
  editor.on("change", debounce(doRender, 300));

  // CodeMirror measures gutter and line geometry once at creation and never
  // re-measures when its container resizes; refresh() re-runs that measurement.
  if (window.ResizeObserver) {
    const refresh = debounce(() => editor.refresh(), 100);
    new ResizeObserver(refresh).observe(el("editor-pane"));
  }

  // Test hook: drive the editor and renderer from automated trials.
  window.__nfMetro = {
    getValue: () => editor.getValue(),
    setValue: (v) => editor.setValue(v),
    render: doRender,
    setMode,
    getMode: () => editMode,
    select: setSelection,
    getSelection: () => selection,
    addStationToSection,
    addSection,
    connect,
    renameStation,
    renameSection,
    reassignEdgeLine,
    setSectionGrid,
    deleteStation,
    deleteEdge,
    deleteSection,
    splitEdge,
    parseEdges,
  };
}

/* --------------------------------- boot -------------------------------- */

function setBootMsg(msg) {
  el("boot-msg").textContent = msg;
}

async function resolveWheel() {
  // Prefer a dev wheel shipped alongside the page (built from the current
  // source); fall back to the released package on PyPI.
  try {
    const resp = await fetch("wheels/index.json", { cache: "no-store" });
    if (resp.ok) {
      const { wheel } = await resp.json();
      if (wheel) return new URL("wheels/" + wheel, location.href).href;
    }
  } catch (_) {
    /* no dev wheel; use PyPI */
  }
  return "nf-metro";
}

async function boot() {
  try {
    setBootMsg("Starting Python runtime…");
    const pyodide = await loadPyodide({
      indexURL: `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`,
    });
    setBootMsg("Installing nf-metro…");
    await pyodide.loadPackage("micropip");
    const micropip = pyodide.pyimport("micropip");
    await micropip.install(await resolveWheel());
    pyodide.runPython(PY_GLUE);
    pyRender = pyodide.globals.get("nfm_render");
    pyConvert = pyodide.globals.get("nfm_convert");
    nfMetroVersion = pyodide.runPython("import nf_metro; nf_metro.__version__");
    el("boot").classList.add("hidden");
    doRender();
    // Readiness means the runtime is up, independent of whether the first
    // render happened to succeed.
    window.__nfMetroReady = true;
  } catch (err) {
    setBootMsg("Failed to start: " + err);
    el("boot").querySelector(".spinner").classList.add("hidden");
  }
}

/* -------------------------------- render ------------------------------- */

function currentOptions() {
  // Layout/style directives live in the source (parsed on render); only the
  // preview-overlay toggles are passed as render overrides here.
  return {
    theme: themeKeyFromSource(),
    mode: modeFromSource(),
    debug: el("opt-debug").checked,
    layout_options: {
      animate: el("opt-animate").checked,
      directional: el("opt-directional").checked,
      // Always on: a guard failure on a novel/edge-case topology should
      // still produce a render (with a visible warning) instead of leaving
      // the preview blank.
      permissive: true,
    },
  };
}

function showError(msg) {
  const box = el("error");
  if (!msg) {
    box.classList.add("hidden");
    box.textContent = "";
  } else {
    box.textContent = msg;
    box.classList.remove("hidden");
  }
}

function doRender() {
  if (!pyRender) return;
  // Snapshot inputs before the setTimeout so we render the state at the moment
  // doRender fired, not what the editor contains when the callback runs.
  const mmd = editor.getValue();
  const optsJson = JSON.stringify(currentOptions());
  // Mark the preview as rendering and yield one paint cycle so the browser can
  // show the dimmed state before the synchronous Python call blocks the thread.
  el("preview").classList.add("rendering");
  setTimeout(() => {
    let res;
    try {
      res = JSON.parse(pyRender(mmd, optsJson));
    } catch (err) {
      el("preview").classList.remove("rendering");
      showError("Render runtime error: " + err);
      return;
    }
    el("preview").classList.remove("rendering");
    // permissive mode means a guard failure can still hand back an svg (of the
    // defective geometry) alongside the warning(s) it downgraded, so the two
    // are reported independently rather than one replacing the other.
    if (res.svg) {
      lastSvg = res.svg;
      el("preview").innerHTML = res.svg;
      applyZoom();
    }
    const warnings =
      res.warnings && res.warnings.length ? res.warnings.join("\n\n") : "";
    if (!res.ok) {
      // No svg at all (or a fatal error on top of any downgraded guards):
      // keep the last good render visible and report everything we know.
      const parts = warnings ? [warnings, res.error] : [res.error];
      showError(friendlyRenderError(parts.join("\n\n")));
    } else if (warnings) {
      showError(friendlyRenderError(warnings));
    } else {
      showError(null);
    }
    refreshLineColors();
    syncDirectiveControls();
    reapplySelection();
  }, 0);
}

/* --------------------------------- zoom -------------------------------- */

// null = fit-to-view (responsive); a number is a scale factor on the SVG's
// intrinsic (viewBox) size. Re-applied after every render since the <svg> is
// replaced.
let zoomFactor = null;
const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.1;
const ZOOM_MAX = 8;

function viewBoxWidth(svg) {
  const m = (svg.getAttribute("viewBox") || "").match(
    /[-\d.]+ [-\d.]+ ([-\d.]+) [-\d.]+/,
  );
  return m ? parseFloat(m[1]) : svg.getBoundingClientRect().width;
}

function applyZoom() {
  const svg = el("preview").querySelector("svg");
  if (!svg) return;
  el("preview").classList.toggle("zoomed", zoomFactor !== null);
  if (zoomFactor === null) {
    svg.style.maxWidth = "100%";
    svg.style.width = "";
    svg.style.height = "";
  } else {
    svg.style.maxWidth = "none";
    svg.style.width = viewBoxWidth(svg) * zoomFactor + "px";
    svg.style.height = "auto";
  }
}

function currentScale() {
  const svg = el("preview").querySelector("svg");
  if (!svg) return 1;
  return svg.getBoundingClientRect().width / viewBoxWidth(svg);
}

function zoomBy(step) {
  // Starting from fit, continue smoothly from the currently displayed scale.
  const base = zoomFactor === null ? currentScale() : zoomFactor;
  zoomFactor = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, base * step));
  applyZoom();
}

function zoomFit() {
  zoomFactor = null;
  applyZoom();
}

/* ------------------------- directive controls ------------------------- */

// Controls that change the map's layout or styling are source-of-truth: each
// writes a `%%metro <key>:` directive into the editor and is synced back from
// it, so the change is saved with the map and travels with export/share. (The
// animate/chevrons/debug toggles are preview overlays handled separately.)

// The selector picks the brand (palette identity); light/dark is the separate
// Mode control, so brand and mode stay independent. Legacy sources may still
// carry `%%metro style: dark`, which reads back as the nfcore brand.
const THEME_KEYS = ["nfcore", "seqera"];
const STYLE_ALIASES = { dark: "nfcore" };

// [control id, directive key, kind]
const DIRECTIVE_CONTROLS = [
  ["opt-line-spread", "line_spread", "choice"],
  ["opt-diamond-style", "diamond_style", "choice"],
  ["opt-line-order", "line_order", "choice"],
  ["opt-center-ports", "center_ports", "bool"],
  ["opt-compact-offsets", "compact_offsets", "bool"],
  ["opt-track-gap", "track_gap", "number"],
  ["opt-font-scale", "font_scale", "number"],
  ["opt-fold-threshold", "fold_threshold", "number"],
  ["opt-x-spacing", "x_spacing", "number"],
  ["opt-y-spacing", "y_spacing", "number"],
];

function readDirective(key) {
  const m = editor
    .getValue()
    .match(new RegExp(`^\\s*%%metro\\s+${key}:\\s*(.+?)\\s*$`, "m"));
  return m ? m[1] : null;
}

// value === null removes the directive line; otherwise it is set (inserted
// after a %%metro title: line if present, else at the top - directives must
// precede the graph block).
function setDirective(key, value) {
  const lines = editor.getValue().split("\n");
  const idx = lines.findIndex((l) =>
    new RegExp(`^\\s*%%metro\\s+${key}:`).test(l),
  );
  if (value === null) {
    if (idx >= 0)
      editor.replaceRange("", { line: idx, ch: 0 }, { line: idx + 1, ch: 0 });
  } else if (idx >= 0) {
    const updated = lines[idx].replace(
      new RegExp(`(%%metro\\s+${key}:\\s*).*`),
      `$1${value}`,
    );
    editor.replaceRange(
      updated,
      { line: idx, ch: 0 },
      { line: idx, ch: lines[idx].length },
    );
  } else {
    const titleIdx = lines.findIndex((l) => /^\s*%%metro\s+title:/.test(l));
    const at = titleIdx >= 0 ? titleIdx + 1 : 0;
    editor.replaceRange(`%%metro ${key}: ${value}\n`, { line: at, ch: 0 });
  }
}

function applyDirectiveControl(id, key, kind) {
  const control = el(id);
  let value;
  if (kind === "bool") {
    value = control.checked ? "true" : null;
  } else {
    const raw = control.value.trim();
    value = raw === "" ? null : raw;
  }
  setDirective(key, value);
  doRender();
}

function themeKeyFromSource() {
  const value = (readDirective("style") || "").toLowerCase();
  const key = STYLE_ALIASES[value] || value;
  return THEME_KEYS.includes(key) ? key : "nfcore";
}

function setThemeDirective(themeKey) {
  setDirective("style", themeKey);
  doRender();
}

// Render mode (light/dark) is a property of the produced map, distinct from the
// playground UI theme. It defaults to the UI theme but is set independently via
// the Mode control, persisted as %%metro mode: so it travels with the map.
function modeFromSource() {
  const value = (readDirective("mode") || "").toLowerCase();
  if (value === "light" || value === "dark") return value;
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

// The preview SVG inherits the preview container's color-scheme (it carries no
// scheme of its own), so its light-dark() chrome shows the chosen render mode
// regardless of the surrounding UI theme.
function applyPreviewMode(mode) {
  el("preview").style.colorScheme = mode;
}

function setModeDirective(mode) {
  setDirective("mode", mode);
  applyPreviewMode(mode);
  doRender();
}

const _TRUE = new Set(["true", "yes", "1"]);

function syncDirectiveControls() {
  el("opt-theme").value = themeKeyFromSource();
  const mode = modeFromSource();
  el("opt-mode").value = mode;
  applyPreviewMode(mode);
  for (const [id, key, kind] of DIRECTIVE_CONTROLS) {
    const value = readDirective(key);
    if (kind === "bool")
      el(id).checked = _TRUE.has((value || "").toLowerCase());
    else el(id).value = value ?? "";
  }
}

/* ----------------------------- line colors ---------------------------- */

const LINE_RE =
  /^\s*%%metro\s+line:\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(#[0-9a-fA-F]{3,8})/;

function expandHex(hex) {
  if (/^#[0-9a-fA-F]{3}$/.test(hex)) {
    return "#" + hex.slice(1).replace(/./g, (c) => c + c);
  }
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? hex : null;
}

function parseLineDefs() {
  const defs = [];
  const doc = editor.getValue().split("\n");
  doc.forEach((line, n) => {
    const m = line.match(LINE_RE);
    if (m) defs.push({ line: n, id: m[1], name: m[2], color: m[3] });
  });
  return defs;
}

// Rebuild the swatch row only when the set of lines changes, not when a colour
// changes: tearing down a live <input type="color"> would freeze the picker
// the user is dragging in.
let lineColorSignature = "";

function refreshLineColors() {
  const defs = parseLineDefs();
  const signature = defs.map((d) => `${d.line}:${d.id}`).join("|");
  if (signature === lineColorSignature) return;
  lineColorSignature = signature;

  const box = el("line-colors");
  box.textContent = "";
  defs.forEach((def) => {
    const value = expandHex(def.color);
    if (!value) return;
    const label = document.createElement("label");
    label.className = "swatch";
    label.title = `${def.name} (${def.color})`;
    const input = document.createElement("input");
    input.type = "color";
    input.value = value;
    input.addEventListener("input", () => setLineColor(def.line, input.value));
    const span = document.createElement("span");
    span.textContent = def.id;
    label.append(input, span);
    box.append(label);
  });
}

function setLineColor(lineNo, hex) {
  const text = editor.getLine(lineNo);
  if (text === undefined) return;
  const updated = text.replace(/#[0-9a-fA-F]{3,8}/, hex);
  editor.replaceRange(
    updated,
    { line: lineNo, ch: 0 },
    { line: lineNo, ch: text.length },
  );
  doRender();
}

/* ------------------------------- snippets ------------------------------ */

const SNIPPETS = {
  "btn-section":
    "    subgraph new_section [New Section]\n" +
    "        node1[Node 1]\n" +
    "    end\n",
  "btn-line": "%%metro line: new_line | New Line | #ff7f50\n",
  "btn-edge": "    node_a -->|line_id| node_b\n",
};

function insertSnippet(id) {
  const text = SNIPPETS[id];
  editor.replaceSelection(text);
  editor.focus();
  doRender();
}

/* --------------------------- graphical editing ------------------------- */

// The .mmd text stays the single source of truth: every graphical action is
// translated into a surgical text edit and the map is re-rendered. Selection is
// re-derived by id after each render because the SVG innerHTML is replaced
// wholesale, so element references never survive a render.

let editMode = "select"; // "select" | "add-station" | "add-edge"
let selection = null; // { kind: "station" | "section" | "line", id }
let pendingSource = null; // source station id chosen in connect mode

const ID_PART = "[A-Za-z0-9_]+";
const ARROW = "(?:--+>|--+|==+>|-\\.->)";
const SHAPE =
  "(?:\\[\\[[^\\]]*\\]\\]|\\(\\([^)]*\\)\\)|\\(\\[[^\\]]*\\]\\)|\\[[^\\]]*\\]|\\([^)]*\\)|\\{[^}]*\\})";
const EDGE_RE = new RegExp(
  "^(\\s*)(" +
    ID_PART +
    ")\\s*" +
    SHAPE +
    "?\\s*" +
    ARROW +
    "\\s*\\|([^|]*)\\|\\s*(" +
    ID_PART +
    ")",
);
const DECL_RE = new RegExp("^\\s*(" + ID_PART + ")\\s*" + SHAPE + "?\\s*$");
const HAS_ARROW = /--+>|--+|==+>|-\.->/;

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function cssEsc(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s;
}

/* ----------------------------- text parsing --------------------------- */

function docLines() {
  return editor.getValue().split("\n");
}

function parseEdges() {
  const out = [];
  docLines().forEach((line, n) => {
    const m = line.match(EDGE_RE);
    if (!m) return;
    out.push({
      lineNo: n,
      src: m[2],
      tgt: m[4],
      lines: m[3]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    });
  });
  return out;
}

// Depth-aware subgraph blocks: [{ id, name, start, end }] (end = the `end` line).
function sectionBlocks() {
  const lines = docLines();
  const stack = [];
  const blocks = [];
  lines.forEach((line, n) => {
    const open = line.match(
      /^\s*subgraph\s+("[^"]*"|[A-Za-z0-9_]+)\s*(?:\[(.*)\])?/,
    );
    if (open) {
      const id = open[1].startsWith('"') ? open[1].slice(1, -1) : open[1];
      stack.push({ id, name: open[2] != null ? open[2] : id, start: n });
      return;
    }
    if (/^\s*end\s*$/.test(line) && stack.length) {
      const b = stack.pop();
      b.end = n;
      blocks.push(b);
    }
  });
  return blocks;
}

function findSectionBlock(id) {
  return sectionBlocks().find((b) => b.id === id) || null;
}

function findStationDecl(id) {
  const lines = docLines();
  for (let n = 0; n < lines.length; n++) {
    if (HAS_ARROW.test(lines[n])) continue;
    const m = lines[n].match(DECL_RE);
    if (m && m[1] === id) return n;
  }
  return -1;
}

function stationLabel(id) {
  const n = findStationDecl(id);
  if (n < 0) return id;
  const m = editor
    .getLine(n)
    .match(/^\s*[A-Za-z0-9_]+\s*[[({]+(.*?)[\])}]+\s*$/);
  return m ? m[1].trim() : id;
}

function existingIds() {
  const ids = new Set();
  sectionBlocks().forEach((b) => ids.add(b.id));
  parseEdges().forEach((e) => {
    ids.add(e.src);
    ids.add(e.tgt);
  });
  parseLineDefs().forEach((d) => ids.add(d.id));
  docLines().forEach((line) => {
    if (HAS_ARROW.test(line)) return;
    const m = line.match(DECL_RE);
    if (m) ids.add(m[1]);
  });
  return ids;
}

function uniqueId(prefix) {
  const ids = existingIds();
  let i = 1;
  while (ids.has(prefix + i)) i++;
  return prefix + i;
}

function findEdgeLineNo(src, tgt, line) {
  const e = parseEdges().find(
    (x) => x.src === src && x.tgt === tgt && x.lines.includes(line),
  );
  return e ? e.lineNo : -1;
}

// The drawn manifest exposes every station's centre; routes do not carry their
// edge identity, so a clicked segment is mapped back to an edge by matching its
// own endpoints to stations (works whenever both ends sit on a station, i.e.
// every in-section edge). Inter-section legs end on invisible ports and resolve
// to null, falling back to selecting the whole line.
let _manifestText = "";
let _manifestValue = null;
function currentManifest() {
  const node = el("preview").querySelector("#diagram-manifest");
  const text = node ? node.textContent || "" : "";
  if (text !== _manifestText) {
    _manifestText = text;
    try {
      _manifestValue = JSON.parse(text);
    } catch (_) {
      _manifestValue = null;
    }
  }
  return _manifestValue;
}

function elementEndpoints(elm) {
  if (elm.tagName.toLowerCase() === "line") {
    return [
      [+elm.getAttribute("x1"), +elm.getAttribute("y1")],
      [+elm.getAttribute("x2"), +elm.getAttribute("y2")],
    ];
  }
  const nums = (elm.getAttribute("d") || "").match(/-?\d+(?:\.\d+)?/g);
  if (!nums || nums.length < 4) return null;
  return [nums.slice(0, 2).map(Number), nums.slice(-2).map(Number)];
}

function nearestStation(pt, tol) {
  const manifest = currentManifest();
  if (!manifest || !manifest.nodes) return null;
  let best = null;
  let bestSq = tol * tol;
  for (const node of manifest.nodes) {
    const dx = node.x - pt[0];
    const dy = node.y - pt[1];
    const sq = dx * dx + dy * dy;
    if (sq <= bestSq) {
      bestSq = sq;
      best = node.id;
    }
  }
  return best;
}

function resolveEdge(elm, lineId) {
  const ends = elementEndpoints(elm);
  if (!ends) return null;
  const a = nearestStation(ends[0], 18);
  const b = nearestStation(ends[1], 18);
  if (!a || !b || a === b) return null;
  const edge = parseEdges().find(
    (e) =>
      e.lines.includes(lineId) &&
      ((e.src === a && e.tgt === b) || (e.src === b && e.tgt === a)),
  );
  return edge ? { src: edge.src, tgt: edge.tgt, line: lineId } : null;
}

// Which section block (if any) references a node id, used to decide whether a
// new edge lives inside a section or in the inter-section block.
function sectionOf(id) {
  const lines = docLines();
  const re = new RegExp("\\b" + escapeRe(id) + "\\b");
  return (
    sectionBlocks().find((b) => {
      for (let n = b.start; n <= b.end; n++) if (re.test(lines[n])) return true;
      return false;
    }) || null
  );
}

/* ---------------------------- text mutations -------------------------- */

function replaceLine(n, text) {
  editor.replaceRange(
    text,
    { line: n, ch: 0 },
    { line: n, ch: editor.getLine(n).length },
  );
}

function insertLineAt(n, text) {
  editor.replaceRange(text + "\n", { line: n, ch: 0 });
}

function removeLines(indices) {
  [...new Set(indices)]
    .sort((a, b) => b - a)
    .forEach((n) =>
      editor.replaceRange("", { line: n, ch: 0 }, { line: n + 1, ch: 0 }),
    );
}

function addStationToSection(sectionId, label) {
  const block = findSectionBlock(sectionId);
  if (!block) return null;
  const id = uniqueId("node");
  insertLineAt(block.end, "        " + id + "[" + (label || "New node") + "]");
  doRender();
  setSelection({ kind: "station", id });
  return id;
}

function addSection(name) {
  const id = uniqueId("section");
  const nodeId = uniqueId("node");
  const lines = docLines();
  const blocks = sectionBlocks();
  let at;
  if (blocks.length) {
    at = Math.max(...blocks.map((b) => b.end)) + 1;
  } else {
    const g = lines.findIndex((l) => /^\s*graph\b/.test(l));
    at = g >= 0 ? g + 1 : lines.length;
  }
  editor.replaceRange(
    "    subgraph " +
      id +
      " [" +
      (name || "New Section") +
      "]\n" +
      "        " +
      nodeId +
      "[New node]\n" +
      "    end\n",
    { line: at, ch: 0 },
  );
  doRender();
  setSelection({ kind: "section", id });
  return id;
}

function connect(src, tgt, line) {
  if (!src || !tgt || !line || src === tgt) return false;
  const bs = sectionOf(src);
  const bt = sectionOf(tgt);
  if (bs && bt && bs.id === bt.id) {
    insertLineAt(bs.end, "        " + src + " -->|" + line + "| " + tgt);
  } else {
    const last = editor.lineCount() - 1;
    const tail = editor.getLine(last);
    const lead = tail.trim() === "" ? "" : "\n";
    editor.replaceRange(
      lead + "    " + src + " -->|" + line + "| " + tgt + "\n",
      {
        line: last,
        ch: tail.length,
      },
    );
  }
  doRender();
  return true;
}

function reassignEdgeLine(lineNo, newLine) {
  const text = editor.getLine(lineNo);
  if (text == null) return;
  replaceLine(lineNo, text.replace(/\|([^|]*)\|/, "|" + newLine + "|"));
  doRender();
}

function renameStation(id, label) {
  const n = findStationDecl(id);
  if (n < 0) return false;
  const line = editor.getLine(n);
  const shaped = line.match(
    /^(\s*[A-Za-z0-9_]+\s*)([[({]+)(.*?)([\])}]+)(\s*)$/,
  );
  replaceLine(
    n,
    shaped
      ? shaped[1] + shaped[2] + label + shaped[4] + shaped[5]
      : line.replace(/^(\s*[A-Za-z0-9_]+)\s*$/, "$1[" + label + "]"),
  );
  doRender();
  return true;
}

function renameSection(id, name) {
  const b = findSectionBlock(id);
  if (!b) return false;
  const line = editor.getLine(b.start);
  replaceLine(
    b.start,
    /\[.*\]\s*$/.test(line)
      ? line.replace(/\[.*\]\s*$/, "[" + name + "]")
      : line.replace(
          /(subgraph\s+(?:"[^"]*"|[A-Za-z0-9_]+))\s*$/,
          "$1 [" + name + "]",
        ),
  );
  doRender();
  return true;
}

// col === null removes the grid directive (back to auto placement).
function setSectionGrid(id, col, row) {
  const lines = docLines();
  const idx = lines.findIndex((l) =>
    new RegExp("^\\s*%%metro\\s+grid:\\s*" + escapeRe(id) + "\\s*\\|").test(l),
  );
  if (col === null) {
    if (idx >= 0) removeLines([idx]);
  } else if (idx >= 0) {
    replaceLine(idx, "%%metro grid: " + id + " | " + col + "," + row);
  } else {
    const directives = lines
      .map((l, i) => (/^\s*%%metro\b/.test(l) ? i : -1))
      .filter((i) => i >= 0);
    const at = directives.length ? Math.max(...directives) + 1 : 0;
    insertLineAt(at, "%%metro grid: " + id + " | " + col + "," + row);
  }
  doRender();
}

function deleteStation(id) {
  const remove = new Set();
  const decl = findStationDecl(id);
  if (decl >= 0) remove.add(decl);
  parseEdges().forEach((e) => {
    if (e.src === id || e.tgt === id) remove.add(e.lineNo);
  });
  removeLines([...remove]);
  doRender();
  clearSelection();
}

function deleteEdge(lineNo) {
  removeLines([lineNo]);
  doRender();
}

function deleteSection(id) {
  const b = findSectionBlock(id);
  if (!b) return;
  const lines = docLines();
  const remove = new Set();
  for (let n = b.start; n <= b.end; n++) remove.add(n);
  const inside = new Set();
  for (let n = b.start + 1; n < b.end; n++) {
    if (HAS_ARROW.test(lines[n])) continue;
    const m = lines[n].match(DECL_RE);
    if (m) inside.add(m[1]);
  }
  parseEdges().forEach((e) => {
    if (e.lineNo > b.start && e.lineNo < b.end) {
      inside.add(e.src);
      inside.add(e.tgt);
    }
  });
  parseEdges().forEach((e) => {
    if (
      (e.lineNo < b.start || e.lineNo > b.end) &&
      (inside.has(e.src) || inside.has(e.tgt))
    )
      remove.add(e.lineNo);
  });
  removeLines([...remove]);
  doRender();
  clearSelection();
}

// Splice a new station into an edge: src -->|L| tgt becomes src -->|L| new and
// new -->|L| tgt. When both ends share a section the new node is declared there
// (for a friendly label); otherwise the edge is inter-section and the node is
// left to its edge-implied placement.
function splitEdge(src, tgt, line) {
  const lineNo = findEdgeLineNo(src, tgt, line);
  if (lineNo < 0) return;
  const text = editor.getLine(lineNo);
  const indent = (text.match(/^\s*/) || [""])[0];
  // Reuse the edge's full line token so a multi-line edge keeps every line.
  const token = (text.match(/\|([^|]*)\|/) || [null, line])[1];
  const newId = uniqueId("node");
  const bs = sectionOf(src);
  const bt = sectionOf(tgt);
  replaceLine(lineNo, indent + src + " -->|" + token + "| " + newId);
  insertLineAt(lineNo + 1, indent + newId + " -->|" + token + "| " + tgt);
  if (bs && bt && bs.id === bt.id) {
    const block = findSectionBlock(bs.id);
    if (block) insertLineAt(block.start + 1, "        " + newId + "[New node]");
  }
  doRender();
  setSelection({ kind: "station", id: newId });
}

/* ------------------------------ selection ----------------------------- */

function selectorFor(sel) {
  if (!sel) return null;
  if (sel.kind === "station")
    return '[data-station-id="' + cssEsc(sel.id) + '"]';
  if (sel.kind === "section")
    return (
      'rect.nf-metro-section-box[data-section-id="' + cssEsc(sel.id) + '"]'
    );
  if (sel.kind === "line") return '[data-line-id="' + cssEsc(sel.id) + '"]';
  return null;
}

function setSelection(sel) {
  selection = sel;
  highlightSelection();
  renderPropPanel();
  if (sel && sel.kind === "station") jumpToStation(sel.id);
}

function clearSelection() {
  selection = null;
  pendingSource = null;
  highlightSelection();
  renderPropPanel();
}

// Drop a selection whose element no longer exists in the freshly drawn SVG
// (after a delete, or loading a different map), so the panel never goes stale.
function reapplySelection() {
  if (selection) {
    if (selection.kind === "edge") {
      if (findEdgeLineNo(selection.src, selection.tgt, selection.line) < 0)
        selection = null;
    } else {
      const sel = selectorFor(selection);
      if (sel && !el("preview").querySelector(sel)) selection = null;
    }
  }
  highlightSelection();
  renderPropPanel();
}

function highlightSelection() {
  const preview = el("preview");
  preview
    .querySelectorAll(".nfm-sel, .nfm-edge-src")
    .forEach((n) => n.classList.remove("nfm-sel", "nfm-edge-src"));
  const sel = selectorFor(selection);
  if (sel)
    preview.querySelectorAll(sel).forEach((n) => n.classList.add("nfm-sel"));
  if (pendingSource) {
    preview
      .querySelectorAll('[data-station-id="' + cssEsc(pendingSource) + '"]')
      .forEach((n) => n.classList.add("nfm-edge-src"));
  }
}

function jumpToStation(id) {
  const n = findStationDecl(id);
  if (n < 0) return;
  editor.setCursor({ line: n, ch: editor.getLine(n).length });
  editor.scrollIntoView({ line: n, ch: 0 }, 60);
}

/* ---------------------------- mode + clicks --------------------------- */

function setMode(mode) {
  editMode = mode;
  pendingSource = null;
  closeLinePicker();
  document
    .querySelectorAll(".mode-btn")
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode)),
    );
  const preview = el("preview");
  preview.classList.toggle("mode-add-station", mode === "add-station");
  preview.classList.toggle("mode-add-edge", mode === "add-edge");
  highlightSelection();
  setEditHint(
    mode === "add-station"
      ? "Click a section to add a station."
      : mode === "add-edge"
        ? "Click a source station, then a target."
        : "Click an element to select it.",
  );
}

function setEditHint(text) {
  el("edit-hint").textContent = text;
}

function hitTest(target) {
  const station = target.closest("[data-station-id]");
  if (station)
    return { kind: "station", id: station.getAttribute("data-station-id") };
  const line = target.closest("[data-line-id]");
  if (line) return { kind: "line", id: line.getAttribute("data-line-id") };
  const section = target.closest("[data-section-id]");
  if (section)
    return { kind: "section", id: section.getAttribute("data-section-id") };
  return null;
}

function onPreviewClick(e) {
  const hit = hitTest(e.target);
  if (editMode === "add-station") {
    if (hit && hit.kind === "section") addStationToSection(hit.id);
    else toast("Click a section to add a station.");
    return;
  }
  if (editMode === "add-edge") {
    if (!hit || hit.kind !== "station") {
      toast("Click a station.");
      return;
    }
    if (!pendingSource) {
      pendingSource = hit.id;
      highlightSelection();
      setEditHint("Source: " + hit.id + ". Now click the target station.");
      return;
    }
    if (hit.id === pendingSource) {
      toast("Pick a different target station.");
      return;
    }
    openLinePicker(e, pendingSource, hit.id);
    return;
  }
  // Select mode: a station or section selects directly; a route resolves to a
  // specific edge when its endpoints sit on stations, else selects the line.
  const stationEl = e.target.closest("[data-station-id]");
  if (stationEl) {
    setSelection({
      kind: "station",
      id: stationEl.getAttribute("data-station-id"),
    });
    return;
  }
  const lineEl = e.target.closest("[data-line-id]");
  if (lineEl) {
    const lineId = lineEl.getAttribute("data-line-id");
    const edge = resolveEdge(lineEl, lineId);
    if (edge) {
      setSelection({
        kind: "edge",
        src: edge.src,
        tgt: edge.tgt,
        line: edge.line,
      });
      lineEl.classList.add("nfm-sel");
    } else {
      setSelection({ kind: "line", id: lineId });
    }
    return;
  }
  const sectionEl = e.target.closest("[data-section-id]");
  if (sectionEl) {
    setSelection({
      kind: "section",
      id: sectionEl.getAttribute("data-section-id"),
    });
    return;
  }
  clearSelection();
}

/* ----------------------------- line picker ---------------------------- */

function openLinePicker(e, src, tgt) {
  const defs = parseLineDefs();
  if (!defs.length) {
    toast("Define a line first with + Line.");
    return;
  }
  const picker = el("line-picker");
  picker.textContent = "";
  const title = document.createElement("div");
  title.className = "picker-title";
  title.textContent = "Connect " + src + " → " + tgt + " on:";
  picker.append(title);
  defs.forEach((d) => {
    const b = document.createElement("button");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = d.color;
    const name = document.createElement("span");
    name.textContent = d.id;
    b.append(dot, name);
    b.addEventListener("click", () => {
      connect(src, tgt, d.id);
      closeLinePicker();
      pendingSource = null;
      highlightSelection();
      setEditHint("Click a source station, then a target.");
    });
    picker.append(b);
  });
  const pane = el("preview-pane").getBoundingClientRect();
  picker.style.left = Math.min(e.clientX - pane.left, pane.width - 160) + "px";
  picker.style.top = Math.min(e.clientY - pane.top, pane.height - 90) + "px";
  picker.classList.remove("hidden");
}

function closeLinePicker() {
  el("line-picker").classList.add("hidden");
}

/* --------------------------- property panel --------------------------- */

function propRow(labelText, control) {
  const row = document.createElement("div");
  row.className = "prop-row";
  const label = document.createElement("label");
  label.textContent = labelText;
  row.append(label, control);
  return row;
}

function textControl(value, onCommit) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  const commit = () => onCommit(input.value);
  input.addEventListener("change", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      commit();
      input.blur();
    }
  });
  return input;
}

function deleteControl(text, onClick) {
  const button = document.createElement("button");
  button.className = "prop-delete";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function actionControl(text, onClick) {
  const button = document.createElement("button");
  button.className = "prop-action";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function idRow(text) {
  const row = document.createElement("div");
  row.className = "prop-row";
  const label = document.createElement("label");
  label.textContent = text;
  row.append(label);
  return row;
}

function renderPropPanel() {
  const panel = el("prop-panel");
  if (!selection) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  el("prop-kind").textContent = selection.kind;
  const body = el("prop-body");
  body.textContent = "";
  if (selection.kind === "station") renderStationProps(body, selection.id);
  else if (selection.kind === "section") renderSectionProps(body, selection.id);
  else if (selection.kind === "line") renderLineProps(body, selection.id);
  else if (selection.kind === "edge") renderEdgeProps(body, selection);
}

function renderEdgeProps(body, sel) {
  body.append(idRow(sel.src + " →|" + sel.line + "| " + sel.tgt));
  body.append(
    actionControl("Add station on this edge", () =>
      splitEdge(sel.src, sel.tgt, sel.line),
    ),
  );
  body.append(
    deleteControl("Delete edge", () => {
      const lineNo = findEdgeLineNo(sel.src, sel.tgt, sel.line);
      if (lineNo >= 0) deleteEdge(lineNo);
      clearSelection();
    }),
  );
}

function renderStationProps(body, id) {
  body.append(idRow("id: " + id));
  if (findStationDecl(id) >= 0) {
    body.append(
      propRow(
        "Label",
        textControl(stationLabel(id), (v) => renameStation(id, v.trim() || id)),
      ),
    );
  } else {
    const note = document.createElement("div");
    note.className = "prop-empty";
    note.textContent = "Declared inline in an edge; rename it in the text.";
    body.append(note);
  }
  body.append(deleteControl("Delete station", () => deleteStation(id)));
}

function renderSectionProps(body, id) {
  const block = findSectionBlock(id);
  body.append(idRow("id: " + id));
  if (block) {
    body.append(
      propRow(
        "Name",
        textControl((block.name || "").trim(), (v) =>
          renameSection(id, v.trim() || id),
        ),
      ),
    );
    const grid = currentGrid(id);
    const wrap = document.createElement("div");
    wrap.className = "prop-grid";
    const colInput = numControl(grid ? grid.col : "");
    const rowInput = numControl(grid ? grid.row : "");
    const commit = () => {
      const c = colInput.value.trim();
      const r = rowInput.value.trim();
      if (c === "" && r === "") setSectionGrid(id, null);
      else setSectionGrid(id, c === "" ? 0 : c, r === "" ? 0 : r);
    };
    colInput.addEventListener("change", commit);
    rowInput.addEventListener("change", commit);
    wrap.append(colInput, rowInput);
    body.append(propRow("Grid col, row (blank = auto)", wrap));
  }
  body.append(deleteControl("Delete section", () => deleteSection(id)));
}

function renderLineProps(body, id) {
  const defs = parseLineDefs();
  const def = defs.find((d) => d.id === id);
  body.append(idRow("line: " + id));
  if (def) {
    body.append(
      propRow(
        "Display name",
        textControl(def.name, (v) => renameLine(def.line, v.trim() || id)),
      ),
    );
    const color = document.createElement("input");
    color.type = "color";
    const hex = expandHex(def.color);
    if (hex) color.value = hex;
    color.addEventListener("input", () => setLineColor(def.line, color.value));
    body.append(propRow("Colour", color));
  }
  const heading = document.createElement("label");
  heading.className = "prop-row";
  heading.textContent = "Edges on this line";
  body.append(heading);
  const list = document.createElement("div");
  list.className = "prop-edges";
  const edges = parseEdges().filter((e) => e.lines.includes(id));
  if (!edges.length) {
    const empty = document.createElement("div");
    empty.className = "prop-empty";
    empty.textContent = "No edges on this line.";
    list.append(empty);
  }
  edges.forEach((e) => list.append(edgeRow(e, id, defs)));
  body.append(list);
}

function edgeRow(edge, lineId, defs) {
  const row = document.createElement("div");
  row.className = "prop-edge";
  const ends = document.createElement("span");
  ends.className = "ends";
  ends.textContent = edge.src + " → " + edge.tgt;
  ends.title = ends.textContent;
  row.append(ends);
  if (edge.lines.length === 1) {
    const select = document.createElement("select");
    defs.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = d.id;
      if (d.id === lineId) opt.selected = true;
      select.append(opt);
    });
    select.addEventListener("change", () =>
      reassignEdgeLine(edge.lineNo, select.value),
    );
    row.append(select);
  }
  const add = document.createElement("button");
  add.className = "add";
  add.textContent = "+";
  add.title = "Add a station on this edge";
  add.addEventListener("click", () => splitEdge(edge.src, edge.tgt, lineId));
  row.append(add);
  const del = document.createElement("button");
  del.className = "del";
  del.textContent = "×";
  del.title = "Delete edge";
  del.addEventListener("click", () => deleteEdge(edge.lineNo));
  row.append(del);
  return row;
}

function numControl(value) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  input.value = value;
  return input;
}

function currentGrid(id) {
  const m = editor
    .getValue()
    .match(
      new RegExp(
        "^\\s*%%metro\\s+grid:\\s*" +
          escapeRe(id) +
          "\\s*\\|\\s*(\\d+)\\s*,\\s*(\\d+)",
        "m",
      ),
    );
  return m ? { col: m[1], row: m[2] } : null;
}

function renameLine(lineNo, name) {
  const text = editor.getLine(lineNo);
  if (text == null) return;
  replaceLine(
    lineNo,
    text.replace(
      /^(\s*%%metro\s+line:\s*[^|]+\|\s*)([^|]+?)(\s*\|)/,
      "$1" + name + "$3",
    ),
  );
  doRender();
}

function wireEditTools() {
  document
    .querySelectorAll(".mode-btn")
    .forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  el("btn-add-section").addEventListener("click", () => {
    setMode("select");
    addSection();
  });
  el("preview").addEventListener("click", onPreviewClick);
  el("prop-close").addEventListener("click", clearSelection);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!el("line-picker").classList.contains("hidden")) {
      closeLinePicker();
      pendingSource = null;
      highlightSelection();
    } else if (editMode !== "select") {
      setMode("select");
    } else if (selection) {
      clearSelection();
    }
  });
  // A click outside the picker that is also outside the canvas dismisses it; a
  // click inside the canvas is the connect flow itself, so it is left alone.
  document.addEventListener("click", (e) => {
    const picker = el("line-picker");
    if (picker.classList.contains("hidden")) return;
    if (!picker.contains(e.target) && !e.target.closest("#preview"))
      closeLinePicker();
  });
}

/* -------------------------------- export ------------------------------- */

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// The preview SVG carries no color-scheme (it inherits the preview pane's), so a
// standalone export needs the chosen render mode baked onto the root for
// light-dark() to resolve to it wherever the file is opened or rasterized.
function pinColorScheme(svg, mode) {
  if (/<svg[^>]*\bcolor-scheme/.test(svg)) return svg;
  return svg.replace(/<svg\s/, `<svg style="color-scheme: ${mode}" `);
}

function exportSvg() {
  if (!lastSvg) return;
  const svg = pinColorScheme(lastSvg, modeFromSource());
  downloadBlob(new Blob([svg], { type: "image/svg+xml" }), "metro_map.svg");
}

function svgWithIntrinsicSize(svg) {
  // A responsive SVG carries only a viewBox; canvas rasterization needs an
  // intrinsic width/height, so derive them from the viewBox.
  const m = svg.match(/viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"/);
  const w = m ? parseFloat(m[3]) : 1200;
  const h = m ? parseFloat(m[4]) : 800;
  const sized = /<svg[^>]*\swidth=/.test(svg)
    ? svg
    : svg.replace(/<svg\s/, `<svg width="${w}" height="${h}" `);
  return { svg: sized, w, h };
}

async function exportPng() {
  if (!lastSvg) return;
  const scale = 2;
  const pinned = pinColorScheme(lastSvg, modeFromSource());
  const { svg, w, h } = svgWithIntrinsicSize(pinned);
  const url = URL.createObjectURL(
    new Blob([svg], { type: "image/svg+xml;charset=utf-8" }),
  );
  try {
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = resolve;
      img.onerror = () => reject(new Error("rasterization failed"));
      img.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(w * scale));
    canvas.height = Math.max(1, Math.round(h * scale));
    const ctx = canvas.getContext("2d");
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0);
    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/png"),
    );
    if (!blob) throw new Error("canvas produced no image");
    downloadBlob(blob, "metro_map.png");
  } catch (err) {
    toast("PNG export failed: " + err.message);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* ------------------------------- sharing ------------------------------- */

function _bytesToB64url(arr) {
  let bin = "";
  arr.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function _b64urlToBytes(b64) {
  const pad = b64.length % 4 ? "=".repeat(4 - (b64.length % 4)) : "";
  const bin = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + pad);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function b64urlEncode(str) {
  return _bytesToB64url(new TextEncoder().encode(str));
}

function b64urlDecode(b64) {
  return new TextDecoder().decode(_b64urlToBytes(b64));
}

async function b64urlEncodeGz(str) {
  const cs = new CompressionStream("gzip");
  const writer = cs.writable.getWriter();
  writer.write(new TextEncoder().encode(str));
  writer.close();
  return _bytesToB64url(
    new Uint8Array(await new Response(cs.readable).arrayBuffer()),
  );
}

async function b64urlDecodeGz(b64) {
  const ds = new DecompressionStream("gzip");
  const writer = ds.writable.getWriter();
  writer.write(_b64urlToBytes(b64));
  writer.close();
  return new TextDecoder().decode(
    await new Response(ds.readable).arrayBuffer(),
  );
}

function _hashParam(key) {
  const m = location.hash.match(new RegExp("[#&]" + key + "=([^&]+)"));
  return m ? decodeURIComponent(m[1]) : null;
}

function loadFromHash() {
  const raw = _hashParam("mmd");
  if (!raw) return null;
  try {
    return b64urlDecode(raw);
  } catch (_) {
    return null;
  }
}

async function loadFromHashGz() {
  const raw = _hashParam("mmd-gz");
  if (!raw) return null;
  try {
    return await b64urlDecodeGz(raw);
  } catch (_) {
    return null;
  }
}

function _pageUrl(hash) {
  return location.origin + location.pathname + location.search + hash;
}

function shareUrl() {
  return _pageUrl(
    "#mmd=" + encodeURIComponent(b64urlEncode(editor.getValue())),
  );
}

async function compressedShareUrl() {
  return _pageUrl(
    "#mmd-gz=" + encodeURIComponent(await b64urlEncodeGz(editor.getValue())),
  );
}

async function shareLink() {
  const url = await compressedShareUrl();
  history.replaceState(null, "", url);
  try {
    await navigator.clipboard.writeText(url);
    toast("Share link copied to clipboard");
  } catch (_) {
    toast("Share link is in the address bar");
  }
}

async function copySource() {
  try {
    await navigator.clipboard.writeText(editor.getValue());
    toast("Source copied to clipboard");
  } catch (_) {
    toast("Copy failed — select all in the editor and copy manually");
  }
}

/* ----------------------------- bug report ----------------------------- */

function buildIssueUrl(explanation) {
  const opts = currentOptions();
  const mmd = editor.getValue();
  const MAX = 6000;
  const mmdBlock =
    mmd.length > MAX
      ? mmd.slice(0, MAX) + "\n... (truncated; full map in the reproduce link)"
      : mmd;
  const lo = opts.layout_options;
  const body = `## What's wrong

${explanation}

## Map source

\`\`\`
${mmdBlock}
\`\`\`

## Reproduce

[Open this map in the playground](${shareUrl()})

## Environment

- nf-metro: ${nfMetroVersion || "unknown"}
- build: ${buildSha || "unknown"}
- theme: ${opts.theme}
- debug: ${opts.debug}
- animate: ${lo.animate}
- directional: ${lo.directional}
- page: ${location.href.split("#")[0]}
- user agent: ${navigator.userAgent}
`;
  const firstLine = explanation.trim().split("\n")[0].slice(0, 70);
  const params = new URLSearchParams({
    title: `[playground] ${firstLine}`,
    body,
    labels: "playground",
  });
  return `https://github.com/${REPO}/issues/new?${params.toString()}`;
}

function openReport() {
  el("report-text").value = "";
  el("report-submit").disabled = true;
  el("report-modal").classList.remove("hidden");
  el("report-text").focus();
}

function closeReport() {
  el("report-modal").classList.add("hidden");
}

function submitReport() {
  const explanation = el("report-text").value.trim();
  if (!explanation) {
    el("report-text").focus();
    return;
  }
  const url = buildIssueUrl(explanation);
  // Exposed so the e2e suite can assert the prefilled issue without
  // navigating to github.com.
  window.__nfMetroLastIssueUrl = url;
  window.open(url, "_blank", "noopener");
  closeReport();
}

/* -------------------------- nextflow import --------------------------- */

function openConvert() {
  el("convert-text").value = SAMPLE_NEXTFLOW_DAG;
  el("convert-error").classList.add("hidden");
  el("convert-modal").classList.remove("hidden");
  el("convert-text").focus();
}

function closeConvert() {
  el("convert-modal").classList.add("hidden");
}

function submitConvert() {
  const dag = el("convert-text").value;
  if (!dag.trim() || !pyConvert) return;
  let res;
  try {
    res = JSON.parse(pyConvert(dag));
  } catch (err) {
    res = { ok: false, error: String(err) };
  }
  if (!res.ok) {
    const box = el("convert-error");
    box.textContent = "Conversion failed: " + res.error;
    box.classList.remove("hidden");
    return;
  }
  editor.setValue(res.mmd);
  closeConvert();
}

/* ----------------------------- logo upload ------------------------------ */

// The playground runs entirely in the browser (Pyodide has no access to the
// user's disk), so a %%metro logo: directive can only resolve a path that
// already exists inside that sandbox - which is never true for an uploaded
// image. Embedding the image as a data: URI sidesteps the filesystem
// entirely: the bytes travel as inline text in the map source itself, so
// nf-metro can decode and render them with no path lookup at all.
const LOGO_DATA_URI_WARN_LENGTH = 70_000; // ~50KB of image data, base64-inflated

// The data URI chosen in the logo modal, applied on "Use this logo".
let pendingLogoUri = null;

function readFileAsDataUri(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () =>
      reject(reader.error || new Error("file read failed"));
    reader.readAsDataURL(file);
  });
}

function openLogo() {
  el("logo-file").value = "";
  el("logo-preview").src = "";
  el("logo-preview").classList.add("hidden");
  el("logo-warn").classList.add("hidden");
  el("logo-error").classList.add("hidden");
  el("logo-submit").disabled = true;
  pendingLogoUri = null;
  el("logo-modal").classList.remove("hidden");
}

function closeLogo() {
  el("logo-modal").classList.add("hidden");
}

async function handleLogoFile(file) {
  el("logo-error").classList.add("hidden");
  if (!file) return;
  let uri;
  try {
    uri = await readFileAsDataUri(file);
  } catch (err) {
    el("logo-error").textContent = "Could not read that file: " + err;
    el("logo-error").classList.remove("hidden");
    return;
  }
  el("logo-preview").src = uri;
  el("logo-preview").classList.remove("hidden");
  el("logo-warn").classList.toggle(
    "hidden",
    uri.length <= LOGO_DATA_URI_WARN_LENGTH,
  );
  pendingLogoUri = uri;
  el("logo-submit").disabled = false;
}

function submitLogo() {
  if (!pendingLogoUri) return;
  setDirective("logo", pendingLogoUri);
  closeLogo();
  doRender();
}

function removeLogo() {
  setDirective("logo", null);
  closeLogo();
  doRender();
}

// A %%metro logo: path the playground genuinely cannot resolve (it isn't a
// data URI and there is no source repo on disk to resolve it against) is the
// single most common reason a pasted map fails to render here; point at the
// fix rather than leaving the raw parser error to puzzle out.
function friendlyRenderError(msg) {
  if (/%%metro logo:.*not found/.test(msg)) {
    return (
      msg +
      '\n\nThe playground can\'t read logo files from disk - use the "+ Logo" button to attach the image instead.'
    );
  }
  return msg;
}

/* -------------------------------- utils -------------------------------- */

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

let toastTimer;
function toast(msg) {
  const t = el("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 1800);
}

/* ------------------------------ examples ------------------------------ */

async function loadExamples() {
  let groups;
  try {
    const resp = await fetch("examples.json", { cache: "no-store" });
    if (!resp.ok) return;
    groups = await resp.json();
  } catch (_) {
    return; // no manifest shipped; the starter remains available
  }
  const select = el("example-select");
  groups.forEach(({ label, entries }) => {
    const optgroup = document.createElement("optgroup");
    optgroup.label = label;
    entries.forEach(({ name, mmd }) => {
      examples[name] = mmd;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      optgroup.append(opt);
    });
    select.append(optgroup);
  });
}

/* ----------------------------- build info ------------------------------ */

// Generated by the docs deploy workflow (no equivalent in local dev, where the
// fetch 404s and the hint just stays hidden) so a deployed build can be traced
// back to the exact nf-metro commit it was published from.
async function loadBuildInfo() {
  let sha;
  try {
    const resp = await fetch("build-info.json", { cache: "no-store" });
    if (!resp.ok) return;
    ({ sha } = await resp.json());
  } catch (_) {
    return;
  }
  if (!sha) return;
  buildSha = sha;
  const hint = el("build-hint");
  if (!hint) return;
  const short = sha.slice(0, 7);
  hint.textContent = short;
  hint.href = `https://github.com/${REPO}/commit/${sha}`;
  hint.title = `Playground build ${short} — view commit on GitHub`;
  hint.classList.remove("hidden");
}

function loadExample(value) {
  if (!value) return;
  const mmd = value === "__seed__" ? SEED : examples[value];
  if (mmd != null) editor.setValue(mmd);
  // The dropdown is an action menu, not a state mirror: reset to the
  // placeholder so re-picking the same entry fires `change` again.
  el("example-select").value = "";
}

function wireControls() {
  el("example-select").addEventListener("change", (e) =>
    loadExample(e.target.value),
  );
  el("opt-theme").addEventListener("change", (e) =>
    setThemeDirective(e.target.value),
  );
  el("opt-mode").addEventListener("change", (e) =>
    setModeDirective(e.target.value),
  );
  DIRECTIVE_CONTROLS.forEach(([id, key, kind]) =>
    el(id).addEventListener("change", () =>
      applyDirectiveControl(id, key, kind),
    ),
  );
  ["opt-animate", "opt-directional", "opt-debug"].forEach((id) =>
    el(id).addEventListener("change", doRender),
  );
  Object.keys(SNIPPETS).forEach((id) =>
    el(id).addEventListener("click", () => insertSnippet(id)),
  );
  el("btn-svg").addEventListener("click", exportSvg);
  el("btn-png").addEventListener("click", exportPng);
  el("btn-share").addEventListener("click", shareLink);
  el("btn-copy-source").addEventListener("click", copySource);

  el("zoom-in").addEventListener("click", () => zoomBy(ZOOM_STEP));
  el("zoom-out").addEventListener("click", () => zoomBy(1 / ZOOM_STEP));
  el("zoom-fit").addEventListener("click", zoomFit);

  el("btn-report").addEventListener("click", openReport);
  el("report-cancel").addEventListener("click", closeReport);
  el("report-submit").addEventListener("click", submitReport);
  el("report-text").addEventListener("input", (e) => {
    el("report-submit").disabled = e.target.value.trim() === "";
  });
  el("report-modal").addEventListener("click", (e) => {
    if (e.target === el("report-modal")) closeReport();
  });

  el("btn-convert").addEventListener("click", openConvert);
  el("convert-cancel").addEventListener("click", closeConvert);
  el("convert-submit").addEventListener("click", submitConvert);
  el("convert-modal").addEventListener("click", (e) => {
    if (e.target === el("convert-modal")) closeConvert();
  });

  el("btn-logo").addEventListener("click", openLogo);
  el("logo-file").addEventListener("change", (e) =>
    handleLogoFile(e.target.files[0]),
  );
  el("logo-cancel").addEventListener("click", closeLogo);
  el("logo-remove").addEventListener("click", removeLogo);
  el("logo-submit").addEventListener("click", submitLogo);
  el("logo-modal").addEventListener("click", (e) => {
    if (e.target === el("logo-modal")) closeLogo();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!el("report-modal").classList.contains("hidden")) closeReport();
    if (!el("convert-modal").classList.contains("hidden")) closeConvert();
    if (!el("logo-modal").classList.contains("hidden")) closeLogo();
  });

  wireEditTools();
}

/* --------------------------- light / dark theme ----------------------- */
// Follow the docs site's preference (shared via the `starlight-theme`
// localStorage key) and stay independently toggleable. The page's color-scheme
// drives the inlined preview SVG's light-dark() chrome, so the map tracks the
// UI with no re-render.
function wireTheme() {
  const KEY = "starlight-theme";
  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch {}
  const btn = el("btn-theme");
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    if (btn) btn.textContent = theme === "dark" ? "☀️" : "☾";
  };
  apply(
    stored === "light" || stored === "dark"
      ? stored
      : matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light",
  );
  if (btn) {
    btn.addEventListener("click", () => {
      const next =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch {}
      apply(next);
    });
  }
}

wireTheme();
initEditor();
wireControls();
loadExamples();
loadBuildInfo();
boot();
