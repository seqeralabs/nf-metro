"""HTML wrapper around the SVG renderer for interactive metro maps."""

from __future__ import annotations

import hashlib
import html
import json
from string import Template

from nf_metro.parser.model import MetroGraph
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
    animate: bool = False,
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
        shared_js=_SHARED_JS,
    )


def _build_inline_snippet(svg: str, lines: list[dict], sid: str) -> str:
    return _INLINE_TEMPLATE.substitute(
        sid=sid,
        svg=svg,
        lines_json=json.dumps(lines),
        shared_js=_SHARED_JS,
    )


# ----------------------------------------------------------------------------
# Shared JS (one IIFE used by both the standalone page and the embed snippet)
# ----------------------------------------------------------------------------
# `attachMetroMap(opts)` accepts:
#   root:      element wrapping everything (.nf-metro-canvas, legend, tip, etc.)
#   lines:     [{id, label, color, style}]
#   embed:     {basename, snippet} or null (standalone only)
# Class names are intentionally hard-coded so both templates share one CSS
# vocabulary; the embed snippet scopes the same names under .nfmm-<sid>.
_SHARED_JS = r"""
function attachMetroMap(opts) {
  const root = opts.root;
  const lines = opts.lines;
  const embed = opts.embed || null;
  const canvas = root.querySelector('.nf-metro-canvas');
  const svg = canvas.querySelector('svg');
  if (!svg) return;

  // ---- Pan + zoom via viewBox -----------------------------------------------
  const vb0 = svg.viewBox.baseVal;
  const initial = { x: vb0.x, y: vb0.y, w: vb0.width, h: vb0.height };
  let current = { ...initial };

  function setVB(x, y, w, h) {
    current = { x, y, w, h };
    svg.setAttribute('viewBox', `${x} ${y} ${w} ${h}`);
  }
  function animateVB(target, duration = 350) {
    const start = { ...current };
    const t0 = performance.now();
    function frame(now) {
      const t = Math.min(1, (now - t0) / duration);
      const k = 1 - Math.pow(1 - t, 3);
      setVB(
        start.x + (target.x - start.x) * k,
        start.y + (target.y - start.y) * k,
        start.w + (target.w - start.w) * k,
        start.h + (target.h - start.h) * k,
      );
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  let drag = null;
  canvas.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, vbX: current.x, vbY: current.y };
    root.classList.add('nf-metro-grabbing');
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    const rect = canvas.getBoundingClientRect();
    setVB(
      drag.vbX - (e.clientX - drag.x) * (current.w / rect.width),
      drag.vbY - (e.clientY - drag.y) * (current.h / rect.height),
      current.w, current.h,
    );
  });
  window.addEventListener('mouseup', () => {
    drag = null;
    root.classList.remove('nf-metro-grabbing');
  });

  // Embedded mode requires a modifier so we don't hijack page scroll.
  const requireModifier = embed === null;
  canvas.addEventListener('wheel', e => {
    if (requireModifier && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    const cx = current.x + current.w * px;
    const cy = current.y + current.h * py;
    const f = Math.exp(e.deltaY * 0.0015);
    const nw = Math.min(initial.w * 3, Math.max(initial.w / 30, current.w * f));
    const nh = Math.min(initial.h * 3, Math.max(initial.h / 30, current.h * f));
    setVB(
      cx - (cx - current.x) * (nw / current.w),
      cy - (cy - current.y) * (nh / current.h),
      nw, nh,
    );
  }, { passive: false });

  // ---- Filter ---------------------------------------------------------------
  let active = null;
  const setOf = (el, attr) => {
    const v = el.getAttribute(attr);
    return new Set(v ? v.split(',').filter(Boolean) : []);
  };
  const stationRects = svg.querySelectorAll('[data-station-lines]');
  const sectionBoxes = svg.querySelectorAll('[data-section-lines]');
  const stationDeps = svg.querySelectorAll(
    '[data-station-id]:not([data-station-lines])'
  );
  const sectionDeps = svg.querySelectorAll(
    '[data-section-id]:not([data-section-lines]):not([data-station-lines])'
  );
  const edges = svg.querySelectorAll('[data-line-id]');

  function applyFilter() {
    const hiddenSt = new Set();
    stationRects.forEach(el => {
      const hide = active !== null && !setOf(el, 'data-station-lines').has(active);
      el.classList.toggle('nf-metro-hidden', hide);
      if (hide) hiddenSt.add(el.getAttribute('data-station-id'));
    });
    stationDeps.forEach(el => {
      el.classList.toggle('nf-metro-hidden',
        hiddenSt.has(el.getAttribute('data-station-id')));
    });
    const hiddenSec = new Set();
    sectionBoxes.forEach(el => {
      const hide = active !== null && !setOf(el, 'data-section-lines').has(active);
      el.classList.toggle('nf-metro-hidden', hide);
      if (hide) hiddenSec.add(el.getAttribute('data-section-id'));
    });
    sectionDeps.forEach(el => {
      el.classList.toggle('nf-metro-hidden',
        hiddenSec.has(el.getAttribute('data-section-id')));
    });
    edges.forEach(el => {
      el.classList.toggle('nf-metro-line-active',
        active !== null && el.getAttribute('data-line-id') === active);
    });
    root.classList.toggle('nf-metro-filtering', active !== null);
  }

  function visibleBBox() {
    // Reading the rendered geometry attributes avoids forced layout from
    // getBBox(), which dominates filter-click time on large maps.
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const visit = el => {
      if (el.classList.contains('nf-metro-hidden')) return;
      const x = parseFloat(el.getAttribute('x') || el.getAttribute('cx') || 'NaN');
      const y = parseFloat(el.getAttribute('y') || el.getAttribute('cy') || 'NaN');
      const w = parseFloat(el.getAttribute('width') || '0');
      const h = parseFloat(el.getAttribute('height') || '0');
      if (Number.isNaN(x) || Number.isNaN(y)) return;
      minX = Math.min(minX, x); minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + w); maxY = Math.max(maxY, y + h);
    };
    stationRects.forEach(visit);
    sectionBoxes.forEach(visit);
    if (!isFinite(minX)) return null;
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  function zoomToVisible() {
    const b = visibleBBox();
    if (!b) return;
    const pad = 40;
    let w = b.w + pad * 2, h = b.h + pad * 2;
    const aspect = initial.w / initial.h;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    animateVB({
      x: b.x + b.w / 2 - w / 2,
      y: b.y + b.h / 2 - h / 2,
      w, h,
    });
  }

  function setActiveLine(id) {
    active = active === id ? null : id;
    applyFilter();
    root.querySelectorAll('.nf-metro-chip').forEach(c => {
      const rid = c.dataset.lineId;
      c.classList.toggle('nf-metro-chip-active', active === rid);
      c.classList.toggle('nf-metro-chip-dim', active !== null && active !== rid);
    });
    if (active === null) animateVB(initial); else zoomToVisible();
  }

  function reset() {
    active = null;
    applyFilter();
    root.querySelectorAll('.nf-metro-chip').forEach(c => {
      c.classList.remove('nf-metro-chip-active', 'nf-metro-chip-dim');
    });
    animateVB(initial);
  }

  // ---- Legend chips ---------------------------------------------------------
  const legend = root.querySelector('.nf-metro-legend');
  lines.forEach(ln => {
    const chip = document.createElement('div');
    chip.className = 'nf-metro-chip';
    chip.dataset.lineId = ln.id;
    chip.innerHTML =
      `<div class="nf-metro-swatch" style="background:${ln.color}"></div>` +
      `<span>${ln.label}</span>`;
    chip.addEventListener('click', () => setActiveLine(ln.id));
    legend.appendChild(chip);
  });
  root.querySelectorAll('.nf-metro-reset').forEach(b => {
    b.addEventListener('click', () => reset());
  });

  // ---- Tooltip --------------------------------------------------------------
  const linesById = new Map(lines.map(l => [l.id, l]));
  const tip = root.querySelector('.nf-metro-tip');
  function buildTip(rect) {
    const label = rect.getAttribute('data-station-label') ||
                  rect.getAttribute('data-station-id');
    const section = rect.getAttribute('data-section-name') || '';
    const ids = (rect.getAttribute('data-station-lines') || '')
                  .split(',').filter(Boolean);
    const lineHtml = ids.map(id => {
      const ln = linesById.get(id);
      if (!ln) return '';
      return `<div class="nf-metro-tt-line">` +
        `<div class="nf-metro-swatch" style="background:${ln.color}"></div>` +
        `${ln.label}</div>`;
    }).join('');
    return `<div class="nf-metro-tt-title">${label}</div>` +
      (section ? `<div class="nf-metro-tt-section">${section}</div>` : '') +
      lineHtml;
  }
  function positionTip(e) {
    const pad = 14;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + tip.offsetWidth > window.innerWidth) {
      x = e.clientX - tip.offsetWidth - pad;
    }
    if (y + tip.offsetHeight > window.innerHeight) {
      y = e.clientY - tip.offsetHeight - pad;
    }
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  // Event delegation: one listener for every station, cheap at any scale.
  canvas.addEventListener('mouseover', e => {
    const rect = e.target.closest('.nf-metro-station');
    if (!rect || drag) return;
    tip.innerHTML = buildTip(rect);
    tip.classList.add('nf-metro-tip-show');
    positionTip(e);
  });
  canvas.addEventListener('mousemove', e => {
    if (tip.classList.contains('nf-metro-tip-show')) positionTip(e);
  });
  canvas.addEventListener('mouseout', e => {
    const rect = e.target.closest('.nf-metro-station');
    const to = e.relatedTarget;
    if (rect && (!to || !to.closest || !to.closest('.nf-metro-station'))) {
      tip.classList.remove('nf-metro-tip-show');
    }
  });

  window.addEventListener('keydown', e => { if (e.key === 'Escape') reset(); });

  // ---- Embed modal (standalone only) ----------------------------------------
  if (embed) {
    const modal = root.querySelector('.nf-metro-embed-modal');
    const sIframe = root.querySelector('.nf-metro-snippet-iframe');
    const sSvg = root.querySelector('.nf-metro-snippet-svg');
    const sInline = root.querySelector('.nf-metro-snippet-inline');
    function openModal() {
      sInline.textContent = embed.snippet;
      sIframe.textContent =
        `<iframe src="${embed.basename}" width="100%" height="640" ` +
        `style="border:0;border-radius:8px;" loading="lazy"></iframe>`;
      sSvg.textContent = new XMLSerializer().serializeToString(svg);
      modal.classList.add('nf-metro-modal-open');
    }
    root.querySelector('.nf-metro-embed-btn').addEventListener('click', openModal);
    root.querySelector('.nf-metro-modal-close').addEventListener('click',
      () => modal.classList.remove('nf-metro-modal-open'));
    modal.addEventListener('click', e => {
      if (e.target === modal) modal.classList.remove('nf-metro-modal-open');
    });
    const srcBy = { inline: sInline, iframe: sIframe, svg: sSvg };
    modal.querySelectorAll('[data-copy]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const text = srcBy[btn.dataset.copy].textContent;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = 'Copied!';
          btn.classList.add('nf-metro-copied');
          setTimeout(() => {
            btn.textContent = 'Copy';
            btn.classList.remove('nf-metro-copied');
          }, 1400);
        } catch (err) {
          btn.textContent = 'Press Cmd+C';
        }
      });
    });
  }
}
"""


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
attachMetroMap({
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
attachMetroMap({
  root: document.currentScript.closest('.nfmm-@@sid'),
  lines: @@lines_json,
  embed: null,
});
</script>
</div>""")
