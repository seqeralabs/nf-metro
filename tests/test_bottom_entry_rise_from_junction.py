"""A bundle rising into a BOTTOM entry port.

* The lines keep their order through every turn of the rise: a fan traverse
  turned onto from an ascent nests in the mirror order of one turned onto from
  a descent.
* A source already standing below the target's channel turns into the port
  once, instead of climbing a riser too short for its two corners.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_bundle_order_preserved
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

REPROS = Path(__file__).resolve().parent / "fixtures" / "curve_invariant_repros"
RISE_FROM_JUNCTION = REPROS / "bt_bottom_entry_rise_from_junction.mmd"
BUNDLE_RISES_FROM_JUNCTION = REPROS / "bt_bottom_entry_bundle_rises_from_junction.mmd"
SOURCE_BEYOND_CHANNEL = REPROS / "bt_bottom_entry_source_beyond_channel.mmd"
SOURCE_BEYOND_CHANNEL_SINGLE_BRANCH = (
    REPROS / "bt_bottom_entry_source_beyond_channel_single_branch.mmd"
)


def _laid_out(path: Path) -> MetroGraph:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph


@pytest.mark.parametrize(
    "path",
    [
        RISE_FROM_JUNCTION,
        BUNDLE_RISES_FROM_JUNCTION,
        SOURCE_BEYOND_CHANNEL,
        SOURCE_BEYOND_CHANNEL_SINGLE_BRANCH,
    ],
    ids=lambda p: p.stem,
)
def test_render_completes(path: Path) -> None:
    render_string(path.read_text(), source_dir=str(path.parent))


def test_bundle_rising_into_bottom_entry_keeps_its_order() -> None:
    graph = _laid_out(RISE_FROM_JUNCTION)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_bundle_order_preserved(routes)
    assert violations == [], violations[0].message()


@pytest.mark.parametrize(
    "path",
    [SOURCE_BEYOND_CHANNEL, SOURCE_BEYOND_CHANNEL_SINGLE_BRANCH],
    ids=lambda p: p.stem,
)
def test_source_beyond_channel_turns_into_bottom_entry_once(path: Path) -> None:
    graph = _laid_out(path)
    offsets = compute_station_offsets(graph)
    routes = [
        route
        for route in route_edges(graph, station_offsets=offsets)
        if route.edge.target == "z3__entry_bottom_3"
    ]
    assert {route.line_id for route in routes} == {"m0", "m1", "m2"}
    for route in routes:
        assert len(route.points) == 3, (route.line_id, route.points)
        (_, sy), (cx, cy), (tx, ty) = route.points
        assert cy == sy and cx == tx and ty < sy, (route.line_id, route.points)
