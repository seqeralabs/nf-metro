"""The U-shaped bypass route is built via a centreline + the bundle builder.

``_route_bypass`` describes the down -> across -> up loop as a centreline through
the two gap channels and fans it with ``build_tapered_bundle`` rather than
assembling per-line ``points`` / ``curve_radii`` by hand.  Each gap anchors its
own innermost-of-turn line at the base radius, so a deep fan never pinches to a
sub-base (or negative) arc on the inside of a turn -- the failure mode a single
shared base radius would reintroduce on the deepest bundles.

These tests pin that on the fixtures whose bypasses run the deepest fans
(``funcprofiler_upstream`` fans eight lines, ``bypass_gap2_rightward_overflow``
and ``upward_bypass`` seven) alongside the tapering cases where the two gaps
carry different line counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
    check_no_pinched_corner_radii,
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
def test_bypass_corner_radii_never_pinch(path: Path) -> None:
    """Every U-shaped bypass corner keeps a positive radius.

    Anchoring each gap on its own innermost line keeps the inside-of-turn arc of
    a deep fan at or above the base radius.  Collapsing both gaps onto one shared
    base radius would drive the inside arc of the deepest fan negative -- an
    unrenderable pinch this assertion catches.
    """
    _graph, _offsets, routes = _route(path)
    bypasses = _bypass_routes(routes)
    assert bypasses, f"{path.stem}: expected at least one U-shaped bypass route"
    offenders = [
        (r.edge.source, r.edge.target, r.line_id, r.curve_radii)
        for r in bypasses
        if any(radius <= 0 for radius in r.curve_radii)
    ]
    assert not offenders, f"{path.stem}: bypass corners pinched to <= 0: {offenders}"


@pytest.mark.parametrize("path", BYPASS_FIXTURES, ids=lambda p: p.stem)
def test_bypass_routes_are_offset_baked(path: Path) -> None:
    _graph, _offsets, routes = _route(path)
    bypasses = _bypass_routes(routes)
    assert bypasses, f"{path.stem}: expected at least one U-shaped bypass route"
    assert all(r.offsets_applied for r in bypasses)


def _route_with_radii(radii: list[float]) -> RoutedPath:
    edge = Edge(source="a", target="b", line_id="l")
    return RoutedPath(
        edge=edge,
        line_id="l",
        points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)],
        is_inter_section=True,
        curve_radii=radii,
    )


def test_pinch_guard_flags_non_positive_radius() -> None:
    assert check_no_pinched_corner_radii([_route_with_radii([10.0])]) == []
    flagged = check_no_pinched_corner_radii([_route_with_radii([-2.0])])
    assert len(flagged) == 1 and flagged[0].radius == -2.0
    assert check_no_pinched_corner_radii([_route_with_radii([0.0])])
