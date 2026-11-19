"""A connector into the side port facing away from it loops round the section.

Reaching a section's far-side port by running across the section is never a
drawing: the loop runs in the band beside the row on the side its exit faces,
comes down outside the section's far edge and turns in on the port's planned
lanes.  Every loop into one port descends from one base, each one lane outside
every looping lane nested inside it, so the loops nest concentrically.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.constants import (
    CURVE_RADIUS,
    OFFSET_STEP,
    SECTION_ROUTE_CLEARANCE,
)
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.parser.model import PortSide
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parents[1]
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"

LOOP_FIXTURES = [
    REGRESSIONS / "departure_order_perpendicular_seam.mmd",
    REGRESSIONS / "outward_loop_top_right_entry.mmd",
    REGRESSIONS / "outward_loop_top_left_entry.mmd",
    REGRESSIONS / "outward_loop_bottom_right_entry.mmd",
    REGRESSIONS / "outward_loop_bottom_left_entry.mmd",
]
SHARED_PORT_FIXTURES = [
    *LOOP_FIXTURES,
    ROOT / "examples" / "topologies" / "fan_over_top_reach_order.mmd",
]


def _render(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    plan = render_graph_result(
        graph, resolve_theme(None, graph, "light"), RenderConfig()
    ).plan
    return plan.graph, list(zip(plan.routes, plan.route_polylines, strict=True))


def _far_side_loops(graph, drawn):
    """``{port: [(line, polyline)]}`` for routes fed from behind a side port."""
    loops: dict[str, list] = {}
    for route, line in drawn:
        port = graph.ports.get(route.edge.target)
        if port is None or not port.is_entry:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        outward = 1.0 if port.side is PortSide.RIGHT else -1.0
        tx = graph.stations[route.edge.target].x
        if (graph.stations[route.edge.source].x - tx) * outward < 0:
            loops.setdefault(route.edge.target, []).append((route.line_id, line))
    return loops


@pytest.mark.parametrize("path", LOOP_FIXTURES, ids=lambda p: p.stem)
def test_a_far_side_port_is_reached_round_its_section(path: Path) -> None:
    graph, drawn = _render(path)
    loops = _far_side_loops(graph, drawn)
    assert loops, "fixture has no connector into a far-side port"
    for port_id, members in loops.items():
        sec = graph.sections[graph.ports[port_id].section_id]
        interior = (
            sec.bbox_x + 2,
            sec.bbox_y + 2,
            sec.bbox_x + sec.bbox_w - 2,
            sec.bbox_y + sec.bbox_h - 2,
        )
        for line_id, line in members:
            assert not any(
                segment_intersects_bbox(a[0], a[1], b[0], b[1], interior)
                for a, b in pairwise(line)
            ), f"{line_id} crosses {sec.id} to reach {port_id}"


@pytest.mark.parametrize("path", SHARED_PORT_FIXTURES, ids=lambda p: p.stem)
def test_loops_into_one_port_nest_one_lane_apart_from_one_base(path: Path) -> None:
    graph, drawn = _render(path)
    base = max(CURVE_RADIUS + OFFSET_STEP, SECTION_ROUTE_CLEARANCE)
    nested = 0
    for port_id, members in _far_side_loops(graph, drawn).items():
        port = graph.ports[port_id]
        sec = graph.sections[port.section_id]
        outward = 1.0 if port.side is PortSide.RIGHT else -1.0
        edge_x = sec.bbox_x + (sec.bbox_w if outward > 0 else 0.0)
        port_y = graph.stations[port_id].y
        by_side: dict[bool, list[tuple[float, float]]] = {}
        for _line_id, line in members:
            (dx, dy), (_x, landing_y) = line[-3], line[-2]
            from_above = dy < port_y
            by_side.setdefault(from_above, []).append(
                ((landing_y if from_above else -landing_y), (dx - edge_x) * outward)
            )
        for arrivals in by_side.values():
            if len(arrivals) < 2:
                continue
            nested += 1
            reach = [r for _lane, r in sorted(arrivals)]
            assert reach[0] == pytest.approx(base), (port_id, reach)
            assert all(
                b - a == pytest.approx(OFFSET_STEP) for a, b in pairwise(reach)
            ), (port_id, reach)
    assert nested, "fixture has no port two loops share"


OWN_SECTION_FIXTURES = [
    REGRESSIONS / "perp_entry_column_two_feeders.mmd",
    REGRESSIONS / "exit_side_wrap_top_entry.mmd",
    REGRESSIONS / "exit_side_wrap_bottom_entry.mmd",
]


@pytest.mark.parametrize("path", OWN_SECTION_FIXTURES, ids=lambda p: p.stem)
def test_a_feeder_wrapping_to_a_perpendicular_port_clears_its_own_section(
    path: Path,
) -> None:
    graph, drawn = _render(path)
    wraps = 0
    for route, line in drawn:
        port = graph.ports.get(route.edge.target)
        if port is None or port.side not in (PortSide.TOP, PortSide.BOTTOM):
            continue
        own = {
            graph.stations[route.edge.source].section_id,
            graph.stations[route.edge.target].section_id,
        }
        if None in own or len(own) < 2:
            continue
        wraps += 1
        for sid in own:
            sec = graph.sections[sid]
            interior = (
                sec.bbox_x + 2,
                sec.bbox_y + 2,
                sec.bbox_x + sec.bbox_w - 2,
                sec.bbox_y + sec.bbox_h - 2,
            )
            assert not any(
                segment_intersects_bbox(a[0], a[1], b[0], b[1], interior)
                for a, b in pairwise(line)
            ), f"{route.edge.source}->{route.edge.target} {route.line_id} crosses {sid}"
    assert wraps, "fixture has no feeder into a perpendicular port"
