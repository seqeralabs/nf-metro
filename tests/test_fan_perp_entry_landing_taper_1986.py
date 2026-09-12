"""A fan-out's peel order propagates to its free upstream perp-entry feeder.

Regression lock for #1986.  Section ``a`` exits LEFT with a two-line bundle
``g,h`` into section ``b``'s shared TOP entry port.  ``b`` owns the divergence
junction that peels ``g`` on to a top-entry target and ``h`` to a side-entry
target, which pins ``b``'s per-line crossing lanes.  ``a`` is a free source, so
its bundle order is propagated to match ``b``'s peel order: both lines descend
straight from ``a`` into their landing lanes, sharing no crossing point off a
station and meeting their intra-section continuations collinearly.
"""

from __future__ import annotations

import warnings

from layout_validator import apply_route_offsets, check_route_segment_crossings
from test_fan_left_exit_landing_settled_1980 import _TOP_ENTRY_FAN

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

_NON_FREE_FEEDER = """\
%%metro title: Non-free perp-entry feeder
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: z | 2,0
%%metro grid: a | 1,0
%%metro grid: b | 1,1
%%metro grid: c | 1,2
%%metro grid: d | 0,1

graph LR
    subgraph z [Zeta]
        %%metro exit: left | g, h
        z1[Z one]
        z2[Z two]
        z1 -->|g,h| z2
    end
    subgraph a [Alpha]
        %%metro entry: right | g, h
        %%metro exit: left | g, h
        a1[A one]
        a2[A two]
        a1 -->|g,h| a2
    end
    subgraph b [Beta]
        %%metro entry: top | g, h
        %%metro exit: left | g, h
        b1[B one]
        b2[B two]
        b1 -->|g,h| b2
    end
    subgraph c [Gamma]
        %%metro entry: top | g
        c1[C one]
        c2[C two]
        c1 -->|g| c2
    end
    subgraph d [Delta]
        %%metro entry: right | h
        d1[D one]
        d2[D two]
        d1 -->|h| d2
    end
    z2 -->|g,h| a1
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""


def _route() -> tuple:
    graph = parse_metro_mermaid(_TOP_ENTRY_FAN)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


def _perp_entry_port_id(graph) -> str:
    return next(
        pid
        for pid, port in graph.ports.items()
        if port.is_entry
        and port.section_id == "b"
        and port.side in (PortSide.TOP, PortSide.BOTTOM)
    )


def _diagonal_segments(points: list[tuple[float, float]]) -> list:
    return [
        (points[i], points[i + 1])
        for i in range(len(points) - 1)
        if abs(points[i][0] - points[i + 1][0]) > 0.5
        and abs(points[i][1] - points[i + 1][1]) > 0.5
    ]


def test_perp_entry_fan_descends_without_crossing() -> None:
    graph, offsets, routes = _route()
    crossings = check_route_segment_crossings(graph, (offsets, routes))
    assert crossings == [], "\n".join(v.message() for v in crossings)


def test_perp_entry_descent_runs_straight() -> None:
    graph, offsets, routes = _route()
    port_id = _perp_entry_port_id(graph)
    for line_id in ("g", "h"):
        descent = next(
            r for r in routes if r.edge.target == port_id and r.line_id == line_id
        )
        points = apply_route_offsets(descent, offsets)
        diagonals = _diagonal_segments(points)
        assert diagonals == [], (
            f"line {line_id} descends via a crossover, not a straight drop: "
            f"diagonal segments {diagonals}"
        )
        departure = next(
            r for r in routes if r.edge.source == port_id and r.line_id == line_id
        )
        dep_start = apply_route_offsets(departure, offsets)[0]
        assert points[-1] == dep_start, (
            f"line {line_id} approach lands at {points[-1]} but its "
            f"intra-section continuation leaves from {dep_start}"
        )


def test_perp_entry_fan_lands_on_distinct_peel_lanes() -> None:
    graph, offsets, routes = _route()
    port_id = _perp_entry_port_id(graph)

    def _landing_x(line_id: str) -> float:
        descent = next(
            r for r in routes if r.edge.target == port_id and r.line_id == line_id
        )
        return apply_route_offsets(descent, offsets)[-1][0]

    landings = {line_id: _landing_x(line_id) for line_id in ("g", "h")}
    assert landings["g"] == 374.0
    assert landings["h"] == 370.0


def test_constrained_feeder_keeps_its_own_order() -> None:
    graph = parse_metro_mermaid(_NON_FREE_FEEDER)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
    feeder = {lid: offsets[("a1", lid)] for lid in ("g", "h")}
    divergence = {lid: offsets[("b1", lid)] for lid in ("g", "h")}
    assert feeder == {"g": 0.0, "h": 4.0}, (
        "an upstream-fed feeder is not free and must keep its own bundle order"
    )
    assert divergence == {"g": 4.0, "h": 0.0}, (
        "the divergence section still re-slots onto its own peel order"
    )
