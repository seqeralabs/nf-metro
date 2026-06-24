"""Live-progress HTTP/SSE server.

Renders a metro map once, then lights up its stations from the event stream a
Nextflow run posts via ``-with-weblog http://HOST:PORT/events``. The layout is
computed once and a transparent status overlay is drawn on top, so the map
never re-flows as state changes. Standard library only.
"""

from __future__ import annotations

import html
import json
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

from nf_metro.layout import compute_layout
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.routing.reversal import tb_positive_fan_sections
from nf_metro.live.mapping import stations_for_process
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render import render_svg
from nf_metro.render.style import Theme
from nf_metro.render.svg import station_marker_box

SVG_DIM_RE = re.compile(r'<svg[^>]*?\bwidth="([\d.]+)"[^>]*?\bheight="([\d.]+)"')

# Overlay styles, in the order they appear in the page's style picker. Each is a
# self-contained look painted on the same status overlay; the run only ever
# reports states (pending/queued/running/done/failed), so switching style is
# purely a client-side CSS swap and the map never re-renders.
OVERLAY_STYLES: tuple[str, ...] = ("ring", "pulse", "dot", "led")
OVERLAY_LABELS: dict[str, str] = {
    "ring": "Ring",
    "pulse": "Pulse",
    "dot": "Status dot",
    "led": "Neon LED",
}
DEFAULT_OVERLAY = "ring"


def _is_light_bg(color: str) -> bool:
    """Whether a theme's background reads as light, so the page chrome can match.

    Transparent backgrounds (``none``) are treated as light: those themes are
    drawn for placement on a light host page.
    """
    c = color.strip().lower()
    if c in ("", "none", "transparent"):
        return True
    if c.startswith("#") and len(c) in (4, 7):
        if len(c) == 4:
            c = "#" + "".join(ch * 2 for ch in c[1:])
        r, g, b = (int(c[i : i + 2], 16) for i in (1, 3, 5))
        return 0.299 * r + 0.587 * g + 0.114 * b > 140
    return False


# Page chrome and status palette, per page scheme. Light values are tuned for a
# Seqera Platform / light-mode host (e.g. embedding in a light dashboard).
_CHROME: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0b1021",
        "fg": "#e6e9f0",
        "muted": "#8ea0c8",
        "border": "#222a44",
        "hover": "#141a30",
        "field-bg": "#141a30",
        "field-border": "#2a3656",
    },
    "light": {
        "bg": "#f8f9fa",
        "fg": "#242424",
        "muted": "#6c757d",
        "border": "#dee2e6",
        "hover": "#f1f3f5",
        "field-bg": "#ffffff",
        "field-border": "#ced4da",
    },
}
_STATE_COLORS: dict[str, dict[str, str]] = {
    "dark": {
        "pending": "#3a4a6b",
        "queued": "#ffb020",
        "running": "#ffc23a",
        "done": "#2bee92",
        "failed": "#ff4d4d",
    },
    "light": {
        "pending": "#adb5bd",
        "queued": "#f59f00",
        "running": "#f08c00",
        "done": "#2f9e44",
        "failed": "#e03131",
    },
}


class StationGeom(TypedDict):
    """An overlaid station: its id and its drawn marker box in SVG pixel space.

    ``x``/``y`` are the marker centre; ``w``/``h``/``rx`` describe the pill so an
    overlay mark takes the same shape (a circle for one line, a capsule spanning
    the bundle for several).
    """

    id: str
    x: float
    y: float
    w: float
    h: float
    rx: float


class MapModel:
    """Static map: SVG body, canvas size, overlay station geometry, mapping."""

    def __init__(self, graph: MetroGraph, theme: Theme) -> None:
        self.mapping = graph.process_mapping
        self.is_light = _is_light_bg(theme.background_color)
        svg = render_svg(graph, theme)
        self.svg_body = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)

        dim = SVG_DIM_RE.search(svg)
        self.width = float(dim.group(1)) if dim else float(graph.width or 1000)
        self.height = float(dim.group(2)) if dim else float(graph.height or 600)

        offsets = compute_station_offsets(graph)
        positive_fan = tb_positive_fan_sections(graph)
        self.stations: list[StationGeom] = []
        for s in graph.stations.values():
            if s.is_port or s.id not in self.mapping:
                continue
            cx, cy, w, h, rx = station_marker_box(
                graph, theme, s, offsets, positive_fan
            )
            self.stations.append(
                {
                    "id": s.id,
                    "x": round(cx, 1),
                    "y": round(cy, 1),
                    "w": round(w, 1),
                    "h": round(h, 1),
                    "rx": round(rx, 1),
                }
            )

    def stations_for_process(self, process: str) -> list[str]:
        return stations_for_process(process, self.mapping)


class ProgressState:
    """Aggregates Nextflow task events into per-station display state."""

    def __init__(self, model: MapModel) -> None:
        self.model = model
        self.lock = threading.Lock()
        self.run: dict[str, str | None] = {"name": None, "state": "idle"}
        self.tasks: dict[str, dict[str, set[int]]] = {
            st["id"]: {
                "submitted": set(),
                "running": set(),
                "done": set(),
                "failed": set(),
            }
            for st in model.stations
        }
        self.subscribers: set[queue.Queue[str]] = set()
        # Lets the CLI auto-stop after a run reaches a terminal state.
        self.run_ended = threading.Event()

    def _station_state(self, t: dict[str, set[int]]) -> tuple[str, int, int]:
        total = len(t["submitted"]) or (len(t["done"]) + len(t["failed"]))
        done = len(t["done"])
        if t["failed"]:
            return "failed", done, total
        if t["running"]:
            return "running", done, total
        terminal = t["done"] | t["failed"]
        if t["submitted"] and not t["submitted"] <= terminal:
            return "queued", done, total
        if done:
            return "done", done, total
        return "pending", done, total

    def snapshot(self) -> dict[str, Any]:
        # Locked: reads the task-id sets that ingest() mutates from other
        # handler threads, which would otherwise race set iteration.
        with self.lock:
            stations = {}
            for sid, t in self.tasks.items():
                state, done, total = self._station_state(t)
                stations[sid] = {"state": state, "done": done, "total": total}
            return {"run": dict(self.run), "stations": stations}

    def ingest(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        trace = payload.get("trace") or {}
        with self.lock:
            if event == "started":
                self.run = {"name": payload.get("runName"), "state": "running"}
                for t in self.tasks.values():
                    for s in t.values():
                        s.clear()
                self.run_ended.clear()
            elif event in ("completed", "error"):
                self.run["state"] = "error" if event == "error" else "complete"
                self.run_ended.set()
            else:
                self._ingest_task(event, trace)
        self._broadcast()

    def _ingest_task(self, event: str | None, trace: dict[str, Any]) -> None:
        process = str(trace.get("process", ""))
        try:
            tid = int(trace.get("task_id", -1))
        except (TypeError, ValueError):
            tid = -1
        status = str(trace.get("status", "")).upper()
        for sid in self.model.stations_for_process(process):
            t = self.tasks[sid]
            if event == "process_submitted":
                t["submitted"].add(tid)
            elif event == "process_started":
                t["submitted"].add(tid)
                t["running"].add(tid)
            elif event == "process_completed":
                t["running"].discard(tid)
                if status == "FAILED":
                    t["failed"].add(tid)
                else:
                    t["done"].add(tid)

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue()
        with self.lock:
            self.subscribers.add(q)
        q.put(json.dumps(self.snapshot()))
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self.lock:
            self.subscribers.discard(q)

    def _broadcast(self) -> None:
        msg = json.dumps(self.snapshot())
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            q.put(msg)


# How far the ring style's outline sits outside the station's own marker.
_RING_GAP = 3.5


def _ov_rect(cls: str, bx: float, by: float, bw: float, bh: float, brx: float) -> str:
    return (
        f'<rect class="{cls}" x="{round(bx, 1)}" y="{round(by, 1)}" '
        f'width="{round(bw, 1)}" height="{round(bh, 1)}" '
        f'rx="{round(brx, 1)}" ry="{round(brx, 1)}"/>'
    )


def _halo_svg(st: StationGeom) -> str:
    """The status overlay for one station: ripple, ring, and dot marks.

    Each mark takes the station's own pill shape (circle / capsule) and size, so
    a dot fills the marker and a ring hugs it. The ring is inflated by
    ``_RING_GAP`` so it sits just outside the drawn marker.
    """
    w, h, rx = st["w"], st["h"], st["rx"]
    x, y = st["x"] - w / 2, st["y"] - h / 2
    g = _RING_GAP
    return (
        f'<g class="halo pending" id="halo-{html.escape(st["id"])}">'
        + _ov_rect("ov-ripple", x, y, w, h, rx)
        + _ov_rect("ov-ring", x - g, y - g, w + 2 * g, h + 2 * g, rx + g)
        + _ov_rect("ov-dot", x, y, w, h, rx)
        + "</g>"
    )


def _root_vars(scheme: str) -> str:
    """The ``:root`` custom properties driving page chrome and status colours."""
    lines = [f"  color-scheme: {scheme};"]
    lines += [f"  --{k}: {v};" for k, v in _CHROME[scheme].items()]
    lines += [f"  --st-{k}: {v};" for k, v in _STATE_COLORS[scheme].items()]
    return ":root {\n" + "\n".join(lines) + "\n}\n"


def build_page(
    model: MapModel, stream_url: str = "/stream", overlay: str = DEFAULT_OVERLAY
) -> str:
    if overlay not in OVERLAY_STYLES:
        overlay = DEFAULT_OVERLAY
    halos = "\n".join(_halo_svg(st) for st in model.stations)
    overlay_svg = (
        f'<svg class="overlay" width="{model.width}" height="{model.height}" '
        f'viewBox="0 0 {model.width} {model.height}" '
        f'xmlns="http://www.w3.org/2000/svg">{halos}</svg>'
    )
    scheme = "light" if model.is_light else "dark"
    css = _root_vars(scheme) + _LAYOUT_CSS + _OVERLAY_CSS
    options = "".join(
        f'<option value="{v}"{" selected" if v == overlay else ""}>'
        f"{html.escape(OVERLAY_LABELS[v])}</option>"
        for v in OVERLAY_STYLES
    )
    script = _SCRIPT.replace("%STREAM_URL%", stream_url)
    return (
        PAGE_TEMPLATE.replace("%CSS%", css)
        .replace("%OPTIONS%", options)
        .replace("%OVERLAY%", overlay)
        .replace("%WIDTH%", str(model.width))
        .replace("%HEIGHT%", str(model.height))
        .replace("%SCRIPT%", script)
        .replace("%BASE_SVG%", model.svg_body)
        .replace("%OVERLAY_SVG%", overlay_svg)
    )


_LAYOUT_CSS = """
  body { margin: 0; background: var(--bg); color: var(--fg);
         font-family: -apple-system, "Segoe UI", Roboto, Inter, sans-serif; }
  header { display: flex; align-items: center; gap: 1.25rem;
           padding: 0.6rem 1rem; border-bottom: 1px solid var(--border); }
  #run { font-size: 0.95rem; }
  #run b { color: var(--muted); font-weight: 600; }
  .controls { display: flex; align-items: center; gap: 1.25rem; margin-left: auto; }
  .style-picker { display: inline-flex; align-items: center; gap: 0.4rem;
                  font-size: 0.8rem; color: var(--muted); }
  .style-picker select { font: inherit; color: var(--fg); background: var(--field-bg);
         border: 1px solid var(--field-border); border-radius: 6px;
         padding: 0.15rem 0.4rem; }
  .legend { display: flex; gap: 1rem; font-size: 0.8rem; }
  .legend span { display: inline-flex; align-items: center; gap: 0.35rem; }
  .legend i { width: 12px; height: 12px; border-radius: 50%;
              border: 2px solid; display: inline-block; }
  .stage { overflow: auto; padding: 1rem; }
  .wrap { position: relative; }
  .wrap > svg { position: absolute; top: 0; left: 0; }
"""

# Every style paints the same overlay DOM (a ripple, a ring, and a dot per
# station), each shaped like the station's own marker. A style opts in to the
# sub-marks it uses; the halo group carries the current status colour in --c,
# which the picked style reads. The running animations are shape-agnostic (they
# scale, breathe, or march the dash) so they read correctly on a capsule as well
# as a circle.
_OVERLAY_CSS = """
  .overlay { pointer-events: none; }
  .overlay rect { transform-box: fill-box; transform-origin: center; }
  .ov-dot, .ov-ring, .ov-ripple { transition: fill .3s, stroke .3s, opacity .3s;
         display: none; }
  .halo.pending { --c: var(--st-pending); }
  .halo.queued  { --c: var(--st-queued); }
  .halo.running { --c: var(--st-running); }
  .halo.done    { --c: var(--st-done); }
  .halo.failed  { --c: var(--st-failed); }

  /* ring: an outline hugging the station; the dash marches around while running */
  [data-overlay="ring"] .ov-ring {
         display: inline; fill: none; stroke: var(--c); stroke-width: 3.5;
         stroke-linecap: round; }
  [data-overlay="ring"] .halo.pending .ov-ring { opacity: .35; }
  [data-overlay="ring"] .halo.queued  .ov-ring { opacity: .6; stroke-dasharray: 2 6; }
  [data-overlay="ring"] .halo.running .ov-ring {
         stroke-dasharray: 11 9; animation: ov-march .7s linear infinite; }

  /* pulse: a crisp status dot with a radar ripple while active */
  [data-overlay="pulse"] .ov-dot { display: inline; fill: var(--c); }
  [data-overlay="pulse"] .halo.pending .ov-dot { opacity: .3; }
  [data-overlay="pulse"] .halo.queued  .ov-dot { opacity: .65; }
  [data-overlay="pulse"] .halo.running .ov-ripple,
  [data-overlay="pulse"] .halo.failed  .ov-ripple {
         display: inline; fill: var(--c); animation: ov-ripple 1.6s ease-out infinite; }

  /* dot: a flat status dot that breathes while running, no glow */
  [data-overlay="dot"] .ov-dot { display: inline; fill: var(--c); }
  [data-overlay="dot"] .halo.pending .ov-dot { opacity: .3; }
  [data-overlay="dot"] .halo.queued  .ov-dot { opacity: .6; }
  [data-overlay="dot"] .halo.running .ov-dot {
         animation: ov-breathe 1.5s ease-in-out infinite; }

  /* led: the original neon glow */
  [data-overlay="led"] .ov-dot { display: inline; fill: var(--c);
         filter: drop-shadow(0 0 4px var(--c)) drop-shadow(0 0 10px var(--c)); }
  [data-overlay="led"] .halo.pending .ov-dot { opacity: .45; }
  [data-overlay="led"] .halo.queued  .ov-dot { opacity: .7; }
  [data-overlay="led"] .halo.running .ov-dot {
         animation: ov-led-pulse 1.1s ease-in-out infinite; }
  [data-overlay="led"] .halo.failed  .ov-dot {
         animation: ov-led-pulse .7s ease-in-out infinite; }

  @keyframes ov-march { to { stroke-dashoffset: -20; } }
  @keyframes ov-ripple { from { transform: scale(1); opacity: .55; }
                         to   { transform: scale(3.2); opacity: 0; } }
  @keyframes ov-breathe { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  @keyframes ov-led-pulse {
    0%,100% { transform: scale(1);
      filter: drop-shadow(0 0 4px var(--c)) drop-shadow(0 0 9px var(--c)); }
    50%     { transform: scale(1.35);
      filter: drop-shadow(0 0 7px var(--c)) drop-shadow(0 0 18px var(--c)); }
  }

  @media (prefers-reduced-motion: reduce) {
    .ov-dot, .ov-ring, .ov-ripple { animation: none !important; }
    [data-overlay="pulse"] .halo.running .ov-ripple,
    [data-overlay="pulse"] .halo.failed  .ov-ripple { display: none; }
  }
"""

_SCRIPT = """<script>
  const wrap = document.querySelector('.wrap');
  const picker = document.getElementById('overlay-style');
  const STORE = 'nf-metro-overlay';
  const saved = localStorage.getItem(STORE);
  if (saved && [...picker.options].some(o => o.value === saved)) {
    picker.value = saved;
    wrap.setAttribute('data-overlay', saved);
  }
  picker.addEventListener('change', () => {
    wrap.setAttribute('data-overlay', picker.value);
    localStorage.setItem(STORE, picker.value);
  });
  const es = new EventSource('%STREAM_URL%');
  es.onmessage = (e) => {
    const data = JSON.parse(e.data);
    document.getElementById('run-name').textContent =
        data.run.name || 'waiting for events';
    document.getElementById('run-state').textContent = data.run.state;
    for (const [sid, s] of Object.entries(data.stations)) {
      const g = document.getElementById('halo-' + sid);
      if (g) g.setAttribute('class', 'halo ' + s.state);
    }
  };
</script>"""

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>nf-metro live</title>
<style>
%CSS%
</style>
</head>
<body>
<header>
  <div id="run">Run: <b id="run-name">waiting for events</b> &middot;
       <span id="run-state">idle</span></div>
  <div class="controls">
    <label class="style-picker">Style
      <select id="overlay-style">%OPTIONS%</select>
    </label>
    <div class="legend">
      <span><i style="border-color:var(--st-running)"></i>running</span>
      <span><i style="border-color:var(--st-done)"></i>done</span>
      <span><i style="border-color:var(--st-failed)"></i>failed</span>
    </div>
  </div>
</header>
<div class="stage"><div class="wrap" data-overlay="%OVERLAY%"
     style="width:%WIDTH%px;height:%HEIGHT%px">
  %BASE_SVG%
  %OVERLAY_SVG%
</div></div>
%SCRIPT%
</body>
</html>"""


class _QuietHandler(BaseHTTPRequestHandler):
    """BaseHTTPRequestHandler that doesn't log every request to stderr."""

    def log_message(self, *args: object) -> None:
        pass


def _send_body(
    handler: BaseHTTPRequestHandler, code: int, body: str, ctype: str = "text/plain"
) -> None:
    data = body.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _token_ok(handler: BaseHTTPRequestHandler, token: str | None) -> bool:
    if token is None:
        return True
    qs = urllib.parse.urlparse(handler.path).query
    given = urllib.parse.parse_qs(qs).get("token", [None])[0]
    return (given or handler.headers.get("X-Metro-Token")) == token


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        length = 0
    return handler.rfile.read(length) if length else b""


def _sse_response(handler: BaseHTTPRequestHandler, state: ProgressState) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    q = state.subscribe()
    try:
        while True:
            try:
                msg = q.get(timeout=15)
                handler.wfile.write(f"data: {msg}\n\n".encode())
            except queue.Empty:
                handler.wfile.write(b": ping\n\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        state.unsubscribe(q)


def make_handler(
    model: MapModel,
    state: ProgressState,
    token: str | None,
    overlay: str = DEFAULT_OVERLAY,
) -> type[BaseHTTPRequestHandler]:
    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                page = build_page(model, overlay=overlay)
                _send_body(self, 200, page, "text/html; charset=utf-8")
            elif path == "/state":
                _send_body(self, 200, json.dumps(state.snapshot()), "application/json")
            elif path == "/stream":
                _sse_response(self, state)
            else:
                _send_body(self, 404, "not found")

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/events":
                _send_body(self, 404, "not found")
                return
            if not _token_ok(self, token):
                _send_body(self, 401, "unauthorized")
                return
            try:
                state.ingest(json.loads(_read_body(self) or b"{}"))
            except (json.JSONDecodeError, ValueError):
                pass
            _send_body(self, 200, "ok")

    return Handler


class MetroServer(ThreadingHTTPServer):
    """A serving HTTP server that carries its ProgressState.

    Exposing ``state`` lets :func:`run_lifecycle` wait on the run's terminal
    event to auto-stop, without a side channel.
    """

    state: ProgressState


def serve(
    graph: MetroGraph,
    theme: Theme,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
    overlay: str = DEFAULT_OVERLAY,
) -> MetroServer:
    """Build the model and return a serving ``MetroServer``.

    The caller drives the server (``serve_forever``); separating construction
    makes the handler and state testable without binding a socket. ``overlay``
    is the status-overlay style shown until a viewer picks another in the page.
    """
    model = MapModel(graph, theme)
    state = ProgressState(model)
    httpd = MetroServer((host, port), make_handler(model, state, token, overlay))
    httpd.state = state
    return httpd


class RunRegistry:
    """A persistent server's set of live runs, keyed by a short id.

    A run is registered by POSTing its .mmd to ``/maps``; the registry parses
    and lays it out once, then holds a MapModel + ProgressState that the run's
    weblog events drive. Many pipelines can report into one server.
    """

    def __init__(
        self, theme: Theme, max_runs: int = 100, overlay: str = DEFAULT_OVERLAY
    ) -> None:
        self.theme = theme
        self.max_runs = max_runs
        self.overlay = overlay if overlay in OVERLAY_STYLES else DEFAULT_OVERLAY
        self.is_light = _is_light_bg(theme.background_color)
        self.lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}

    def register(self, mmd_text: str, name: str | None) -> str:
        graph = parse_metro_mermaid(mmd_text)
        compute_layout(graph)
        model = MapModel(graph, self.theme)
        rid = uuid.uuid4().hex[:8]
        with self.lock:
            self.runs[rid] = {
                "model": model,
                "state": ProgressState(model),
                "name": name or rid,
                "created": time.time(),
            }
            # Bound memory on a long-lived server: drop the oldest runs.
            while len(self.runs) > self.max_runs:
                del self.runs[min(self.runs, key=lambda k: self.runs[k]["created"])]
        return rid

    def get(self, rid: str) -> dict[str, Any] | None:
        with self.lock:
            return self.runs.get(rid)

    def listing(self) -> list[dict[str, Any]]:
        with self.lock:
            runs = list(self.runs.items())
        out = []
        for rid, r in runs:
            snap = r["state"].snapshot()
            out.append(
                {
                    "id": rid,
                    "name": r["name"],
                    "created": r["created"],
                    "run_state": snap["run"]["state"],
                    "done": sum(
                        1 for v in snap["stations"].values() if v["state"] == "done"
                    ),
                    "total": len(snap["stations"]),
                }
            )
        out.sort(key=lambda r: r["created"], reverse=True)
        return out


def build_index(registry: RunRegistry) -> str:
    rows = "\n".join(
        f'<a class="run {html.escape(r["run_state"])}" href="/r/{r["id"]}/">'
        f'<span class="name">{html.escape(r["name"])}</span>'
        f'<span class="meta">{html.escape(r["run_state"])} &middot; '
        f"{r['done']}/{r['total']} done</span></a>"
        for r in registry.listing()
    )
    if not rows:
        rows = '<p class="empty">No runs yet. Point a pipeline at this server.</p>'
    scheme = "light" if registry.is_light else "dark"
    return INDEX_TEMPLATE.replace("%CSS%", _root_vars(scheme) + _INDEX_CSS).replace(
        "%ROWS%", rows
    )


_INDEX_CSS = """
  body { margin: 0; background: var(--bg); color: var(--fg);
         font-family: -apple-system, "Segoe UI", Roboto, Inter, sans-serif; }
  header { padding: 0.8rem 1rem; border-bottom: 1px solid var(--border);
           font-weight: 600; }
  .runs { display: flex; flex-direction: column; gap: 0.5rem; padding: 1rem;
          max-width: 720px; }
  .run { display: flex; justify-content: space-between; align-items: center;
         padding: 0.7rem 1rem; border: 1px solid var(--border); border-radius: 8px;
         text-decoration: none; color: var(--fg); border-left-width: 4px; }
  .run:hover { background: var(--hover); }
  .run.running  { border-left-color: var(--st-running); }
  .run.complete { border-left-color: var(--st-done); }
  .run.error    { border-left-color: var(--st-failed); }
  .run.idle     { border-left-color: var(--st-pending); }
  .name { font-weight: 600; }
  .meta { font-size: 0.8rem; color: var(--muted); }
  .empty { padding: 1rem; color: var(--muted); }
"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="3"/>
<title>nf-metro live</title>
<style>
%CSS%
</style>
</head>
<body>
<header>nf-metro live &middot; runs</header>
<div class="runs">
%ROWS%
</div>
</body>
</html>"""


def make_multi_handler(
    registry: RunRegistry, token: str | None
) -> type[BaseHTTPRequestHandler]:
    """Handler for the persistent multi-run server.

    Routes: ``GET /`` index, ``POST /maps`` register a run, and per-run
    ``GET /r/<id>/`` page, ``GET /r/<id>/state``, ``GET /r/<id>/stream``,
    ``POST /r/<id>/events``.
    """
    run_re = re.compile(r"^/r/([0-9a-f]+)/(state|stream|events)?$")

    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                _send_body(self, 200, build_index(registry), "text/html; charset=utf-8")
                return
            m = run_re.match(path)
            if not m:
                _send_body(self, 404, "not found")
                return
            run = registry.get(m.group(1))
            if run is None:
                _send_body(self, 404, "unknown run")
                return
            kind = m.group(2)
            state: ProgressState = run["state"]
            if kind is None:
                page = build_page(
                    run["model"],
                    stream_url=f"/r/{m.group(1)}/stream",
                    overlay=registry.overlay,
                )
                _send_body(self, 200, page, "text/html; charset=utf-8")
            elif kind == "state":
                _send_body(self, 200, json.dumps(state.snapshot()), "application/json")
            elif kind == "stream":
                _sse_response(self, state)
            else:
                _send_body(self, 404, "not found")

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if not _token_ok(self, token):
                _send_body(self, 401, "unauthorized")
                return
            raw = _read_body(self)

            if path == "/maps":
                name = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                ).get("name", [None])[0]
                try:
                    rid = registry.register(raw.decode("utf-8"), name)
                except Exception as exc:  # parse/layout failure -> 400, never 500
                    _send_body(self, 400, f"bad map: {exc}")
                    return
                body = json.dumps(
                    {"id": rid, "view": f"/r/{rid}/", "events": f"/r/{rid}/events"}
                )
                _send_body(self, 200, body, "application/json")
                return

            m = run_re.match(path)
            if m and m.group(2) == "events":
                run = registry.get(m.group(1))
                if run is None:
                    _send_body(self, 404, "unknown run")
                    return
                try:
                    run["state"].ingest(json.loads(raw or b"{}"))
                except (json.JSONDecodeError, ValueError):
                    pass
                _send_body(self, 200, "ok")
                return

            _send_body(self, 404, "not found")

    return Handler


def serve_multi(
    theme: Theme,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
    overlay: str = DEFAULT_OVERLAY,
) -> ThreadingHTTPServer:
    """A persistent multi-run server: pipelines POST their .mmd to ``/maps``."""
    registry = RunRegistry(theme, overlay=overlay)
    return ThreadingHTTPServer((host, port), make_multi_handler(registry, token))


def _weblog_command(launch_cmd: Sequence[str], events_url: str) -> list[str]:
    """The launch command with ``-with-weblog <events_url>`` appended.

    Left untouched if the caller already passed their own ``-with-weblog``.
    """
    cmd = list(launch_cmd)
    if "-with-weblog" not in cmd:
        cmd += ["-with-weblog", events_url]
    return cmd


def run_lifecycle(
    httpd: MetroServer,
    page_url: str,
    events_url: str,
    *,
    launch_cmd: Sequence[str] = (),
    shutdown_after_complete: bool = False,
    grace: float = 10.0,
    open_browser: bool = False,
    echo: Callable[[str], None] = print,
) -> None:
    """Run the server and tie its lifetime to the workflow.

    Serves in a background thread. When ``launch_cmd`` is given it runs that
    command (a ``nextflow run ...``) with the weblog wired up and waits for it
    to exit; otherwise the run is launched separately. With
    ``shutdown_after_complete`` the server stops ``grace`` seconds after the
    run's terminal event (or after the launched command exits); otherwise it
    serves until interrupted. Always tears the server down on the way out.
    """
    if open_browser:
        webbrowser.open(page_url)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    proc: subprocess.Popen[bytes] | None = None
    try:
        if launch_cmd:
            proc = subprocess.Popen(_weblog_command(launch_cmd, events_url))
            proc.wait()
            echo(f"\nWorkflow finished. Map still live at {page_url}")
        if shutdown_after_complete:
            if not launch_cmd:
                httpd.state.run_ended.wait()
            echo(f"Shutting down in {grace:g}s ...")
            time.sleep(grace)
        else:
            echo("Press Ctrl-C to stop.")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        echo("\nStopping.")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        httpd.shutdown()
        httpd.server_close()
