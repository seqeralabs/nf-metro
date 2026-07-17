"""Schema conformance for the live progress snapshot.

Mirrors ``tests/test_manifest.py``: the server's actually-emitted
``ProgressState.snapshot()`` output is validated against the shipped JSON
Schema (``nf_metro/live/state_schema.json``) at every stage of a run, so the
schema cannot silently drift from what the server really produces.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
from test_live_server import _ev, _model

from nf_metro.live import STATE_SCHEMA_VERSION, state_schema
from nf_metro.live.server import ProgressState


def _assert_valid(snapshot: dict[str, Any]) -> None:
    jsonschema.validate(snapshot, state_schema())


def test_state_schema_has_expected_version() -> None:
    schema = state_schema()
    assert schema["required"] == ["run", "stations"]
    assert STATE_SCHEMA_VERSION == "1.0"


def test_initial_snapshot_validates() -> None:
    state = ProgressState(_model())
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["run"] == {"name": None, "state": "idle"}
    assert snap["stations"]["trim"] == {"state": "pending", "done": 0, "total": 0}


def test_run_lifecycle_snapshots_validate() -> None:
    state = ProgressState(_model())
    state.ingest({"event": "started", "runName": "r1"})
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["run"] == {"name": "r1", "state": "running"}

    state.ingest({"event": "completed"})
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["run"]["state"] == "complete"

    state.ingest({"event": "started", "runName": "r2"})
    state.ingest({"event": "error"})
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["run"] == {"name": "r2", "state": "error"}


@pytest.mark.parametrize(
    "events,expected",
    [
        pytest.param([], {"state": "pending", "done": 0, "total": 0}, id="pending"),
        pytest.param(
            [("process_submitted", 1, "TRIMGALORE", "SUBMITTED")],
            {"state": "queued", "done": 0, "total": 1},
            id="queued",
        ),
        pytest.param(
            [
                ("process_submitted", 1, "TRIMGALORE", "SUBMITTED"),
                ("process_started", 1, "TRIMGALORE", "RUNNING"),
            ],
            {"state": "running", "done": 0, "total": 1},
            id="running",
        ),
        pytest.param(
            [
                ("process_submitted", 1, "TRIMGALORE", "SUBMITTED"),
                ("process_started", 1, "TRIMGALORE", "RUNNING"),
                ("process_completed", 1, "TRIMGALORE", "COMPLETED"),
            ],
            {"state": "done", "done": 1, "total": 1},
            id="done",
        ),
        pytest.param(
            [
                ("process_submitted", 1, "TRIMGALORE", "SUBMITTED"),
                ("process_started", 1, "TRIMGALORE", "RUNNING"),
                ("process_completed", 1, "TRIMGALORE", "FAILED"),
            ],
            {"state": "failed", "done": 0, "total": 1},
            id="failed",
        ),
    ],
)
def test_every_display_state_validates(
    events: list[tuple[str, int, str, str]], expected: dict[str, Any]
) -> None:
    state = ProgressState(_model())
    for event, task_id, process, status in events:
        state.ingest(_ev(event, task_id, process, status))
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["stations"]["trim"] == expected


def test_failed_state_is_sticky_and_keeps_validating() -> None:
    """A station that ever fails stays failed even once a later task for it
    runs and completes successfully - the sticky-failure rule the schema
    documents must hold for the snapshot to keep validating too."""
    state = ProgressState(_model())
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "FAILED"))
    state.ingest(_ev("process_submitted", 2, "TRIMGALORE", "SUBMITTED"))
    state.ingest(_ev("process_started", 2, "TRIMGALORE", "RUNNING"))
    state.ingest(_ev("process_completed", 2, "TRIMGALORE", "COMPLETED"))
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["stations"]["trim"]["state"] == "failed"


def test_multi_station_snapshot_validates() -> None:
    state = ProgressState(_model())
    state.ingest({"event": "started", "runName": "r"})
    state.ingest(_ev("process_submitted", 1, "TRIMGALORE", "SUBMITTED"))
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    state.ingest(_ev("process_completed", 2, "FASTQC", "COMPLETED"))
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["stations"]["trim"]["state"] == "running"
    assert snap["stations"]["qc"]["state"] == "done"


def test_new_run_reset_snapshot_validates() -> None:
    state = ProgressState(_model())
    state.ingest(_ev("process_completed", 1, "TRIMGALORE", "COMPLETED"))
    _assert_valid(state.snapshot())
    state.ingest({"event": "started", "runName": "r2"})
    snap = state.snapshot()
    _assert_valid(snap)
    assert snap["stations"]["trim"] == {"state": "pending", "done": 0, "total": 0}


def test_subscribe_snapshot_validates() -> None:
    """The snapshot pushed immediately to a new SSE subscriber also conforms."""
    state = ProgressState(_model())
    state.ingest(_ev("process_started", 1, "TRIMGALORE", "RUNNING"))
    q = state.subscribe()
    pushed = json.loads(q.get_nowait())
    _assert_valid(pushed)
    assert pushed["stations"]["trim"]["state"] == "running"
