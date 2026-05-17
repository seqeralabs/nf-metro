"""HTML wrapper around the SVG renderer for interactive metro maps.

Produces a single self-contained HTML file with:
- The metro SVG inlined (so it can be copied out as a static asset)
- Pan/zoom via viewBox manipulation (no external library)
- Legend panel: clicking a line filters to it and zooms to the bbox of the
  remaining visible elements; clicking again resets
- Embed buttons that copy iframe or inline-SVG snippets to the clipboard

This is a thin wrapper - it does not re-do layout or hit the network.
"""

from __future__ import annotations

import hashlib
import html
import json

from nf_metro.parser.model import MetroGraph
from nf_metro.render.style import Theme
from nf_metro.render.svg import render_svg


def render_html(
    graph: MetroGraph,
    theme: Theme,
    width: int | None = None,
    height: int | None = None,
    animate: bool = False,
    debug: bool = False,
    embed_basename: str = "metro_map.html",
) -> str:
    """Render the graph to an interactive standalone HTML page.

    Parameters
    ----------
    embed_basename:
        Filename used inside the "copy iframe snippet" button so the
        suggested embed code points at the user's chosen output path.
    """
    # Suppress the inline SVG legend - the HTML side panel replaces it
    # in interactive mode.  We restore the original setting afterwards
    # so the graph object is not mutated for callers downstream.
    original_legend_position = graph.legend_position
    graph.legend_position = "none"
    try:
        svg = render_svg(
            graph, theme, width=width, height=height, animate=animate, debug=debug
        )
    finally:
        graph.legend_position = original_legend_position

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

    # Build the inline snippet (self-contained HTML with scoped CSS+JS).
    # Hash the SVG so re-renders of the same input produce the same ID,
    # and so two different maps on the same host page do not collide.
    snippet_id = "m" + hashlib.sha1(svg.encode("utf-8")).hexdigest()[:8]
    inline_snippet = _build_inline_snippet(svg, lines, snippet_id)

    # When we embed the inline snippet inside the outer page's <script>
    # tag as a JS string literal, raw </script> sequences would terminate
    # the outer <script> element (HTML parser is greedy and doesn't care
    # about JS string context).  Escape </ as <\/ inside the JSON literal;
    # JSON decodes \/ back to /, so the snippet survives round-trip
    # through the clipboard unchanged.
    inline_snippet_json = json.dumps(inline_snippet).replace("</", "<\\/")

    return _TEMPLATE.format(
        title=html.escape(title),
        svg=svg,
        lines_json=json.dumps(lines),
        embed_basename=html.escape(embed_basename),
        inline_snippet_json=inline_snippet_json,
    )


def _build_inline_snippet(svg: str, lines: list[dict], sid: str) -> str:
    """Self-contained <div> snippet for pasting into any HTML host."""
    return _INLINE_TEMPLATE.format(
        sid=sid,
        svg=svg,
        lines_json=json.dumps(lines),
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --bg: #1d1d1d; --panel: #161616; --panel-border: #333;
    --text: #e8e8e8; --muted: #888; --hover: #232323; --active: #2a3a4a;
  }}
  html, body {{ margin: 0; padding: 0; height: 100%; background: var(--bg);
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                color: var(--text); overflow: hidden; }}
  #app {{ display: grid; grid-template-columns: 1fr 260px;
          grid-template-rows: 44px 1fr; height: 100vh; }}
  header {{ grid-column: 1 / span 2; display: flex; align-items: center;
            padding: 0 14px; background: var(--panel);
            border-bottom: 1px solid var(--panel-border); gap: 12px; }}
  header h1 {{ font-size: 14px; font-weight: 600; margin: 0; flex: 1;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  header .hint {{ font-size: 11px; color: var(--muted); }}
  .btn {{ background: transparent; color: #ccc; border: 1px solid #444;
          padding: 4px 10px; border-radius: 4px; font-size: 12px;
          cursor: pointer; font-family: inherit; }}
  .btn:hover {{ background: var(--hover); color: #fff; }}
  .btn.primary {{ border-color: #555; background: #2a2a2a; }}
  .btn.copied {{ background: #2e4a2e; color: #c8efc8; border-color: #4a8a4a; }}
  #map {{ position: relative; overflow: hidden; background: var(--bg);
          cursor: grab; }}
  #map.grabbing {{ cursor: grabbing; }}
  #map svg {{ width: 100% !important; height: 100% !important;
              display: block; user-select: none; }}
  #map .nf-metro-station {{ cursor: pointer; }}
  #map .nf-metro-station:hover {{ stroke-width: 3; }}
  #tooltip {{ position: fixed; pointer-events: none; z-index: 50;
              background: #1f1f1f; border: 1px solid #444; border-radius: 6px;
              padding: 8px 10px; font-size: 12px; max-width: 240px;
              box-shadow: 0 4px 12px rgba(0,0,0,0.4);
              opacity: 0; transition: opacity 0.08s; }}
  #tooltip.show {{ opacity: 1; }}
  #tooltip .tt-title {{ font-weight: 600; font-size: 13px; margin-bottom: 3px; }}
  #tooltip .tt-section {{ color: var(--muted); font-size: 11px;
                          margin-bottom: 6px; }}
  #tooltip .tt-line {{ display: flex; align-items: center; gap: 6px;
                       margin-top: 2px; }}
  #tooltip .tt-line .swatch {{ width: 14px; height: 3px; flex-shrink: 0; }}
  aside {{ background: var(--panel); border-left: 1px solid var(--panel-border);
           padding: 12px; overflow-y: auto; font-size: 13px; }}
  aside h2 {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
              color: var(--muted); margin: 0 0 6px; font-weight: 600; }}
  .line-row {{ display: flex; align-items: center; gap: 10px; padding: 6px 8px;
               border-radius: 4px; cursor: pointer; user-select: none;
               margin: 1px -8px; transition: background 0.1s; }}
  .line-row:hover {{ background: var(--hover); }}
  .line-row.active {{ background: var(--active); }}
  .line-row.dim {{ opacity: 0.35; }}
  .swatch {{ width: 18px; height: 3px; border-radius: 2px; flex-shrink: 0; }}
  .line-row span {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .footer-help {{ font-size: 11px; color: var(--muted); margin-top: 16px;
                  line-height: 1.6; }}
  .footer-help kbd {{ background: #2a2a2a; border: 1px solid #444; padding: 1px 5px;
                      border-radius: 3px; font-family: inherit; font-size: 10px; }}
  /* Filter dimming via the existing per-line classes on edge paths */
  #map.filtering [class^="metro-line-"] {{ opacity: 0.05;
                                            transition: opacity 0.2s; }}
  #map.filtering [class^="metro-line-"].is-active {{ opacity: 1; }}
  .hidden-by-filter {{ display: none !important; }}
  /* Modal */
  .modal-backdrop {{ position: fixed; inset: 0; background: rgba(0,0,0,0.6);
                     display: none; align-items: center; justify-content: center;
                     z-index: 100; }}
  .modal-backdrop.open {{ display: flex; }}
  .modal {{ background: #1f1f1f; border: 1px solid #444; border-radius: 8px;
            padding: 20px; max-width: 640px; width: 90%; }}
  .modal h3 {{ margin: 0 0 12px; font-size: 15px; }}
  .modal p {{ margin: 0 0 8px; font-size: 12px; color: var(--muted); }}
  .modal pre {{ background: #0d0d0d; border: 1px solid #333; border-radius: 4px;
                padding: 10px; font-size: 11px; overflow-x: auto;
                margin: 4px 0 12px; max-height: 200px; overflow-y: auto;
                white-space: pre-wrap; word-break: break-all; }}
  .modal .row {{ display: flex; gap: 8px; align-items: center;
                  margin-bottom: 4px; }}
  .modal .row label {{ font-size: 11px; color: var(--muted); flex: 1; }}
  .modal-close {{ float: right; }}
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>{title}</h1>
    <span class="hint">drag to pan, scroll to zoom, click a line to focus</span>
    <button class="btn" id="reset-btn">Reset</button>
    <button class="btn primary" id="embed-btn">Embed&hellip;</button>
  </header>
  <main id="map">
    {svg}
  </main>
  <div id="tooltip"></div>
  <aside>
    <h2>Lines</h2>
    <div id="line-list"></div>
    <div class="footer-help">
      <strong>Click</strong> a line to isolate it.<br>
      <strong>Click again</strong> or <kbd>Esc</kbd> to reset.<br>
      <strong>Drag</strong> to pan, <strong>scroll</strong> to zoom.
    </div>
  </aside>
</div>

<div class="modal-backdrop" id="embed-modal">
  <div class="modal">
    <button class="btn modal-close" id="embed-close">Close</button>
    <h3>Embed snippets</h3>
    <p><strong>Interactive HTML (inline).</strong> Self-contained snippet with
       scoped CSS and JS - paste into any HTML host (MkDocs, Confluence,
       Notion, blog templates) and it keeps full pan / zoom / line-filter.
       No iframe, no hosting required.</p>
    <div class="row">
      <label>inline HTML</label>
      <button class="btn" data-copy="inline">Copy</button>
    </div>
    <pre id="snippet-inline"></pre>
    <p><strong>Interactive (iframe).</strong> Host this HTML file and embed
       the iframe in any HTML host that allows iframes. GitHub READMEs strip
       iframes - link to a hosted page from the README instead.</p>
    <div class="row">
      <label>iframe</label>
      <button class="btn" data-copy="iframe">Copy</button>
    </div>
    <pre id="snippet-iframe"></pre>
    <p><strong>Static SVG.</strong> Inline this anywhere that accepts raw HTML
       (or save it as a .svg). No interactivity but it renders even where
       scripts are stripped.</p>
    <div class="row">
      <label>inline SVG</label>
      <button class="btn" data-copy="svg">Copy</button>
    </div>
    <pre id="snippet-svg"></pre>
  </div>
</div>

<script>
(() => {{
  const LINES = {lines_json};
  const EMBED_BASENAME = "{embed_basename}";

  const map = document.getElementById('map');
  const svg = map.querySelector('svg');
  if (!svg) return;

  // -- Pan & zoom via viewBox -------------------------------------------------
  // Capture the original viewBox so reset and zoom-to-region work after the
  // user has been panning around.
  const vb0 = svg.viewBox.baseVal;
  const initial = {{ x: vb0.x, y: vb0.y, w: vb0.width, h: vb0.height }};
  let current = {{ ...initial }};

  function setViewBox(x, y, w, h) {{
    current = {{ x, y, w, h }};
    svg.setAttribute('viewBox', `${{x}} ${{y}} ${{w}} ${{h}}`);
  }}

  function animateViewBox(target, duration = 350) {{
    const start = {{ ...current }};
    const t0 = performance.now();
    function frame(now) {{
      const t = Math.min(1, (now - t0) / duration);
      // ease-out cubic
      const k = 1 - Math.pow(1 - t, 3);
      setViewBox(
        start.x + (target.x - start.x) * k,
        start.y + (target.y - start.y) * k,
        start.w + (target.w - start.w) * k,
        start.h + (target.h - start.h) * k,
      );
      if (t < 1) requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }}

  // Drag-to-pan
  let dragging = null;
  map.addEventListener('mousedown', e => {{
    if (e.button !== 0) return;
    dragging = {{ x: e.clientX, y: e.clientY, vbX: current.x, vbY: current.y }};
    map.classList.add('grabbing');
    e.preventDefault();
  }});
  window.addEventListener('mousemove', e => {{
    if (!dragging) return;
    const rect = map.getBoundingClientRect();
    const scaleX = current.w / rect.width;
    const scaleY = current.h / rect.height;
    setViewBox(
      dragging.vbX - (e.clientX - dragging.x) * scaleX,
      dragging.vbY - (e.clientY - dragging.y) * scaleY,
      current.w, current.h,
    );
  }});
  window.addEventListener('mouseup', () => {{
    dragging = null;
    map.classList.remove('grabbing');
  }});

  // Wheel-to-zoom (centered on cursor position)
  map.addEventListener('wheel', e => {{
    e.preventDefault();
    const rect = map.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    const cursorX = current.x + current.w * px;
    const cursorY = current.y + current.h * py;
    const factor = Math.exp(e.deltaY * 0.0015);
    // Clamp zoom: don't allow zooming out beyond ~3x the initial view, or
    // in beyond ~30x.
    const newW = Math.min(initial.w * 3, Math.max(initial.w / 30, current.w * factor));
    const newH = Math.min(initial.h * 3, Math.max(initial.h / 30, current.h * factor));
    const newX = cursorX - (cursorX - current.x) * (newW / current.w);
    const newY = cursorY - (cursorY - current.y) * (newH / current.h);
    setViewBox(newX, newY, newW, newH);
  }}, {{ passive: false }});

  // -- Line filter ------------------------------------------------------------
  let activeLine = null;

  function lineSetOf(el, attr) {{
    const v = el.getAttribute(attr);
    return v ? new Set(v.split(',').filter(Boolean)) : new Set();
  }}

  function applyFilter() {{
    // Stations: hide if their line list doesn't include the active line
    svg.querySelectorAll('[data-station-id]').forEach(el => {{
      const lines = lineSetOf(el, 'data-station-lines');
      // Elements with only data-station-id (no -lines) are labels/icons - they
      // get hidden iff their station is hidden, computed below.
      if (el.hasAttribute('data-station-lines')) {{
        const hide = activeLine !== null && !lines.has(activeLine);
        el.classList.toggle('hidden-by-filter', hide);
      }}
    }});
    // Now hide labels/icons whose station was hidden
    const hiddenStations = new Set();
    svg.querySelectorAll('[data-station-lines].hidden-by-filter').forEach(el => {{
      hiddenStations.add(el.getAttribute('data-station-id'));
    }});
    svg.querySelectorAll('[data-station-id]:not([data-station-lines])').forEach(el => {{
      el.classList.toggle('hidden-by-filter',
        hiddenStations.has(el.getAttribute('data-station-id')));
    }});
    // Sections: hide if no overlapping lines
    svg.querySelectorAll('[data-section-id]').forEach(el => {{
      if (el.hasAttribute('data-section-lines')) {{
        const lines = lineSetOf(el, 'data-section-lines');
        const hide = activeLine !== null && !lines.has(activeLine);
        el.classList.toggle('hidden-by-filter', hide);
      }}
    }});
    const hiddenSections = new Set();
    svg.querySelectorAll('[data-section-lines].hidden-by-filter').forEach(el => {{
      hiddenSections.add(el.getAttribute('data-section-id'));
    }});
    // Section labels and number circles (data-section-id, no data-section-lines,
    // and not a station rect): show only if their section is still visible.
    svg.querySelectorAll(
      '[data-section-id]:not([data-section-lines]):not([data-station-lines])'
    ).forEach(el => {{
      el.classList.toggle('hidden-by-filter',
        hiddenSections.has(el.getAttribute('data-section-id')));
    }});
    // Edges: mark the active line's paths as is-active (CSS does the dimming)
    svg.querySelectorAll('[data-line-id]').forEach(el => {{
      el.classList.toggle('is-active',
        activeLine !== null && el.getAttribute('data-line-id') === activeLine);
    }});
    map.classList.toggle('filtering', activeLine !== null);
  }}

  function visibleBBox() {{
    // Compute the union bbox of every still-visible station and section.
    // We use SVG coordinates (getBBox is in the element's local coord space,
    // which for our elements equals the root SVG coord space since we don't
    // apply transforms).
    const els = [
      ...svg.querySelectorAll('[data-station-lines]:not(.hidden-by-filter)'),
      ...svg.querySelectorAll('[data-section-lines]:not(.hidden-by-filter)'),
    ];
    if (!els.length) return null;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const el of els) {{
      const b = el.getBBox();
      if (b.width === 0 && b.height === 0) continue;
      minX = Math.min(minX, b.x);
      minY = Math.min(minY, b.y);
      maxX = Math.max(maxX, b.x + b.width);
      maxY = Math.max(maxY, b.y + b.height);
    }}
    if (!isFinite(minX)) return null;
    return {{ x: minX, y: minY, w: maxX - minX, h: maxY - minY }};
  }}

  function zoomToVisible() {{
    const b = visibleBBox();
    if (!b) return;
    // Pad and preserve aspect ratio against the original viewBox so the
    // remaining content fills the canvas without squishing.
    const pad = 40;
    const targetW = b.w + pad * 2;
    const targetH = b.h + pad * 2;
    const aspect = initial.w / initial.h;
    let w = targetW, h = targetH;
    if (w / h > aspect) {{ h = w / aspect; }} else {{ w = h * aspect; }}
    const cx = b.x + b.w / 2;
    const cy = b.y + b.h / 2;
    animateViewBox({{ x: cx - w / 2, y: cy - h / 2, w, h }});
  }}

  function setActiveLine(id) {{
    activeLine = activeLine === id ? null : id;
    applyFilter();
    // Update legend visuals
    document.querySelectorAll('.line-row').forEach(row => {{
      const rid = row.dataset.lineId;
      row.classList.toggle('active', activeLine === rid);
      row.classList.toggle('dim', activeLine !== null && activeLine !== rid);
    }});
    if (activeLine === null) {{
      animateViewBox(initial);
    }} else {{
      zoomToVisible();
    }}
  }}

  function reset() {{
    activeLine = null;
    applyFilter();
    document.querySelectorAll('.line-row').forEach(row => {{
      row.classList.remove('active', 'dim');
    }});
    animateViewBox(initial);
  }}

  // -- Legend list ------------------------------------------------------------
  const list = document.getElementById('line-list');
  for (const ln of LINES) {{
    const row = document.createElement('div');
    row.className = 'line-row';
    row.dataset.lineId = ln.id;
    row.innerHTML = `<div class="swatch" style="background:${{ln.color}}"></div>
                     <span>${{ln.label}}</span>`;
    row.addEventListener('click', () => setActiveLine(ln.id));
    list.appendChild(row);
  }}

  document.getElementById('reset-btn').addEventListener('click', reset);
  window.addEventListener('keydown', e => {{ if (e.key === 'Escape') reset(); }});

  // -- Station hover tooltip --------------------------------------------------
  const linesById = new Map(LINES.map(l => [l.id, l]));
  const tip = document.getElementById('tooltip');
  function buildTip(rect) {{
    const label = rect.getAttribute('data-station-label') ||
                  rect.getAttribute('data-station-id');
    const section = rect.getAttribute('data-section-name') || '';
    const lineIds = (rect.getAttribute('data-station-lines') || '')
                      .split(',').filter(Boolean);
    const lines = lineIds.map(id => {{
      const ln = linesById.get(id);
      if (!ln) return '';
      return `<div class="tt-line">` +
        `<div class="swatch" style="background:${{ln.color}}"></div>` +
        `${{ln.label}}</div>`;
    }}).join('');
    return `<div class="tt-title">${{label}}</div>` +
           (section ? `<div class="tt-section">${{section}}</div>` : '') +
           lines;
  }}
  function positionTip(e) {{
    const pad = 14;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + tw > window.innerWidth) x = e.clientX - tw - pad;
    if (y + th > window.innerHeight) y = e.clientY - th - pad;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }}
  svg.querySelectorAll('.nf-metro-station').forEach(rect => {{
    rect.addEventListener('mouseenter', e => {{
      if (dragging) return;
      tip.innerHTML = buildTip(rect);
      tip.classList.add('show');
      positionTip(e);
    }});
    rect.addEventListener('mousemove', positionTip);
    rect.addEventListener('mouseleave', () => tip.classList.remove('show'));
  }});

  // -- Embed modal ------------------------------------------------------------
  // The inline-HTML snippet is baked into the page at render time (it has a
  // hashed wrapper class so it does not collide with this page or with any
  // other embed sharing the same host page).
  const INLINE_SNIPPET = {inline_snippet_json};
  const modal = document.getElementById('embed-modal');
  const snippetInline = document.getElementById('snippet-inline');
  const snippetIframe = document.getElementById('snippet-iframe');
  const snippetSvg = document.getElementById('snippet-svg');

  function openEmbed() {{
    snippetInline.textContent = INLINE_SNIPPET;
    snippetIframe.textContent =
      `<iframe src="${{EMBED_BASENAME}}" width="100%" height="640" ` +
      `style="border:0;border-radius:8px;" loading="lazy"></iframe>`;
    // Use the SVG markup from the document (the inlined copy), serialised.
    snippetSvg.textContent = new XMLSerializer().serializeToString(svg);
    modal.classList.add('open');
  }}
  document.getElementById('embed-btn').addEventListener('click', openEmbed);
  document.getElementById('embed-close').addEventListener('click',
    () => modal.classList.remove('open'));
  modal.addEventListener('click', e => {{
    if (e.target === modal) modal.classList.remove('open');
  }});
  const sourceByKey = {{
    inline: () => snippetInline,
    iframe: () => snippetIframe,
    svg: () => snippetSvg,
  }};
  modal.querySelectorAll('[data-copy]').forEach(btn => {{
    btn.addEventListener('click', async () => {{
      const src = sourceByKey[btn.dataset.copy];
      if (!src) return;
      const text = src().textContent;
      try {{
        await navigator.clipboard.writeText(text);
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => {{
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }}, 1400);
      }} catch (err) {{
        btn.textContent = 'Press Cmd+C';
      }}
    }});
  }});
}})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Inline embed snippet
# ----------------------------------------------------------------------------
# A self-contained <div> ... </div> with scoped CSS and an IIFE script.  The
# wrapper class name is uniquified per render so multiple snippets can coexist
# on a single host page.  All selectors are prefixed with .nfmm-{sid} so they
# don't leak.  The script uses document.currentScript.closest(...) to locate
# the wrapper, so the snippet is location-independent.
#
# UX differences from the full-page version:
# - No header / side panel; floating overlay legend in the corner.
# - Wheel-to-zoom requires Ctrl/Cmd so it doesn't hijack page scroll.
# - Reset button only appears when a filter is active.
_INLINE_TEMPLATE = """<div class="nfmm-embed nfmm-{sid}">
<style>
.nfmm-{sid} {{ display: block; width: 100%;
  background: #1d1d1d; border: 1px solid #2a2a2a; border-radius: 8px;
  overflow: hidden; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
  color: #e8e8e8; line-height: 1.4; }}
.nfmm-{sid} .nfmm-canvas {{ position: relative; }}
.nfmm-{sid} svg {{ display: block; width: 100%; height: auto;
  user-select: none; cursor: grab; }}
.nfmm-{sid}.nfmm-grabbing svg {{ cursor: grabbing; }}
.nfmm-{sid} .nf-metro-station {{ cursor: pointer; }}
.nfmm-{sid} .nf-metro-station:hover {{ stroke-width: 3; }}
.nfmm-{sid} .nfmm-tip {{ position: fixed; pointer-events: none; z-index: 9999;
  background: #1f1f1f; border: 1px solid #444; border-radius: 6px;
  padding: 8px 10px; font-size: 12px; max-width: 240px; color: #e8e8e8;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4); opacity: 0;
  transition: opacity 0.08s; }}
.nfmm-{sid} .nfmm-tip.nfmm-show {{ opacity: 1; }}
.nfmm-{sid} .nfmm-tt-title {{ font-weight: 600; font-size: 13px;
  margin-bottom: 3px; }}
.nfmm-{sid} .nfmm-tt-section {{ color: #888; font-size: 11px;
  margin-bottom: 6px; }}
.nfmm-{sid} .nfmm-tt-line {{ display: flex; align-items: center;
  gap: 6px; margin-top: 2px; }}
.nfmm-{sid} .nfmm-tt-line .nfmm-swatch {{ width: 14px; height: 3px;
  flex-shrink: 0; }}
.nfmm-{sid} .nfmm-legend {{ display: flex; flex-wrap: wrap; gap: 4px 6px;
  padding: 8px 10px; background: #161616; border-top: 1px solid #2a2a2a;
  font-size: 11px; }}
.nfmm-{sid} .nfmm-chip {{ display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 8px; border-radius: 12px; cursor: pointer;
  color: #ccc; user-select: none; white-space: nowrap;
  background: #1f1f1f; border: 1px solid #2f2f2f; }}
.nfmm-{sid} .nfmm-chip:hover {{ background: #2a2a2a; }}
.nfmm-{sid} .nfmm-chip.nfmm-active {{ background: #2a3a4a; color: #fff;
  border-color: #3a5a7a; }}
.nfmm-{sid} .nfmm-chip.nfmm-dim {{ opacity: 0.45; }}
.nfmm-{sid} .nfmm-swatch {{ width: 14px; height: 3px; border-radius: 2px;
  flex-shrink: 0; }}
.nfmm-{sid} .nfmm-reset {{ position: absolute; top: 8px; left: 8px;
  background: rgba(22,22,22,0.88); color: #ccc; border: 1px solid #444;
  padding: 4px 10px; border-radius: 4px; font-size: 11px; cursor: pointer;
  display: none; font-family: inherit; }}
.nfmm-{sid}.nfmm-filtering .nfmm-reset {{ display: block; }}
.nfmm-{sid}.nfmm-filtering [class^="metro-line-"] {{ opacity: 0.06;
  transition: opacity 0.2s; }}
.nfmm-{sid}.nfmm-filtering [class^="metro-line-"].nfmm-line-active {{ opacity: 1; }}
.nfmm-{sid} .nfmm-hidden {{ display: none !important; }}
.nfmm-{sid} .nfmm-hint {{ position: absolute; bottom: 6px; right: 10px;
  font-size: 10px; color: #666; pointer-events: none; }}
</style>
<div class="nfmm-canvas">
{svg}
<button class="nfmm-reset" type="button">Reset</button>
<div class="nfmm-hint">drag pan / cmd+scroll zoom / click line to focus</div>
</div>
<div class="nfmm-legend"></div>
<div class="nfmm-tip"></div>
<script>
(function() {{
  var script = document.currentScript;
  var root = script && script.closest('.nfmm-{sid}');
  if (!root) return;
  var svg = root.querySelector('svg');
  if (!svg) return;
  var LINES = {lines_json};

  var vb0 = svg.viewBox.baseVal;
  var initial = {{x: vb0.x, y: vb0.y, w: vb0.width, h: vb0.height}};
  var current = Object.assign({{}}, initial);

  function setVB(x, y, w, h) {{
    current = {{x: x, y: y, w: w, h: h}};
    svg.setAttribute('viewBox', x + ' ' + y + ' ' + w + ' ' + h);
  }}
  function animate(target, duration) {{
    var start = Object.assign({{}}, current);
    var t0 = performance.now();
    function frame(now) {{
      var t = Math.min(1, (now - t0) / (duration || 350));
      var k = 1 - Math.pow(1 - t, 3);
      setVB(start.x + (target.x - start.x) * k,
            start.y + (target.y - start.y) * k,
            start.w + (target.w - start.w) * k,
            start.h + (target.h - start.h) * k);
      if (t < 1) requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }}

  var canvas = root.querySelector('.nfmm-canvas');

  // Pan (bound to canvas so legend clicks don't trigger a phantom drag)
  var drag = null;
  canvas.addEventListener('mousedown', function(e) {{
    if (e.button !== 0) return;
    drag = {{x: e.clientX, y: e.clientY, vbX: current.x, vbY: current.y}};
    root.classList.add('nfmm-grabbing');
    e.preventDefault();
  }});
  window.addEventListener('mousemove', function(e) {{
    if (!drag) return;
    var rect = canvas.getBoundingClientRect();
    setVB(drag.vbX - (e.clientX - drag.x) * (current.w / rect.width),
          drag.vbY - (e.clientY - drag.y) * (current.h / rect.height),
          current.w, current.h);
  }});
  window.addEventListener('mouseup', function() {{
    drag = null; root.classList.remove('nfmm-grabbing');
  }});

  // Zoom (requires Ctrl/Cmd so we don't steal page scroll)
  canvas.addEventListener('wheel', function(e) {{
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var px = (e.clientX - rect.left) / rect.width;
    var py = (e.clientY - rect.top) / rect.height;
    var cx = current.x + current.w * px;
    var cy = current.y + current.h * py;
    var f = Math.exp(e.deltaY * 0.0015);
    var nw = Math.min(initial.w * 3, Math.max(initial.w / 30, current.w * f));
    var nh = Math.min(initial.h * 3, Math.max(initial.h / 30, current.h * f));
    setVB(cx - (cx - current.x) * (nw / current.w),
          cy - (cy - current.y) * (nh / current.h), nw, nh);
  }}, {{passive: false}});

  var active = null;
  function setOf(el, attr) {{
    var v = el.getAttribute(attr);
    return new Set(v ? v.split(',').filter(Boolean) : []);
  }}
  function apply() {{
    svg.querySelectorAll('[data-station-lines]').forEach(function(el) {{
      var s = setOf(el, 'data-station-lines');
      el.classList.toggle('nfmm-hidden', active !== null && !s.has(active));
    }});
    var hiddenSt = new Set();
    svg.querySelectorAll('[data-station-lines].nfmm-hidden').forEach(
      function(el) {{ hiddenSt.add(el.getAttribute('data-station-id')); }}
    );
    svg.querySelectorAll(
      '[data-station-id]:not([data-station-lines])'
    ).forEach(function(el) {{
      el.classList.toggle(
        'nfmm-hidden',
        hiddenSt.has(el.getAttribute('data-station-id'))
      );
    }});
    svg.querySelectorAll('[data-section-lines]').forEach(function(el) {{
      var s = setOf(el, 'data-section-lines');
      el.classList.toggle('nfmm-hidden', active !== null && !s.has(active));
    }});
    var hiddenSec = new Set();
    svg.querySelectorAll('[data-section-lines].nfmm-hidden').forEach(
      function(el) {{ hiddenSec.add(el.getAttribute('data-section-id')); }}
    );
    svg.querySelectorAll(
      '[data-section-id]:not([data-section-lines]):not([data-station-lines])'
    ).forEach(function(el) {{
      el.classList.toggle(
        'nfmm-hidden',
        hiddenSec.has(el.getAttribute('data-section-id'))
      );
    }});
    svg.querySelectorAll('[data-line-id]').forEach(function(el) {{
      el.classList.toggle('nfmm-line-active',
        active !== null && el.getAttribute('data-line-id') === active);
    }});
    root.classList.toggle('nfmm-filtering', active !== null);
  }}
  function zoomVisible() {{
    var els = []
      .concat(Array.from(svg.querySelectorAll('[data-station-lines]:not(.nfmm-hidden)')))
      .concat(Array.from(svg.querySelectorAll('[data-section-lines]:not(.nfmm-hidden)')));
    if (!els.length) return;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    els.forEach(function(el) {{
      var b = el.getBBox();
      if (b.width === 0 && b.height === 0) return;
      minX = Math.min(minX, b.x); minY = Math.min(minY, b.y);
      maxX = Math.max(maxX, b.x + b.width); maxY = Math.max(maxY, b.y + b.height);
    }});
    if (!isFinite(minX)) return;
    var pad = 40;
    var w = (maxX - minX) + pad * 2;
    var h = (maxY - minY) + pad * 2;
    var aspect = initial.w / initial.h;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    var cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    animate({{x: cx - w / 2, y: cy - h / 2, w: w, h: h}});
  }}
  function setActive(id) {{
    active = active === id ? null : id;
    apply();
    root.querySelectorAll('.nfmm-chip').forEach(function(c) {{
      var rid = c.getAttribute('data-line-id');
      c.classList.toggle('nfmm-active', active === rid);
      c.classList.toggle('nfmm-dim', active !== null && active !== rid);
    }});
    if (active === null) animate(initial); else zoomVisible();
  }}

  var legend = root.querySelector('.nfmm-legend');
  LINES.forEach(function(ln) {{
    var chip = document.createElement('div');
    chip.className = 'nfmm-chip';
    chip.setAttribute('data-line-id', ln.id);
    chip.innerHTML =
      '<div class="nfmm-swatch" style="background:' + ln.color + '"></div>' +
                     '<span>' + ln.label + '</span>';
    chip.addEventListener('click', function() {{ setActive(ln.id); }});
    legend.appendChild(chip);
  }});
  root.querySelector('.nfmm-reset').addEventListener('click', function() {{
    setActive(active);
  }});

  // Station hover tooltip
  var linesById = {{}};
  LINES.forEach(function(l) {{ linesById[l.id] = l; }});
  var tip = root.querySelector('.nfmm-tip');
  function buildTip(rect) {{
    var label = rect.getAttribute('data-station-label') ||
                rect.getAttribute('data-station-id');
    var section = rect.getAttribute('data-section-name') || '';
    var ids = (rect.getAttribute('data-station-lines') || '')
                .split(',').filter(Boolean);
    var lines = ids.map(function(id) {{
      var ln = linesById[id];
      if (!ln) return '';
      return '<div class="nfmm-tt-line"><div class="nfmm-swatch" ' +
             'style="background:' + ln.color + '"></div>' + ln.label + '</div>';
    }}).join('');
    return '<div class="nfmm-tt-title">' + label + '</div>' +
           (section ? '<div class="nfmm-tt-section">' + section + '</div>' : '') +
           lines;
  }}
  function positionTip(e) {{
    var pad = 14;
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = e.clientX + pad, y = e.clientY + pad;
    if (x + tw > window.innerWidth) x = e.clientX - tw - pad;
    if (y + th > window.innerHeight) y = e.clientY - th - pad;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }}
  svg.querySelectorAll('.nf-metro-station').forEach(function(rect) {{
    rect.addEventListener('mouseenter', function(e) {{
      if (drag) return;
      tip.innerHTML = buildTip(rect);
      tip.classList.add('nfmm-show');
      positionTip(e);
    }});
    rect.addEventListener('mousemove', positionTip);
    rect.addEventListener('mouseleave', function() {{
      tip.classList.remove('nfmm-show');
    }});
  }});
}})();
</script>
</div>"""
