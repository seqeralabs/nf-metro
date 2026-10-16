"""A vertical section takes a fan's peel order only from a fan on its own row.

Zone 2 flows TB and leaves both its lines through one RIGHT exit: ``m2`` climbs
to Zone 3 on the same row, ``m0`` drops to Zone 4 on the row below, and ``m1``
climbs through the same gap from below to reach Zone 3.  The cross-row fan
keeps Zone 2's own lane order, so the two branches reach the gap on separate
columns and the counter-running ``m1`` climb is seated clear of the ``m0``
descent.
"""

from __future__ import annotations

import warnings

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import BUNDLE_TO_BUNDLE_CLEARANCE
from nf_metro.themes import resolve_theme

CROSS_ROW_FAN = """\
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
graph LR
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
    end
    subgraph z3 [Zone 3]
        q3x0 -->|m1,m2| q3x1
    end
    subgraph z4 [Zone 4]
        q4x0 -->|m0| q4x1
    end
    q0x2 -->|m1| q3x0
    q2x0 -->|m0| q4x0
    q2x1 -->|m2| q3x0
"""


def _vertical_x(points: list[tuple[float, float]]) -> float:
    (x, _y), _ = max(
        zip(points, points[1:], strict=False),
        key=lambda pair: (
            abs(pair[1][1] - pair[0][1]) if pair[0][0] == pair[1][0] else -1.0
        ),
    )
    return x


def test_cross_row_fan_keeps_counter_running_climb_clear() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(CROSS_ROW_FAN)
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    columns = {
        route.line_id: _vertical_x(route.points)
        for route in plan.routes
        if route.edge.target in ("z3__entry_left_2", "z4__entry_left_3")
        and route.line_id in ("m0", "m1")
        and route.is_inter_section
    }
    assert set(columns) == {"m0", "m1"}
    assert abs(columns["m0"] - columns["m1"]) >= BUNDLE_TO_BUNDLE_CLEARANCE - 0.01
