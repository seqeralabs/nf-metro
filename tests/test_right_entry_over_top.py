"""TB RIGHT-entry over-the-top loop: builder, render guard, and #707 lock.

Three layers:

* the constructive :func:`build_concentric_bundle` primitive fans a bundle
  without flips and with concentric corners;
* the always-on render-path guard :func:`assert_render_curve_invariants`
  rejects a flipped bundle regardless of ``validate``;
* the ``tb_right_entry_stack`` fixture lays out, routes, and renders with no
  curve defect.

See issue #707.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.bundle import build_concentric_bundle
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "tb_right_entry_stack.mmd"
)


def _laid_out():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _over_top_centerline():
    # A same-Y source looping over the top into a right-edge port: the shape
    # whose U-turn transposes the bundle end-to-end.
    return [
        (190, 121.5),
        (206, 121.5),
        (206, 44),
        (378, 44),
        (378, 121.5),
        (362, 121.5),
    ]


# --- the constructive primitive -------------------------------------------


def test_build_concentric_bundle_never_flips_over_a_u_turn():
    ea = Edge(source="s", target="p", line_id="alpha")
    eb = Edge(source="s", target="p", line_id="beta")
    routes = build_concentric_bundle(
        [(ea, "alpha", -1.5), (eb, "beta", 1.5)],
        _over_top_centerline(),
        base_radius=10.0,
    )
    assert check_bundle_order_preserved(routes) == []


def test_build_concentric_bundle_corners_are_concentric():
    ea = Edge(source="s", target="p", line_id="alpha")
    eb = Edge(source="s", target="p", line_id="beta")
    routes = build_concentric_bundle(
        [(ea, "alpha", -1.5), (eb, "beta", 1.5)],
        _over_top_centerline(),
        base_radius=10.0,
    )
    # Concentric arcs at a wholesale corner differ by the lines' perpendicular
    # separation (|-1.5 - 1.5| == 3) at every shared bend.
    ra, rb = (r.curve_radii for r in routes)
    assert len(ra) == len(rb) == 4
    for a, b in zip(ra, rb):
        assert abs(abs(a - b) - 3.0) < 1e-6


def test_build_concentric_bundle_rejects_diagonal_centerline():
    e = Edge(source="s", target="p", line_id="x")
    with pytest.raises(ValueError, match="diagonal"):
        build_concentric_bundle([(e, "x", 0.0)], [(0, 0), (10, 10)], base_radius=10.0)


# --- the always-on render guard -------------------------------------------


def test_render_guard_rejects_a_flipped_bundle():
    graph, offsets, routes = _laid_out()
    # Swap the points of the two over-the-top members so the bundle crosses;
    # the guard must reject this regardless of compute_layout's validate flag.
    over_top = [
        r
        for r in routes
        if r.is_inter_section and r.edge.target.startswith("upper__entry_right")
    ]
    assert len(over_top) == 2
    a, b = over_top
    flipped = [r for r in routes if r not in over_top]
    flipped.append(
        RoutedPath(
            edge=a.edge, line_id=a.line_id, points=b.points, is_inter_section=True
        )
    )
    flipped.append(
        RoutedPath(
            edge=b.edge, line_id=b.line_id, points=a.points, is_inter_section=True
        )
    )
    with pytest.raises(CurveInvariantError):
        assert_render_curve_invariants(graph, flipped, offsets)


def test_render_guard_accepts_the_clean_fixture():
    graph, offsets, routes = _laid_out()
    assert_render_curve_invariants(graph, routes, offsets)


# --- end-to-end #707 lock --------------------------------------------------


def test_707_lays_out_under_validation():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph, validate=True)


def test_707_over_top_loop_has_no_curve_defect():
    graph, offsets, routes = _laid_out()
    assert check_bundle_order_preserved(routes) == []
    assert check_concentric_bundle_corners(graph, routes, offsets) == []


def test_707_right_entry_approached_from_its_outward_side():
    # The loop reaches the right-edge port from the right, so the inter-section
    # route's last waypoint before the port is at or past the port's X rather
    # than ploughing in from the left interior.
    graph, _offsets, routes = _laid_out()
    port_id = next(
        p.id
        for p in graph.ports.values()
        if p.is_entry and p.id.startswith("upper__entry_right")
    )
    port_x = graph.stations[port_id].x
    inter = [r for r in routes if r.edge.target == port_id and r.is_inter_section]
    assert inter
    for r in inter:
        assert r.points[-2][0] >= port_x - 1.0
