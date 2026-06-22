"""The U-shaped bypass route is built via a centreline + the bundle builder.

``_route_bypass`` describes the down -> across -> up loop as a centreline through
the two gap channels and fans it with ``build_tapered_bundle`` rather than
assembling per-line ``points`` / ``curve_radii`` by hand.  It declares each gap's
full fan via ``bundle_offsets``, so the builder anchors every corner on that
gap's innermost-of-turn line and no arc on the inside of a deep fan falls below
the floor radius.

These tests pin that on the fixtures whose bypasses run the deepest fans
(``funcprofiler_upstream`` fans eight lines, ``bypass_gap2_rightward_overflow``,
``bypass_leftward_overflow`` and ``upward_bypass`` seven) alongside the tapering
cases where the two gaps carry different line counts.  ``bypass_leftward_overflow``
is a reverse-flow (right-to-left) bypass: its trunk leads out leftward, the mirror
of every other fixture, so the concentric order and corner radii must follow the
trunk's travel direction rather than a fixed rightward assumption.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.bundle import build_tapered_bundle
from nf_metro.layout.routing.centrelines import route_tapered_anchored
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises the U-shaped bypass: deep
# uniform fans (where a sub-base inner radius would surface) and tapering loops
# whose two gaps carry different line counts.
BYPASS_FIXTURES = [
    EXAMPLES / "topologies" / "funcprofiler_upstream.mmd",
    EXAMPLES / "topologies" / "bypass_gap2_rightward_overflow.mmd",
    EXAMPLES / "topologies" / "bypass_leftward_overflow.mmd",
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
    assert all(r.offset_regime is OffsetRegime.BAKED for r in bypasses)


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


def test_route_tapered_anchored_pairs_the_two_channel_fans() -> None:
    """The anchored helper reproduces the bypass's hand-paired ``bundle_offsets``.

    ``_route_bypass`` describes its U as a centreline plus two *independent*
    channel fans (the source gap's and the target gap's), paired so each gap's
    spread anchors only its own corners.  ``route_tapered_anchored`` assembles
    that pairing internally; building the same single member through
    ``build_tapered_bundle`` with the pairing done by hand must yield the
    identical route.  Asymmetric fan sizes (a two-line source gap, a three-line
    target gap) exercise the tapering case the helper exists for.
    """
    edge = Edge(source="a", target="b", line_id="x")
    centerline = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, -30.0),
        (40.0, -30.0),
        (40.0, -60.0),
        (60.0, -60.0),
    ]
    src_off, tgt_off = 4.0, -3.0
    src_fan = [4.0, 12.0]
    tgt_fan = [-3.0, 5.0, 13.0]
    member = (edge, edge.line_id, src_off, tgt_off)

    got = route_tapered_anchored(
        member,
        centerline,
        transition_leg=3,
        base_radius=CURVE_RADIUS,
        src_bundle_offsets=src_fan,
        tgt_bundle_offsets=tgt_fan,
        normalize_exempt=False,
    )

    manual = [(s, tgt_off) for s in src_fan] + [(src_off, t) for t in tgt_fan]
    expected = build_tapered_bundle(
        [member],
        centerline,
        transition_leg=3,
        base_radius=CURVE_RADIUS,
        bundle_offsets=manual,
        normalize_exempt=False,
    )[0]

    assert got.points == expected.points
    assert got.curve_radii == expected.curve_radii
