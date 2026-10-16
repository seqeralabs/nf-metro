"""Lines meeting at a fork or join keep a full lane gap along their diagonals.

A lane offset is a displacement along one fixed axis, so on a diagonal its
perpendicular component shrinks; at a hub each branch's diagonal is shifted
along the flow axis to restore the gap.  A vertical-flow section is the
quarter-turned horizontal one, so every orientation must keep the same gap.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import pytest
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import render_string
from nf_metro.layout.constants import OFFSET_STEP
from nf_metro.render.validate import parse_route_polylines

FIXTURES = Path(__file__).parent / "fixtures" / "curve_invariant_repros"
HUBS = {
    "merge": FIXTURES / "tb_lane_side_merge_lr.mmd",
    "fork": FIXTURES / "tb_lane_side_fork_lr.mmd",
}
SOURCE_BEYOND_CHANNEL = FIXTURES / "bt_bottom_entry_source_beyond_channel.mmd"


def _point_segment(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    length2 = (bx - ax) ** 2 + (by - ay) ** 2
    t = 0.0
    if length2:
        t = max(
            0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length2)
        )
    return math.hypot(px - ax - t * (bx - ax), py - ay - t * (by - ay))


def _crosses(a, b, c, d) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def _is_diagonal(a, b) -> bool:
    return abs(a[0] - b[0]) > 0.5 and abs(a[1] - b[1]) > 0.5


def _closest_diagonal_approach(svg: str) -> float:
    """Smallest gap between two lines' segments where either is a diagonal."""
    segments = [
        (line_id, a, b)
        for line_id, subpaths in parse_route_polylines(svg)
        for pts in subpaths
        for a, b in zip(pts, pts[1:])
        if math.dist(a, b) > 0.5
    ]
    closest = math.inf
    for i, (la, a, b) in enumerate(segments):
        for lb, c, d in segments[i + 1 :]:
            if la == lb or not (_is_diagonal(a, b) or _is_diagonal(c, d)):
                continue
            if _crosses(a, b, c, d):
                continue
            gap = min(
                _point_segment(a, c, d),
                _point_segment(b, c, d),
                _point_segment(c, a, b),
                _point_segment(d, a, b),
            )
            if gap > 0.05:
                closest = min(closest, gap)
    return closest


def _svg(source: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return render_string(source)


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
@pytest.mark.parametrize("hub", sorted(HUBS))
def test_hub_diagonals_keep_a_full_lane_gap(hub: str, orientation: Orientation) -> None:
    svg = _svg(transform_source(HUBS[hub].read_text(), orientation))
    assert _closest_diagonal_approach(svg) >= OFFSET_STEP - 0.05


def test_tb_merge_under_a_bottom_entry_keeps_a_full_lane_gap() -> None:
    svg = _svg(SOURCE_BEYOND_CHANNEL.read_text())
    assert _closest_diagonal_approach(svg) >= OFFSET_STEP - 0.05
