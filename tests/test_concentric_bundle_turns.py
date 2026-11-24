"""Bundled lanes turn any angle about one shared centre."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.routing.corners import concentric_turn_radius
from nf_metro.render.svg import _drawn_pieces, _piece_at, build_render_plan
from nf_metro.themes import resolve_theme

_ROOT = Path(__file__).resolve().parents[1]
_STEP = 4.0


def _lane(offset: float, turn_degrees: float) -> list[tuple[float, float]]:
    """A lane of a bundle turning *turn_degrees* left, shifted *offset* outward."""
    phi = math.radians(turn_degrees)
    corner = (100.0 + math.tan(phi / 2) * offset, 100.0 + offset)
    end = (corner[0] + 100 * math.cos(phi), corner[1] - 100 * math.sin(phi))
    return [(0.0, 100.0 + offset), corner, end]


def _samples(points, radii, n=400):
    out = []
    for piece in _drawn_pieces(points, radii):
        for k in range(n + 1):
            out.append((_piece_at(piece, k / n)[0], piece.control is not None))
    return out


def _centre(points, arm):
    (p0, v, p2) = points
    u_in = ((v[0] - p0[0]) / math.dist(p0, v), (v[1] - p0[1]) / math.dist(p0, v))
    u_out = ((p2[0] - v[0]) / math.dist(v, p2), (p2[1] - v[1]) / math.dist(v, p2))
    phi = math.acos(max(-1.0, min(1.0, u_in[0] * u_out[0] + u_in[1] * u_out[1])))
    radius = arm / math.tan(phi / 2)
    before = (v[0] - u_in[0] * arm, v[1] - u_in[1] * arm)
    turn = u_in[0] * u_out[1] - u_in[1] * u_out[0]
    normal = (-u_in[1], u_in[0]) if turn > 0 else (u_in[1], -u_in[0])
    return (before[0] + normal[0] * radius, before[1] + normal[1] * radius)


def _pair(turn_degrees: float):
    inner, outer = _lane(0.0, turn_degrees), _lane(_STEP, turn_degrees)
    displacement = (outer[1][0] - inner[1][0], outer[1][1] - inner[1][1])
    return inner, outer, concentric_turn_radius(*outer, displacement, 10.0)


def _gap(inner, inner_arm, outer, outer_arm) -> float:
    inner_pts = [q for q, _ in _samples(inner, [inner_arm])]
    return max(
        min(math.dist(q, s) for s in inner_pts) - _STEP
        for q, on_curve in _samples(outer, [outer_arm])
        if on_curve
    )


@pytest.mark.parametrize("turn_degrees", [45.0, 61.0, 90.0, 120.0, 135.0])
def test_adjacent_lanes_share_a_centre_at_any_turn(turn_degrees):
    inner, outer, outer_arm = _pair(turn_degrees)
    assert outer_arm > 10.0
    assert _centre(inner, 10.0) == pytest.approx(_centre(outer, outer_arm), abs=1e-6)


@pytest.mark.parametrize("turn_degrees", [45.0, 61.0, 90.0, 120.0, 135.0])
def test_adjacent_lanes_stay_touching_through_a_diagonal_turn(turn_degrees):
    inner, outer, outer_arm = _pair(turn_degrees)
    assert _gap(inner, 10.0, outer, outer_arm) <= 0.1


def test_right_angle_turns_keep_the_existing_concentric_rule():
    inner = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    outer = [(0.0, -4.0), (104.0, -4.0), (104.0, 100.0)]
    assert concentric_turn_radius(*outer, (4.0, -4.0), 10.0) == pytest.approx(14.0)
    assert concentric_turn_radius(*inner, (0.0, 0.0), 10.0) == pytest.approx(10.0)


def test_touching_fan_corners_leave_no_seam():
    path = _ROOT / "examples" / "rnaseq_sections.mmd"
    text = "%%metro track_gap: 0\n" + path.read_text()
    graph = prepare_graph(text, source_dir=str(path.parent))
    theme = resolve_theme(None, graph)
    plan = build_render_plan(graph, theme)
    lanes = defaultdict(list)
    for route, points, radii in zip(
        plan.routes, plan.route_polylines, plan.route_curve_radii
    ):
        if (route.edge.source, route.edge.target) == (
            "umi_tools_extract",
            "trimgalore",
        ):
            lanes[route.line_id] = (
                list(points),
                None if radii is None else list(radii),
            )
    assert len(lanes) == 6
    worst = 0.0
    ordered = sorted(lanes.values(), key=lambda lane: lane[0][0][1])
    for (pa, ra), (pb, rb) in zip(ordered, ordered[1:]):
        other = [q for q, _ in _samples(pb, rb, 150)]
        for q, on_curve in _samples(pa, ra, 150):
            if on_curve:
                worst = max(
                    worst, min(math.dist(q, s) for s in other) - theme.line_width
                )
    assert worst <= 0.1


# One cubic per corner holds a circle to 1e-3 of its radius up to a right
# angle, the largest turn routing draws; past it the single cubic's own
# approximation error grows, to about 3.1e-3 at 135 degrees.
@pytest.mark.parametrize(
    ("turn_degrees", "bound"),
    [
        (30.0, 1e-3),
        (45.0, 1e-3),
        (61.0, 1e-3),
        (90.0, 1e-3),
        (120.0, 2e-3),
        (135.0, 3.5e-3),
    ],
)
def test_a_drawn_corner_is_a_circular_arc(turn_degrees, bound):
    """Every point of a drawn corner sits on the circle tangent to both legs."""
    from nf_metro.layout.routing.corners import corner_arc_at

    phi = math.radians(turn_degrees)
    arm = 10.0
    corner = (100.0, 100.0)
    before = (corner[0] - arm, corner[1])
    after = (corner[0] + arm * math.cos(phi), corner[1] - arm * math.sin(phi))
    radius = arm / math.tan(phi / 2)
    centre = (before[0], before[1] - radius)
    worst = max(
        abs(
            math.dist(corner_arc_at(before, corner, after, k / 200)[0], centre) - radius
        )
        for k in range(201)
    )
    assert worst <= bound * radius
