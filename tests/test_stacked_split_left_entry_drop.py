"""A stacked half-turn must not make a split entry cross and recross."""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    check_stacked_split_no_line_recrossing,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "topologies" / "stacked_split_left_entry_drop.mmd"


def test_stacked_split_entry_does_not_cross_and_recross() -> None:
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    entry = graph.stations["target__entry_left_1"]
    final_a = graph.stations["final_a"]
    final_b = graph.stations["final_b"]
    assert entry.y + offsets[(entry.id, "a")] > entry.y + offsets[(entry.id, "b")]
    assert (
        final_a.y + offsets[(final_a.id, "a")] > final_b.y + offsets[(final_b.id, "b")]
    )
    assert not check_stacked_split_no_line_recrossing(graph, routes, offsets)


def test_recrossing_invariant_detects_unreversed_consumer_tracks() -> None:
    """Putting the consumer tracks back in their unreversed order, under lanes
    the layout sealed for the reflected ones, draws each line across the other
    twice."""
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph, validate=False)
    final_a = graph.stations["final_a"]
    final_b = graph.stations["final_b"]
    final_a.y, final_b.y = final_b.y, final_a.y
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    violations = check_stacked_split_no_line_recrossing(graph, routes, offsets)

    assert len(violations) == 1
    assert violations[0].section_id == "target"
    assert len(violations[0].crossings) == 2
