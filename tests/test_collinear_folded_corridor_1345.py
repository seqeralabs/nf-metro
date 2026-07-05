"""Distinct lines stay in adjacent lanes through a folded return corridor.

Regression coverage for issue #1345 (collinear overlays of distinct lines in
dense folded maps). ``folded_corridor_distinct_lanes`` co-travels two lines
through a trunk and its RL fold drop, diverges them where one line bypasses a
section the other routes through, and reconverges them at the final section --
the shape that surfaced the reported overlays. The engine must keep every
shared channel an ``OFFSET_STEP`` apart rather than collapsing one line's
stroke onto the other's:

* the three collinear-overlay guards report no violation, and
* any two distinct-line vertical legs that share a corridor (come within a
  couple of ``OFFSET_STEP`` of each other while overlapping in Y) sit at least
  one full ``OFFSET_STEP`` apart -- adjacent lanes, never fused.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.constants import OFFSET_STEP
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    _axis_aligned,
    apply_route_offsets,
    check_intra_section_collinear_distinct_lines,
    check_no_collinear_distinct_diagonals,
    check_no_collinear_distinct_lines,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = "folded_corridor_distinct_lanes"
TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

# Two vertical legs "share a corridor" when their xs sit within this band and
# their Y extents overlap; distinct lines that do so must not fuse.
_CORRIDOR_BAND = 2 * OFFSET_STEP
_MIN_Y_OVERLAP = 8.0


def _laid_out():
    graph = parse_metro_mermaid((TOPOLOGIES / f"{FIXTURE}.mmd").read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def test_no_collinear_overlay():
    """No two distinct lines coincide on a shared channel anywhere."""
    graph, offsets, routes = _laid_out()
    assert not check_no_collinear_distinct_lines(graph, routes, offsets)
    assert not check_intra_section_collinear_distinct_lines(graph, routes, offsets)
    assert not check_no_collinear_distinct_diagonals(graph, routes, offsets)


def _distinct_line_vertical_legs(routes, offsets):
    """(line_id, x, y_lo, y_hi) for every inter-section vertical leg."""
    legs = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        pts = apply_route_offsets(rp, offsets)
        for p1, p2 in zip(pts, pts[1:]):
            axis, coord = _axis_aligned(p1, p2)
            if axis == "V":
                legs.append((rp.line_id, coord, min(p1[1], p2[1]), max(p1[1], p2[1])))
    return legs


def test_shared_corridor_legs_are_one_offset_step_apart():
    """Distinct lines sharing a vertical corridor sit on adjacent lanes.

    The overlay guard only forbids exact coincidence (within ~1px); this
    asserts the stronger property #1345 is about -- two lines crowded into one
    corridor render as two lanes an ``OFFSET_STEP`` apart, not one fat stroke.
    """
    _graph, offsets, routes = _laid_out()
    legs = _distinct_line_vertical_legs(routes, offsets)
    shared = 0
    for i, (la, xa, lo_a, hi_a) in enumerate(legs):
        for lb, xb, lo_b, hi_b in legs[i + 1 :]:
            if la == lb or abs(xa - xb) > _CORRIDOR_BAND:
                continue
            if min(hi_a, hi_b) - max(lo_a, lo_b) <= _MIN_Y_OVERLAP:
                continue
            shared += 1
            assert abs(xa - xb) >= OFFSET_STEP - 0.5, (
                f"distinct lines {la!r}/{lb!r} share a corridor only "
                f"{abs(xa - xb):.1f}px apart (< OFFSET_STEP)"
            )
    # A shared distinct-line corridor is the precondition this test asserts on;
    # its absence would make the pass vacuous.
    assert shared, "fixture produces no shared distinct-line corridor to check"
