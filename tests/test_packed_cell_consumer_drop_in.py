"""Regression: a consumer feed into the far member of a packed cell.

A packed cell (``%%metro grid: a, b | col,row``) seats two sections side by
side in one grid cell.  When the right (far) member is fed by a consumer
arriving from a section in the row above and to the right, and that member
carries only cross-axis entry hints (``top``/``left``) inherited from a
geometry-blind default, the entry side must be inferred from where the feed
actually comes from.

The defect (issue #1311): with the entry pinned to the hinted ``left`` side,
the feed could not reach the far-left port without wrapping all the way down
below every section, across the canvas, and back up - a long detour that also
crossed an unrelated connection.  Geometry-aware entry-side inference (#1342,
#1347) resolves the member's entry to its right edge instead, so the feed
drops straight in from the row above.

This locks that outcome: the fed member's entry sits on its right side and the
consumer feed never descends below the packed cell.  It does *not* assert that
every packed-cell feed avoids a bottom-wrap - a feed *out* of a packed cell to
a far target whose entry side is fixed can still dive (tracked separately).
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.core import route_edges_centred
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
FIXTURE = "packed_cell_consumer_drop_in"
MEMBER = "rightmem"
TOL = 2.0


def _layout():
    graph = parse_metro_mermaid((TOPOLOGIES / f"{FIXTURE}.mmd").read_text())
    compute_layout(graph)
    return graph


def test_far_member_entry_resolves_to_right_side() -> None:
    """The fed member's ``main`` entry drops in on its right edge, not its left.

    The fixture hints ``entry: top`` and ``entry: left`` for the member, yet
    the feeder sits above and to the right; geometry-aware inference must place
    the entry port on the member's right edge so the feed drops straight in.
    """
    graph = _layout()
    member = graph.sections[MEMBER]
    right_edge = member.bbox_x + member.bbox_w

    assert member.entry_ports, f"{MEMBER} has no entry port"
    for pid in member.entry_ports:
        port = graph.stations[pid]
        assert port.x >= right_edge - TOL, (
            f"{pid} at x={port.x:.1f} is not on the member's right edge "
            f"({right_edge:.1f}); a left/top entry forces the bottom-wrap of #1311"
        )


def test_consumer_feed_does_not_dive_below_the_cell() -> None:
    """The consumer feed into the far member never descends below the cell.

    The #1311 dive ran the feed down past the bottom of every section before
    turning back up into the port.  Every inter-section route reaching the
    member's entry must stay at or above the packed cell's bottom edge.
    """
    graph = _layout()
    member = graph.sections[MEMBER]
    cell_mate = graph.sections["leftmem"]
    cell_bottom = max(
        member.bbox_y + member.bbox_h,
        cell_mate.bbox_y + cell_mate.bbox_h,
    )

    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    feeds = [
        rp
        for rp in routes
        if rp.is_inter_section and rp.edge.target in member.entry_ports
    ]
    assert feeds, f"expected an inter-section feed into {MEMBER}"
    for rp in feeds:
        max_y = max(y for _x, y in rp.points)
        assert max_y <= cell_bottom + TOL, (
            f"feed {rp.edge.source}->{rp.edge.target} dives to y={max_y:.1f}, "
            f"below the cell bottom ({cell_bottom:.1f}) - the #1311 bottom-wrap"
        )
