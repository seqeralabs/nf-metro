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
from nf_metro.parser.mermaid import parse_metro_mermaid

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
