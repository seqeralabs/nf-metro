"""Tests for the live-progress server: state machine, page, and HTTP smoke."""

import json
import threading
import types
import urllib.request
import warnings

import pytest

from nf_metro.layout import compute_layout
from nf_metro.live import server as server_mod
from nf_metro.live.server import (
    DEFAULT_OVERLAY,
    OVERLAY_STYLES,
    MapModel,
    ProgressState,
    _is_light_bg,
    _weblog_command,
    build_page,
    run_lifecycle,
    serve,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.themes import THEMES

MMD = (
    "%%metro line: a | A | #ff0000 | solid\n"
    "%%metro process: trim | TRIMGALORE\n"
    "%%metro process: qc | FASTQC\n"
    "graph LR\n"
    "    input[In] -->|a| trim[Trim]\n"
    "    trim -->|a| qc[QC]\n"
)


def _graph():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(MMD)
    compute_layout(graph)
    return graph


def _model() -> MapModel:
    return MapModel(_graph(), THEMES["nfcore"])


def _ev(event, task_id, process, status=""):
    return {
        "event": event,
        "trace": {"task_id": task_id, "process": process, "status": status},
    }


def test_only_mapped_stations_are_overlaid():
    model = _model()
    ids = {st["id"] for st in model.stations}
    assert ids == {"trim", "qc"}  # 'input' has no process: directive


def test_state_machine_queued_running_done():
    state = ProgressState(_model())
    state.ingest({"event": "started", "runName": "r"})
    state.ingest(_ev("process_submitted", 1, "TRIMGALORE", "SUBMITTED"))
    state.ingest(_ev("process_submitted", 2, "TRIMGALORE", "SUBMITTED"))
    assert state.snapshot()["stations"]["trim"]["state"] == "queued"
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    assert state.snapshot()["stations"]["trim"]["state"] == "running"
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "COMPLETED"))
    state.ingest(_ev("process_completed", 2, "TRIMGALORE", "COMPLETED"))
    trim = state.snapshot()["stations"]["trim"]
    assert trim["state"] == "done" and trim["done"] == 2 and trim["total"] == 2


def test_completed_station_stays_done_for_rest_of_run():
    # A station that finishes must not "un-green" as other stations progress;
    # a successful run ends with every station that ran in the done state.
    state = ProgressState(_model())
    state.ingest({"event": "started", "runName": "r"})
    state.ingest(_ev("process_submitted", 1, "TRIMGALORE", "SUBMITTED"))
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "COMPLETED"))
    assert state.snapshot()["stations"]["trim"]["state"] == "done"

    # qc is submitted, runs, and completes; trim stays done throughout.
    state.ingest(_ev("process_submitted", 2, "FASTQC", "SUBMITTED"))
    assert state.snapshot()["stations"]["trim"]["state"] == "done"
    state.ingest(_ev("process_started", 2, "FASTQC", "RUNNING"))
    assert state.snapshot()["stations"]["trim"]["state"] == "done"
    state.ingest(_ev("process_completed", 2, "FASTQC", "COMPLETED"))
    snap = state.snapshot()["stations"]
    assert snap["trim"]["state"] == "done" and snap["qc"]["state"] == "done"


def test_only_a_new_run_resets_a_done_station():
    state = ProgressState(_model())
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "COMPLETED"))
    assert state.snapshot()["stations"]["trim"]["state"] == "done"
    state.ingest({"event": "started", "runName": "r2"})
    assert state.snapshot()["stations"]["trim"]["state"] == "pending"


def test_state_machine_failure():
    state = ProgressState(_model())
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "FAILED"))
    assert state.snapshot()["stations"]["trim"]["state"] == "failed"


def test_state_machine_matches_qualified_name():
    state = ProgressState(_model())
    state.ingest(_ev("process_started", 9, "NFCORE_X:Y:FASTQC", "RUNNING"))
    assert state.snapshot()["stations"]["qc"]["state"] == "running"


def test_started_resets_prior_run():
    state = ProgressState(_model())
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "COMPLETED"))
    state.ingest({"event": "started", "runName": "r2"})
    assert state.snapshot()["stations"]["trim"]["state"] == "pending"
    assert state.snapshot()["run"]["name"] == "r2"


def test_build_page_contains_halo_ids():
    page = build_page(_model())
    assert 'id="halo-trim"' in page and 'id="halo-qc"' in page
    assert 'id="halo-input"' not in page


def test_each_halo_carries_the_shared_overlay_marks():
    page = build_page(_model())
    # One DOM serves every style: a ripple, a ring, and a dot per station.
    for cls in ("ov-ripple", "ov-ring", "ov-dot"):
        assert page.count(f'class="{cls}"') == 2  # trim + qc


def test_overlay_marks_match_the_drawn_station_pill():
    import re

    from nf_metro.layout.routing.offsets import compute_station_offsets
    from nf_metro.render.svg import station_marker_box

    graph = _graph()
    offsets = compute_station_offsets(graph)
    model = MapModel(graph, THEMES["nfcore"])
    page = build_page(model)
    for st in model.stations:
        cx, cy, w, h, rx = station_marker_box(
            graph, THEMES["nfcore"], graph.stations[st["id"]], offsets
        )
        # The dot rect is the marker box exactly (within rounding).
        assert st["w"] == round(w, 1) and st["h"] == round(h, 1)
        m = re.search(
            rf'id="halo-{st["id"]}">.*?class="ov-dot" x="([\d.-]+)" y="([\d.-]+)" '
            rf'width="([\d.-]+)" height="([\d.-]+)"',
            page,
        )
        assert m, f"no ov-dot rect for {st['id']}"
        assert abs(float(m.group(3)) - round(w, 1)) < 0.2
        assert abs(float(m.group(4)) - round(h, 1)) < 0.2


def test_page_offers_every_overlay_style():
    page = build_page(_model())
    assert 'id="overlay-style"' in page
    for style in OVERLAY_STYLES:
        assert f'value="{style}"' in page


def test_default_overlay_is_selected_and_applied():
    page = build_page(_model())
    assert f'data-overlay="{DEFAULT_OVERLAY}"' in page
    assert f'<option value="{DEFAULT_OVERLAY}" selected>' in page


def test_explicit_overlay_drives_the_initial_style():
    page = build_page(_model(), overlay="pulse")
    assert 'data-overlay="pulse"' in page
    assert '<option value="pulse" selected>' in page
    assert "pulse" != DEFAULT_OVERLAY  # exercises the non-default path


def test_unknown_overlay_falls_back_to_default():
    page = build_page(_model(), overlay="bogus")
    assert f'data-overlay="{DEFAULT_OVERLAY}"' in page


def test_dark_theme_page_uses_dark_chrome():
    page = build_page(MapModel(_graph(), THEMES["nfcore"]))
    assert "color-scheme: dark" in page


def test_light_theme_page_uses_light_chrome():
    page = build_page(MapModel(_graph(), THEMES["seqera-light"]))
    assert "color-scheme: light" in page
    assert "--bg: #f8f9fa" in page


def test_is_light_bg_classifies_themes():
    assert _is_light_bg("#f8f9fa") is True
    assert _is_light_bg("none") is True
    assert _is_light_bg("#2b2b2b") is False


@pytest.mark.parametrize("token", [None, "sekret"])
def test_http_smoke(token):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(MMD)
    compute_layout(graph)
    httpd = serve(graph, THEMES["nfcore"], host="127.0.0.1", port=0, token=token)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/") as r:
            assert r.status == 200

        suffix = f"?token={token}" if token else ""
        body = json.dumps(_ev("process_started", 1, "TRIMGALORE", "RUNNING")).encode()
        req = urllib.request.Request(
            base + "/events" + suffix, data=body, method="POST"
        )
        with urllib.request.urlopen(req) as r:
            assert r.status == 200

        with urllib.request.urlopen(base + "/state") as r:
            snap = json.load(r)
        assert snap["stations"]["trim"]["state"] == "running"

        if token:
            bad = urllib.request.Request(base + "/events", data=b"{}", method="POST")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(bad)
            assert exc.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_ended_tracks_terminal_state():
    state = ProgressState(_model())
    assert not state.run_ended.is_set()
    state.ingest({"event": "completed"})
    assert state.run_ended.is_set()
    state.ingest({"event": "started", "runName": "r"})
    assert not state.run_ended.is_set()
    state.ingest({"event": "error"})
    assert state.run_ended.is_set()


def test_weblog_command_appends_when_absent():
    assert _weblog_command(["nextflow", "run", "x"], "http://h/events") == [
        "nextflow",
        "run",
        "x",
        "-with-weblog",
        "http://h/events",
    ]


def test_weblog_command_keeps_caller_supplied():
    cmd = ["nextflow", "run", "x", "-with-weblog", "http://other/events"]
    assert _weblog_command(cmd, "http://h/events") == cmd


class _FakeProc:
    last_cmd: list[str] | None = None

    def __init__(self, cmd):
        _FakeProc.last_cmd = cmd

    def wait(self):
        return 0

    def poll(self):
        return 0


def _fake_server():
    s = types.SimpleNamespace()
    s.state = types.SimpleNamespace(run_ended=threading.Event())
    s.serve_forever = lambda: None
    s.calls = []
    s.shutdown = lambda: s.calls.append("shutdown")
    s.server_close = lambda: s.calls.append("close")
    return s


def test_run_lifecycle_stops_after_complete(monkeypatch):
    monkeypatch.setattr(server_mod.time, "sleep", lambda s: None)
    srv = _fake_server()
    srv.state.run_ended.set()  # run already finished
    run_lifecycle(
        srv,
        "http://x/",
        "http://x/events",
        shutdown_after_complete=True,
        grace=0,
        echo=lambda m: None,
    )
    assert srv.calls == ["shutdown", "close"]


def test_run_lifecycle_launch_wires_weblog(monkeypatch):
    monkeypatch.setattr(server_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(server_mod.subprocess, "Popen", _FakeProc)
    srv = _fake_server()
    run_lifecycle(
        srv,
        "http://h/",
        "http://h/events",
        launch_cmd=("nextflow", "run", "x"),
        shutdown_after_complete=True,
        grace=0,
        echo=lambda m: None,
    )
    assert _FakeProc.last_cmd == [
        "nextflow",
        "run",
        "x",
        "-with-weblog",
        "http://h/events",
    ]
    assert srv.calls == ["shutdown", "close"]


def test_run_lifecycle_opens_browser(monkeypatch):
    monkeypatch.setattr(server_mod.time, "sleep", lambda s: None)
    opened = []
    monkeypatch.setattr(server_mod.webbrowser, "open", lambda u: opened.append(u))
    srv = _fake_server()
    srv.state.run_ended.set()
    run_lifecycle(
        srv,
        "http://page/",
        "http://page/events",
        shutdown_after_complete=True,
        grace=0,
        open_browser=True,
        echo=lambda m: None,
    )
    assert opened == ["http://page/"]


def test_serve_cli_wires_launch_and_prints_url(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from nf_metro import cli as cli_mod

    mmd = tmp_path / "m.mmd"
    mmd.write_text(
        "%%metro line: a | A | #ff0000 | solid\n"
        "%%metro process: x | FOO\n"
        "graph LR\n"
        "    a1[A] -->|a| x[X]\n"
    )
    calls = {}

    def fake_serve_map(graph, theme, host=None, port=None, token=None, overlay=None):
        calls["overlay"] = overlay
        return object()

    def fake_lifecycle(httpd, page_url, events_url, **kw):
        calls.update(page_url=page_url, events_url=events_url, **kw)

    monkeypatch.setattr("nf_metro.live.server.serve", fake_serve_map)
    monkeypatch.setattr("nf_metro.live.server.run_lifecycle", fake_lifecycle)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "serve",
            str(mmd),
            "--port",
            "9999",
            "--overlay",
            "ring",
            "--open",
            "--",
            "nextflow",
            "run",
            "p",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls["launch_cmd"] == ("nextflow", "run", "p")
    assert calls["open_browser"] is True
    assert calls["overlay"] == "ring"
    assert calls["page_url"] == "http://127.0.0.1:9999/"
    assert calls["events_url"] == "http://127.0.0.1:9999/events"
    assert "▶ Open: http://127.0.0.1:9999/" in result.output


def test_serve_rejects_unknown_overlay():
    from click.testing import CliRunner

    from nf_metro import cli as cli_mod

    result = CliRunner().invoke(cli_mod.cli, ["serve", "x.mmd", "--overlay", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output


def test_registry_register_and_listing():
    from nf_metro.live.server import RunRegistry

    reg = RunRegistry(THEMES["nfcore"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rid = reg.register(MMD, name="run-a")
    run = reg.get(rid)
    assert run is not None
    assert {st["id"] for st in run["model"].stations} == {"trim", "qc"}
    listing = reg.listing()
    assert listing[0]["id"] == rid and listing[0]["name"] == "run-a"
    assert reg.get("nope") is None


def test_multi_server_http_smoke():
    from nf_metro.live.server import serve_multi

    httpd = serve_multi(THEMES["nfcore"], host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # register a map
        req = urllib.request.Request(
            base + "/maps?name=smoke", data=MMD.encode(), method="POST"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with urllib.request.urlopen(req) as r:
                reg_resp = json.load(r)
        rid = reg_resp["id"]
        assert reg_resp["view"] == f"/r/{rid}/"

        # index lists it; run page has halos
        with urllib.request.urlopen(base + "/") as r:
            assert b"smoke" in r.read()
        with urllib.request.urlopen(base + f"/r/{rid}/") as r:
            assert b'id="halo-trim"' in r.read()

        # events for this run drive its state
        body = json.dumps(_ev("process_started", 1, "TRIMGALORE", "RUNNING")).encode()
        ereq = urllib.request.Request(
            base + f"/r/{rid}/events", data=body, method="POST"
        )
        with urllib.request.urlopen(ereq) as r:
            assert r.status == 200
        with urllib.request.urlopen(base + f"/r/{rid}/state") as r:
            snap = json.load(r)
        assert snap["stations"]["trim"]["state"] == "running"

        # unparseable input is handled leniently (empty run), never a 500
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            garbage = urllib.request.Request(
                base + "/maps", data=b"not a map", method="POST"
            )
            with urllib.request.urlopen(garbage) as r:
                assert r.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
