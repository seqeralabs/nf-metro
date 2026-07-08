"""Regression: routing a feed into the far member of a packed cell.

A packed cell (``%%metro grid: a, b | col,row``) seats two sections side by
side in one grid cell.  The right (far) member is fed both by a consumer from
the row above-right (a cross-row inter-row bypass) and by its left cell-mate (a
same-row feed).  The member's single entry sits on its right edge, facing the
above-right feeder, so the cell-mate feed reaches it by looping over the
member's top -- an over-top wrap pinned deep in the inter-row gap by the
member's header clearance.

Issue #1311 covers two faults this layout exposes:

* the consumer feed from the row above must drop straight in, never diving
  below the whole cell to reach the port (``test_consumer_feed_does_not_dive``);
* the same-row cell-mate's over-top wrap must nest *beneath* the longer-haul
  cross-row bypass sharing the gap -- the bypass belongs further up the gap --
  so the two run as parallel lanes rather than the wrap's riser crossing the
  bypass (``test_intra_row_wrap_nests_under_inter_row_bypass``).
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
CELL_MATE = "leftmem"
TOL = 2.0


def _layout():
    graph = parse_metro_mermaid((TOPOLOGIES / f"{FIXTURE}.mmd").read_text())
    compute_layout(graph)
    return graph


def _routes(graph):
    offsets = compute_station_offsets(graph)
    return route_edges_centred(graph, station_offsets=offsets)


def _segments(points):
    return list(zip(points, points[1:]))


def _segments_cross(a, b) -> bool:
    """Whether any segment of polyline *a* properly crosses one of *b*."""

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    for p1, p2 in _segments(a):
        for p3, p4 in _segments(b):
            d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
            d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
            if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
                return True
    return False


def test_far_member_entry_resolves_to_right_side() -> None:
    """The fed member's ``main`` entry sits on its right edge, facing the feeder.

    The fixture hints ``entry: top`` and ``entry: left``, yet the feeder sits
    above and to the right; geometry-aware inference places the entry on the
    member's right edge so the above-right feed drops straight in.
    """
    graph = _layout()
    member = graph.sections[MEMBER]
    right_edge = member.bbox_x + member.bbox_w
    assert member.entry_ports, f"{MEMBER} has no entry port"
    for pid in member.entry_ports:
        port = graph.stations[pid]
        assert port.x >= right_edge - TOL, (
            f"{pid} at x={port.x:.1f} is not on the member's right edge "
            f"({right_edge:.1f})"
        )


def test_consumer_feed_does_not_dive() -> None:
    """The above-row consumer feed never descends below the packed cell.

    The #1311 dive ran the feed down past the bottom of every section before
    turning back up into the port; the drop-in must stay at or above the cell.
    """
    graph = _layout()
    member = graph.sections[MEMBER]
    cell_mate = graph.sections[CELL_MATE]
    cell_bottom = max(
        member.bbox_y + member.bbox_h,
        cell_mate.bbox_y + cell_mate.bbox_h,
    )
    feeds = [
        rp
        for rp in _routes(graph)
        if rp.is_inter_section and rp.edge.target in member.entry_ports
    ]
    assert feeds, f"expected an inter-section feed into {MEMBER}"
    for rp in feeds:
        max_y = max(y for _x, y in rp.points)
        assert max_y <= cell_bottom + TOL, (
            f"feed {rp.edge.source}->{rp.edge.target} dives to y={max_y:.1f}, "
            f"below the cell bottom ({cell_bottom:.1f})"
        )


def test_intra_row_wrap_nests_under_inter_row_bypass() -> None:
    """The cell-mate's over-top wrap does not cross the cross-row bypass.

    The same-row feed from the left cell-mate loops over the member's top to
    reach its right entry; the longer-haul bypass from the row above shares the
    inter-row gap.  The bypass must ride the deeper (higher) lane so the wrap
    nests beneath it, rather than the wrap's riser crossing the bypass run.
    """
    graph = _layout()
    member = graph.sections[MEMBER]
    routes = _routes(graph)

    wrap = next(
        (
            rp
            for rp in routes
            if rp.is_inter_section
            and rp.edge.source.startswith(f"{CELL_MATE}__exit")
            and rp.edge.target in member.entry_ports
        ),
        None,
    )
    bypass = next(
        (
            rp
            for rp in routes
            if rp.is_inter_section and rp.edge.target.startswith(f"{CELL_MATE}__entry")
        ),
        None,
    )
    assert wrap is not None, "expected a same-row cell-mate over-top wrap"
    assert bypass is not None, "expected a cross-row bypass into the cell-mate"

    assert not _segments_cross(wrap.points, bypass.points), (
        "the cell-mate over-top wrap crosses the inter-row bypass"
    )

    # The bypass's highest (smallest-Y) leg must sit above the wrap's peak, so
    # the local wrap nests beneath the longer-haul through route.
    wrap_peak = min(y for _x, y in wrap.points)
    bypass_top = min(y for _x, y in bypass.points)
    assert bypass_top <= wrap_peak + TOL, (
        f"bypass top lane y={bypass_top:.1f} does not sit above the wrap peak "
        f"y={wrap_peak:.1f}"
    )


def test_junction_feeds_descend_as_one_bundle() -> None:
    """The two lines leaving the junction descend together, not on split channels.

    ``main`` (to the packed member) and ``alt`` (to its cell-mate) fan out from
    one junction; they leave the section as one bundle and should descend on
    adjacent tracks, splitting only where ``alt`` turns off - not open on
    independent channels several px apart.
    """
    from nf_metro.layout.constants import OFFSET_STEP

    graph = _layout()
    junction_feeds = [
        rp
        for rp in _routes(graph)
        if rp.is_inter_section and rp.edge.source in graph.junctions
    ]
    descent_x = {rp.line_id: rp.points[1][0] for rp in junction_feeds}
    assert {"main", "alt"} <= descent_x.keys(), "expected main and alt junction feeds"
    assert abs(descent_x["main"] - descent_x["alt"]) <= OFFSET_STEP + TOL, (
        f"junction feeds descend on split channels {descent_x}, not one bundle"
    )
