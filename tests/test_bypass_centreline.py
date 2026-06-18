"""The U-shaped bypass route is built via a centreline + the bundle builder.

``_route_bypass`` describes the down -> across -> up loop as a centreline through
the two gap channels and fans it with ``build_tapered_bundle`` rather than
assembling per-line ``points`` / ``curve_radii`` by hand.  It declares each gap's
full fan via ``bundle_offsets``, so the builder anchors every corner on that
gap's innermost-of-turn line and no arc on the inside of a deep fan falls below
the floor radius.

These tests pin that on the fixtures whose bypasses run the deepest fans
(``funcprofiler_upstream`` fans eight lines, ``bypass_gap2_rightward_overflow``
and ``upward_bypass`` seven) alongside the tapering cases where the two gaps
carry different line counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises the U-shaped bypass: deep
# uniform fans (where a sub-base inner radius would surface) and tapering loops
# whose two gaps carry different line counts.
BYPASS_FIXTURES = [
    EXAMPLES / "topologies" / "funcprofiler_upstream.mmd",
    EXAMPLES / "topologies" / "bypass_gap2_rightward_overflow.mmd",
    EXAMPLES / "topologies" / "upward_bypass.mmd",
    EXAMPLES / "topologies" / "fan_in_merge.mmd",
    EXAMPLES / "longread_variant_calling.mmd",
    EXAMPLES / "differentialabundance.mmd",
]


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _bypass_routes(routes):
    """Inter-section routes shaped as a U: six waypoints, four corners."""
    return [
        r for r in routes if r.is_inter_section and len(r.points) == 6 and r.curve_radii
    ]


@pytest.mark.parametrize("path", BYPASS_FIXTURES, ids=lambda p: p.stem)
def test_bypass_corners_are_concentric_and_unflipped(path: Path) -> None:
    graph, offsets, routes = _route(path)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("path", BYPASS_FIXTURES, ids=lambda p: p.stem)
def test_bypass_corner_radii_anchored_at_floor(path: Path) -> None:
    """Every U-shaped bypass corner sits at or above the floor radius.

    The builder anchors each gap's innermost-of-turn line at ``CURVE_RADIUS``
    from the declared fan, so no inside-of-turn arc of a deep fan falls below it.
    A single-member call that fails to declare its fan produces a sub-floor
    (eventually negative) arc, which this catches.
    """
    _graph, _offsets, routes = _route(path)
    bypasses = _bypass_routes(routes)
    assert bypasses, f"{path.stem}: expected at least one U-shaped bypass route"
    offenders = [
        (r.edge.source, r.edge.target, r.line_id, r.curve_radii)
        for r in bypasses
        if any(radius < CURVE_RADIUS - 0.01 for radius in r.curve_radii)
    ]
    assert not offenders, f"{path.stem}: bypass corners below the floor: {offenders}"


@pytest.mark.parametrize("path", BYPASS_FIXTURES, ids=lambda p: p.stem)
def test_bypass_routes_are_offset_baked(path: Path) -> None:
    _graph, _offsets, routes = _route(path)
    bypasses = _bypass_routes(routes)
    assert bypasses, f"{path.stem}: expected at least one U-shaped bypass route"
    assert all(r.offsets_applied for r in bypasses)


def test_single_line_bypass_descent_turns_tight() -> None:
    """A line that peels off and descends a gap alone turns at the floor radius.

    The QC line in ``bypass_fan_in_outer_slot`` shares a five-line junction fan
    at its lead-in but descends the gap on its own, so its descent corners
    (lead-in turn and the turn into the below-row traverse) must take the
    single-line floor radius.  Anchoring them on the wider junction fan rather
    than the gap's own one-line channel would sweep the lone line wide.
    """
    path = EXAMPLES / "topologies" / "bypass_fan_in_outer_slot.mmd"
    _graph, _offsets, routes = _route(path)
    qc = next(r for r in _bypass_routes(routes) if r.line_id == "qc")
    # curve_radii[0:2] are the two source-side (gap1 descent) corners.
    assert qc.curve_radii[0] == pytest.approx(CURVE_RADIUS, abs=0.01)
    assert qc.curve_radii[1] == pytest.approx(CURVE_RADIUS, abs=0.01)
