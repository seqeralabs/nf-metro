"use strict";

const PYODIDE_VERSION = "v0.27.2";

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

let pyodide;
let pyRender;
let pyConvert;

function progress(message, stage) {
  postMessage({ type: "progress", message, stage });
}

async function resolveWheel() {
  try {
    const response = await fetch("wheels/index.json", { cache: "no-store" });
    if (response.ok) {
      const { wheel } = await response.json();
      if (wheel) return new URL(`wheels/${wheel}`, self.location.href).href;
    }
  } catch (_) {
    // A deployed release can fall back to PyPI when no development wheel exists.
  }
  return "nf-metro";
}

async function boot() {
  progress("Loading the browser runtime", "runtime");
  importScripts(
    `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/pyodide.js`,
  );
  pyodide = await loadPyodide({
    indexURL: `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`,
  });
  progress("Installing nf-metro", "package");
  await pyodide.loadPackage("micropip");
  const micropip = pyodide.pyimport("micropip");
  // resvg-py is a compiled Rust extension with no wasm wheel, so micropip
  // cannot resolve it. The playground only ever renders SVG, and the module
  // that imports it (nf_metro.render.raster) is loaded lazily by the PNG path
  // alone, so a mock satisfies the resolver and is never touched.
  // Keep this version at or above pyproject's resvg-py floor, or micropip
  // rejects the mock as too old (the playground e2e run catches that).
  micropip.add_mock_package("resvg-py", "0.5.0");
  await micropip.install(await resolveWheel());
  pyodide.runPython(PY_GLUE);
  pyRender = pyodide.globals.get("nfm_render");
  pyConvert = pyodide.globals.get("nfm_convert");
  const version = pyodide.runPython("import nf_metro; nf_metro.__version__");
  postMessage({ type: "ready", version });
}

self.onmessage = async ({ data }) => {
  const { type, id } = data;
  try {
    if (type === "boot") {
      await boot();
      return;
    }
    if (type === "render") {
      const started = performance.now();
      const result = JSON.parse(pyRender(data.mmd, data.options));
      postMessage({
        type: "render-result",
        id,
        result,
        duration: Math.round(performance.now() - started),
      });
      return;
    }
    if (type === "convert") {
      postMessage({
        type: "convert-result",
        id,
        result: JSON.parse(pyConvert(data.source)),
      });
    }
  } catch (error) {
    postMessage({
      type: type === "boot" ? "boot-error" : "worker-error",
      id,
      operation: type,
      error: String(error),
    });
  }
};
