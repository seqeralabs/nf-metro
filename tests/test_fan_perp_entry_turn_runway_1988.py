"""Regression lock for #1988.

A left/right-exit fan-out junction that peels a branch down into a
perpendicular (TOP/BOTTOM) entry one row away needs a full curve runway for
that branch's turn, even when a section already occupies the inter-column gap on
the feeding side.  ``left_exit_fan_perp_entry_landing`` is exactly that shape:
``b``'s LEFT exit fans ``g`` down into ``c``'s TOP entry while ``d`` sits in the
gap left of column 1, and the descent's first corner clamped to radius 8.0 of a
requested 10.0.

Two mechanisms together restore the full runway, and each is pinned here so a
regression names its cause:

* the fan-branch lead-in in ``_perp_entry_l_geometry`` stands the branch off its
  junction by the bundle's outer-lane radius (``outer_lane_radius``), matching
  the non-fan lead-in in the same handler rather than the narrower half-width
  stand-off;
* ``_fan_perp_entry_turn_gap_pairs`` recognises the feeding-side gap and reserves
  one curve runway in it, so the section occupying that gap does not starve the
  turn.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.section_placement import _fan_perp_entry_turn_gap_pairs
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "topologies"
    / "left_exit_fan_perp_entry_landing.mmd"
)

_RADIUS_TOL = 0.5


def _laid_out():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    return graph


def test_fan_perp_entry_branch_turn_rounds_at_full_radius() -> None:
    """The fan's descent into ``c``'s TOP entry turns at its full radius.

    Both the lead-in stand-off and the gap reservation are load-bearing for this
    corner: either one alone leaves it clamped below its requested radius.
    """
    graph = _laid_out()
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    branch = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_5"
        and rp.edge.target == "c__entry_top_3"
        and rp.line_id == "g"
    )
    first_corner = list(resolve_curve_radii(branch.points, branch.curve_radii))[0]
    requested = branch.curve_radii[0] if branch.curve_radii else CURVE_RADIUS
    assert first_corner >= requested - _RADIUS_TOL, (
        f"descent corner clamped to radius {first_corner:.1f} of "
        f"requested {requested:.1f}"
    )


def test_feeding_side_gap_is_recognised_for_a_runway_reservation() -> None:
    """The recogniser flags the feeding-side inter-column gap.

    ``b``'s LEFT exit sits in column 1 and ``d`` occupies column 0 across the
    branch's row span, so the gap between columns 0 and 1 owes one curve runway.
    """
    graph = _laid_out()
    col_assign = {sid: section.grid_col for sid, section in graph.sections.items()}
    col_sections: dict[int, list] = {}
    for sid, section in graph.sections.items():
        col_sections.setdefault(col_assign[sid], []).append(section)
    assert _fan_perp_entry_turn_gap_pairs(graph, col_assign, col_sections) == {(0, 1)}


def test_descent_column_clears_the_gap_occupant_by_a_full_radius() -> None:
    """The branch's descent column stands a full radius off ``d``'s near edge.

    The reserved runway keeps the vertical drop clear of the section occupying
    the feeding-side gap, which is what a clamped corner would encroach on.
    """
    graph = _laid_out()
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    branch = next(
        rp
        for rp in routes
        if rp.edge.source == "__junction_5"
        and rp.edge.target == "c__entry_top_3"
        and rp.line_id == "g"
    )
    descent_x = branch.points[1][0]
    d_right = graph.sections["d"].bbox_x + graph.sections["d"].bbox_w
    assert descent_x - d_right >= CURVE_RADIUS - _RADIUS_TOL, (
        f"descent column at x={descent_x:.0f} clears d's edge at "
        f"x={d_right:.0f} by only {descent_x - d_right:.0f}px"
    )
