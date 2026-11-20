"""Perpendicular port-crossing geometry: the shared lateral conventions for a
line crossing a TOP/BOTTOM section boundary.

A "perpendicular crossing" is a line leaving or entering a section through a
TOP or BOTTOM port: it rises out of a horizontal-flow section into the
inter-row corridor, or drops into a TB section's trunk.  The two ends of that
shape are routed in different handler families - the exit (up-and-over) in
``inter_section_handlers._route_perp_exit_over`` and the entry drop in
``tb_handlers._route_perp_entry`` / ``_route_perp_entry_from_corridor`` - but
they must seat their bundles on the *same* per-line lateral so the legs stay
parallel across the shared port.  Those conventions live here so they can be
read and verified together.

TOP vs BOTTOM sign convention
-----------------------------
The per-line lateral order flips between rising and dropping.  A TOP riser
keeps the raw per-line offset (``_get_offset``); a BOTTOM riser reflects it
(``reversed_offset``).  ``_perp_riser_lateral`` is the single source of that rule,
so both the up-and-over exit corridor and the matching entry drop pick up the
identical lateral for a given line and side.

``_perp_entry_crossing_x`` expresses the matching X for the *aligned* case: the
single X at which a bundled inter-section feeder and its intra-section drop both
cross the port, so the line passes straight through the boundary instead of
converging on the marker and re-fanning.  It tracks the feeder's own section
lane (``_tb_x_offset``) for a vertical-flow feeder -- a ``-x`` downward (TB)
drop and its ``+x`` upward (BT) image alike -- and the ``-x`` up-and-over riser
side for any other feeder.  Where distinct lines share the entry on disjoint
feeders (``needs_perp_approach_fan``), each crosses where the lane plan puts it
(``_planned_entry_crossing``), since those feeders share one column trunk.
"""

from __future__ import annotations

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.geometry import lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.routing.common import (
    ENTRY_SIDE_DIRECTION,
    needs_perp_approach_fan,
    resolve_section,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _max_offset_at,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.layout.routing.orientation import lateral_order_sign
from nf_metro.parser.model import (
    MetroGraph,
    Port,
    PortSide,
)


def _lane_stacked_perp_entry(graph: MetroGraph, port_id: str) -> bool:
    """Whether *port_id* is a TOP or BOTTOM entry of a section whose lanes
    run along y."""
    port = graph.ports[port_id]
    if port.side not in (PortSide.TOP, PortSide.BOTTOM):
        return False
    return lanes_run_along_y(graph.section_for_port(port).direction)


def _planned_entry_crossing(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str, port_x: float
) -> float:
    """Where the lane plan crosses *line_id* through a TOP or BOTTOM entry:
    its offset read along the right-hand normal of the entry's travel."""
    side = ctx.graph.ports[entry_port_id].side
    sign = lateral_order_sign(ENTRY_SIDE_DIRECTION[side])
    return port_x + sign * _get_offset(ctx, entry_port_id, line_id)


def _bundled_feeders(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str
) -> list[tuple[int, int, str]]:
    """``(bundle index, bundle size, source)`` for each feeder of *line_id*.

    The inter-section feeders reaching *entry_port_id* on *line_id* that carry a
    cross-boundary bundle index, each with its bundle size and source port.  Empty
    when the line reaches the port with no bundled feeder.
    """
    return [
        (info[0], info[1], edge.source)
        for edge in ctx.graph.edges_to(entry_port_id)
        if edge.line_id == line_id
        and (info := ctx.bundle_info.get((edge.source, entry_port_id, line_id)))
        is not None
    ]


def _perp_entry_crossing_x(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str, port_x: float
) -> float | None:
    """Per-line X at which *line_id* crosses a TOP/BOTTOM entry port.

    The inter-section approach lands, and the intra-section drop departs, at
    this one X so the line passes straight through the boundary rather than
    converging on the port marker and re-fanning.

    A TOP or BOTTOM entry of a section whose lanes run along y, and a
    distinct-line perp entry (:func:`needs_perp_approach_fan`), whose feeder
    lanes all collapse onto one column trunk, are crossed where the lane plan
    puts each line (:func:`_planned_entry_crossing`).

    Failing both, a vertical-flow (TB/BT) feeder dropping a *single* bundle crosses
    on its own section lane -- the exact X
    :func:`inter_section_handlers._route_tb_bottom_exit` lands at,
    :func:`context._tb_x_offset` -- so the crossing tracks that lane.  Its
    per-line lane width (one offset step per distinct line) is narrower than the
    feeder's index in the *whole* cross-boundary bundle, which counts every
    converging feeder's every line: anchoring on the bundle index instead would
    splay the few descending lines across the wider fan and straddle the target
    station, pinching the drop's turn-in corner.

    Any other feeder reaches the drop via the up-and-over riser, whose reference
    leg fans to ``-x``, so its crossing offsets the marker by the feeder's bundle
    index on that side.  Returns ``None`` when no bundled inter-section feeder
    reaches the port for this line (nothing to align to).
    """
    if _lane_stacked_perp_entry(ctx.graph, entry_port_id) or needs_perp_approach_fan(
        ctx.graph, entry_port_id
    ):
        return _planned_entry_crossing(ctx, entry_port_id, line_id, port_x)
    feeders = _bundled_feeders(ctx, entry_port_id, line_id)
    if not feeders:
        return None
    max_index, _count, source = max(feeders)
    feeder_st = ctx.graph.stations.get(source)
    section_id = feeder_st.section_id if feeder_st else None
    feeder_sec = ctx.graph.sections.get(section_id) if section_id else None
    if feeder_sec is None:
        # A junction feeder carries no ``section_id`` of its own; resolve it
        # through its incoming edge to the section it descends from.  A
        # vertical-flow (TB/BT) upstream crosses on the trunk column -- the same
        # X the inter-section approach lands on -- rather than falling to the
        # generic bundle-index fan, which would offset the lone descending line
        # off the trunk and part it from the approach at the boundary.  A
        # horizontal-flow upstream is left unresolved so it keeps that fan.
        upstream = resolve_section(ctx.graph, feeder_st, prefer_upstream=True)
        if upstream is not None and lanes_run_along_x(upstream.direction):
            return port_x + _tb_x_offset(ctx, source, line_id, upstream.id)
    if feeder_sec is not None and lanes_run_along_x(feeder_sec.direction):
        return port_x + _tb_x_offset(ctx, source, line_id, section_id)
    if feeder_sec is not None and lanes_run_along_y(feeder_sec.direction):
        # Horizontal-flow feeder: the up-and-over drop lands each line on the
        # entry port's own per-line offset, so the crossing tracks that, not the
        # feeder's section lane.
        return port_x + _get_offset(ctx, entry_port_id, line_id)
    return port_x - max_index * ctx.offset_step


def _aligned_horizontal_drop_entry(ctx: _RoutingCtx, exit_port_id: str) -> str | None:
    """The TOP/BOTTOM entry port a perp exit drops straight into, if aligned.

    A TOP/BOTTOM exit whose only edge feeds one column-aligned perpendicular
    entry on a horizontal-flow (LR/RL) section: the line leaves vertically and
    drops straight onto that entry without an up-and-over corridor.  Returns the
    entry port id for that case, else ``None`` (a cross-column drop, a fan to
    several entries, a distinct-line approach fan, or a vertical-flow target all
    keep the perp reflection).
    """
    graph = ctx.graph
    exit_port = graph.ports.get(exit_port_id)
    if exit_port is None or exit_port.is_entry:
        return None
    targets = {edge.target for edge in graph.edges_from(exit_port_id)}
    entry_port = graph.ports.get(next(iter(targets))) if len(targets) == 1 else None
    if entry_port is None or not entry_port.is_entry:
        return None
    if entry_port.side not in (PortSide.TOP, PortSide.BOTTOM):
        return None
    entry_section = graph.section_for_port(entry_port)
    if not lanes_run_along_y(entry_section.direction):
        return None
    if needs_perp_approach_fan(graph, entry_port.id):
        return None
    exit_st = graph.stations[exit_port_id]
    entry_st = graph.stations[entry_port.id]
    if abs(exit_st.x - entry_st.x) > COORD_TOLERANCE:
        return None
    return entry_port.id


def _perp_riser_lateral(
    ctx: _RoutingCtx, station_id: str, line_id: str, side: PortSide
) -> float:
    """Per-line lateral X continuing a perpendicular riser's convention.

    A TOP riser keeps the raw per-line offset; a BOTTOM riser reflects it via
    ``reversed_offset`` (the lateral order flips between rising and dropping).  Both the
    up-and-over exit corridor and the matching entry drop seat their bundle
    with this lateral so the two legs stay parallel across the shared port.

    A perpendicular exit dropping straight into a horizontal-flow section's
    aligned entry is the exception: the exit carries only the lines shared across
    the seam, but the receiving section anchors them against its full bundle
    (reserving slots for lines that peel off inside it), so the exit must inherit
    the entry's per-line offset to stay co-aligned through the boundary -- the
    same per-line X the entry's intra drop departs from.
    """
    entry_port_id = _aligned_horizontal_drop_entry(ctx, station_id)
    if entry_port_id is not None:
        return _planned_entry_crossing(ctx, entry_port_id, line_id, 0.0)
    if side == PortSide.TOP:
        return _get_offset(ctx, station_id, line_id)
    off = _get_offset(ctx, station_id, line_id)
    return reversed_offset(off, _max_offset_at(ctx, station_id))


def perp_corridor_feeder(graph: MetroGraph, port_id: str) -> Port | None:
    """The perpendicular exit port feeding entry *port_id* over the corridor.

    A TOP/BOTTOM exit port on a same-row section rises into the inter-row
    corridor band and runs across to feed a perpendicular entry (the
    up-and-over shape); the exit and entry ports then share the corridor Y.
    The drop out of that entry must continue the corridor's per-line descent
    order, so it needs the feeding exit port's side.

    A perpendicular exit that drops across rows into the entry below (the
    LR -> TB top-drop) is *not* collinear with the entry and keeps its own
    drop convention, so it is excluded by the shared-Y test.

    This mirrors the exemption in ``_guard_perp_entry_feed_not_collinear``:
    that guard permits exactly the collinear perp-exit feed this routes.
    """
    entry_y = graph.stations[port_id].y
    for feed in graph.edges_to(port_id):
        port = graph.ports.get(feed.source)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and abs(graph.station_for_edge_source(feed).y - entry_y) <= COORD_TOLERANCE
        ):
            return port
    return None
