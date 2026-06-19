"""HTML wrapper around the SVG renderer for interactive metro maps."""

from __future__ import annotations

import hashlib
import html
import json
from string import Template

from nf_metro.parser.model import MetroGraph
from nf_metro.render.driver import get_driver_js
from nf_metro.render.style import Theme
from nf_metro.render.svg import render_svg


class _JsTemplate(Template):
    # `$` and `${...}` collide with JS template literals (`${x}`, `${ln.color}`).
    # Use `@@` so JS source can be pasted verbatim into the templates.
    delimiter = "@@"


def render_html(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    animate: bool | None = None,
    debug: bool = False,
    embed_basename: str = "metro_map.html",
) -> str:
    """Render the graph to an interactive standalone HTML page.

    The HTML side panel replaces the SVG legend in interactive mode.
    """
    svg = render_svg(
        graph,
        theme,
        width=width,
        height=height,
        animate=animate,
        debug=debug,
        legend_position="none",
    )

    title = graph.title or "nf-metro map"
    lines = [
        {
            "id": lid,
            "label": ln.display_name,
            "color": ln.color,
            "style": ln.style or "solid",
        }
        for lid, ln in graph.lines.items()
    ]
    snippet_id = "m" + hashlib.sha1(svg.encode("utf-8")).hexdigest()[:8]
    inline_snippet = _build_inline_snippet(svg, lines, snippet_id)

    # Browsers terminate the outer <script> the moment they see literal
    # </script>, regardless of JS string context. JSON's optional `\/`
    # escape decodes back to `/`, so the snippet survives round-trip.
    inline_snippet_json = json.dumps(inline_snippet).replace("</", "<\\/")

    return _STANDALONE_TEMPLATE.substitute(
        title=html.escape(title),
        svg=svg,
        lines_json=json.dumps(lines),
        embed_basename=html.escape(embed_basename),
        inline_snippet_json=inline_snippet_json,
        shared_js=get_driver_js(),
    )


def _build_inline_snippet(svg: str, lines: list[dict[str, str]], sid: str) -> str:
    return _INLINE_TEMPLATE.substitute(
        sid=sid,
        svg=svg,
        lines_json=json.dumps(lines),
        shared_js=get_driver_js(),
    )


_STANDALONE_TEMPLATE = _JsTemplate("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@title</title>
<style>
  :root {
    --bg: #1d1d1d; --panel: #161616; --panel-border: #333;
    --text: #e8e8e8; --muted: #888; --hover: #232323; --active: #2a3a4a;
  }
  html, body { margin: 0; padding: 0; height: 100%; background: var(--bg);
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                color: var(--text); overflow: hidden; }
  #app { display: grid; grid-template-columns: 1fr 260px;
          grid-template-rows: 44px 1fr; height: 100vh; }
  header { grid-column: 1 / span 2; display: flex; align-items: center;
            padding: 0 14px; background: var(--panel);
            border-bottom: 1px solid var(--panel-border); gap: 12px; }
  header h1 { font-size: 14px; font-weight: 600; margin: 0; flex: 1;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  header .hint { font-size: 11px; color: var(--muted); }
  .btn { background: transparent; color: #ccc; border: 1px solid #444;
          padding: 4px 10px; border-radius: 4px; font-size: 12px;
          cursor: pointer; font-family: inherit; }
  .btn:hover { background: var(--hover); color: #fff; }
  .btn.primary { border-color: #555; background: #2a2a2a; }
  .nf-metro-copied { background: #2e4a2e !important; color: #c8efc8 !important;
                     border-color: #4a8a4a !important; }
  .nf-metro-canvas { position: relative; overflow: hidden; background: var(--bg);
                      cursor: grab; }
  .nf-metro-grabbing .nf-metro-canvas { cursor: grabbing; }
  .nf-metro-canvas svg { width: 100% !important; height: 100% !important;
                          display: block; user-select: none; }
  .nf-metro-station { cursor: pointer; }
  .nf-metro-station:hover { stroke-width: 3; }
  aside { background: var(--panel); border-left: 1px solid var(--panel-border);
           padding: 12px; overflow-y: auto; font-size: 13px; }
  aside h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
              color: var(--muted); margin: 0 0 6px; font-weight: 600; }
  .nf-metro-legend { display: flex; flex-direction: column; }
  .nf-metro-chip { display: flex; align-items: center; gap: 10px; padding: 6px 8px;
                    border-radius: 4px; cursor: pointer; user-select: none;
                    margin: 1px -8px; }
  .nf-metro-chip:hover { background: var(--hover); }
  .nf-metro-chip-active { background: var(--active); }
  .nf-metro-chip-dim { opacity: 0.35; }
  .nf-metro-swatch { width: 18px; height: 3px; border-radius: 2px; flex-shrink: 0; }
  .nf-metro-chip span { overflow: hidden; text-overflow: ellipsis;
                         white-space: nowrap; }
  .footer-help { font-size: 11px; color: var(--muted); margin-top: 16px;
                  line-height: 1.6; }
  .footer-help kbd { background: #2a2a2a; border: 1px solid #444; padding: 1px 5px;
                      border-radius: 3px; font-family: inherit; font-size: 10px; }
  .nf-metro-filtering [class^="metro-line-"] { opacity: 0.05;
                                                transition: opacity 0.2s; }
  .nf-metro-filtering [class^="metro-line-"].nf-metro-line-active { opacity: 1; }
  .nf-metro-hidden { display: none !important; }
  .nf-metro-tip { position: fixed; pointer-events: none; z-index: 50;
                   background: #1f1f1f; border: 1px solid #444; border-radius: 6px;
                   padding: 8px 10px; font-size: 12px; max-width: 240px;
                   box-shadow: 0 4px 12px rgba(0,0,0,0.4);
                   opacity: 0; transition: opacity 0.08s; }
  .nf-metro-tip-show { opacity: 1; }
  .nf-metro-tt-title { font-weight: 600; font-size: 13px; margin-bottom: 3px; }
  .nf-metro-tt-section { color: var(--muted); font-size: 11px; margin-bottom: 6px; }
  .nf-metro-tt-line { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .nf-metro-tt-line .nf-metro-swatch { width: 14px; height: 3px; }
  .nf-metro-embed-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.6);
                          display: none; align-items: center; justify-content: center;
                          z-index: 100; }
  .nf-metro-modal-open { display: flex; }
  .nf-metro-modal { background: #1f1f1f; border: 1px solid #444; border-radius: 8px;
                    padding: 20px; max-width: 640px; width: 90%; }
  .nf-metro-modal h3 { margin: 0 0 12px; font-size: 15px; }
  .nf-metro-modal p { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
  .nf-metro-modal pre { background: #0d0d0d; border: 1px solid #333; border-radius: 4px;
                        padding: 10px; font-size: 11px; overflow-x: auto;
                        margin: 4px 0 12px; max-height: 200px; overflow-y: auto;
                        white-space: pre-wrap; word-break: break-all; }
  .nf-metro-modal .row { display: flex; gap: 8px; align-items: center;
                          margin-bottom: 4px; }
  .nf-metro-modal .row label { font-size: 11px; color: var(--muted); flex: 1; }
  .nf-metro-modal-close { float: right; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>@@title</h1>
    <span class="hint">drag to pan, scroll to zoom, click a line to focus</span>
    <button class="btn nf-metro-reset">Reset</button>
    <button class="btn primary nf-metro-embed-btn">Embed&hellip;</button>
  </header>
  <main class="nf-metro-canvas">
    @@svg
  </main>
  <aside>
    <h2>Lines</h2>
    <div class="nf-metro-legend"></div>
    <div class="footer-help">
      <strong>Click</strong> a line to isolate it.<br>
      <strong>Click again</strong> or <kbd>Esc</kbd> to reset.<br>
      <strong>Drag</strong> to pan, <strong>scroll</strong> to zoom.
    </div>
  </aside>
</div>
<div class="nf-metro-tip"></div>

<div class="nf-metro-embed-modal">
  <div class="nf-metro-modal">
    <button class="btn nf-metro-modal-close">Close</button>
    <h3>Embed snippets</h3>
    <p><strong>Interactive HTML (inline).</strong> Self-contained snippet -
       paste into any HTML host (MkDocs, Confluence, Notion, blog templates)
       and it keeps full pan / zoom / line-filter. No iframe, no hosting.</p>
    <div class="row">
      <label>inline HTML</label>
      <button class="btn" data-copy="inline">Copy</button>
    </div>
    <pre class="nf-metro-snippet-inline"></pre>
    <p><strong>Interactive (iframe).</strong> Host this HTML file and embed
       the iframe. GitHub READMEs strip iframes - link to a hosted page
       from the README instead.</p>
    <div class="row">
      <label>iframe</label>
      <button class="btn" data-copy="iframe">Copy</button>
    </div>
    <pre class="nf-metro-snippet-iframe"></pre>
    <p><strong>Static SVG.</strong> Inline this anywhere that accepts raw HTML
       (or save it as .svg). No interactivity but it renders even where
       scripts are stripped.</p>
    <div class="row">
      <label>inline SVG</label>
      <button class="btn" data-copy="svg">Copy</button>
    </div>
    <pre class="nf-metro-snippet-svg"></pre>
  </div>
</div>

<script>
@@shared_js
window.nfMetroApi = attachMetroMap({
  root: document.body,
  lines: @@lines_json,
  embed: { basename: "@@embed_basename", snippet: @@inline_snippet_json },
});
</script>
</body>
</html>
""")


# ----------------------------------------------------------------------------
# Inline embed snippet: a <div> with scoped CSS and an IIFE that runs the
# same shared JS as the standalone page. The wrapper class is hashed per
# render so multiple maps coexist on a single host page.
# ----------------------------------------------------------------------------
_INLINE_TEMPLATE = _JsTemplate("""<div class="nfmm-@@sid nf-metro-root">
<style>
.nfmm-@@sid { display: block; width: 100%; background: #1d1d1d;
  border: 1px solid #2a2a2a; border-radius: 8px; overflow: hidden;
  font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
  color: #e8e8e8; line-height: 1.4; position: relative; }
.nfmm-@@sid .nf-metro-canvas { position: relative; }
.nfmm-@@sid .nf-metro-canvas svg { display: block; width: 100%; height: auto;
  user-select: none; cursor: grab; }
.nfmm-@@sid.nf-metro-grabbing .nf-metro-canvas svg { cursor: grabbing; }
.nfmm-@@sid .nf-metro-station { cursor: pointer; }
.nfmm-@@sid .nf-metro-station:hover { stroke-width: 3; }
.nfmm-@@sid .nf-metro-legend { display: flex; flex-wrap: wrap; gap: 4px 6px;
  padding: 8px 10px; background: #161616; border-top: 1px solid #2a2a2a;
  font-size: 11px; }
.nfmm-@@sid .nf-metro-chip { display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 8px; border-radius: 12px; cursor: pointer; color: #ccc;
  user-select: none; white-space: nowrap; background: #1f1f1f;
  border: 1px solid #2f2f2f; }
.nfmm-@@sid .nf-metro-chip:hover { background: #2a2a2a; }
.nfmm-@@sid .nf-metro-chip-active { background: #2a3a4a; color: #fff;
  border-color: #3a5a7a; }
.nfmm-@@sid .nf-metro-chip-dim { opacity: 0.45; }
.nfmm-@@sid .nf-metro-swatch { width: 14px; height: 3px; border-radius: 2px;
  flex-shrink: 0; }
.nfmm-@@sid .nf-metro-reset { position: absolute; top: 8px; left: 8px;
  background: rgba(22,22,22,0.88); color: #ccc; border: 1px solid #444;
  padding: 4px 10px; border-radius: 4px; font-size: 11px; cursor: pointer;
  display: none; font-family: inherit; }
.nfmm-@@sid.nf-metro-filtering .nf-metro-reset { display: block; }
.nfmm-@@sid.nf-metro-filtering [class^="metro-line-"] { opacity: 0.06;
  transition: opacity 0.2s; }
.nfmm-@@sid.nf-metro-filtering [class^="metro-line-"].nf-metro-line-active {
  opacity: 1; }
.nfmm-@@sid .nf-metro-hidden { display: none !important; }
.nfmm-@@sid .nf-metro-hint { position: absolute; bottom: 6px; right: 10px;
  font-size: 10px; color: #666; pointer-events: none; }
.nfmm-@@sid .nf-metro-tip { position: fixed; pointer-events: none; z-index: 9999;
  background: #1f1f1f; border: 1px solid #444; border-radius: 6px;
  padding: 8px 10px; font-size: 12px; max-width: 240px; color: #e8e8e8;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4); opacity: 0;
  transition: opacity 0.08s; }
.nfmm-@@sid .nf-metro-tip-show { opacity: 1; }
.nfmm-@@sid .nf-metro-tt-title { font-weight: 600; font-size: 13px;
  margin-bottom: 3px; }
.nfmm-@@sid .nf-metro-tt-section { color: #888; font-size: 11px;
  margin-bottom: 6px; }
.nfmm-@@sid .nf-metro-tt-line { display: flex; align-items: center;
  gap: 6px; margin-top: 2px; }
.nfmm-@@sid .nf-metro-tt-line .nf-metro-swatch { width: 14px; height: 3px; }
</style>
<div class="nf-metro-canvas">
@@svg
<button class="nf-metro-reset" type="button">Reset</button>
<div class="nf-metro-hint">drag pan / cmd+scroll zoom / click line to focus</div>
</div>
<div class="nf-metro-legend"></div>
<div class="nf-metro-tip"></div>
<script>
@@shared_js
(function() {
  var _root = document.currentScript.closest('.nfmm-@@sid');
  var _api = attachMetroMap({ root: _root, lines: @@lines_json, embed: null });
  _root.dispatchEvent(new CustomEvent(
    'nfmetro:ready', { detail: { api: _api }, bubbles: true }));
})();
</script>
</div>""")
