"""A bypass that runs on along its source track has no trunk to re-band.

A fan branch whose below-row lane is its own source track opens with no
descent: the lead-out and the traverse stand on one level.  The traverse is
that track carried on, not the floor of a U, so the trunk passes that nest
U floors one step apart must leave it where it is; nudging it off the track
leaves a step a few pixels deep with no room for its two corners.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result
from nf_metro.layout.routing.common import RoutedPath, iter_horizontal_trunks
from nf_metro.themes import resolve_theme

# ``l1`` peels off the junction's middle lane; ``l0`` and ``l3`` carry on along
# their own tracks under ``s1n2`` and rise together into Target.
SOURCE_TRACK_PAIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "curve_invariant_repros"
    / "fan_bypass_pair_rides_source_tracks.mmd"
).read_text()


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
