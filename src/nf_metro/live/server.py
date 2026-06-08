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
import webbrowser
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

from nf_metro.live.mapping import stations_for_process
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
        # Set when the run reaches a terminal state (completed/error); cleared
        # when a fresh run starts. Lets the CLI auto-stop after a run finishes.
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


def build_page(model: MapModel) -> str:
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
  const es = new EventSource('/stream');
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


def make_handler(
    model: MapModel, state: ProgressState, token: str | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, code: int, body: str, ctype: str = "text/plain") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if token is None:
                return True
            qs = urllib.parse.urlparse(self.path).query
            given = urllib.parse.parse_qs(qs).get("token", [None])[0]
            return (given or self.headers.get("X-Metro-Token")) == token

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self._send(200, build_page(model), "text/html; charset=utf-8")
            elif path == "/state":
                self._send(200, json.dumps(state.snapshot()), "application/json")
            elif path == "/stream":
                self._stream()
            else:
                self._send(404, "not found")

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/events":
                self._send(404, "not found")
                return
            if not self._authorized():
                self._send(401, "unauthorized")
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                state.ingest(json.loads(raw or b"{}"))
            except (json.JSONDecodeError, ValueError):
                pass
            self._send(200, "ok")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = state.subscribe()
            try:
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.unsubscribe(q)

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
