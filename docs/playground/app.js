"use strict";

// Pinned so the install path is reproducible; bump deliberately.
const PYODIDE_VERSION = "v0.27.2";

const REPO = "pinin4fjords/nf-metro";

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

// Glue defined inside the Pyodide runtime: returns a JSON envelope so a render
// error surfaces as data rather than a thrown PythonError to unwind in JS.
const PY_GLUE = `
import json
from nf_metro.api import render_string
from nf_metro.convert import convert_nextflow_dag

def nfm_render(mmd, opts_json):
    opts = json.loads(opts_json)
    layout = {k: v for k, v in (opts.get("layout_options") or {}).items() if v is not None}
    try:
        svg = render_string(
            mmd,
            theme=opts.get("theme") or None,
            responsive=True,
            embed_font=True,
            debug=bool(opts.get("debug")),
            layout_options=layout,
        )
        return json.dumps({"ok": True, "svg": svg})
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})

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
  editor.on("change", debounce(doRender, 300));

  // Test hook: drive the editor and renderer from automated trials.
  window.__nfMetro = {
    getValue: () => editor.getValue(),
    setValue: (v) => editor.setValue(v),
    render: doRender,
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
    debug: el("opt-debug").checked,
    layout_options: {
      animate: el("opt-animate").checked,
      directional: el("opt-directional").checked,
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
  const mmd = editor.getValue();
  let res;
  try {
    res = JSON.parse(pyRender(mmd, JSON.stringify(currentOptions())));
  } catch (err) {
    showError("Render runtime error: " + err);
    return;
  }
  if (res.ok) {
    showError(null);
    lastSvg = res.svg;
    el("preview").innerHTML = res.svg;
  } else {
    // Keep the last good render visible; just report the problem.
    showError(res.error);
  }
  refreshLineColors();
  syncDirectiveControls();
}

/* ------------------------- directive controls ------------------------- */

// Controls that change the map's layout or styling are source-of-truth: each
// writes a `%%metro <key>:` directive into the editor and is synced back from
// it, so the change is saved with the map and travels with export/share. (The
// animate/chevrons/debug toggles are preview overlays handled separately.)

// directive key for the theme; values are friendly aliases (dark == nfcore).
const THEME_KEYS = ["nfcore", "light"];
const THEME_STYLE_TOKEN = { nfcore: "dark", light: "light" };
const STYLE_ALIASES = { dark: "nfcore" };

// [control id, directive key, kind]
const DIRECTIVE_CONTROLS = [
  ["opt-line-spread", "line_spread", "choice"],
  ["opt-diamond-style", "diamond_style", "choice"],
  ["opt-line-order", "line_order", "choice"],
  ["opt-center-ports", "center_ports", "bool"],
  ["opt-compact-offsets", "compact_offsets", "bool"],
  ["opt-font-scale", "font_scale", "number"],
  ["opt-fold-threshold", "fold_threshold", "number"],
  ["opt-x-spacing", "x_spacing", "number"],
  ["opt-y-spacing", "y_spacing", "number"],
];

function readDirective(key) {
  const m = editor.getValue().match(new RegExp(`^\\s*%%metro\\s+${key}:\\s*(.+?)\\s*$`, "m"));
  return m ? m[1] : null;
}

// value === null removes the directive line; otherwise it is set (inserted
// after a %%metro title: line if present, else at the top - directives must
// precede the graph block).
function setDirective(key, value) {
  const lines = editor.getValue().split("\n");
  const idx = lines.findIndex((l) => new RegExp(`^\\s*%%metro\\s+${key}:`).test(l));
  if (value === null) {
    if (idx >= 0) editor.replaceRange("", { line: idx, ch: 0 }, { line: idx + 1, ch: 0 });
  } else if (idx >= 0) {
    const updated = lines[idx].replace(new RegExp(`(%%metro\\s+${key}:\\s*).*`), `$1${value}`);
    editor.replaceRange(updated, { line: idx, ch: 0 }, { line: idx, ch: lines[idx].length });
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
  setDirective("style", THEME_STYLE_TOKEN[themeKey] || themeKey);
  doRender();
}

const _TRUE = new Set(["true", "yes", "1"]);

function syncDirectiveControls() {
  el("opt-theme").value = themeKeyFromSource();
  for (const [id, key, kind] of DIRECTIVE_CONTROLS) {
    const value = readDirective(key);
    if (kind === "bool") el(id).checked = _TRUE.has((value || "").toLowerCase());
    else el(id).value = value ?? "";
  }
}

/* ----------------------------- line colors ---------------------------- */

const LINE_RE = /^\s*%%metro\s+line:\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(#[0-9a-fA-F]{3,8})/;

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
    { line: lineNo, ch: text.length }
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

function exportSvg() {
  if (!lastSvg) return;
  downloadBlob(new Blob([lastSvg], { type: "image/svg+xml" }), "metro_map.svg");
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
  const { svg, w, h } = svgWithIntrinsicSize(lastSvg);
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
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
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("canvas produced no image");
    downloadBlob(blob, "metro_map.png");
  } catch (err) {
    toast("PNG export failed: " + err.message);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* ------------------------------- sharing ------------------------------- */

function b64urlEncode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(b64) {
  const pad = b64.length % 4 ? "=".repeat(4 - (b64.length % 4)) : "";
  const bin = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + pad);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function loadFromHash() {
  const m = location.hash.match(/[#&]mmd=([^&]+)/);
  if (!m) return null;
  try {
    return b64urlDecode(decodeURIComponent(m[1]));
  } catch (_) {
    return null;
  }
}

function shareUrl() {
  const hash = "#mmd=" + encodeURIComponent(b64urlEncode(editor.getValue()));
  return location.origin + location.pathname + location.search + hash;
}

async function shareLink() {
  const url = shareUrl();
  history.replaceState(null, "", url);
  try {
    await navigator.clipboard.writeText(url);
    toast("Share link copied to clipboard");
  } catch (_) {
    toast("Share link is in the address bar");
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
  el("convert-text").value = "";
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

function loadExample(value) {
  if (!value) return;
  const mmd = value === "__seed__" ? SEED : examples[value];
  if (mmd != null) editor.setValue(mmd);
  // The dropdown is an action menu, not a state mirror: reset to the
  // placeholder so re-picking the same entry fires `change` again.
  el("example-select").value = "";
}

function wireControls() {
  el("example-select").addEventListener("change", (e) => loadExample(e.target.value));
  el("opt-theme").addEventListener("change", (e) => setThemeDirective(e.target.value));
  DIRECTIVE_CONTROLS.forEach(([id, key, kind]) =>
    el(id).addEventListener("change", () => applyDirectiveControl(id, key, kind))
  );
  ["opt-animate", "opt-directional", "opt-debug"].forEach((id) =>
    el(id).addEventListener("change", doRender)
  );
  Object.keys(SNIPPETS).forEach((id) => el(id).addEventListener("click", () => insertSnippet(id)));
  el("btn-svg").addEventListener("click", exportSvg);
  el("btn-png").addEventListener("click", exportPng);
  el("btn-share").addEventListener("click", shareLink);

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

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!el("report-modal").classList.contains("hidden")) closeReport();
    if (!el("convert-modal").classList.contains("hidden")) closeConvert();
  });
}

initEditor();
wireControls();
loadExamples();
boot();
