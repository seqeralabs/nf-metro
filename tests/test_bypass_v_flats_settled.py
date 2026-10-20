"""Bypass-V flats and label clearance hold on the graph prepare_graph ships.

``prepare_graph`` lays a map out more than once (the geometric-bypass pass
re-lays with helper stations, then settles once more), and every pass runs the
label-strike clearance loop.  The loop's verdicts must hold for the geometry
that is finally drawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.phases.spacing import (
    _bypass_v_collapsed_flat_gaps,
    _label_clearance_issues,
)

ROOT = Path(__file__).resolve().parents[1]

FIXTURES = (
    "tests/data/bypass_label_obstacles/relayout_bypass_v_flats.mmd",
    "examples/topologies/bypass_label_rake.mmd",
    "examples/topologies/bypass_label_rake_left.mmd",
    "examples/topologies/bypass_label_rake_wide.mmd",
    "examples/topologies/bypass_v_tight.mmd",
    "examples/topologies/fold_bypass_creep.mmd",
)


@pytest.mark.parametrize("relative_path", FIXTURES)
def test_settled_bypass_v_flats_and_labels_are_clear(relative_path: str) -> None:
    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    assert any(st.bypasses_station_id for st in graph.stations.values())
    assert _bypass_v_collapsed_flat_gaps(graph) == set()
    struck, collinear, flat_gaps = _label_clearance_issues(graph)
    assert (struck, collinear, flat_gaps) == (set(), False, set())
