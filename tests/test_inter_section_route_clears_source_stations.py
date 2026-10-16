"""An inter-section route never runs back over its own source section's stations.

A route leaves its source through an exit port on the section's edge, so its
lead-out heads away from the box.  ``perp_exit_single_feeder_landing`` exits
Gather 2 through a LEFT port while its target sits up and to the right; the
route must leave on the port's side and pass under the box, not double back
along the trunk over every station it just served.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = [
    ROOT / "tests" / "fixtures" / "regressions" / "perp_exit_single_feeder_landing.mmd",
    ROOT / "examples" / "topologies" / "bottom_entry_two_feeders_own_lanes.mmd",
    ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "bt_bottom_entry_rise_from_junction.mmd",
]

Point = tuple[float, float]


def _distance_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    t = 0.0
    if length_sq > 0:
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq))
    cx, cy = a[0] + t * dx, a[1] + t * dy
    return ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_route_does_not_run_over_its_source_stations(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    overruns = []
    for route, points in zip(plan.routes, plan.route_polylines, strict=True):
        source = graph.stations[route.edge.source]
        if not (route.is_inter_section and source.is_port and source.section_id):
            continue
        section = graph.sections[source.section_id]
        stations = [
            graph.stations[sid]
            for sid in section.station_ids
            if not graph.stations[sid].is_port
        ]
        for station, (a, b) in itertools.product(
            stations, itertools.pairwise([tuple(p) for p in points])
        ):
            if _distance_to_segment((station.x, station.y), a, b) < 6.0:
                overruns.append((route.edge.source, route.line_id, station.id))
                break
    assert not overruns, overruns
