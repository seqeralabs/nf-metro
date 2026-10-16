"""An inter-section route leaves its section without running back over it.

A route out of a section's exit port heads away from the box, so the only
stations of that section it may touch are the ones it serves.  Leading out on
the wrong side would carry it back across the section's own trunk, drawn over
stations it never stops at.
"""

from __future__ import annotations

import itertools
import math
import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
CASES = [REGRESSIONS / "perp_exit_single_feeder_landing.mmd"]

_CLEARANCE = 6.0


def _distance_to_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


@pytest.mark.parametrize("path", CASES, ids=lambda v: v.stem)
def test_route_passes_no_station_of_its_source_section(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    struck = []
    for route, points in zip(plan.routes, plan.route_polylines, strict=True):
        if not route.is_inter_section:
            continue
        source = graph.stations[route.edge.source]
        if source.section_id is None:
            continue
        served = {route.edge.source, route.edge.target}
        for station_id in graph.sections[source.section_id].station_ids:
            station = graph.stations[station_id]
            if station_id in served or station.is_port or station.is_hidden:
                continue
            if any(
                _distance_to_segment((station.x, station.y), a, b) < _CLEARANCE
                for a, b in itertools.pairwise(points)
            ):
                struck.append((route.edge.source, route.edge.target, station_id))
    assert not struck, struck
