"""Opposite-running inter-section corridors get direction-specific lanes (#1520).

When one metro line runs left-to-right through an inter-section corridor and
also right-to-left (a fold / return-row layout), the two flows must sit on
distinct, clearly separated horizontal corridors.  Sharing a corridor -- or
sitting a single ``OFFSET_STEP`` (4px) sliver apart -- reads as the line
doubling straight back over its own track.  ``_guard_no_opposing_line_overlap``
catches only the exact-coincidence end; this test pins the separation floor:
counter-running legs that overlap in X must be at least a full lane apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COLLINEAR_AXIS_TOL,
    COORD_TOLERANCE,
    GUARD_TOLERANCE,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

FIXTURES = [
    "opposing_bypass_corridor.mmd",
    "opposing_return_row_pair.mmd",
]


class _HLeg:
    __slots__ = ("line_id", "y", "x_lo", "x_hi", "sign", "src", "tgt")

    def __init__(self, line_id, y, x_lo, x_hi, sign, src, tgt):
        self.line_id = line_id
        self.y = y
        self.x_lo = x_lo
        self.x_hi = x_hi
        self.sign = sign
        self.src = src
        self.tgt = tgt


def _horizontal_inter_section_legs(graph):
    """Every horizontal leg of an inter-section route, with travel direction."""
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    legs: list[_HLeg] = []
    for r in routes:
        if not r.is_inter_section:
            continue
        pts = apply_route_offsets(r, offsets)
        for k in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[k], pts[k + 1]
            if abs(y1 - y2) <= COLLINEAR_AXIS_TOL and abs(x1 - x2) > GUARD_TOLERANCE:
                legs.append(
                    _HLeg(
                        r.line_id,
                        (y1 + y2) / 2,
                        min(x1, x2),
                        max(x1, x2),
                        1 if x2 > x1 else -1,
                        r.edge.source,
                        r.edge.target,
                    )
                )
    return legs


def _opposing_underseparated(legs, min_sep):
    """Pairs of counter-running same-line legs closer than *min_sep* in Y.

    Restricted to legs that actually overlap in X (share the corridor); the
    ``GUARD_TOLERANCE`` X-overlap floor mirrors the runtime overlap guard.
    """
    bad = []
    for i, a in enumerate(legs):
        for b in legs[i + 1 :]:
            if a.line_id != b.line_id or a.sign * b.sign >= 0:
                continue
            if min(a.x_hi, b.x_hi) - max(a.x_lo, b.x_lo) <= GUARD_TOLERANCE:
                continue
            if abs(a.y - b.y) < min_sep:
                bad.append((a, b))
    return bad


@pytest.mark.parametrize("fixture", FIXTURES)
def test_opposing_inter_section_corridors_are_a_lane_apart(fixture):
    """Counter-running legs of one line never share (or 4px-share) a corridor.

    A coincident pair reads as a fold-back and a 4px sliver is a near-miss of
    the same defect; both flows must sit at least a full
    ``BUNDLE_TO_BUNDLE_CLEARANCE`` lane apart.
    """
    graph = parse_metro_mermaid((TOPOLOGIES / fixture).read_text())
    compute_layout(graph, validate=False)
    legs = _horizontal_inter_section_legs(graph)
    min_sep = BUNDLE_TO_BUNDLE_CLEARANCE - COORD_TOLERANCE
    bad = _opposing_underseparated(legs, min_sep)

    def _fmt(leg: _HLeg) -> str:
        return f"{leg.src}->{leg.tgt} (y={leg.y:.1f}, {'R' if leg.sign > 0 else 'L'})"

    assert not bad, "\n".join(
        f"line {a.line_id!r}: {_fmt(a)} vs {_fmt(b)}; "
        f"x-overlap [{max(a.x_lo, b.x_lo):.0f},{min(a.x_hi, b.x_hi):.0f}], "
        f"y-gap {abs(a.y - b.y):.1f} < {min_sep:.1f}"
        for a, b in bad
    )
