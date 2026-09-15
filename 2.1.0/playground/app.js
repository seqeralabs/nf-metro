"use strict";

const REPO = "seqeralabs/nf-metro";
const DRAFT_KEY = "nf-metro-playground-draft-v2";
const RECENTS_KEY = "nf-metro-playground-recents-v1";
const WELCOME_KEY = "nf-metro-playground-welcome-v1";

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

const SOURCE_COMPLETIONS = [
  ["%%metro title: ", "Map title"],
  ["%%metro line: id | Name | #19b7a5", "Define a route"],
  ["%%metro style: nfcore", "Map brand"],
  ["%%metro mode: light", "Light or dark output"],
  ["%%metro direction: LR", "Section direction"],
  ["%%metro grid: section | 0,0", "Place a section"],
  ["%%metro file: station | FORMAT", "File terminus"],
  ["%%metro files: station | FORMAT", "Multiple-file terminus"],
  ["%%metro dir: station | Results", "Directory terminus"],
  ["%%metro line_spread: rails", "Parallel route rails"],
  ["%%metro animate: true", "Animated route markers"],
  ["%%metro directional: true", "Direction chevrons"],
  ["subgraph section_id [Section name]", "Start a section"],
  ["station_id[Station label]", "Add a station"],
  ["source -->|line_id| target", "Connect stations"],
  ["end", "End a section"],
].map(([text, detail]) => ({ text, displayText: `${text}  -  ${detail}` }));

const el = (id) => document.getElementById(id);
let editor = null;
let renderWorker = null;
let workerReady = false;
let workerRequestId = 0;
let bootTimeout = null;
let latestRenderId = 0;
let queuedRender = null;
const workerRequests = new Map();
let lastSvg = "";
let nfMetroVersion = "";
let buildSha = "";
let draftTimer = null;
let lastSavedSource = "";
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
    gutters: ["CodeMirror-linenumbers", "nfm-diagnostics"],
    extraKeys: {
      "Ctrl-Space": showSourceCompletions,
      "Cmd-Space": showSourceCompletions,
    },
  });
  editor.setValue(loadInitialSource());
  lastSavedSource = editor.getValue();
  loadFromHashGz().then((src) => {
    if (src != null) editor.setValue(src);
  });
  const renderAfterChange = debounce(doRender, 300);
  editor.on("change", () => {
    renderAfterChange();
    scheduleDraftSave();
    updateDocumentState();
  });

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
    complete: showSourceCompletions,
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

function showSourceCompletions(instance = editor) {
  const cursor = instance.getCursor();
  const line = instance.getLine(cursor.line);
  const start = line.search(/\S|$/);
  const query = line.slice(start, cursor.ch).toLowerCase();
  const list = SOURCE_COMPLETIONS.filter((item) =>
    item.text.toLowerCase().includes(query),
  );
  CodeMirror.showHint(instance, () => ({
    from: CodeMirror.Pos(cursor.line, start),
    to: cursor,
    list: list.length ? list : SOURCE_COMPLETIONS,
  }));
}

/* -------------------------- local documents --------------------------- */

function storedJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) || fallback;
  } catch (_) {
    return fallback;
  }
}

function loadInitialSource() {
  const shared = loadFromHash();
  if (shared != null) return shared;
  const draft = storedJson(DRAFT_KEY, null);
  return draft && draft.source ? draft.source : SEED;
}

function mapTitle(source = editor?.getValue() || "") {
  const match = source.match(/^\s*%%metro\s+title:\s*(.+)$/m);
  return match ? match[1].trim() : "Untitled map";
}

function updateDocumentState() {
  if (!editor) return;
  el("document-name").textContent = mapTitle();
  const changed = editor.getValue() !== lastSavedSource;
  el("draft-status").textContent = changed
    ? "Saving local draft…"
    : "Saved locally";
  el("route-source").classList.toggle("active", changed);
}

function saveDraft() {
  const source = editor.getValue();
  try {
    localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        source,
        title: mapTitle(source),
        updatedAt: Date.now(),
      }),
    );
    lastSavedSource = source;
    updateDocumentState();
  } catch (_) {
    el("draft-status").textContent = "Local saving unavailable";
  }
}

function scheduleDraftSave() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraft, 450);
}

function recentDocuments() {
  return storedJson(RECENTS_KEY, []);
}

function archiveSource(source = editor.getValue()) {
  if (!source.trim() || source === SEED) return;
  const recents = recentDocuments().filter((entry) => entry.source !== source);
  recents.unshift({
    id: String(Date.now()),
    title: mapTitle(source),
    source,
    updatedAt: Date.now(),
  });
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(recents.slice(0, 8)));
  } catch (_) {}
  refreshRecents();
}

function refreshRecents() {
  const select = el("recent-select");
  if (!select) return;
  select.replaceChildren(new Option("Choose…", ""));
  recentDocuments().forEach((entry) => {
    const option = new Option(entry.title, entry.id);
    option.title = new Date(entry.updatedAt).toLocaleString();
    select.append(option);
  });
}

function loadRecent(id) {
  const entry = recentDocuments().find((item) => item.id === id);
  if (!entry) return;
  archiveSource();
  editor.setValue(entry.source);
  el("recent-select").value = "";
  toast(`Opened ${entry.title}`);
}

function replaceDocument(source, message) {
  archiveSource();
  editor.setValue(source);
  editor.clearHistory();
  saveDraft();
  showMobileView("source");
  if (message) toast(message);
}

function newDocument() {
  if (
    editor.getValue() !== lastSavedSource &&
    !confirm("Start a new map? Your current work is saved in Recent maps.")
  )
    return;
  replaceDocument(SEED, "Started a new map");
}

async function openSourceFile(file) {
  if (!file) return;
  const source = await file.text();
  if (
    /^\s*(?:flowchart|graph)\s+(?:TB|TD)\b/m.test(source) &&
    !source.includes("%%metro")
  ) {
    if (!workerReady) {
      toast("The DAG importer is still loading");
      return;
    }
    setRenderStatus("Converting Nextflow DAG", "loading");
    const result = await workerCall("convert", { source });
    if (!result.ok) {
      showError(`Conversion failed: ${result.error}`);
      return;
    }
    replaceDocument(result.mmd, `Imported ${file.name}`);
    return;
  }
  replaceDocument(source, `Opened ${file.name}`);
}

function setRenderStatus(message, state) {
  const status = el("render-status");
  if (!status) return;
  status.dataset.state = state;
  const text = status.querySelector(".status-text");
  if (text) text.textContent = message;
  else
    status.replaceChildren(
      Object.assign(document.createElement("span"), {
        className: "status-dot",
      }),
      document.createTextNode(message),
    );
}

/* --------------------------------- boot -------------------------------- */

function setBootMsg(msg) {
  el("boot-msg").textContent = msg;
  setRenderStatus(msg, "loading");
}

function showBootFailure(message) {
  clearTimeout(bootTimeout);
  setBootMsg(message);
  setRenderStatus("Renderer unavailable", "error");
  el("boot").querySelector(".spinner").classList.add("hidden");
  el("boot-retry").classList.remove("hidden");
}

function workerCall(type, payload = {}) {
  return new Promise((resolve, reject) => {
    const id = ++workerRequestId;
    workerRequests.set(id, { resolve, reject });
    renderWorker.postMessage({ type, id, ...payload });
  });
}

function boot() {
  clearTimeout(bootTimeout);
  if (renderWorker) renderWorker.terminate();
  workerReady = false;
  el("boot").classList.remove("hidden");
  el("boot").querySelector(".spinner").classList.remove("hidden");
  el("boot-retry").classList.add("hidden");
  setBootMsg("Loading the browser runtime");
  renderWorker = new Worker("worker.js");
  renderWorker.onmessage = ({ data }) => {
    if (data.type === "progress") {
      setBootMsg(data.message);
      return;
    }
    if (data.type === "ready") {
      clearTimeout(bootTimeout);
      workerReady = true;
      nfMetroVersion = data.version;
      el("runtime-version").textContent = `nf-metro ${nfMetroVersion}`;
      setRenderStatus("Renderer ready", "ready");
      el("route-layout").classList.add("complete");
      showWelcomeIfNeeded();
      el("boot").classList.add("hidden");
      doRender();
      window.__nfMetroReady = true;
      return;
    }
    if (data.type === "boot-error") {
      showBootFailure(`Renderer failed to start: ${data.error}`);
      return;
    }
    if (data.type === "render-result") {
      handleRenderResult(data);
      return;
    }
    const pending = workerRequests.get(data.id);
    if (!pending) return;
    workerRequests.delete(data.id);
    if (data.type === "worker-error") pending.reject(new Error(data.error));
    else pending.resolve(data.result);
  };
  renderWorker.onerror = (event) => {
    showBootFailure(`Renderer failed to start: ${event.message}`);
  };
  bootTimeout = setTimeout(
    () =>
      showBootFailure(
        "Renderer startup timed out. Check your connection and retry.",
      ),
    90000,
  );
  renderWorker.postMessage({ type: "boot" });
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

function diagnosticPosition(message) {
  const match = String(message || "").match(
    /line\s+(\d+)(?:[, :] +column\s+(\d+))?/i,
  );
  if (!match) return null;
  return {
    line: Math.max(0, Number(match[1]) - 1),
    ch: Math.max(0, Number(match[2] || 1) - 1),
  };
}

function clearDiagnosticMarker() {
  editor.clearGutter("nfm-diagnostics");
}

function showError(msg, severity = "error") {
  const box = el("error");
  clearDiagnosticMarker();
  if (!msg) {
    box.classList.add("hidden");
    box.textContent = "";
    box.dataset.severity = "";
  } else {
    box.replaceChildren();
    box.dataset.severity = severity;
    const label = document.createElement("strong");
    label.textContent =
      severity === "warning" ? "Layout warning" : "Source error";
    const text = document.createElement("span");
    text.textContent = msg;
    box.append(label, text);
    const position = diagnosticPosition(msg);
    if (position) {
      const marker = document.createElement("span");
      marker.className = `diagnostic-marker ${severity}`;
      marker.textContent = severity === "warning" ? "!" : "×";
      marker.title = severity === "warning" ? "Layout warning" : "Source error";
      editor.setGutterMarker(position.line, "nfm-diagnostics", marker);
      const jump = document.createElement("button");
      jump.className = "diagnostic-jump";
      jump.textContent = `Go to line ${position.line + 1}`;
      jump.addEventListener("click", () => {
        editor.setCursor(position);
        editor.focus();
      });
      box.append(jump);
    }
    box.classList.remove("hidden");
  }
}

function doRender() {
  queuedRender = {
    mmd: editor.getValue(),
    options: JSON.stringify(currentOptions()),
  };
  if (!workerReady) return;
  const id = ++workerRequestId;
  latestRenderId = id;
  const payload = queuedRender;
  queuedRender = null;
  el("preview").classList.add("rendering");
  el("stale-preview").classList.toggle("hidden", !lastSvg);
  setRenderStatus("Rendering map", "loading");
  el("route-layout").classList.add("active");
  renderWorker.postMessage({ type: "render", id, ...payload });
  return id;
}

function handleRenderResult({ id, result: res, duration }) {
  if (id !== latestRenderId) return;
  el("preview").classList.remove("rendering");
  el("stale-preview").classList.add("hidden");
  el("route-layout").classList.remove("active");
  if (res.svg) {
    lastSvg = res.svg;
    el("preview").innerHTML = res.svg;
    applyZoom();
  }
  const warnings =
    res.warnings && res.warnings.length ? res.warnings.join("\n\n") : "";
  if (!res.ok) {
    const parts = warnings ? [warnings, res.error] : [res.error];
    showError(friendlyRenderError(parts.join("\n\n")), "error");
    setRenderStatus(`Render failed after ${duration} ms`, "error");
    el("route-layout").classList.add("error");
  } else if (warnings) {
    showError(friendlyRenderError(warnings), "warning");
    setRenderStatus(`Rendered with warnings in ${duration} ms`, "warning");
    el("route-layout").classList.add("warning");
  } else {
    showError(null);
    setRenderStatus(`Rendered in ${duration} ms`, "ready");
    el("route-layout").classList.remove("error", "warning");
    el("route-layout").classList.add("complete");
  }
  refreshLineColors();
  syncDirectiveControls();
  reapplySelection();
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
  ["opt-stroke-scale", "stroke_scale", "number"],
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
  const focused = document.activeElement;
  const themeControl = el("opt-theme");
  if (themeControl !== focused) themeControl.value = themeKeyFromSource();
  const mode = modeFromSource();
  const modeControl = el("opt-mode");
  if (modeControl !== focused) modeControl.value = mode;
  applyPreviewMode(mode);
  for (const [id, key, kind] of DIRECTIVE_CONTROLS) {
    const control = el(id);
    if (control === focused) continue;
    const value = readDirective(key);
    if (kind === "bool")
      control.checked = _TRUE.has((value || "").toLowerCase());
    else control.value = value ?? "";
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
  if (sel) showControlPanel("inspect");
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
  markExportComplete();
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
    markExportComplete();
  } catch (err) {
    toast("PNG export failed: " + err.message);
  } finally {
    URL.revokeObjectURL(url);
  }
}

function exportSourceFile() {
  const source = editor.getValue();
  const name =
    mapTitle(source)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "") || "metro-map";
  downloadBlob(new Blob([source], { type: "text/plain" }), `${name}.mmd`);
  saveDraft();
  markExportComplete();
}

function markExportComplete() {
  el("route-export").classList.add("complete");
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

async function compressedShareUrl(source = editor.getValue()) {
  return _pageUrl(
    "#mmd-gz=" + encodeURIComponent(await b64urlEncodeGz(source)),
  );
}

async function shareLink() {
  const url = await compressedShareUrl();
  history.replaceState(null, "", url);
  markExportComplete();
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

const MAX_ISSUE_URL_LENGTH = 7500;
const MAX_ISSUE_SOURCE_LENGTH = 6000;
const MAX_ISSUE_EXPLANATION_LENGTH = 3000;

function issueSourceBlock(mmd, limit, hasReproduceLink) {
  if (mmd.length <= limit) return mmd;
  const location = hasReproduceLink
    ? "full map in the reproduce link"
    : "attach the full .mmd file to this issue";
  return mmd.slice(0, limit) + `\n... (truncated; ${location})`;
}

function issueExplanationBlock(explanation, limit) {
  if (explanation.length <= limit) return explanation;
  return (
    explanation.slice(0, limit) +
    "\n... (truncated; add the remaining details after opening this issue)"
  );
}

function issueUrl(explanation, opts, mmd, sourceLimit, reproduceUrl) {
  const mmdBlock = issueSourceBlock(mmd, sourceLimit, Boolean(reproduceUrl));
  const lo = opts.layout_options;
  const reproduce = reproduceUrl
    ? `[Open this map in the playground](${reproduceUrl})`
    : "The map is too large for a reproduce link. Attach the `.mmd` file to this issue.";
  const body = `## What's wrong

${explanation}

## Map source

\`\`\`
${mmdBlock}
\`\`\`

## Reproduce

${reproduce}

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

function fitIssueUrl(explanation, opts, mmd, reproduceUrl) {
  let low = 0;
  let high = Math.min(mmd.length, MAX_ISSUE_SOURCE_LENGTH);
  let best = issueUrl(explanation, opts, mmd, 0, reproduceUrl);
  if (best.length > MAX_ISSUE_URL_LENGTH) return null;
  const fullest = issueUrl(explanation, opts, mmd, high, reproduceUrl);
  if (fullest.length <= MAX_ISSUE_URL_LENGTH) return fullest;
  if (high === mmd.length) high -= 1;

  while (low <= high) {
    const limit = Math.floor((low + high) / 2);
    const candidate = issueUrl(explanation, opts, mmd, limit, reproduceUrl);
    if (candidate.length <= MAX_ISSUE_URL_LENGTH) {
      best = candidate;
      low = limit + 1;
    } else {
      high = limit - 1;
    }
  }
  return best;
}

function fitIssueExplanation(explanation, opts, mmd) {
  let low = 0;
  let high = Math.min(explanation.length, MAX_ISSUE_EXPLANATION_LENGTH);
  let best = issueUrl(
    issueExplanationBlock(explanation, 0),
    opts,
    mmd,
    0,
    null,
  );

  while (low <= high) {
    const limit = Math.floor((low + high) / 2);
    const candidate = issueUrl(
      issueExplanationBlock(explanation, limit),
      opts,
      mmd,
      0,
      null,
    );
    if (candidate.length <= MAX_ISSUE_URL_LENGTH) {
      best = candidate;
      low = limit + 1;
    } else {
      high = limit - 1;
    }
  }
  return best;
}

async function buildIssueUrl(explanation) {
  const opts = currentOptions();
  const mmd = editor.getValue();
  const issueExplanation = issueExplanationBlock(
    explanation,
    MAX_ISSUE_EXPLANATION_LENGTH,
  );
  const reproduceUrl = await compressedShareUrl(mmd);
  const withReproduce =
    reproduceUrl.length < MAX_ISSUE_URL_LENGTH
      ? fitIssueUrl(issueExplanation, opts, mmd, reproduceUrl)
      : null;
  return (
    withReproduce ||
    fitIssueUrl(issueExplanation, opts, mmd, null) ||
    fitIssueExplanation(explanation, opts, mmd)
  );
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

async function submitReport() {
  const explanation = el("report-text").value.trim();
  if (!explanation) {
    el("report-text").focus();
    return;
  }
  const reportWindow = window.open("about:blank", "_blank");
  if (reportWindow) reportWindow.opener = null;
  closeReport();
  const url = await buildIssueUrl(explanation);
  // Exposed so the e2e suite can assert the prefilled issue without
  // navigating to github.com.
  window.__nfMetroLastIssueUrl = url;
  if (reportWindow) reportWindow.location.href = url;
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

async function submitConvert() {
  const dag = el("convert-text").value;
  if (!dag.trim() || !workerReady) return;
  el("convert-submit").disabled = true;
  el("convert-submit").textContent = "Converting…";
  let res;
  try {
    res = await workerCall("convert", { source: dag });
  } catch (err) {
    res = { ok: false, error: String(err) };
  } finally {
    el("convert-submit").disabled = false;
    el("convert-submit").textContent = "Convert";
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
  const available = new Map(
    groups
      .flatMap(({ entries }) => entries)
      .map((entry) => [entry.name, entry.mmd]),
  );
  const curated = [
    ["Start here", ["simple_pipeline", "rnaseq_auto", "rnaseq_sections"]],
    [
      "Pipeline shapes",
      [
        "fanout_bundle_plus_spurs",
        "folded_corridor_distinct_lanes",
        "cross_track_interchange",
      ],
    ],
    [
      "Presentation",
      ["directional_flow", "line_spread", "marker_styles", "file_icons"],
    ],
  ];
  curated.forEach(([label, names]) => {
    const optgroup = document.createElement("optgroup");
    optgroup.label = label;
    names.forEach((name) => {
      const mmd = available.get(name);
      if (mmd == null) return;
      examples[name] = mmd;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name
        .replaceAll("_", " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
      optgroup.append(opt);
    });
    if (optgroup.children.length) select.append(optgroup);
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
  if (mmd != null) replaceDocument(mmd, "Loaded example");
  // The dropdown is an action menu, not a state mirror: reset to the
  // placeholder so re-picking the same entry fires `change` again.
  el("example-select").value = "";
}

function closeWelcome() {
  el("welcome-modal")?.classList.add("hidden");
  try {
    localStorage.setItem(WELCOME_KEY, "seen");
  } catch (_) {}
}

function showWelcomeIfNeeded() {
  if (new URLSearchParams(location.search).has("skip-welcome")) return;
  let seen = false;
  try {
    seen = localStorage.getItem(WELCOME_KEY) === "seen";
  } catch (_) {}
  if (!seen && !location.hash) {
    const draft = storedJson(DRAFT_KEY, null);
    el("welcome-continue").classList.toggle(
      "hidden",
      !draft?.source || draft.source === SEED,
    );
    el("welcome-modal").classList.remove("hidden");
    el("welcome-import").focus();
  }
}

function showControlPanel(name) {
  document.querySelectorAll(".preview-tab").forEach((button) => {
    const active = button.dataset.panel === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".control-panel").forEach((panel) => {
    const active = panel.id === `panel-${name}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  if (name === "layout") el("advanced").open = true;
}

function showMobileView(view) {
  document.querySelectorAll(".mobile-view").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  el("split").dataset.mobileView = view;
  if (view === "source") setTimeout(() => editor.refresh(), 0);
}

function wireFileDrop() {
  const target = el("drop-target");
  ["dragenter", "dragover"].forEach((type) =>
    target.addEventListener(type, (event) => {
      event.preventDefault();
      el("drop-invitation").classList.remove("hidden");
    }),
  );
  ["dragleave", "drop"].forEach((type) =>
    target.addEventListener(type, (event) => {
      event.preventDefault();
      el("drop-invitation").classList.add("hidden");
    }),
  );
  target.addEventListener("drop", (event) =>
    openSourceFile(event.dataTransfer.files[0]),
  );
}

function cycleFocusInModal(event) {
  if (event.key !== "Tab") return;
  const modal = event.currentTarget;
  const focusable = [
    ...modal.querySelectorAll("button, input, textarea, select, a[href]"),
  ].filter((node) => !node.disabled && !node.classList.contains("hidden"));
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

let commandSelection = 0;

function commandActions() {
  return [
    ["Import a Nextflow DAG", "Document", openConvert],
    ["Open a map file", "Document", () => el("file-open").click()],
    ["Start a new map", "Document", newDocument],
    [
      "Show source completions",
      "Source",
      () => {
        closeCommands();
        editor.focus();
        showSourceCompletions();
      },
    ],
    ["Insert a section block", "Source", () => insertSnippet("btn-section")],
    ["Insert a line", "Source", () => insertSnippet("btn-line")],
    ["Insert an edge", "Source", () => insertSnippet("btn-edge")],
    ["Undo source edit", "Source", () => editor.undo()],
    ["Redo source edit", "Source", () => editor.redo()],
    ["Open style controls", "Preview", () => showControlPanel("style")],
    ["Open layout controls", "Preview", () => showControlPanel("layout")],
    ["Inspect the map", "Preview", () => showControlPanel("inspect")],
    ["Fit map to view", "Preview", zoomFit],
    ["Share this map", "Export", shareLink],
    ["Download SVG", "Export", exportSvg],
    ["Download PNG", "Export", exportPng],
    ["Download metro source", "Export", exportSourceFile],
    ["Report a problem", "Help", openReport],
  ].map(([label, group, run]) => ({ label, group, run }));
}

function visibleCommands() {
  const query = el("command-search").value.trim().toLowerCase();
  return commandActions().filter(({ label, group }) =>
    `${label} ${group}`.toLowerCase().includes(query),
  );
}

function renderCommands() {
  const list = el("command-list");
  const actions = visibleCommands();
  commandSelection = Math.min(
    commandSelection,
    Math.max(0, actions.length - 1),
  );
  list.replaceChildren();
  actions.forEach((action, index) => {
    const button = document.createElement("button");
    button.className = `command-item${index === commandSelection ? " selected" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === commandSelection));
    button.append(
      document.createTextNode(action.label),
      Object.assign(document.createElement("span"), {
        textContent: action.group,
      }),
    );
    button.addEventListener("mouseenter", () => {
      commandSelection = index;
      renderCommands();
    });
    button.addEventListener("click", () => runCommand(action));
    list.append(button);
  });
}

function runCommand(action = visibleCommands()[commandSelection]) {
  if (!action) return;
  closeCommands();
  action.run();
}

function openCommands() {
  commandSelection = 0;
  el("command-search").value = "";
  renderCommands();
  el("command-modal").classList.remove("hidden");
  el("command-search").focus();
}

function closeCommands() {
  el("command-modal").classList.add("hidden");
}

function handleCommandKeys(event) {
  const actions = visibleCommands();
  if (event.key === "ArrowDown") {
    event.preventDefault();
    commandSelection = Math.min(actions.length - 1, commandSelection + 1);
    renderCommands();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    commandSelection = Math.max(0, commandSelection - 1);
    renderCommands();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runCommand(actions[commandSelection]);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closeCommands();
  }
}

function wireControls() {
  refreshRecents();
  updateDocumentState();
  showMobileView("source");
  el("example-select").addEventListener("change", (e) =>
    loadExample(e.target.value),
  );
  el("recent-select").addEventListener("change", (e) =>
    loadRecent(e.target.value),
  );
  el("btn-new").addEventListener("click", newDocument);
  el("btn-open").addEventListener("click", () => el("file-open").click());
  el("file-open").addEventListener("change", (event) => {
    openSourceFile(event.target.files[0]);
    event.target.value = "";
  });
  el("btn-undo").addEventListener("click", () => editor.undo());
  el("btn-redo").addEventListener("click", () => editor.redo());
  el("btn-commands").addEventListener("click", openCommands);
  el("boot-retry").addEventListener("click", boot);
  el("command-search").addEventListener("input", () => {
    commandSelection = 0;
    renderCommands();
  });
  el("command-search").addEventListener("keydown", handleCommandKeys);
  el("command-modal").addEventListener("click", (event) => {
    if (event.target === el("command-modal")) closeCommands();
  });
  document
    .querySelectorAll(".preview-tab")
    .forEach((button) =>
      button.addEventListener("click", () =>
        showControlPanel(button.dataset.panel),
      ),
    );
  document
    .querySelectorAll(".mobile-view")
    .forEach((button) =>
      button.addEventListener("click", () =>
        showMobileView(button.dataset.view),
      ),
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

  el("welcome-import").addEventListener("click", () => {
    closeWelcome();
    openConvert();
  });
  el("welcome-new").addEventListener("click", () => {
    closeWelcome();
    replaceDocument(SEED, "Started a simple map");
  });
  el("welcome-example").addEventListener("click", () => {
    closeWelcome();
    el("example-select").focus();
  });
  el("welcome-continue").addEventListener("click", closeWelcome);

  document
    .querySelectorAll(".modal-overlay")
    .forEach((modal) => modal.addEventListener("keydown", cycleFocusInModal));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!el("command-modal").classList.contains("hidden")) closeCommands();
      if (!el("report-modal").classList.contains("hidden")) closeReport();
      if (!el("convert-modal").classList.contains("hidden")) closeConvert();
      if (!el("logo-modal").classList.contains("hidden")) closeLogo();
      return;
    }
    if (!(e.metaKey || e.ctrlKey)) return;
    if (e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCommands();
    } else if (e.key.toLowerCase() === "s") {
      e.preventDefault();
      exportSourceFile();
    } else if (e.key.toLowerCase() === "o") {
      e.preventDefault();
      el("file-open").click();
    }
  });

  wireFileDrop();
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
window.addEventListener("pagehide", () => {
  if (editor) saveDraft();
});
boot();
