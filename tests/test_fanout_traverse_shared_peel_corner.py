"""Fan traverses that turn off together nest concentrically through that turn.

A fan's distinct-line traverses are re-banded one step apart beneath the
innermost branch.  Branches bound for one entry port leave that band together,
so the corner where they turn off is a bundle corner like the one they turned
on at, and it has to stay concentric after the re-band.
"""

from __future__ import annotations

import warnings

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.themes import resolve_theme

# ``m2`` peels off the fan first, up into Zone 2's BOTTOM entry, so the band
# nests ``m0``/``m1`` beneath it; both then rise together into Zone 3's LEFT
# entry.
SHARED_RISE = """\
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
graph LR
    subgraph z2 [Zone 2]
        %%metro direction: BT
        q2x1[Q2X1]
    end
    subgraph z3 [Zone 3]
        q3x0 -->|m0| q3x1
    end
    q0x0 -->|m0| q3x0
    q0x0 -->|m1| q3x1
    q0x2 -->|m2| q2x1
    q2x1 -->|m2| q3x1
"""

Point = tuple[float, float]


def _unit(a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5
    return dx / length, dy / length


def _arc_centre(a: Point, p: Point, b: Point, r: float) -> Point:
    u, v = _unit(a, p), _unit(p, b)
    return p[0] - r * u[0] + r * v[0], p[1] - r * u[1] + r * v[1]


def test_branches_rising_into_one_port_keep_concentric_corners() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(SHARED_RISE)
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    rises = {
        route.line_id: (route.points, route.curve_radii)
        for route in plan.routes
        if route.edge.target == "z3__entry_left_2"
        and route.edge.source.startswith("__junction")
    }
    assert set(rises) == {"m0", "m1"}
    centres = {}
    for line_id, (points, radii) in rises.items():
        assert radii is not None and len(points) == 6
        centres[line_id] = _arc_centre(points[2], points[3], points[4], radii[2])
    (x0, y0), (x1, y1) = centres["m0"], centres["m1"]
    assert abs(x0 - x1) <= 0.5 and abs(y0 - y1) <= 0.5, centres
