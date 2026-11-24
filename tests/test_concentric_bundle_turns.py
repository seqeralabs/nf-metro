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


def _lane(offset: float) -> list[tuple[float, float]]:
    """A lane of a bundle turning 45 degrees up, shifted *offset* outward."""
    d = 1 / math.sqrt(2)
    corner = (100.0 + math.tan(math.radians(22.5)) * offset, 100.0 + offset)
    return [(0.0, 100.0 + offset), corner, (corner[0] + 100 * d, corner[1] - 100 * d)]


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


def test_adjacent_lanes_at_a_45_degree_turn_share_a_centre_and_stay_touching():
    inner, outer = _lane(0.0), _lane(_STEP)
    arm_inner = 10.0
    displacement = (outer[1][0] - inner[1][0], outer[1][1] - inner[1][1])
    arm_outer = concentric_turn_radius(*outer, displacement, arm_inner)
    assert arm_outer > arm_inner
    assert _centre(inner, arm_inner) == pytest.approx(
        _centre(outer, arm_outer), abs=1e-6
    )
    inner_pts = [q for q, _ in _samples(inner, [arm_inner])]
    gap = max(
        min(math.dist(q, s) for s in inner_pts) - _STEP
        for q, on_curve in _samples(outer, [arm_outer])
        if on_curve
    )
    assert gap <= 0.1


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
