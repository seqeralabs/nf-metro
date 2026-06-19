"""Versioned embed driver for interactive nf-metro SVG maps.

``get_driver_js()`` returns the ``attachMetroMap`` function source, which both
the standalone HTML page and the inline embed snippet inline verbatim so the
two output paths share one implementation.

``attachMetroMap(opts)`` returns a public API object:
``highlightLine``, ``clearHighlight``, ``getManifest``, ``selectNode``, ``reset``.

See ``docs/embed.md`` for the full contract, CSS class names, and integration
examples.
"""

from __future__ import annotations

DRIVER_CONTRACT_VERSION = "1.0"


def get_driver_js() -> str:
    """Return the embed driver JS source string."""
    return _DRIVER_JS


_DRIVER_JS = r"""
function attachMetroMap(opts) {
  const root = opts.root;
  const lines = opts.lines;
  const embed = opts.embed || null;
  const canvas = root.querySelector('.nf-metro-canvas');
  const svg = canvas.querySelector('svg');
  if (!svg) return {};

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

  // ---- Line filter ----------------------------------------------------------
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
  const allStationEls = svg.querySelectorAll('[data-station-id]');

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

  function updateChips() {
    root.querySelectorAll('.nf-metro-chip').forEach(c => {
      const rid = c.dataset.lineId;
      c.classList.toggle('nf-metro-chip-active', active === rid);
      c.classList.toggle('nf-metro-chip-dim', active !== null && active !== rid);
    });
  }

  function setActiveLine(id) {
    active = active === id ? null : id;
    applyFilter();
    updateChips();
    if (active === null) animateVB(initial); else zoomToVisible();
  }

  // ---- Station selection (selectNode API) -----------------------------------
  let activeStations = null;

  function applyStationSelection() {
    allStationEls.forEach(el => {
      const sid = el.getAttribute('data-station-id');
      if (activeStations === null) {
        el.classList.remove('nf-metro-station-selected', 'nf-metro-station-dim');
      } else {
        const match = activeStations.has(sid);
        el.classList.toggle('nf-metro-station-dim', !match);
        el.classList.toggle('nf-metro-station-selected',
          match && el.hasAttribute('data-station-lines'));
      }
    });
    root.classList.toggle('nf-metro-selecting', activeStations !== null);
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
    b.addEventListener('click', () => clearHighlight());
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

  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') clearHighlight();
  });

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

  // ---- Public API -----------------------------------------------------------

  let _manifestCache;
  function getManifest() {
    if (_manifestCache !== undefined) return _manifestCache;
    const el = svg.querySelector('#diagram-manifest');
    if (!el) return (_manifestCache = null);
    try { return (_manifestCache = JSON.parse(el.textContent)); }
    catch { return (_manifestCache = null); }
  }

  function highlightLine(id) {
    active = id;
    applyFilter();
    updateChips();
    zoomToVisible();
  }

  function selectNode(processName) {
    const manifest = getManifest();
    if (!manifest) return;
    const ids = new Set();
    for (const node of (manifest.nodes || [])) {
      for (const pat of (node.patterns || [])) {
        try {
          if (new RegExp(pat, 'i').test(processName)) { ids.add(node.id); break; }
        } catch { /* skip invalid pattern */ }
      }
    }
    activeStations = ids.size ? ids : null;
    applyStationSelection();
  }

  function clearHighlight() {
    active = null;
    activeStations = null;
    applyFilter();
    applyStationSelection();
    root.querySelectorAll('.nf-metro-chip').forEach(c => {
      c.classList.remove('nf-metro-chip-active', 'nf-metro-chip-dim');
    });
    animateVB(initial);
  }

  return {
    highlightLine, clearHighlight, getManifest, selectNode, reset: clearHighlight,
  };
}
"""
