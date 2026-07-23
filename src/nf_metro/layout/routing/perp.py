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
feeders (``needs_perp_approach_fan``), ``_perp_approach_fan_x`` instead fans them
by bundle index, since those feeders share one column trunk.
"""

from __future__ import annotations

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.geometry import lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.routing.common import needs_perp_approach_fan, resolve_section
from nf_metro.layout.routing.context import (
    _get_offset,
    _max_offset_at,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.parser.model import (
    PortSide,
)


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


def _perp_approach_fan_x(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str, port_x: float
) -> float:
    """Per-line X channel a line takes into a distinct-line perp entry port.

    Where distinct lines share a perpendicular entry (:func:`needs_perp_approach_fan`)
    the single-line feeders all sit on one column trunk, so each must fan onto its
    own approach channel rather than share the trunk X.  The bundle index orders
    the feeders by approach: index 0 is the outermost feeder (the one descending
    from furthest away, which wraps around the intervening boxes), so it takes the
    channel furthest toward the turn side (``-x``) and the trunk-near feeder
    (highest index) stays on ``port_x``.  This keeps the outermost approach on the
    outside of the bend, matching the intra-section bundle order, so the feeders
    do not cross.  The inter-section feeder drop and the intra-section drop both
    anchor on this one X.  Lines without a bundled feeder stay on the trunk.
    """
    index, count, _source = next(
        iter(_bundled_feeders(ctx, entry_port_id, line_id)), (0, 1, "")
    )
    return port_x - (count - 1 - index) * ctx.offset_step


def _perp_entry_crossing_x(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str, port_x: float
) -> float | None:
    """Per-line X at which *line_id* crosses a TOP/BOTTOM entry port.

    The inter-section approach lands, and the intra-section drop departs, at
    this one X so the line passes straight through the boundary rather than
    converging on the port marker and re-fanning.

    A distinct-line perp entry (:func:`needs_perp_approach_fan`) fans its
    single-line feeders onto parallel approach channels by bundle index --
    :func:`_perp_approach_fan_x` -- since their feeder lanes all collapse onto
    one column trunk.

    Otherwise, a vertical-flow (TB/BT) feeder dropping a *single* bundle crosses
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
    if needs_perp_approach_fan(ctx.graph, entry_port_id):
        return _perp_approach_fan_x(ctx, entry_port_id, line_id, port_x)
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
        # vertical-flow (TB/BT) feeder dropping into the port is then recognised
        # below and crosses on the trunk column -- the same X the inter-section
        # approach lands on -- rather than falling to the generic bundle-index
        # fan, which would offset the lone descending line off the trunk and part
        # it from the approach at the boundary.
        upstream = resolve_section(ctx.graph, feeder_st, prefer_upstream=True)
        if upstream is not None and lanes_run_along_x(upstream.direction):
            feeder_sec = upstream
            section_id = upstream.id
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
    ctx: _RoutingCtx,
    station_id: str,
    line_id: str,
    side: PortSide,
    section_id: str | None,
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
        return _get_offset(ctx, entry_port_id, line_id)
    if side == PortSide.TOP:
        return _get_offset(ctx, station_id, line_id)
    off = _get_offset(ctx, station_id, line_id)
    return reversed_offset(off, _max_offset_at(ctx, station_id))
