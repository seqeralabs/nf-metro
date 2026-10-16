"""A bypass that runs on along its source track has no trunk to re-band.

A fan branch whose below-row lane is its own source track opens with no
descent: the lead-out and the traverse stand on one level.  The traverse is
that track carried on, not the floor of a U, so the trunk passes that nest
U floors one step apart must leave it where it is; nudging it off the track
leaves a step a few pixels deep with no room for its two corners.
"""

from __future__ import annotations

import warnings

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.routing.common import RoutedPath, iter_horizontal_trunks
from nf_metro.themes import resolve_theme

# ``l1`` peels off the junction's middle lane; ``l0`` and ``l3`` carry on along
# their own tracks under ``s1n2`` and rise together into Target.
SOURCE_TRACK_PAIR = """\
%%metro line: l0 | L0 | #e6194b
%%metro line: l1 | L1 | #3cb44b
%%metro line: l2 | L2 | #4363d8
%%metro line: l3 | L3 | #f58231
%%metro line: l4 | L4 | #911eb4
%%metro line: l5 | L5 | #46f0f0
graph LR
    subgraph sec0 [Source]
        %%metro direction: TB
        s0n3 -->|l3,l5| s0n4
    end
    subgraph sec2 [Target]
        s2n0 -->|l2,l3,l4,l5| s2n1
        s2n1 -->|l2,l3,l4,l5| s2n2
        s2n2 -->|l0,l2,l3,l4,l5| s2n3
    end
    s0n3 -->|l0| s2n2
    s0n4 -->|l1| s1n2
    s1n2 -->|l2| s2n1
    s0n4 -->|l3| s2n0
"""


def _routes(text: str) -> list[RoutedPath]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(text)
        return render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan.routes


def test_source_track_bypasses_stay_on_their_tracks() -> None:
    riding = [
        route
        for route in _routes(SOURCE_TRACK_PAIR)
        if route.edge.target == "sec2__entry_left_2"
        and route.edge.source.startswith("__junction")
    ]
    assert {route.line_id for route in riding} == {"l0", "l3"}
    for route in riding:
        lead_y = route.points[0][1]
        assert all(abs(y - lead_y) <= 0.01 for _x, y in route.points[:4]), (
            route.line_id,
            route.points,
        )


def test_zero_length_flank_is_not_a_trunk_flank() -> None:
    route = next(
        route
        for route in _routes(SOURCE_TRACK_PAIR)
        if route.edge.target == "sec2__entry_left_2" and route.line_id == "l0"
    )
    assert route.points[1] == route.points[2]
    assert list(iter_horizontal_trunks(route)) == []
