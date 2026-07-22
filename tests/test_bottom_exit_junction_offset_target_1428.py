"""A bottom-exit junction feeding an offset target must not plough through an
intervening section.

A section's ``exit: bottom`` that fans to several targets in the row below is
routed by ``_route_bottom_exit_junction``.  A target one or more columns past a
same-row neighbour is reached over the clear inter-row gap, so its horizontal
leg never cuts through the neighbour's interior.
"""

from __future__ import annotations

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.normalize import (
    _h_segment_crosses_other_section,
    _v_segment_crosses_other_section,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = "examples/topologies/bottom_exit_junction_offset_target.mmd"


def _graph():
    graph = parse_metro_mermaid(open(FIXTURE).read())
    compute_layout(graph, validate=False)
    return graph


def _routes(graph):
    return route_edges(graph, station_offsets=compute_station_offsets(graph))


def test_fixture_validates() -> None:
    """The fixture lays out without any inter-section routing guard firing."""
    compute_layout(parse_metro_mermaid(open(FIXTURE).read()), validate=True)


def test_offset_feed_clears_intervening_section() -> None:
    """The bottom-exit junction's feed to the offset LEFT-entry target must not
    cross the interior of the section between the descent column and the port."""
    graph = _graph()
    offset_feeds = [
        r
        for r in _routes(graph)
        if r.edge.source.startswith("__junction")
        and r.edge.target.startswith("second__entry")
    ]
    assert offset_feeds, "expected the junction feed into the offset target"
    for rp in offset_feeds:
        exclude = {
            sid
            for sid in (
                graph.stations[rp.edge.source].section_id,
                graph.stations[rp.edge.target].section_id,
            )
            if sid
        }
        for (x1, y1), (x2, y2) in zip(rp.points, rp.points[1:]):
            if abs(y1 - y2) <= 1.0:  # horizontal leg
                assert not _h_segment_crosses_other_section(
                    graph, x1, x2, y1, exclude
                ), f"{rp.edge.source}->{rp.edge.target} runs through a section box"
            if abs(x1 - x2) <= 1.0:  # vertical leg
                assert not _v_segment_crosses_other_section(
                    graph, x1, y1, y2, exclude
                ), f"{rp.edge.source}->{rp.edge.target} drops through a section box"
