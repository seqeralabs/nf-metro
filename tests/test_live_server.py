"""Tests for the live-progress server: state machine, page, and HTTP smoke."""

import json
import threading
import urllib.request
import warnings

import pytest

from nf_metro.layout import compute_layout
from nf_metro.live.server import MapModel, ProgressState, build_page, serve
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
