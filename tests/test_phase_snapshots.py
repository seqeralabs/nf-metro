"""Tests for per-phase coordinate snapshots (issue #363).

Snapshots are pure observation: enabling them must not perturb layout, and
re-rendering the same fixture twice must produce byte-identical snapshots.
The diff CLI must report no divergence for identical trees and localise the
first phase for a perturbed one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.snapshots import (
    capture_phase_snapshot,
    phase_snapshots_enabled,
    serialise_graph_coords,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURES = [
    "examples/rnaseq_sections.mmd",
    "examples/rnaseq_auto.mmd",
]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIFF_SCRIPT = _REPO_ROOT / "scripts" / "diff_phase_snapshots.py"


def _load_diff_module():
    spec = importlib.util.spec_from_file_location("diff_phase_snapshots", _DIFF_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _laid_out_graph(fixture: str):
    graph = parse_metro_mermaid((_REPO_ROOT / fixture).read_text())
    compute_layout(graph)
    return graph


def test_enable_flag_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NF_METRO_PHASE_SNAPSHOTS", raising=False)
    assert phase_snapshots_enabled() is False
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "1")
    assert phase_snapshots_enabled() is True
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "0")
    assert phase_snapshots_enabled() is False


def test_capture_is_noop_when_disabled(tmp_path: Path) -> None:
    graph = _laid_out_graph(FIXTURES[0])
    capture_phase_snapshot(graph, "final", enabled=False)
    # No files written, no directories created.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fixture", FIXTURES)
def test_snapshots_dont_perturb_layout(
    fixture: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling snapshots must leave final coords byte-identical."""
    monkeypatch.delenv("NF_METRO_PHASE_SNAPSHOTS", raising=False)
    plain = serialise_graph_coords(_laid_out_graph(fixture))

    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "1")
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOT_DIR", str(tmp_path))
    snapped = serialise_graph_coords(_laid_out_graph(fixture))

    assert plain == snapped


@pytest.mark.parametrize("fixture", FIXTURES)
def test_diff_reports_no_divergence_for_identical_runs(
    fixture: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "1")
    base = tmp_path / "base"
    pr = tmp_path / "pr"
    for root in (base, pr):
        monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOT_DIR", str(root))
        _laid_out_graph(fixture)

    fixtures = {p.name for p in base.iterdir()}
    assert len(fixtures) == 1
    slug = next(iter(fixtures))
    # A meaningful number of phases were captured.
    assert len(list((base / slug).glob("*.json"))) >= 30

    diff = _load_diff_module()
    assert diff.diff_fixture(base, pr, slug) is False


def test_diff_localises_first_divergent_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOTS", "1")
    base = tmp_path / "base"
    pr = tmp_path / "pr"
    for root in (base, pr):
        monkeypatch.setenv("NF_METRO_PHASE_SNAPSHOT_DIR", str(root))
        _laid_out_graph(FIXTURES[0])

    slug = next(iter(p.name for p in base.iterdir()))
    target = pr / slug / "6.4.json"
    data = json.loads(target.read_text())
    a_station = next(iter(data["stations"]))
    data["stations"][a_station]["y"] += 19.0
    target.write_text(json.dumps(data, indent=2, sort_keys=True))

    diff = _load_diff_module()
    assert diff.diff_fixture(base, pr, slug) is True
