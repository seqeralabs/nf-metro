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
    MapModel,
    ProgressState,
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


def _model() -> MapModel:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(MMD)
    compute_layout(graph)
    return MapModel(graph, THEMES["nfcore"])


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

    def fake_serve_map(graph, theme, host=None, port=None, token=None):
        return object()

    def fake_lifecycle(httpd, page_url, events_url, **kw):
        calls.update(page_url=page_url, events_url=events_url, **kw)

    monkeypatch.setattr("nf_metro.live.server.serve", fake_serve_map)
    monkeypatch.setattr("nf_metro.live.server.run_lifecycle", fake_lifecycle)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["serve", str(mmd), "--port", "9999", "--open", "--", "nextflow", "run", "p"],
    )
    assert result.exit_code == 0, result.output
    assert calls["launch_cmd"] == ("nextflow", "run", "p")
    assert calls["open_browser"] is True
    assert calls["page_url"] == "http://127.0.0.1:9999/"
    assert calls["events_url"] == "http://127.0.0.1:9999/events"
    assert "▶ Open: http://127.0.0.1:9999/" in result.output


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
