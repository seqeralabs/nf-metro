"""Left-exit fan splitting a bundle to a perp-entry target routes without abort.

Regression lock for #1980. A section that receives a two-line bundle, exits
LEFT, and fans that bundle to a perpendicular-entry target plus a side target
carries an upstream exit-turn member whose landing-side corner is deliberately
settled after gap allocation. The settled-turn radius validator must honour that
family contract instead of demanding the not-yet-settled landing corner.
"""

from __future__ import annotations

import warnings

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_bundle_order_preserved,
    check_perp_entry_boundary_consistent,
)
from nf_metro.layout.routing.offsets import _perp_entry_run_turns_right
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

_TOP_ENTRY_FAN = """\
%%metro title: Left-exit fan riser
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: a | 1,0
%%metro grid: b | 1,1
%%metro grid: c | 1,2
%%metro grid: d | 0,1

graph LR
    subgraph a [Alpha]
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
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""

_BOTTOM_ENTRY_FAN = """\
%%metro title: Left-exit fan bottom
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: a | 1,2
%%metro grid: b | 1,1
%%metro grid: c | 1,0
%%metro grid: d | 0,1

graph LR
    subgraph a [Alpha]
        %%metro exit: left | g, h
        a1[A one]
        a2[A two]
        a1 -->|g,h| a2
    end
    subgraph b [Beta]
        %%metro entry: bottom | g, h
        %%metro exit: left | g, h
        b1[B one]
        b2[B two]
        b1 -->|g,h| b2
    end
    subgraph c [Gamma]
        %%metro entry: bottom | g
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
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""

_TOP_ENTRY_FAN_TURN_RIGHT = """\
%%metro title: Right-exit fan riser
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: a | 1,0
%%metro grid: b | 1,1
%%metro grid: c | 1,2
%%metro grid: d | 2,1

graph LR
    subgraph a [Alpha]
        %%metro exit: right | g, h
        a1[A one]
        a2[A two]
        a1 -->|g,h| a2
    end
    subgraph b [Beta]
        %%metro entry: top | g, h
        %%metro exit: right | g, h
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
        %%metro entry: left | h
        d1[D one]
        d2[D two]
        d1 -->|h| d2
    end
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""

_BOTTOM_ENTRY_FAN_TURN_RIGHT = """\
%%metro title: Right-exit fan bottom
%%metro line: g | Green | #2db572
%%metro line: h | Blue | #3f7fdf
%%metro grid: a | 1,2
%%metro grid: b | 1,1
%%metro grid: c | 1,0
%%metro grid: d | 2,1

graph LR
    subgraph a [Alpha]
        %%metro exit: right | g, h
        a1[A one]
        a2[A two]
        a1 -->|g,h| a2
    end
    subgraph b [Beta]
        %%metro entry: bottom | g, h
        %%metro exit: right | g, h
        b1[B one]
        b2[B two]
        b1 -->|g,h| b2
    end
    subgraph c [Gamma]
        %%metro entry: bottom | g
        c1[C one]
        c2[C two]
        c1 -->|g| c2
    end
    subgraph d [Delta]
        %%metro entry: left | h
        d1[D one]
        d2[D two]
        d1 -->|h| d2
    end
    a2 -->|g,h| b1
    b2 -->|g| c1
    b2 -->|h| d1
"""

_PERP_ENTRY_FAN_CASES = {
    "top-entry-turn-left": (_TOP_ENTRY_FAN, False),
    "bottom-entry-turn-left": (_BOTTOM_ENTRY_FAN, False),
    "top-entry-turn-right": (_TOP_ENTRY_FAN_TURN_RIGHT, True),
    "bottom-entry-turn-right": (_BOTTOM_ENTRY_FAN_TURN_RIGHT, True),
}


@pytest.mark.parametrize(
    "label",
    ("top-entry", "bottom-entry"),
)
def test_left_exit_fan_to_perp_entry_routes(label: str) -> None:
    source = _TOP_ENTRY_FAN if label == "top-entry" else _BOTTOM_ENTRY_FAN
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        routes = route_edges(graph, station_offsets=offsets)

    upstream_turn = [
        route
        for route in routes
        if route.edge.source == "a__exit_left_0" and route.line_id == "g"
    ]
    assert upstream_turn, f"{label}: upstream exit-turn member did not route"
    turn = upstream_turn[0]
    assert turn.exit_turn_segment_rank is not None
    assert turn.points[0] != turn.points[-1], (
        f"{label}: settled turn collapsed to a zero-length path"
    )


@pytest.mark.parametrize(
    "label",
    tuple(_PERP_ENTRY_FAN_CASES),
)
def test_left_exit_fan_perp_entry_bundle_order_preserved(label: str) -> None:
    """The bundle turning in from the shared perp-entry port must not cross itself.

    The fan-out divergence in Beta peels ``g`` and ``h`` to different targets, so
    the trunk carries them in peel order.  The bundle rises (or drops) into
    Beta's entry port and turns once into that trunk; unless the port is stacked
    as the mirror of the trunk for its entry side and turn direction, the two
    lines swap sides through the corner.  The arrival order reverses on an XOR of
    two independent axes -- the entry side (TOP versus BOTTOM) and the direction
    the run turns out of the port (:func:`_perp_entry_run_turns_right`) -- so all
    four combinations are pinned here.  Two invariants hold each: the intra turn
    keeps one bundle-order sign across the corner, and the inter-section approach
    and the intra drop cross the port boundary at one consistent per-line X (no
    S-cusp on the box edge).
    """
    source, expected_turns_right = _PERP_ENTRY_FAN_CASES[label]
    graph = parse_metro_mermaid(source)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        routes = route_edges(graph, station_offsets=offsets)

    entry_port_id = next(
        pid
        for pid, port in graph.ports.items()
        if port.is_entry
        and port.section_id == "b"
        and port.side in (PortSide.TOP, PortSide.BOTTOM)
    )
    assert _perp_entry_run_turns_right(graph, entry_port_id) is expected_turns_right, (
        f"{label}: fixture does not exercise the intended turn direction"
    )
    entry_bundle = [
        v
        for v in check_bundle_order_preserved(routes)
        if v.edge_source == entry_port_id and v.edge_target == "b1"
    ]
    assert not entry_bundle, (
        f"{label}: bundle crosses at Beta's perp entry: "
        + "; ".join(v.message() for v in entry_bundle)
    )

    boundary = [
        v
        for v in check_perp_entry_boundary_consistent(graph, routes)
        if v.port_id == entry_port_id
    ]
    assert not boundary, (
        f"{label}: line reverses lateral direction at Beta's perp entry boundary: "
        + "; ".join(v.message() for v in boundary)
    )
