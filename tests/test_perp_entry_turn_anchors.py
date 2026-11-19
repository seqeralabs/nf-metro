"""The final corner into a TOP/BOTTOM entry nests with the port's lines that
turn into it the same way.

:func:`_perp_entry_turn_anchors` names the landing offsets that corner anchors
its radius on: every line of the port whose source (through a merge, the
merge's feeders) lies behind its own landing lane along the corner's approach.
A line arriving from the far side turns the other way and anchors nothing.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from nf_metro.api import render_string
from nf_metro.layout.routing import inter_section_handlers as H
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.parser.model import Edge
from nf_metro.render import svg

_LANDINGS = {"near": 96.0, "far": 104.0, "merged": 100.0}


def _station(x: float, sid: str) -> SimpleNamespace:
    return SimpleNamespace(id=sid, x=x, y=0.0)


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    stations = {
        "port": _station(100.0, "port"),
        "left_src": _station(0.0, "left_src"),
        "right_src": _station(300.0, "right_src"),
        "merge": _station(100.0, "merge"),
        "merge_feeder": _station(10.0, "merge_feeder"),
    }
    into = {
        "port": [
            Edge("left_src", "port", "near"),
            Edge("right_src", "port", "far"),
            Edge("merge", "port", "merged"),
        ],
        "merge": [Edge("merge_feeder", "merge", "merged")],
    }
    graph = SimpleNamespace(stations=stations, edges_to=lambda node: into.get(node, []))
    monkeypatch.setattr(
        H, "_perp_entry_landing_x", lambda _ctx, _e, _sec, _tx, lid: _LANDINGS[lid]
    )
    return SimpleNamespace(
        graph=graph, merge=SimpleNamespace(entry_port_for={"merge": "port"})
    )


def test_the_final_corner_anchors_on_the_lines_turning_its_way(ctx) -> None:
    """A rightward approach into a BOTTOM entry anchors on the line fed from
    the left and the merged line whose feeder lies left, not the line fed from
    the right."""
    edge = Edge("merge_feeder", "merge", "merged")
    points = ((10.0, 0.0), (50.0, 0.0), (50.0, 200.0), (100.0, 200.0), (100.0, 150.0))
    anchors = H._perp_entry_turn_anchors(
        ctx, edge, ctx.graph.stations["port"], None, points, False
    )
    assert sorted(anchors) == [-4.0, 0.0]


def test_a_leftward_approach_anchors_on_the_far_side_line(ctx) -> None:
    edge = Edge("right_src", "port", "far")
    points = (
        (300.0, 0.0),
        (250.0, 0.0),
        (250.0, 200.0),
        (104.0, 200.0),
        (104.0, 150.0),
    )
    anchors = H._perp_entry_turn_anchors(
        ctx, edge, ctx.graph.stations["port"], None, points, False
    )
    assert anchors == (0.0,)


_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("relative_path", "source", "line_id", "radius"),
    [
        (
            "tests/fixtures/regressions/departure_order_perpendicular_seam.mmd",
            "__junction_8",
            "m1",
            18.0,
        ),
        (
            "examples/topologies/bottom_entry_two_feeders_own_lanes.mmd",
            "__junction_6",
            "bam",
            14.0,
        ),
        (
            "tests/fixtures/regressions/approach_order_bottom_entry_refused.mmd",
            "sec4__exit_right_3",
            "l1",
            18.0,
        ),
    ],
)
def test_drawn_final_corner_takes_its_lane_radius_among_the_port_lines(
    relative_path: str, source: str, line_id: str, radius: float
) -> None:
    """Drawn: the last corner into the port is one lane radius per landing lane
    it stands outside the innermost line turning the same way."""
    captured: dict[str, object] = {}
    judge = svg._assert_settled_geometry_is_drawable

    def capture(graph, settled):  # type: ignore[no-untyped-def]
        captured["settled"] = settled
        return judge(graph, settled)

    path = _ROOT / relative_path
    with (
        warnings.catch_warnings(),
        mock.patch.object(svg, "_assert_settled_geometry_is_drawable", capture),
    ):
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))
    route = next(
        r
        for r in captured["settled"].routes  # type: ignore[attr-defined]
        if r.edge.source == source and r.line_id == line_id
    )
    assert resolve_curve_radii(route.points, route.curve_radii)[-1] == pytest.approx(
        radius
    )
