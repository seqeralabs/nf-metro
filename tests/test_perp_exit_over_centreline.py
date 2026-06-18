"""The perpendicular-exit up-and-over route is built via a centreline + builder.

``_route_perp_exit_over`` describes the rise -> corridor -> descent -> turn-in
loop out of a TOP/BOTTOM exit as a centreline and fans it with the bundle
builder rather than assembling per-line ``points`` / ``curve_radii`` by hand.  It
declares the co-travelling bundle via ``bundle_offsets``, so the builder anchors
every corner on the bundle's innermost-of-turn line and no inside-of-turn arc
falls below the floor radius.

These tests pin that on the fixtures whose perpendicular exits run the
up-and-over route: TOP and BOTTOM exits into both a perpendicular entry (a
column-offset trunk drop) and a side entry (a turn into a LEFT-entry station).
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
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# Fixtures whose inter-section routing exercises the up-and-over perp exit:
# TOP and BOTTOM exits, each feeding a perpendicular entry and a side entry.
PERP_EXIT_FIXTURES = [
    EXAMPLES / "topologies" / "lr_perp_top_exit_perp_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_top_exit_side_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_bottom_exit_perp_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_bottom_exit_side_entry.mmd",
    EXAMPLES / "topologies" / "lr_perp_top_exit_perp_entry_diverging.mmd",
]


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _over_routes(graph, routes):
    """Inter-section routes leaving a perpendicular exit by the up-and-over path.

    A perpendicular (TOP/BOTTOM) exit that does not drop straight rises into the
    header corridor and turns over; its route has four waypoints (a perp-entry
    trunk drop) or five (a side-entry turn-in), distinguishing it from the
    two-waypoint straight drop.
    """
    over = []
    for r in routes:
        port = graph.ports.get(r.edge.source)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and r.is_inter_section
            and len(r.points) in (4, 5)
            and r.curve_radii
        ):
            over.append(r)
    return over


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_perp_exit_over_corners_are_concentric_and_unflipped(path: Path) -> None:
    graph, offsets, routes = _route(path)
    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    assert check_bundle_order_preserved(routes) == []
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_perp_exit_over_corner_radii_anchored_at_floor(path: Path) -> None:
    """Every up-and-over corner sits at or above the floor radius.

    The builder anchors the bundle's innermost-of-turn line at ``CURVE_RADIUS``
    from the declared fan, so no inside-of-turn arc falls below it.  Anchoring
    corners on the raw port centre rather than the declared fan lets an
    inside-of-turn line dip below the floor; this catches that.
    """
    graph, _offsets, routes = _route(path)
    over = _over_routes(graph, routes)
    assert over, f"{path.stem}: expected at least one up-and-over perp-exit route"
    offenders = [
        (r.edge.source, r.edge.target, r.line_id, r.curve_radii)
        for r in over
        if any(radius < CURVE_RADIUS - 0.01 for radius in r.curve_radii)
    ]
    assert not offenders, f"{path.stem}: perp-exit corners below the floor: {offenders}"


@pytest.mark.parametrize("path", PERP_EXIT_FIXTURES, ids=lambda p: p.stem)
def test_perp_exit_over_routes_are_offset_baked(path: Path) -> None:
    graph, _offsets, routes = _route(path)
    over = _over_routes(graph, routes)
    assert over, f"{path.stem}: expected at least one up-and-over perp-exit route"
    assert all(r.offsets_applied for r in over)
