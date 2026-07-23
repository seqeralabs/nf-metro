"""A bottom-exit junction dropping collinearly into a TOP entry must meet its
in-section drop as one stroke on the trunk column.

A section's ``exit: bottom`` fanning to two targets in the row below, one of
which sits directly beneath the junction and is entered on its TOP: the single
line descends the trunk column into that horizontal-flow (LR) target.  The
inter-section approach (``__junction -> first__entry_top``) must land on the
trunk column and terminate at the port boundary, and the in-section drop
(``first__entry_top -> f1``) must depart from that same column, so the two meet
as one straight stroke at the boundary rather than parting onto a bundle-index
fan lane.

The seam machinery must also agree with the seam-local classifier that this
``BOTTOM -> TOP`` continuation is reversed: the feeding bottom exit is resolved
through the fan-out junction on both sides.
"""

from __future__ import annotations

import warnings

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.reversal import detect_reversed_sections
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = "examples/topologies/bottom_exit_junction_collinear_top_entry.mmd"


def _graph():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(open(FIXTURE).read())
        compute_layout(graph, validate=False)
    return graph


def _routes(graph):
    return route_edges(graph, station_offsets=compute_station_offsets(graph))


def _feed(routes):
    return next(
        r
        for r in routes
        if r.edge.source.startswith("__junction")
        and r.edge.target.startswith("first__entry")
    )


def _drop(routes):
    return next(
        r
        for r in routes
        if r.edge.source.startswith("first__entry") and r.edge.target == "f1"
    )


def test_fixture_validates() -> None:
    """The fixture lays out without any routing guard firing."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(parse_metro_mermaid(open(FIXTURE).read()), validate=True)


def test_feed_and_drop_share_trunk_column() -> None:
    """The inter-section approach and the in-section drop cross the TOP port at
    one X (the trunk column), so they meet as a single straight stroke."""
    graph = _graph()
    routes = _routes(graph)
    port = graph.stations["first__entry_top_1"]
    feed = _feed(routes)
    drop = _drop(routes)
    # The approach lands on, and the drop departs from, the port's trunk column.
    assert abs(feed.points[-1][0] - port.x) <= 1.0, (
        f"approach lands at x={feed.points[-1][0]}, off trunk column {port.x}"
    )
    assert abs(drop.points[0][0] - port.x) <= 1.0, (
        f"drop departs at x={drop.points[0][0]}, off trunk column {port.x}"
    )
    assert abs(feed.points[-1][0] - drop.points[0][0]) <= 1.0, (
        "approach and drop cross the boundary on different columns"
    )


def test_feed_terminates_at_port_boundary() -> None:
    """The collinear approach stops at the target section's boundary, never
    dropping into its interior."""
    graph = _graph()
    section = graph.sections["first"]
    feed = _feed(_routes(graph))
    section_top = section.bbox_y
    assert max(y for _x, y in feed.points) <= section_top + 1.0, (
        f"feed drops to y={max(y for _x, y in feed.points)}, past the section "
        f"boundary at y={section_top}"
    )


def test_bottom_top_seam_machinery_matches_classifier() -> None:
    """The bottom-exit -> top-entry continuation, fed through the fan-out
    junction, is marked reversed by the machinery (matching the classifier)."""
    graph = _graph()
    assert "first" in detect_reversed_sections(graph)
