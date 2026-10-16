"""Lines crossing a vertical section's flow-side entry stay on their lanes.

A BOTTOM entry into a BT section (or a TOP entry into a TB one) hands its lines
straight up (or down) the lanes of the station the drop reaches.  Where that
station also carries a line born inside the section, the port's lanes leave that
line's slot open, so every arriving line meets its internal lane at the box edge
instead of stepping sideways onto it.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
RISE = (
    ROOT
    / "tests"
    / "fixtures"
    / "curve_invariant_repros"
    / "bt_bottom_entry_rise_from_junction.mmd"
)

CASES = [(RISE, "z1__entry_bottom_2")]


@pytest.mark.parametrize(
    ("path", "port_id"), CASES, ids=lambda v: getattr(v, "stem", v)
)
def test_arriving_lines_meet_their_internal_lanes(path: Path, port_id: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    arrivals: dict[str, float] = {}
    departures: dict[str, float] = {}
    for route, points in zip(plan.routes, plan.route_polylines, strict=True):
        if route.edge.target == port_id:
            arrivals[route.line_id] = points[-1][0]
        elif route.edge.source == port_id:
            departures[route.line_id] = points[0][0]
    assert set(arrivals) == set(departures) == set(graph.station_lines(port_id))
    steps = {
        line_id: (x, departures[line_id])
        for line_id, x in arrivals.items()
        if abs(x - departures[line_id]) > 0.5
    }
    assert not steps, steps
