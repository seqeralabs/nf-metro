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
from nf_metro.live.mapping import stations_for_process
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render import render_svg
from nf_metro.render.style import Theme

SVG_DIM_RE = re.compile(r'<svg[^>]*?\bwidth="([\d.]+)"[^>]*?\bheight="([\d.]+)"')


class StationGeom(TypedDict):
    """An overlaid station: its id and its centre in SVG pixel space."""

    id: str
    x: float
    y: float


class MapModel:
    """Static map: SVG body, canvas size, overlay station geometry, mapping."""

    def __init__(self, graph: MetroGraph, theme: Theme) -> None:
        self.mapping = graph.process_mapping
        svg = render_svg(graph, theme)
        self.svg_body = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)

        dim = SVG_DIM_RE.search(svg)
        self.width = float(dim.group(1)) if dim else float(graph.width or 1000)
        self.height = float(dim.group(2)) if dim else float(graph.height or 600)

        self.stations: list[StationGeom] = [
            {"id": s.id, "x": round(s.x, 1), "y": round(s.y, 1)}
            for s in graph.stations.values()
            if not s.is_port and s.id in self.mapping
        ]

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


def build_page(model: MapModel, stream_url: str = "/stream") -> str:
    halos = "\n".join(
        f'<g class="halo pending" id="halo-{html.escape(st["id"])}">'
        f'<circle class="led" cx="{st["x"]}" cy="{st["y"]}" r="7"/>'
        f"</g>"
        for st in model.stations
    )
    overlay = (
        f'<svg class="overlay" width="{model.width}" height="{model.height}" '
        f'viewBox="0 0 {model.width} {model.height}" '
        f'xmlns="http://www.w3.org/2000/svg">{halos}</svg>'
    )
    return PAGE_TEMPLATE.format(
        width=model.width,
        height=model.height,
        base_svg=model.svg_body,
        overlay=overlay,
        stream_url=stream_url,
    )


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>nf-metro live</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background: #0b1021; color: #e6e9f0;
         font-family: -apple-system, Segoe UI, Roboto, sans-serif; }}
  header {{ display: flex; align-items: center; gap: 1.5rem;
           padding: 0.6rem 1rem; border-bottom: 1px solid #222a44; }}
  #run {{ font-size: 0.95rem; }}
  #run b {{ color: #8ea0c8; font-weight: 600; }}
  .legend {{ display: flex; gap: 1rem; margin-left: auto; font-size: 0.8rem; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 0.35rem; }}
  .legend i {{ width: 12px; height: 12px; border-radius: 50%;
              border: 2px solid; display: inline-block; }}
  .stage {{ overflow: auto; padding: 1rem; }}
  .wrap {{ position: relative; width: {width}px; height: {height}px; }}
  .wrap > svg {{ position: absolute; top: 0; left: 0; }}
  .overlay {{ pointer-events: none; }}
  .led {{ --c: #3a4a6b; fill: var(--c); transform-box: fill-box;
         transform-origin: center; transition: fill 0.3s, opacity 0.3s;
         filter: drop-shadow(0 0 4px var(--c)) drop-shadow(0 0 10px var(--c)); }}
  .halo.pending .led {{ --c: #2a3656; opacity: 0.45; }}
  .halo.queued  .led {{ --c: #ffb020; opacity: 0.7; }}
  .halo.running .led {{ --c: #ffc23a; animation: led-pulse 1.1s ease-in-out infinite; }}
  .halo.done    .led {{ --c: #2bee92; }}
  .halo.failed  .led {{ --c: #ff4d4d; animation: led-pulse 0.7s ease-in-out infinite; }}
  @keyframes led-pulse {{
    0%,100% {{ transform: scale(1);
      filter: drop-shadow(0 0 4px var(--c)) drop-shadow(0 0 9px var(--c)); }}
    50%     {{ transform: scale(1.35);
      filter: drop-shadow(0 0 7px var(--c)) drop-shadow(0 0 18px var(--c)); }}
  }}
</style>
</head>
<body>
<header>
  <div id="run">Run: <b id="run-name">waiting for events</b> &middot;
       <span id="run-state">idle</span></div>
  <div class="legend">
    <span><i style="border-color:#ffc23a"></i>running</span>
    <span><i style="border-color:#2bee92"></i>done</span>
    <span><i style="border-color:#ff4d4d"></i>failed</span>
  </div>
</header>
<div class="stage"><div class="wrap">
  {base_svg}
  {overlay}
</div></div>
<script>
  const es = new EventSource('{stream_url}');
  es.onmessage = (e) => {{
    const data = JSON.parse(e.data);
    document.getElementById('run-name').textContent =
        data.run.name || 'waiting for events';
    document.getElementById('run-state').textContent = data.run.state;
    for (const [sid, s] of Object.entries(data.stations)) {{
      const g = document.getElementById('halo-' + sid);
      if (g) g.setAttribute('class', 'halo ' + s.state);
    }}
  }};
</script>
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
    model: MapModel, state: ProgressState, token: str | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                _send_body(self, 200, build_page(model), "text/html; charset=utf-8")
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
) -> MetroServer:
    """Build the model and return a serving ``MetroServer``.

    The caller drives the server (``serve_forever``); separating construction
    makes the handler and state testable without binding a socket.
    """
    model = MapModel(graph, theme)
    state = ProgressState(model)
    httpd = MetroServer((host, port), make_handler(model, state, token))
    httpd.state = state
    return httpd


class RunRegistry:
    """A persistent server's set of live runs, keyed by a short id.

    A run is registered by POSTing its .mmd to ``/maps``; the registry parses
    and lays it out once, then holds a MapModel + ProgressState that the run's
    weblog events drive. Many pipelines can report into one server.
    """

    def __init__(self, theme: Theme, max_runs: int = 100) -> None:
        self.theme = theme
        self.max_runs = max_runs
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
    return INDEX_TEMPLATE.format(rows=rows)


INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="3"/>
<title>nf-metro live</title>
<style>
  body {{ margin: 0; background: #0b1021; color: #e6e9f0;
         font-family: -apple-system, Segoe UI, Roboto, sans-serif; }}
  header {{ padding: 0.8rem 1rem; border-bottom: 1px solid #222a44; font-weight: 600; }}
  .runs {{ display: flex; flex-direction: column; gap: 0.5rem; padding: 1rem;
          max-width: 720px; }}
  .run {{ display: flex; justify-content: space-between; align-items: center;
         padding: 0.7rem 1rem; border: 1px solid #222a44; border-radius: 8px;
         text-decoration: none; color: #e6e9f0; border-left-width: 4px; }}
  .run:hover {{ background: #141a30; }}
  .run.running {{ border-left-color: #ffc23a; }}
  .run.complete {{ border-left-color: #2bee92; }}
  .run.error {{ border-left-color: #ff4d4d; }}
  .run.idle {{ border-left-color: #3a4a6b; }}
  .name {{ font-weight: 600; }}
  .meta {{ font-size: 0.8rem; color: #8ea0c8; }}
  .empty {{ padding: 1rem; color: #8ea0c8; }}
</style>
</head>
<body>
<header>nf-metro live &middot; runs</header>
<div class="runs">
{rows}
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
                page = build_page(run["model"], stream_url=f"/r/{m.group(1)}/stream")
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
) -> ThreadingHTTPServer:
    """A persistent multi-run server: pipelines POST their .mmd to ``/maps``."""
    registry = RunRegistry(theme)
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
