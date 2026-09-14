"""A single-source fan-out bundle nests concentrically into a shared entry port.

Regression for #1951: three lines fan out from one junction, ride a shared
bypass trunk right and down, and peel off into a common LEFT entry port through
a right->down->right approach.  The turn is a half-turn that transposes the
bundle, so approach-X must run *opposite* to trunk-depth order.  The staggering
pass placed the descents in trunk order instead, tripping both the bundle-order
and peel-off-concentric render guards.  The layout must seat the descents on the
slots ``peeloff_target_slots`` (the guards' oracle) earns.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_peeloff_concentric,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "topologies"
    / "shared_entry_bundle_order_repro.mmd"
)


def _route():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def test_single_source_peeloff_nests_concentrically() -> None:
    _graph, _offsets, routes = _route()
    assert check_peeloff_concentric(_graph, routes) == []
    assert check_bundle_order_preserved(routes) == []


def test_single_source_peeloff_passes_render_curve_invariants() -> None:
    graph, offsets, routes = _route()
    assert_render_curve_invariants(graph, routes, offsets)


def test_single_source_peeloff_reverses_x_against_trunk_depth() -> None:
    """The shallowest-trunk line takes the port-nearest (inner) descent column.

    riboseq rides the shallowest trunk and rnaseq the middle one; the half-turn
    transposes X against trunk depth, so riboseq's descent must sit *inboard*
    (larger X) of rnaseq's, not outboard.
    """
    _graph, _offsets, routes = _route()
    descents = {
        r.line_id: r.points[-3][0]
        for r in routes
        if r.edge.source.startswith("__junction_")
        and r.edge.target.startswith("reporting__entry_left")
    }
    assert {"riboseq", "rnaseq", "tiseq"} <= set(descents)
    assert descents["riboseq"] > descents["rnaseq"] > descents["tiseq"]
