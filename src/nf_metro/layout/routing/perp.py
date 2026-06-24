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
converging on the marker and re-fanning.
"""

from __future__ import annotations

from nf_metro.layout.routing.context import (
    _get_offset,
    _max_offset_at,
    _RoutingCtx,
)
from nf_metro.layout.routing.corners import reversed_offset
from nf_metro.parser.model import (
    PortSide,
)


def _perp_entry_crossing_x(
    ctx: _RoutingCtx, entry_port_id: str, line_id: str, port_x: float
) -> float | None:
    """Per-line X at which *line_id* crosses a TOP/BOTTOM entry port.

    The inter-section approach lands, and the intra-section drop departs, at
    this one X so the line passes straight through the boundary rather than
    converging on the port marker and re-fanning.  It is the marker X minus the
    line's inter-section feeder bundle index times the offset step, matching the
    approach's reference-on-marker fan: the reference feeder (index 0, e.g. a
    column-aligned vertical drop) sits on the marker, later-index lines fan one
    consistent side.  Returns ``None`` when no bundled inter-section feeder
    reaches the port for this line (nothing to align a crossing X to).
    """
    indices = [
        info[0]
        for edge in ctx.graph.edges_to(entry_port_id)
        if edge.line_id == line_id
        and (info := ctx.bundle_info.get((edge.source, entry_port_id, line_id)))
        is not None
    ]
    if not indices:
        return None
    return port_x - max(indices) * ctx.offset_step


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
    """
    if side == PortSide.TOP:
        return _get_offset(ctx, station_id, line_id)
    off = _get_offset(ctx, station_id, line_id)
    return reversed_offset(off, _max_offset_at(ctx, station_id))
