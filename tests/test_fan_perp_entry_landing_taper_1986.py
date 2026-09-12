"""A left-exit fan's descent lands on its target's per-line perp-entry lane.

Regression lock for #1986.  When section ``a`` exits LEFT with a two-line
bundle into section ``b``'s shared TOP entry port, the upstream exit turn nests
its descent channel on ``a``'s settled corner axis.  That axis differs from the
port's per-line crossing lane, so a straight descent to the axis lands off the
lane the intra-section drop departs on.  The descent must taper onto the
crossing lane in the last few px above the boundary so the approach and
departure cross the section boundary at one consistent X for line ``g``.
"""

from __future__ import annotations

import warnings

from test_fan_left_exit_landing_settled_1980 import _TOP_ENTRY_FAN

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_perp_entry_boundary_consistent
from nf_metro.parser.mermaid import parse_metro_mermaid


def _route(source: str) -> tuple:
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        routes = route_edges(graph, station_offsets=offsets)
    return graph, routes


def test_top_entry_fan_landing_crosses_boundary_consistently() -> None:
    graph, routes = _route(_TOP_ENTRY_FAN)
    violations = check_perp_entry_boundary_consistent(graph, routes)
    assert violations == [], "\n".join(v.message() for v in violations)


def test_top_entry_fan_g_approach_meets_departure_lane() -> None:
    graph, routes = _route(_TOP_ENTRY_FAN)
    port = graph.stations["b__entry_top_2"]
    approach = next(
        r for r in routes if r.edge.target == "b__entry_top_2" and r.line_id == "g"
    )
    departure = next(
        r for r in routes if r.edge.source == "b__entry_top_2" and r.line_id == "g"
    )
    assert approach.points[-1] == departure.points[0], (
        f"approach lands at {approach.points[-1]} but departure leaves from "
        f"{departure.points[0]}"
    )
    assert abs(approach.points[-1][1] - port.y) <= 1.0
