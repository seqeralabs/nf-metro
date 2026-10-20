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
from nf_metro.layout.engine import _bypass_label_rakes
from nf_metro.layout.phases.spacing import (
    _bypass_label_obstacles,
    _bypass_v_collapsed_flat_gaps,
    _label_clearance_issues,
)
from nf_metro.parser.model import MetroGraph

ROOT = Path(__file__).resolve().parents[1]
DATA = "tests/data/bypass_label_obstacles"

FIXTURES = (
    f"{DATA}/relayout_bypass_v_flats.mmd",
    f"{DATA}/obstacle_box_label_side.mmd",
    f"{DATA}/bypass_v_runout_into_exit_port.mmd",
    f"{DATA}/seated_rake_lever_kept.mmd",
    "examples/topologies/bypass_label_rake.mmd",
    "examples/topologies/bypass_label_rake_left.mmd",
    "examples/topologies/bypass_label_rake_wide.mmd",
    "examples/topologies/bypass_v_tight.mmd",
    "examples/topologies/fold_bypass_creep.mmd",
)

# The flat levers these maps ask for raise a collinear overlay alongside the
# strike levers, so their flats stay short and the strike levers alone have to
# clear every label.
FLAT_LEVER_CONFLICTS = (
    f"{DATA}/flat_lever_collinear_retry.mmd",
    f"{DATA}/flat_lever_collinear_retry_single.mmd",
)


def _settled(relative_path: str) -> MetroGraph:
    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    assert any(st.bypasses_station_id for st in graph.stations.values())
    return graph


@pytest.mark.parametrize("relative_path", FIXTURES)
def test_settled_bypass_v_flats_and_labels_are_clear(relative_path: str) -> None:
    graph = _settled(relative_path)

    assert graph.bypass_label_obstacles == _bypass_label_obstacles(graph)
    assert _bypass_v_collapsed_flat_gaps(graph) == set()
    struck, collinear, flat_gaps = _label_clearance_issues(graph)
    assert (struck, collinear, flat_gaps) == (set(), False, set())
    assert _bypass_label_rakes(graph) == set()


@pytest.mark.parametrize("relative_path", FLAT_LEVER_CONFLICTS)
def test_settled_labels_are_clear_when_flat_levers_conflict(
    relative_path: str,
) -> None:
    graph = _settled(relative_path)

    struck, collinear, _flat_gaps = _label_clearance_issues(graph)
    assert (struck, collinear) == (set(), False)
    assert _bypass_label_rakes(graph) == set()
