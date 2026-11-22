"""A bypass around a hidden station opens on the side its lines' lanes stack.

A branch that forks off a trunk station's feeder, carries a subset of the
trunk's lines past it and rejoins downstream rides the fork's bundle until it
peels away.  Opening on the side the section does not stack those lines on
forces both of its legs across the lines staying on the trunk.

The layout validator's crossing check exempts every edge touching a hidden
station, so the count here is taken over the drawn polylines directly.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

import pytest
from layout_metrics import drawn_polylines, measured_geometry

from nf_metro.api import resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import build_render_plan

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

FIXTURES = [
    "bypass_hidden_lane_side.mmd",
    "bypass_hidden_lane_side_lr.mmd",
    "bypass_hidden_lane_side_rl.mmd",
    "bypass_hidden_lane_side_tb.mmd",
    "bypass_hidden_lane_side_bt.mmd",
    "bypass_hidden_lane_side_tb_seam.mmd",
    "bypass_hidden_lane_side_two.mmd",
    "bypass_hidden_lane_side_symmetric.mmd",
    "bypass_hidden_lane_side_symmetric_bt.mmd",
    "bypass_hidden_lane_side_symmetric_tb_seam.mmd",
    "bypass_hidden_lane_side_chain.mmd",
]

Point = tuple[float, float]


def _orientation(p: Point, q: Point, r: Point) -> int:
    cross = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if abs(cross) < 1e-9:
        return 0
    return 1 if cross > 0 else -1


def _properly_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    return (
        _orientation(a, b, c) * _orientation(a, b, d) < 0
        and _orientation(c, d, a) * _orientation(c, d, b) < 0
    )


def _drawn_crossings(path: Path) -> int:
    """Proper crossings between different lines' drawn segments, hidden edges too."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
        compute_layout(graph)
        plan = build_render_plan(graph, resolve_theme(None, graph))
    offsets, routes = measured_geometry(plan.graph, plan)
    polylines = drawn_polylines(routes, offsets)
    return sum(
        1
        for (route_a, points_a), (route_b, points_b) in itertools.combinations(
            polylines, 2
        )
        if route_a.line_id != route_b.line_id
        for seg_a in itertools.pairwise(points_a)
        for seg_b in itertools.pairwise(points_b)
        if _properly_cross(*seg_a, *seg_b)
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_hidden_bypass_does_not_cross_the_trunk(name: str) -> None:
    assert _drawn_crossings(TOPOLOGIES / name) == 0
