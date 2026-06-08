"""Inter-section edge routing: bypass, entry wraps, around-section,
inter-row corridors, stepped descent, and L-shape handlers.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    JUNCTION_MARGIN,
    MERGE_ROUTE_MARGIN,
    SECTION_ROUTE_CLEARANCE,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    _center_inter_row_channel,
    _sections_in_col,
    bundle_width,
    bypass_bottom_y,
    clear_channel_of_section_edge,
    col_left_edge,
    col_right_edge,
    column_gap_edges,
    endpoint_port_xs,
    horizontal_direction,
    inter_column_channel_x,
    inter_row_channel_y,
    resolve_section,
    row_bottom_edge,
    row_top_edge,
    symmetric_bundle_midpoint,
    vertical_direction,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _has_intervening_sections,
    _resolve_section_col,
    _resolve_section_colrow,
    _resolve_section_row,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import (
    bypass_radii,
    concentric_corner_radius,
    corner_radius,
    l_shape_radii,
    reference_anchored_radius,
)
from nf_metro.layout.routing.normalize import (
    _clear_channel_x_in_band,
    _gap_channel_base,
    _h_segment_crosses_other_section,
    _has_other_row_section_in_col_range,
)
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Station,
)


def _route_inter_section(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route edges between ports/junctions using L-shapes (no diagonals)."""
    graph = ctx.graph
    is_inter = (src.is_port or edge.source in ctx.junction_ids) and (
        tgt.is_port or edge.target in ctx.junction_ids
    )
    if not is_inter:
        return None

    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy
    horizontal = horizontal_direction(dx)
    vertical = vertical_direction(dy)

    i, n = ctx.bundle_info.get((edge.source, edge.target, edge.line_id), (0, 1))

    # Check for TB BOTTOM exit
    src_port = graph.ports.get(edge.source)
    src_is_tb_bottom = (
        src_port is not None
        and not src_port.is_entry
        and src_port.side == PortSide.BOTTOM
        and src.section_id in ctx.tb_sections
    )

    # Resolve section columns and rows for bypass detection
    src_col, src_row = _resolve_section_colrow(graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(graph, tgt)
    needs_bypass = (
        src_col is not None
        and tgt_col is not None
        and abs(tgt_col - src_col) > 1
        and (
            _has_intervening_sections(graph, src_col, tgt_col, src_row)
            # A cross-row L-shape runs its horizontal leg at the target
            # entry Y, which lands in the TARGET row; intervening sections
            # there are plowed through even when the source row is clear.
            or (
                src_row is not None
                and tgt_row is not None
                and tgt_row != src_row
                and _has_intervening_sections(graph, src_col, tgt_col, tgt_row)
            )
        )
    )

    if abs(dy) < COORD_TOLERANCE_FINE and not needs_bypass:
        # Same Y: straight horizontal
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (tx, ty)],
            is_inter_section=True,
        )

    if src_is_tb_bottom and ctx.station_offsets:
        return _route_tb_bottom_exit(edge, src, tgt, ctx)

    # TOP entry port: L-shape so the line gets a proper curve into the
    # section.  Must be checked before the same-X shortcut, which would
    # produce a straight vertical drop with no horizontal lead-in.
    tgt_port = graph.ports.get(edge.target)
    if tgt_port and tgt_port.is_entry and tgt_port.side == PortSide.TOP:
        return _route_top_entry_l_shape(edge, src, tgt, i, n, ctx)

    if abs(dx) < COORD_TOLERANCE:
        # Same X: straight vertical drop
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (tx, ty)],
            is_inter_section=True,
        )

    if edge.source in ctx.bottom_exit_junctions:
        return _route_bottom_exit_junction(edge, src, tgt, i, n, ctx)

    if needs_bypass:
        assert src_col is not None and tgt_col is not None
        # Merge dispatch: trunk gets full bypass to entry port,
        # branches get truncated descent to trunk level.
        if edge.target in ctx.merge.trunk_source:
            if ctx.merge.trunk_source[edge.target] == edge.source:
                return _route_merge_trunk(
                    edge, src, tgt, i, src_col, tgt_col, ctx, src_row
                )
            return _route_merge_branch(edge, src, ctx, src_col)
        # A feeder into a LEFT entry one row directly below descends into that
        # row before its horizontal run, so intervening sections in the SOURCE
        # row are not obstacles. When the L-shape's horizontal at the entry Y
        # is clear of other sections, drop straight in instead of looping to
        # the canvas bottom (_route_bypass). Restricted to adjacent rows: a
        # multi-row descent's vertical leg could pierce an intervening row.
        if (
            tgt_port is not None
            and tgt_port.is_entry
            and tgt_port.side == PortSide.LEFT
            and src_row is not None
            and tgt_row is not None
            and tgt_row == src_row + 1
        ):
            exclude = {
                sid for sid in (src.section_id, tgt.section_id) if sid is not None
            }
            if not _h_segment_crosses_other_section(graph, sx, tx, ty, exclude):
                return _route_l_shape(edge, src, tgt, i, n, ctx)
        # A bypass into a RIGHT entry port whose source is to the LEFT
        # would rise in the inter-column gap LEFT of the target, then run
        # its final horizontal RIGHTWARD across the section interior to
        # reach the right-edge port - entering the far side and doubling
        # back.  Wrap around BELOW the target and rise on its RIGHT side
        # so the approach arrives from the port's own outward side.
        if (
            tgt_port is not None
            and tgt_port.is_entry
            and tgt_port.side == PortSide.RIGHT
            and sx < tx - COORD_TOLERANCE
        ):
            # When the source sits in a row ABOVE the target, going UNDER the
            # whole target row would run the long horizontal counter to the
            # target row's flow (an artefactual counter-flow run).  The clear
            # inter-row gap between the source row and the target row is the
            # natural, with-the-downward-transition channel: run the rightward
            # H there, then drop straight down the RIGHT side into the port.
            # Falls back to the around-below loop when that gap horizontal is
            # not clear of intervening section interiors.
            if (
                src_row is not None
                and tgt_row is not None
                and src_row < tgt_row
                and _right_entry_gap_above_is_clear(graph, src, tgt, tgt, src_row)
            ):
                return _route_right_entry_via_gap_above(
                    edge, src, tgt, tgt, i, n, ctx, src_row
                )
            return _route_right_entry_around_below(edge, src, tgt, tgt, i, n, ctx)
        return _route_bypass(edge, src, tgt, i, src_col, tgt_col, ctx, src_row)

    # Near-vertical: junction to same-column entry with tiny horizontal
    # offset (just the junction margin).  The standard L-shape would
    # place the vertical channel on the wrong side (toward the target,
    # which is back inside the section).  Instead, route the channel
    # further into the inter-column gap (away from the target) so the
    # line continues in the junction's natural direction before dropping.
    # Only fires for true same-column cases: both source and target
    # belong to the same grid column (so the "wrong side" intrudes into
    # their shared column).  When source and target sit in different
    # columns the standard L-shape naturally drops in the inter-column
    # gap and going "away from target" would route backward through a
    # neighbouring section.
    if (
        edge.source in ctx.junction_ids
        and abs(dx) <= JUNCTION_MARGIN + COORD_TOLERANCE
        and abs(dy) > abs(dx) * 3
        and src_col is not None
        and tgt_col is not None
        and src_col == tgt_col
    ):
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        # Push channel away from target into the inter-column gap.
        if horizontal is Direction.L:
            vx = sx + ctx.curve_radius + ctx.offset_step + delta
        else:
            vx = sx - ctx.curve_radius - ctx.offset_step + delta
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (vx, sy), (vx, ty), (tx, ty)],
            is_inter_section=True,
            curve_radii=[r_first, r_second],
        )

    # RIGHT entry port with source to the LEFT: wrap the vertical
    # channel around the right side of the target section so the route
    # goes over the top and in from the right, rather than cutting
    # horizontally through the section interior.
    if (
        tgt_port
        and tgt_port.is_entry
        and tgt_port.side == PortSide.RIGHT
        and horizontal is Direction.R
    ):
        return _route_right_entry_wrap(edge, src, tgt, i, n, ctx)

    # LEFT entry port with source to the RIGHT: mirror of the above.  The
    # standard L-shape would cut through the target section's interior
    # at ty to reach a left-side entry port (the long horizontal lands
    # inside the bbox).  Wrap leftward through the inter-row gap then
    # drop into the entry from the left.  Restricted to cross-row cases
    # where the standard L-shape would actually intrude (same-row
    # neighbours route fine with a normal L).
    if (
        tgt_port
        and tgt_port.is_entry
        and tgt_port.side == PortSide.LEFT
        and dx < 0
        and src_row is not None
        and tgt_row is not None
        and src_row != tgt_row
    ):
        # When the inter-row channel _route_left_entry_wrap would use
        # lands inside an intervening section (e.g. a multi-row jump
        # past a tall middle row), route AROUND BELOW the target
        # section instead.  Otherwise use the standard wrap.  The
        # source section is excluded - the H lead-in just outside the
        # source's right edge is fine even if its Y falls within the
        # source's row.
        wrap_hy = inter_row_channel_y(graph, src, tgt, sy, ty, dy, ctx.curve_radius)
        exclude = {src.section_id} if src.section_id else set[str]()
        if _h_segment_crosses_other_section(graph, sx, tx, wrap_hy, exclude):
            if _corridor_is_viable(ctx, src, tgt):
                return _route_inter_row_gap_corridor(edge, src, tgt, tgt, i, n, ctx)
            return _route_around_section_below(edge, src, tgt, tgt, i, n, ctx)
        return _route_left_entry_wrap(edge, src, tgt, i, n, ctx)

    # Serpentine LEFT exit -> LEFT entry stacked in the same column:
    # an RL section's left exit dropping to the LR section directly below
    # whose left entry sits a few px inward.  The standard L-shape places
    # its vertical channel at sx + radius, which lands inside the source
    # bbox.  Drop the channel on the LEFT of the column instead so the
    # connector stays outside both section boxes.
    if (
        src_port is not None
        and not src_port.is_entry
        and src_port.side == PortSide.LEFT
        and tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side == PortSide.LEFT
        and src_col is not None
        and tgt_col is not None
        and src_col == tgt_col
        and src_row is not None
        and tgt_row != src_row
    ):
        return _route_left_exit_left_entry_drop(edge, src, tgt, i, n, ctx)

    # Non-bypass edges to merge junctions: route to entry port.
    # When dy is tiny, use a straight line to avoid cramped curves.
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    if ep_id:
        ep = graph.stations.get(ep_id)
        if ep:
            if abs(ep.y - sy) < ctx.curve_radius:
                return RoutedPath(
                    edge=edge,
                    line_id=edge.line_id,
                    points=[(sx, sy), (ep.x, ep.y)],
                    is_inter_section=True,
                )
            # If the standard L-shape's horizontal segment at the entry
            # port's Y would cross a section bbox the route doesn't
            # enter, route AROUND BELOW the target section instead.
            # The source section is excluded - its right-edge lead-in
            # is safe even when its bbox spans the route's Y range.
            ep_port = graph.ports.get(ep_id)
            if ep_port and ep_port.side == PortSide.LEFT:
                exclude = {src.section_id} if src.section_id else set[str]()
                if _h_segment_crosses_other_section(graph, sx, ep.x, ep.y, exclude):
                    # When a clear inter-row / inter-column corridor exists for
                    # this downward cross-row feeder, descend it instead of
                    # looping below the canvas.
                    if _corridor_is_viable(ctx, src, ep):
                        return _route_inter_row_gap_corridor(
                            edge, src, tgt, ep, i, n, ctx
                        )
                    return _route_around_section_below(edge, src, tgt, ep, i, n, ctx)
            # RIGHT entry nested inside an oversized source section: a
            # single-channel L-shape would descend far right and sweep
            # back across the whole diagram.  Step down past the
            # source then into a channel near the target instead.
            if edge.source in ctx.junction_ids and _should_step_descent(
                graph, src, ep, ep_port
            ):
                return _route_stepped_descent(edge, src, ep, i, n, ctx)
            return _route_l_shape(edge, src, ep, i, n, ctx)

    # Standard L-shape: when its horizontal segment at the target Y would
    # plough through an intervening same-row section to reach a RIGHT entry
    # port from a higher row, deflect the whole route through the bypass
    # (descend right of the obstacle, run below the row, rise into the port).
    if (
        tgt_port is not None
        and tgt_port.is_entry
        and tgt_port.side == PortSide.RIGHT
        and src_row is not None
        and tgt_row is not None
        and tgt_row > src_row
        and src_col is not None
        and tgt_col is not None
    ):
        exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
        if _h_segment_crosses_other_section(graph, sx, tx, ty, exclude):
            return _route_bypass(edge, src, tgt, i, src_col, tgt_col, ctx, src_row)

    # Standard L-shape
    return _route_l_shape(edge, src, tgt, i, n, ctx)


def _route_tb_bottom_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath:
    """Vertical drop from TB BOTTOM exit with X offsets.

    When the target sits directly below the exit the route is a clean
    vertical drop.  When the target X is offset (e.g. a TOP entry port a
    few px inward of the bottom exit), a straight 2-point connector would
    be a raw diagonal between two perpendicular ports.  Emit an orthogonal
    drop / jog / drop with curved corners instead: down out of the BOTTOM
    port, across the inter-row gap, then down into the target.
    """
    x_off = _tb_x_offset(ctx, edge.source, edge.line_id, src.section_id)
    sx = src.x + x_off
    sy = src.y
    tx = tgt.x + x_off
    ty = tgt.y

    if abs(tx - sx) <= COORD_TOLERANCE:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (tx, ty)],
            is_inter_section=True,
            offsets_applied=True,
        )

    # Misaligned: jog in the inter-row gap so the line leaves the BOTTOM
    # port travelling downward, transitions across with bounded curves,
    # then drops into the target.  Corner radii are clamped to half the
    # horizontal jog so short jogs stay orthogonal rather than collapsing
    # into a diagonal.
    dy = ty - sy
    hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    # Keep the jog Y strictly between the two ports so both vertical legs
    # have positive length for the corner curves to bite into.
    lo, hi = (sy, ty) if dy >= 0 else (ty, sy)
    hy = min(max(hy, lo + ctx.curve_radius), hi - ctx.curve_radius)
    jog_r = reference_anchored_radius(0.0, min(ctx.curve_radius, abs(tx - sx) / 2))
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (sx, hy), (tx, hy), (tx, ty)],
        is_inter_section=True,
        offsets_applied=True,
        normalize_exempt=True,
        curve_radii=[jog_r, jog_r],
    )


def _route_bottom_exit_junction(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Vertical-first L-shape from bottom exit junction."""
    exit_pid = ctx.bottom_exit_junction_ports[edge.source]
    if ctx.station_offsets:
        exit_src = ctx.graph.stations.get(exit_pid)
        sec_id = exit_src.section_id if exit_src else ""
        x_off = _tb_x_offset(ctx, exit_pid, edge.line_id, sec_id or "")
    else:
        x_off = ((n - 1) / 2 - i) * ctx.offset_step

    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    r = reference_anchored_radius(x_off, ctx.curve_radius)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (src.x + x_off, src.y),
            (src.x + x_off, tgt.y + tgt_off),
            (tgt.x, tgt.y + tgt_off),
        ],
        is_inter_section=True,
        curve_radii=[r],
        offsets_applied=True,
    )


def _route_merge_branch(
    edge: Edge,
    src: Station,
    ctx: _RoutingCtx,
    src_col: int,
) -> RoutedPath:
    """Truncated L-shape descent from a junction to the trunk level.

    Routes a 4-point path: horizontal lead-in, curve down, vertical
    drop, curve into trunk direction.  The lead-in is positioned at
    MERGE_ROUTE_MARGIN from the source section edge.
    """
    sx, sy = src.x, src.y
    dx = ctx.graph.stations[edge.target].x - sx
    horizontal = horizontal_direction(dx)
    src_off = _get_offset(ctx, edge.source, edge.line_id)

    # Trunk bypass Y level (branches drop to meet it)
    by = ctx.merge.trunk_by.get(edge.target, sy)

    # Position descent at MERGE_ROUTE_MARGIN from section edge
    if horizontal is Direction.R:
        lead_x = col_right_edge(ctx.graph, src_col) + MERGE_ROUTE_MARGIN
    else:
        lead_x = col_left_edge(ctx.graph, src_col) - MERGE_ROUTE_MARGIN
    # Clamp to at least curve_radius from the junction
    min_lead = sx + horizontal.sign * ctx.curve_radius
    if horizontal is Direction.R:
        lead_x = max(lead_x, min_lead)
    else:
        lead_x = min(lead_x, min_lead)
    tail_x = lead_x + horizontal.sign * ctx.curve_radius * 2

    r_base = reference_anchored_radius(0.0, ctx.curve_radius)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, sy + src_off),
            (lead_x, sy + src_off),
            (lead_x, by),
            (tail_x, by),
        ],
        is_inter_section=True,
        # One branch line per call: a single descent, so both corners take the
        # base radius (no bundle to fan concentrically).
        curve_radii=[r_base, r_base],
        offsets_applied=True,
    )


def _has_around_section_sibling(
    edge: Edge, ep: Station, ep_port: Port | None, ctx: _RoutingCtx
) -> bool:
    """Detect whether another edge to the same entry port will route via
    :func:`_route_around_section_below`.

    The around-section route hugs the target section's left edge with its
    V_up channel at ``section_left - base_gap - extra_clearance - delta``.
    When a merge trunk's bypass also lands in the same inter-column gap,
    the two bundles overlap visually.  Trunks that detect a competing
    around-section sibling can pull their V_up away from the target edge
    (see ``trunk_v_up_pull_away`` in :func:`_route_bypass`).

    Mirrors the dispatcher in ``_route_inter_section``: another edge
    that would ACTUALLY dispatch to ``_route_around_section_below``.
    That requires the sibling to take the merge-non-bypass path (lines
    660-666) - i.e. needs_bypass must be False - and its L-shape's H
    segment at ``ep.y`` to cross a non-source section.  Siblings whose
    span pushes them into the bypass dispatch (line 553) end up as
    merge-branches or trunk routes, not around-section, so they do NOT
    compete for the same channel and pulling the trunk away on their
    behalf produces the visible unbundling that #388 introduced on
    03b_fan_in_merge.
    """
    if ep_port is None or ep_port.side != PortSide.LEFT:
        return False
    graph = ctx.graph
    ep_col = _resolve_section_col(graph, ep)
    # Find all edges whose target is the same merge junction.
    for other in ctx.graph.edges_to(edge.target):
        if other.source == edge.source:
            continue
        other_src = graph.stations.get(other.source)
        if other_src is None:
            continue
        # Skip siblings that would dispatch through the bypass branch.
        # needs_bypass = (|tgt_col - src_col| > 1) AND intervening.
        other_col, other_row = _resolve_section_colrow(graph, other_src)
        if (
            other_col is not None
            and ep_col is not None
            and abs(ep_col - other_col) > 1
            and _has_intervening_sections(graph, other_col, ep_col, other_row)
        ):
            continue
        exclude = {other_src.section_id} if other_src.section_id else set()
        if _h_segment_crosses_other_section(graph, other_src.x, ep.x, ep.y, exclude):
            return True
    return False


def _route_merge_trunk(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    src_col: int,
    tgt_col: int,
    ctx: _RoutingCtx,
    src_row: int | None = None,
) -> RoutedPath:
    """Full U-shape bypass for the trunk carrier, ending at the entry port.

    Delegates to _route_bypass with the entry port as the effective
    target so the route extends past the merge junction to the section
    entry.  Both X and Y of the entry port are overridden because the
    merge junction is virtual and lives inside the target section at a
    different Y from the actual entry port; without the Y override the
    bypass terminates at the merge junction's Y and leaves a visible
    "hanging" curve disconnected from the entry port.

    When the trunk and entry are in the same grid row but separated by
    intervening row-mates, the standard above-row bypass channel sits
    in the inter-row gap that also holds the target row's section
    titles.  Force ``cross_row`` so the channel runs BELOW all sections
    in the column range, mirroring :func:`_route_around_section_below`
    and avoiding overlap with the title text.

    When a sibling edge to the same merge junction will route via
    :func:`_route_around_section_below`, both routes would place
    their V_up channels in the inter-column gap just left of the target
    section, producing overlapping bundles in the same x range.  Detect
    that and pull the trunk's V_up channel further from the target edge
    (towards the previous column) so the two bundles occupy distinct
    columns within the gap.
    """
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    ep = ctx.graph.stations.get(ep_id) if ep_id else None
    ep_port = ctx.graph.ports.get(ep_id) if ep_id else None
    effective_tx = ep.x if ep else tgt.x
    effective_ty = ep.y if ep else tgt.y
    tgt_row = _resolve_section_row(ctx.graph, tgt)
    force_cross_row = (
        src_row is not None
        and tgt_row == src_row
        and _has_other_row_section_in_col_range(ctx.graph, src_col, tgt_col, src_row)
    )
    trunk_v_up_pull_away = ep is not None and _has_around_section_sibling(
        edge, ep, ep_port, ctx
    )
    return _route_bypass(
        edge,
        src,
        tgt,
        i,
        src_col,
        tgt_col,
        ctx,
        src_row,
        effective_tx=effective_tx,
        effective_ty=effective_ty,
        force_cross_row=force_cross_row,
        trunk_v_up_pull_away=trunk_v_up_pull_away,
    )


def _route_bypass(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    src_col: int,
    tgt_col: int,
    ctx: _RoutingCtx,
    src_row: int | None = None,
    effective_tx: float | None = None,
    effective_ty: float | None = None,
    force_cross_row: bool = False,
    trunk_v_up_pull_away: bool = False,
) -> RoutedPath:
    """U-shaped bypass route around intervening sections.

    When *effective_tx* / *effective_ty* are provided, they override
    the target coordinates for gap2 placement (used by merge trunks to
    reach the entry port instead of the merge junction, which sits at a
    different Y inside the section).  When *force_cross_row* is True,
    ``bypass_bottom_y`` is asked to route below ALL sections in the
    column range regardless of whether src and tgt share a row.

    When *trunk_v_up_pull_away* is True, gap2_x is placed in the half
    of the inter-column gap CLOSER to the previous column (i.e. AWAY
    from the target's edge) so it doesn't overlap with a sibling
    around-section route that hugs the target's edge.  Only honoured
    when the displacement keeps gap2_x at least SECTION_ROUTE_CLEARANCE
    from the neighbouring section; otherwise the standard placement is
    used (the bundles will overlap, but the alternative is to put
    gap2_x INSIDE the neighbouring section bbox, which is worse).
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    if effective_tx is None:
        effective_tx = tx
    if effective_ty is not None:
        ty = effective_ty
    dx = tx - sx
    horizontal = horizontal_direction(dx)
    graph = ctx.graph

    ekey = (edge.source, edge.target, edge.line_id)
    g1_j, g1_n, g2_j, g2_n = ctx.bypass_gap_idx.get(ekey, (0, 1, 0, 1))

    fan = ctx.junction_fan_info.get(ekey)

    # Per-line trunk Y keeps lines visually separate on the horizontal.
    if fan is not None:
        nest_offset = g2_j * ctx.offset_step
    else:
        nest_offset = max(i, g2_j) * ctx.offset_step
    # Resolve target row to detect cross-row bypasses.
    tgt_row = _resolve_section_row(graph, tgt)
    cross_row = force_cross_row or (
        src_row is not None and tgt_row is not None and src_row != tgt_row
    )
    base_y = bypass_bottom_y(
        graph,
        src_col,
        tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=cross_row,
        tgt_row=tgt_row,
    )

    # Determine actual vertical direction at each gap from the geometry.
    # Gap1 goes from source Y to trunk Y; gap2 from trunk Y to target Y.
    # Normally gap1 goes down and gap2 goes up, but when the source is
    # below the trunk (bottom of a tall section bypassing a shorter
    # neighbour), gap1 also goes up.
    gap1_vertical = vertical_direction(base_y - sy)
    gap2_vertical = vertical_direction(ty - base_y)

    # Radii and per-line deltas via the same l_shape_radii logic used
    # for all other concentric corners.
    delta1, delta2, r1, _, r3, r4 = bypass_radii(
        g1_j,
        g1_n,
        g2_j,
        g2_n,
        horizontal=horizontal,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
        gap1_vertical=gap1_vertical,
        gap2_vertical=gap2_vertical,
    )
    by = base_y + nest_offset

    # Override r2 so all trunk horizontals begin at the same X
    # (gap1_x + r2 = constant across all lines in the bundle).
    r2 = corner_radius(
        nest_offset,
        (g2_n - 1) * ctx.offset_step,
        outside=gap1_vertical is Direction.D,
        base_radius=ctx.curve_radius,
    )

    # Initial gap-channel centres and per-line positions.  These centre each
    # leg in its (row-aware) gap via _gap_channel_base; the post-routing
    # _normalize_gap_channels pass then re-stacks all inter-section channels
    # into their final centred / B-separated bundle positions.
    half_g1 = (g1_n - 1) * ctx.offset_step / 2
    half_g2 = (g2_n - 1) * ctx.offset_step / 2

    if horizontal is Direction.R:
        if fan is not None:
            # The fan shares its first corner across siblings; centre the
            # channel on the gap slot, but never left of the near-source
            # position or the curve would start behind the junction (nubbin).
            ui, un = fan
            fan_delta, r1, _ = l_shape_radii(
                ui,
                un,
                vertical=gap1_vertical,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            near = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
            slot = _gap_channel_base(graph, src_col, src_row, un, ctx.offset_step)
            fan_mid_x = max(near, slot)
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = _gap_channel_base(
                graph, src_col, src_row, g1_n, ctx.offset_step
            )
            gap1_limit = sx + ctx.curve_radius
            if gap1_base - (g1_n - 1) * ctx.offset_step < gap1_limit:
                gap1_mid = gap1_limit + half_g1
            else:
                gap1_mid = gap1_base - half_g1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(
            graph, tgt_col - 1, tgt_row, g2_n, ctx.offset_step
        )
        gap2_limit = effective_tx - ctx.curve_radius
        if gap2_base + (g2_n - 1) * ctx.offset_step > gap2_limit:
            gap2_mid = gap2_limit - half_g2
        else:
            gap2_mid = gap2_base + half_g2
        if trunk_v_up_pull_away:
            # Two bundles share the gap between (tgt_col - 1) and tgt_col:
            # this bypass (gap2) bundle on the LEFT, paired with an
            # around-section bundle on the RIGHT (placed by
            # _route_around_section_below), positioned symmetrically via
            # symmetric_bundle_midpoint.  When the gap is too narrow to
            # fit both bundles with clearance, fall back to the standard
            # (single-bundle) placement; overlap is the lesser evil
            # compared to a route entering the neighbouring section's bbox.
            gap_left, gap_right = column_gap_edges(
                graph, tgt_col - 1, tgt_col, row=tgt_row
            )
            this_width = bundle_width(g2_n, ctx.offset_step)
            # The around-route bundle's line count equals the merge
            # trunk's effective line count, which today matches g2_n
            # (one around-route line per fan_in line).  Use g2_n as a
            # conservative width estimate.
            around_width = this_width
            pulled_mid_candidate = symmetric_bundle_midpoint(
                gap_left,
                gap_right,
                [this_width, around_width],
                bundle_index=0,
            )
            # Sanity: only honour the symmetric placement when both
            # bundles can fit with at least A clearance from each edge
            # and B inter-bundle separation.  Otherwise the gap was
            # never widened (e.g. layout disabled or pull-away
            # triggered without _enforce_min_column_gaps participating),
            # so fall back to the standard placement.
            this_xmin = pulled_mid_candidate - this_width / 2
            around_mid = symmetric_bundle_midpoint(
                gap_left,
                gap_right,
                [this_width, around_width],
                bundle_index=1,
            )
            around_xmax = around_mid + around_width / 2
            if (
                this_xmin - gap_left >= SECTION_ROUTE_CLEARANCE
                and gap_right - around_xmax >= SECTION_ROUTE_CLEARANCE
            ):
                gap2_mid = pulled_mid_candidate
        gap2_x = gap2_mid + delta2
    else:
        if fan is not None:
            # Mirror of the going-right fan: centre on the gap slot but never
            # right of the near-source position (curve must not start behind
            # the junction).  Wrap-style routes whose source-side curve is on
            # the RIGHT regardless of dx (left-entry wrap, around-section-
            # below) are dispatched through their own handlers, not here.
            ui, un = fan
            fan_delta, r1, _ = l_shape_radii(
                ui,
                un,
                vertical=gap1_vertical,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            near = sx - ctx.curve_radius - (un - 1) * ctx.offset_step / 2
            slot = _gap_channel_base(graph, src_col - 1, src_row, un, ctx.offset_step)
            fan_mid_x = min(near, slot)
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = _gap_channel_base(
                graph, src_col - 1, src_row, g1_n, ctx.offset_step
            )
            gap1_limit = sx - ctx.curve_radius
            if gap1_base + (g1_n - 1) * ctx.offset_step > gap1_limit:
                gap1_mid = gap1_limit - half_g1
            else:
                gap1_mid = gap1_base + half_g1
            gap1_x = gap1_mid + delta1

        gap2_base = _gap_channel_base(graph, tgt_col, tgt_row, g2_n, ctx.offset_step)
        gap2_limit = effective_tx + ctx.curve_radius
        if gap2_base - (g2_n - 1) * ctx.offset_step < gap2_limit:
            gap2_mid = gap2_limit + half_g2
        else:
            gap2_mid = gap2_base - half_g2
        gap2_x = gap2_mid + delta2

    # When the descent crosses other grid rows, the source/target-row gap
    # channel can still pierce an oversized section stacked in a crossed row
    # (its bbox extends into the gap).  Nudge each vertical leg clear of any
    # box its Y-span pierces, bounded to the inter-column gap so the channel
    # stays in clear space.
    if cross_row:
        exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
        if horizontal is Direction.R:
            g1_lo, g1_hi = column_gap_edges(graph, src_col, src_col + 1)
            g2_lo, g2_hi = column_gap_edges(graph, tgt_col - 1, tgt_col)
        else:
            g1_lo, g1_hi = column_gap_edges(graph, src_col - 1, src_col)
            g2_lo, g2_hi = column_gap_edges(graph, tgt_col, tgt_col + 1)
        gap1_x = _clear_channel_x_in_band(
            graph, gap1_x, sy, by, SECTION_ROUTE_CLEARANCE, exclude, g1_lo, g1_hi
        )
        gap2_x = _clear_channel_x_in_band(
            graph, gap2_x, by, ty, SECTION_ROUTE_CLEARANCE, exclude, g2_lo, g2_hi
        )
        # When the source is a junction sitting at/beyond its source
        # section's right edge and the route runs leftward, the gap1
        # lead-in at the source Y would plough back across the source box
        # to reach a left-side descent channel.  Drop the descent on the
        # RIGHT of the source instead (straight down out of the junction),
        # so the long leftward traverse happens below the row at ``by``.
        if horizontal is Direction.L:
            src_sec = resolve_section(graph, src)
            if src_sec is not None and src_sec.bbox_w > 0:
                src_right = src_sec.bbox_x + src_sec.bbox_w
                if sx >= src_right - COORD_TOLERANCE and gap1_x < src_right:
                    gap1_x = max(sx, src_right + SECTION_ROUTE_CLEARANCE)
                    gap1_x = _clear_channel_x_in_band(
                        graph,
                        gap1_x,
                        sy,
                        by,
                        SECTION_ROUTE_CLEARANCE,
                        exclude,
                        bound_left=gap1_x,
                    )

    # Apply per-line offsets directly so the renderer doesn't have to
    # guess which waypoints belong to the source vs target side.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, sy + src_off),
            (gap1_x, sy + src_off),
            (gap1_x, by),
            (gap2_x, by),
            (gap2_x, ty + tgt_off),
            (effective_tx, ty + tgt_off),
        ],
        is_inter_section=True,
        curve_radii=[r1, r2, r3, r4],
        offsets_applied=True,
    )


def _nested_target_clear_channel_x(
    graph: MetroGraph,
    ep: Station,
    y_lo: float,
    y_hi: float,
    clearance: float,
) -> float | None:
    """X of a clear vertical channel just right of the target column's stack.

    For a route whose entry port *ep* sits in a narrow column nested inside
    an oversized source section, the only single-channel descent that
    clears the source is far to the right (producing the dog-leg).
    A stepped descent instead drops to just below the source, steps left
    into the channel returned here - the rightmost edge of any section in
    the target's column that the descent's vertical run (*y_lo*..*y_hi*)
    would otherwise cross, plus *clearance* - then drops to the entry Y.

    Returns ``None`` when the target column has no resolvable sections.
    """
    ep_sec = graph.sections.get(ep.section_id) if ep.section_id else None
    if ep_sec is None:
        return None
    secs = _sections_in_col(graph, ep_sec.grid_col, y_band=(y_lo, y_hi))
    if not secs:
        return None
    return max(s.bbox_x + s.bbox_w for s in secs) + clearance


def _unit_step(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
    """Unit travel direction from *p* to *q* for an axis-aligned segment."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


def _route_stepped_descent(
    edge: Edge,
    src: Station,
    ep: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Stepped descent to an entry port nested inside an oversized source.

    Replaces the far-right dog-leg for a junction-sourced merge
    route whose single-channel L-shape would be shoved to the far edge of
    an oversized source section.  Routes a 6-corner step::

        (sx, sy) -> (corner_x, sy)      ; H lead-in right out of the source
        (corner_x, sy) -> (corner_x, step_y) ; V down past the source bottom
        (corner_x, step_y) -> (chan_x, step_y) ; H left into the target channel
        (chan_x, step_y) -> (chan_x, ey) ; V down to the entry Y
        (chan_x, ey) -> (ex, ey)        ; H into the entry port

    ``corner_x`` clears the source section's right edge; ``step_y`` sits
    just below the source bottom; ``chan_x`` is the clear channel right of
    the target column's stack.  Both leftward legs stay well under half the
    canvas width, eliminating the full-width sweep.
    """
    graph = ctx.graph
    sx, sy = src.x, src.y
    ex, ey = ep.x, ep.y
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    r = ctx.curve_radius

    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    # Trace through the junction to the feeding exit port's section.
    if src_sec is None:
        src_sec = resolve_section(graph, src)
    src_right = (src_sec.bbox_x + src_sec.bbox_w) if src_sec else sx
    src_bottom = (src_sec.bbox_y + src_sec.bbox_h) if src_sec else sy

    # Spread parallel lines: outer lines sit further out / lower.
    spread = (n - 1 - i) * ctx.offset_step
    corner_x = src_right + SECTION_ROUTE_CLEARANCE + spread
    corner_x = max(corner_x, sx + r)
    step_y = src_bottom + EDGE_TO_BUNDLE_CLEARANCE + spread

    chan_x = _nested_target_clear_channel_x(
        graph, ep, step_y, ey + tgt_off, SECTION_ROUTE_CLEARANCE
    )
    if chan_x is None:
        chan_x = ex + SECTION_ROUTE_CLEARANCE
    chan_x += spread

    points = [
        (sx, sy + src_off),
        (corner_x, sy + src_off),
        (corner_x, step_y),
        (chan_x, step_y),
        (chan_x, ey + tgt_off),
        (ex, ey + tgt_off),
    ]
    # Each line's vertical legs sit ``spread`` to the right of the innermost
    # line, so the four bends fan as nested arcs (the two middle rungs are
    # genuinely concentric; the outer two are transition corners).  Size every
    # corner through the one direction-driven routine, reading the turn from the
    # route's own geometry; ``spread == 0`` for the innermost line keeps the
    # single-line case at the base radius.
    radii = [
        concentric_corner_radius(
            _unit_step(points[k - 1], points[k]),
            _unit_step(points[k], points[k + 1]),
            spread,
            ctx.curve_radius,
            min_radius=COORD_TOLERANCE,
        )
        for k in range(1, 5)
    ]

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=points,
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=radii,
        offsets_applied=True,
    )


def _should_step_descent(
    graph: MetroGraph,
    src: Station,
    ep: Station,
    ep_port: Port | None,
) -> bool:
    """Detect the nested-column degenerate geometry that needs a stepped
    descent.

    Fires only when a junction-sourced merge route to a RIGHT entry port
    would otherwise drop in a single far-right channel because the target
    column is geometrically nested inside an oversized source section: the
    source section's right edge sits well to the right of the entry port,
    so a single-channel L-shape must descend far right and sweep all the
    way back left.  The stepped descent replaces that with two short legs.
    """
    if ep_port is None or ep_port.side != PortSide.RIGHT:
        return False
    src_sec = graph.sections.get(src.section_id) if src.section_id else None
    if src_sec is None:
        src_sec = resolve_section(graph, src)
    ep_sec = graph.sections.get(ep.section_id) if ep.section_id else None
    if src_sec is None or ep_sec is None or src_sec.bbox_w <= 0:
        return False
    # Target column must be to the RIGHT in grid terms (forward route)...
    if ep_sec.grid_col <= src_sec.grid_col:
        return False
    src_right = src_sec.bbox_x + src_sec.bbox_w
    # ...yet the entry port sits well LEFT of the source's right edge, so a
    # single-channel descent would be shoved far right of the target (the
    # nested-column degeneracy).  Require a clear target-side channel to
    # step into; if none resolves, fall back to the standard L-shape.
    if src_right <= ep.x + SECTION_ROUTE_CLEARANCE:
        return False
    chan_x = _nested_target_clear_channel_x(
        graph, ep, src_sec.bbox_y + src_sec.bbox_h, ep.y, SECTION_ROUTE_CLEARANCE
    )
    if chan_x is None:
        return False
    # The channel must genuinely improve on the far-right descent: it has
    # to land left of the source's right edge (otherwise we gain nothing).
    return chan_x < src_right - SECTION_ROUTE_CLEARANCE


def _route_l_shape(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Standard L-shape inter-section route with concentric arcs."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy
    horizontal = horizontal_direction(dx)
    vertical = vertical_direction(dy)

    # When the junction has both L-shape and bypass siblings, use
    # unified fan-out positions so all lines share one concentric
    # first corner.
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        # First corner: unified position within the combined fan-out.
        # Use the going_right fan_mid_x formula so corner_x - r_first
        # lands at junction.x and the upstream segment terminates at
        # the curve start with NO nubbin past the curve start.  This
        # is independent of the route's overall dx sign since the
        # source-side curve in a wrap-style fan is always on the
        # OUTSIDE (right) of the source section.
        delta, r_first, _ = l_shape_radii(
            ui,
            un,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        # mid_x places all lines so they diverge at sx
        mid_x = sx + horizontal.sign * (
            ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        )
        # The fan pivots the channel through ``sx +/- curve_radius``,
        # which hugs the source section's edge.  When that edge is also a
        # section bbox border the descent grazes it incidentally:
        # push the channel outward so the nearest line clears the edge.
        half_width = (un - 1) * ctx.offset_step / 2
        mid_x = clear_channel_of_section_edge(
            ctx.graph,
            mid_x,
            half_width,
            min(sy, ty),
            max(sy, ty),
            endpoint_port_xs(ctx.graph, edge),
        )
        # Second corner: from sub-bundle (only L-shape siblings turn here)
        _, _, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
    else:
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        max_r = ctx.curve_radius + (n - 1) * ctx.offset_step
        mid_x = inter_column_channel_x(
            ctx.graph, src, tgt, sx, tx, dx, max_r, ctx.offset_step
        )
        # The near-source fallback hugs the source section's edge; push
        # the channel outward when that edge is a section bbox border the
        # route doesn't enter.
        half_width = (n - 1) * ctx.offset_step / 2
        mid_x = clear_channel_of_section_edge(
            ctx.graph,
            mid_x,
            half_width,
            min(sy, ty),
            max(sy, ty),
            endpoint_port_xs(ctx.graph, edge),
        )

    vx = mid_x + delta

    # When the vertical segment is too short for both corners at full
    # radius, reduce the base radius so r_first + r_second fits while
    # preserving the offset_step difference (concentricity invariant).
    seg = abs(dy)
    if r_first + r_second > seg and seg > 0:
        # r_first + r_second = 2*base + (n-1)*step  (for any i)
        # Solve: 2*new_base + (n-1)*step = seg
        effective_n = max(n, fan[1] if fan else n)
        new_base = max(0.0, (seg - (effective_n - 1) * ctx.offset_step) / 2)
        if fan is not None:
            _, r_first, _ = l_shape_radii(
                fan[0],
                fan[1],
                vertical=vertical,
                offset_step=ctx.offset_step,
                base_radius=new_base,
            )
            _, _, r_second = l_shape_radii(
                i,
                n,
                vertical=vertical,
                offset_step=ctx.offset_step,
                base_radius=new_base,
            )
        else:
            _, r_first, r_second = l_shape_radii(
                i,
                n,
                vertical=vertical,
                offset_step=ctx.offset_step,
                base_radius=new_base,
            )

    # When fan is active, vx == sx (corner at junction).  The first
    # segment (sx, sy) -> (vx, sy) is zero-length, which prevents the
    # renderer from drawing the corner curve.  Extend pts[0] back by
    # curve_radius LEFT of the junction so the first corner gets a
    # standard CURVE_RADIUS arc with horizontal lead-in (overlapping
    # the upstream exit_port->junction segment's last curve_radius
    # pixels, fine since they share the same line colour).
    if fan is not None:
        src_off = _get_offset(ctx, edge.source, edge.line_id)
        tgt_off = _get_offset(ctx, edge.target, edge.line_id)
        r_lead = reference_anchored_radius(0.0, ctx.curve_radius)
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[
                (vx - r_lead, sy + src_off),
                (vx, sy + src_off),
                (vx, ty + tgt_off),
                (tx, ty + tgt_off),
            ],
            is_inter_section=True,
            curve_radii=[r_lead, r_second],
            offsets_applied=True,
        )

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (vx, sy), (vx, ty), (tx, ty)],
        is_inter_section=True,
        curve_radii=[r_first, r_second],
    )


def _route_top_entry_l_shape(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Vertical-first L-shape for TOP entry ports.

    Routes via a short horizontal lead-in so the transition from any
    preceding horizontal edge (e.g. exit -> junction) curves smoothly
    into the vertical drop::

        (sx,sy) -> (lx, sy) -> (lx, hy) -> (tx, hy) -> (tx, ty)

    The horizontal run sits in the inter-row gap just above the target
    section so the line drops cleanly into the TOP port, mirroring how
    LEFT entry ports receive a vertical run in the inter-column gap.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy
    vertical = vertical_direction(dy)

    delta, r_first, r_second = l_shape_radii(
        i,
        n,
        vertical=vertical,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    # Compute Y for the horizontal channel in the inter-row gap.
    # delta is the vertical-channel X offset; on the down->right corner
    # the rightmost line (positive delta) turns inside, so it sits at
    # the smaller (northern) horizontal Y, hence -delta here.
    mid_y = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    hy = mid_y - delta

    # Horizontal lead-in: a short run so the corner from horizontal to
    # vertical gets a proper curve.  When dx is large, the lead-in
    # direction matches dx.  When dx is near-zero (source directly
    # above target), infer direction from the upstream exit port so the
    # line continues with the bundle flow before curving down.
    r_lead = reference_anchored_radius(0.0, ctx.curve_radius)
    if abs(dx) > r_lead:
        lead = horizontal_direction(dx)
    else:
        lead = Direction.R
        if src.id in ctx.graph.junctions:
            for je in ctx.graph.edges:
                if je.target == src.id:
                    js = ctx.graph.stations.get(je.source)
                    if js and js.is_port:
                        lead = Direction.R if js.x < src.x else Direction.L
                        break
    # A bundle that genuinely travels across columns (large dx) forms a
    # horizontal-trunk staircase into the TOP port.  Build it as one
    # consistent perpendicular offset of a reference line so all four bends
    # are concentric and the per-line gap stays constant.  The near-vertical
    # case (source roughly above target, small dx) keeps the original drop
    # below, where the source/target offsets can differ per end.
    if n > 1 and abs(dx) > r_lead:
        return _route_top_entry_offset_bundle(
            edge,
            src,
            tgt,
            lx0=sx + lead.sign * r_lead,
            hy0=mid_y,
            offset=i * ctx.offset_step,
            lead_sign=lead.sign,
            base_radius=r_lead,
        )

    # delta separates bundled lines: as an X offset on the vertical
    # channel (lx) so co-travelling lines don't overlay, and as a Y
    # offset on the horizontal channel (hy) above.
    lx = sx + lead.sign * r_lead + delta
    # Lead-in corner radius for a co-travelling bundle: the lines are
    # already separated in Y by the render offset, so an equal radius makes
    # the arcs non-concentric and the perpendicular gap pinches through the
    # bend.  Share the bend centre with the outermost line (the one whose
    # vertical channel sits furthest along the lead direction, kept at base
    # radius); inner lines turn proportionally tighter so the gap stays
    # constant.  The short lead-in stub and the long drop both absorb the
    # smaller radius.  Restricted to narrow bundles where the innermost
    # radius stays generous; wider bundles keep the flat base radius.
    max_delta = (n - 1) / 2 * ctx.offset_step
    if n > 1 and (n - 1) * ctx.offset_step <= r_lead - ctx.offset_step:
        # Reference line: the one whose vertical channel sits furthest along
        # the lead direction (delta == lead.sign*max_delta), kept at base.
        # Inner lines step down by their distance from that reference.
        r_lead_in = reference_anchored_radius(
            -abs(lead.sign * max_delta - delta), r_lead
        )
    else:
        r_lead_in = r_lead
    # When the lead-in point is close to the target X, skip the
    # intermediate horizontal channel and drop straight down from the
    # lead-in, curving into the target at the end.  This avoids a
    # tiny leftward jink mid-route.
    if abs(lx - tx) <= r_lead:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (lx, sy), (lx, ty), (tx, ty)],
            is_inter_section=True,
            normalize_exempt=True,
            curve_radii=[r_lead_in, r_second],
        )
    # Hold the per-line stagger on the final drop so a multi-line bundle
    # stays parallel down to the port instead of overlaying; converge into
    # the marker via a short jog only when the drop X is genuinely offset.
    drop_x = tx + delta
    if abs(drop_x - tx) < COORD_TOLERANCE:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (lx, sy), (lx, hy), (tx, hy), (tx, ty)],
            is_inter_section=True,
            normalize_exempt=True,
            curve_radii=[r_lead_in, r_first, r_second],
        )
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (lx, sy), (lx, hy), (drop_x, hy), (drop_x, ty), (tx, ty)],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r_lead_in, r_first, r_second, r_second],
    )


def _route_top_entry_offset_bundle(
    edge: Edge,
    src: Station,
    tgt: Station,
    *,
    lx0: float,
    hy0: float,
    offset: float,
    lead_sign: float,
    base_radius: float,
) -> RoutedPath:
    """Concentric multi-line variant of the TOP-entry staircase route.

    The reference line (``offset == 0``) is ``lead-in -> drop -> trunk -> drop
    straight into the port``; it stays continuous with the rest of its bundle
    at the junction and with the other routes leaving it.  Each further line
    is a constant perpendicular offset of that reference: horizontal legs
    shift down (``+offset`` in Y, matching the render-time source offset),
    vertical legs shift toward the junction (``-offset`` in X).  The offset is
    baked into both axes here with ``offsets_applied=True`` so
    ``apply_route_offsets`` adds no further shift, and every 90-degree bend
    keeps ``offset + radius = const`` so the arcs are concentric and the
    per-line gap stays constant through the lead-in corner, the trunk corners
    and the drop into the port.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    lead_in_y = sy + offset
    drop1_x = lx0 - offset
    trunk_y = hy0 + offset
    drop2_x = tx - offset

    # The reference line drops straight into the port; offset lines step down,
    # across and down, sitting inside the lead-in bend (radius base-offset for
    # an East lead, base+offset for a West lead), outside the first trunk bend
    # (base+offset) and inside the second (base-offset).  Each radius is the
    # reference-anchored concentric form base + signed_offset.
    r1 = reference_anchored_radius(-lead_sign * offset, base_radius)
    r2 = reference_anchored_radius(offset, base_radius)
    r3 = reference_anchored_radius(-offset, base_radius)

    if abs(drop2_x - tx) < COORD_TOLERANCE:
        # Reference line: no port jog, drop straight in.
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[
                (sx, lead_in_y),
                (drop1_x, lead_in_y),
                (drop1_x, trunk_y),
                (tx, trunk_y),
                (tx, ty),
            ],
            is_inter_section=True,
            normalize_exempt=True,
            offsets_applied=True,
            curve_radii=[r1, r2, r3],
        )

    # Offset line: tight converging jog onto the shared port point.  The jog
    # can drive base - offset to zero, so floor it at the coordinate tolerance.
    r4 = reference_anchored_radius(-offset, base_radius, min_radius=COORD_TOLERANCE)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, lead_in_y),
            (drop1_x, lead_in_y),
            (drop1_x, trunk_y),
            (drop2_x, trunk_y),
            (drop2_x, ty),
            (tx, ty),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        offsets_applied=True,
        curve_radii=[r1, r2, r3, r4],
    )


def _route_left_exit_left_entry_drop(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Drop a LEFT exit into a LEFT entry stacked directly below.

    Both ports sit on the left edge of one grid column.  Run a short lead
    out to the left of the column, drop vertically in that channel, then
    come back in to the target's left entry port::

        (sx,sy) -> (vx,sy) -> (vx,ty) -> (tx,ty)

    The channel ``vx`` is placed just left of the column's leftmost edge so
    the connector never re-enters either section's bbox.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dy = ty - sy
    vertical = vertical_direction(dy)

    delta, r_first, r_second = l_shape_radii(
        i,
        n,
        vertical=vertical,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    src_col = _resolve_section_col(ctx.graph, src)
    left_edge = col_left_edge(ctx.graph, src_col, default=min(sx, tx))
    vx = min(left_edge, sx, tx) - ctx.curve_radius - ctx.offset_step + delta

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (vx, sy), (vx, ty), (tx, ty)],
        is_inter_section=True,
        curve_radii=[r_first, r_second],
    )


def _left_entry_descent_x(
    ctx: _RoutingCtx, anchor_x: float, n_outer: int, signed_delta: float = 0.0
) -> float:
    """Descent-channel X for a LEFT-entry bundle, left of *anchor_x*.

    Places the bundle ``base_gap`` (curve radius + one offset step) left of
    *anchor_x*, bumping further when that gap would bring the bundle's
    innermost line within ``SECTION_ROUTE_CLEARANCE`` of the edge.  Callers
    pass the per-line stagger as *signed_delta* (``+delta`` when the channel
    sits on the bundle's right, ``-delta`` when on its left) to keep the
    concentric-corner handedness local to each handler.
    """
    base_gap = ctx.curve_radius + ctx.offset_step
    max_delta = (n_outer - 1) * ctx.offset_step / 2
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    return anchor_x - base_gap - extra_clearance + signed_delta


def _radius_inputs(
    fan: tuple[int, int] | None, i: int, n: int, offset_step: float
) -> tuple[float, float]:
    """Concentric-radius inputs ``(off, max_off)`` for a wrap/around corner.

    Uses the unified fan position ``(ui, un)`` when the edge pivots through a
    shared junction fan, else the edge's own ``(i, n)`` sub-bundle index.
    """
    if fan is not None:
        ui, un = fan
        return (un - 1 - ui) * offset_step, (un - 1) * offset_step
    return (n - 1 - i) * offset_step, (n - 1) * offset_step


def _v1_corner_x(ctx: _RoutingCtx, src: Station, sx: float, corner_x: float) -> float:
    """Push *corner_x* right so the source-side V1 channel keeps
    ``SECTION_ROUTE_CLEARANCE`` from the source section's right edge.

    When the source station sits at its section's right edge (e.g. a
    right-side exit port), the default lead-in lands the closest line only
    ~curve_radius past the edge, which reads as flush.  A junction source
    already offset past the edge yields a zero bump.
    """
    src_section = ctx.graph.sections.get(src.section_id) if src.section_id else None
    if src_section and src_section.bbox_w > 0:
        section_right = src_section.bbox_x + src_section.bbox_w
    else:
        section_right = sx
    current_gap = sx + ctx.curve_radius - section_right
    return corner_x + max(0.0, SECTION_ROUTE_CLEARANCE - current_gap)


def _route_left_entry_wrap(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Route to a LEFT entry port by wrapping around the left side.

    When the source is to the RIGHT of a LEFT entry port AND the sections
    are stacked vertically (so the standard L-shape would cut horizontally
    through the target section's interior to reach the left-side entry),
    drop straight down from the source, run leftward in the inter-row gap
    past the target section's left edge, then drop down and into the LEFT
    entry port::

        (sx,sy) -> (sx, hy) -> (vx, hy) -> (vx, ty) -> (tx, ty)

    This mirrors :func:`_route_right_entry_wrap` and avoids the
    "cut through intervening section" anti-pattern.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dy = ty - sy
    vertical = vertical_direction(dy)

    # When the junction has mixed-direction siblings, share the first-
    # corner geometry with the other handlers (bypass / L-shape) by
    # consuming junction_fan_info exactly the way _route_l_shape and
    # _route_bypass do.  This makes ALL outgoing routes from the
    # junction pivot through the same first corner; they only diverge
    # at the second corner (where this wrap turns into the inter-row
    # channel and the bypass continues downward).
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        fan_delta, r_first, _ = l_shape_radii(
            ui,
            un,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        # For the wrap, the source-side first corner sits on the
        # OUTSIDE of section 1 (the right of section 1's right edge,
        # since we're wrapping right then down then left).  Use the
        # going_right convention (+curve_radius + (un-1)*offset_step/2)
        # for fan_mid_x so the curve_start_x = corner_x - r_wrap lands
        # EXACTLY at the junction's x, which means the upstream
        # exit_port -> junction segment terminates at the curve start
        # with no overlap / no "nubbin" past the curve start.
        fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        # Second corner is per-line within this edge's sub-bundle.
        _, _, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        delta = fan_delta
    else:
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = None

    # Per-corner offset propagation rule:
    # The bundle enters going RIGHT with the outer line (i=0, delta > 0
    # going_down) at LARGER x in V1.  Through the four-corner wrap
    # (R -> D -> L -> D -> R; handedness CW, CW, CCW, CCW), the outer
    # line stays "on top" of the bundle when delivered at C4.  Each
    # corner propagates the stagger as:
    #   - V1 (vertical, post-C1): OUTER at LARGER x (sign +delta on corner_x).
    #   - H  (horizontal, post-C2): OUTER at LARGER y (sign +delta on hy).
    #   - V2 (vertical, post-C3): OUTER at LARGER x (sign +delta on vx).
    #   - Entry endpoint (post-C4): OUTER at SMALLER y (natural priority
    #     ordering via station_offsets, no per-corner flip).
    # This is the rule illustrated by the user's A/B example: A starts on
    # top going right, lands on top after C4 with no crossings at any
    # corner.
    hy_base = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    hy = hy_base + delta

    # Ensure the V2 channel (the per-line vertical run on the target's
    # OUTER side, before C4) sits at least SECTION_ROUTE_CLEARANCE past
    # the target section's left edge.  The default vx places the line
    # CLOSEST to the edge (delta=+max_delta in this sign convention) at
    # ~curve_radius from the edge, which reads as flush in renders.
    # A uniform extra shift preserves the per-line delta stagger so the
    # offset propagation rule is unchanged.
    n_for_outer = fan[1] if fan is not None else n
    vx = _left_entry_descent_x(ctx, tx, n_for_outer, delta)
    # When this wrap shares a junction fan with a corridor feeder descending
    # the same target column, anchor the descent channel to the column's LEFT
    # edge so the spine and the corridor overlay as one bundle instead of smearing.
    if fan is not None and _fan_has_corridor_sibling(edge.source, ctx):
        tgt_col = _resolve_section_col(ctx.graph, tgt)
        if tgt_col is not None:
            shared_vx = _fan_left_entry_descent_x(ctx, tgt_col, n_for_outer, delta)
            if shared_vx is not None:
                vx = shared_vx

    # Apply src/tgt station offsets explicitly so the renderer's later
    # _apply_line_offsets pass doesn't double-apply.  Without this, the
    # intermediate hy points fall in the source-vs-target heuristic's
    # target side (closer to ty than sy), get the target offset, and
    # cancel the per-line `delta` here - making the bundle collapse to
    # a single y in the horizontal gap segment.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    # Per-corner concentric radii.  The wrap turns R -> D -> L -> D -> R,
    # giving corner handedness CW, CCW, CCW, CW.  Tracing the outer line
    # (i=0, largest off_for_radius):
    #   * C1 (H_lead -> V1, CW):   outer is OUTSIDE the turn -> LARGE r.
    #   * C2 (V1   -> H,  CCW):    outer is OUTSIDE the turn -> LARGE r.
    #   * C3 (H    -> V2, CCW):    outer is OUTSIDE the turn -> LARGE r.
    #   * C4 (V2   -> H_entry, CW):outer is INSIDE  the turn -> SMALL r.
    # C4's handedness is the mirror of C1: the OUTER line of the bundle
    # is now on the INSIDE of the turn, so it gets base_radius (the
    # smallest radius) while the INNER bundle line gets the largest.
    # This matches the per-line vx stagger (vx = tx - curve_radius -
    # offset_step + delta), which places exactly r_outer_at_C4
    # =base_radius of post-segment for the outer line and
    # r_inner_at_C4 = base_radius + max_off for the inner line.  Using
    # outside=True for C4 (as previously) clamps the outer line's curve
    # radius down to base_radius because the post-C4 segment is too
    # short, producing the visible "outer line collapses at C4"
    # asymmetry against C1/C2/C3.  Mirrors the [r_lead, r_first,
    # r_first, r_second] pattern in _route_right_entry_wrap.
    off_for_radius, max_off_for_radius = _radius_inputs(fan, i, n, ctx.offset_step)
    r_wrap = corner_radius(
        off_for_radius,
        max_off_for_radius,
        outside=True,
        base_radius=ctx.curve_radius,
    )
    r_inside = corner_radius(
        off_for_radius,
        max_off_for_radius,
        outside=False,
        base_radius=ctx.curve_radius,
    )
    if fan_mid_x is not None:
        corner_x = fan_mid_x + fan_delta
    else:
        # No junction: still place the C1 corner OUTSIDE the source
        # section's right edge with per-line stagger, using the same
        # formula as the fan case (treating the bundle as a virtual
        # fan of size n).  Without this lead-in, corner_x == sx puts
        # V1 right at the section boundary and collapses all lines
        # onto a single column, producing the "compressed bundle at
        # the section edge" visual reported on a tall stacked section.
        non_fan_mid_x = sx + ctx.curve_radius + (n - 1) * ctx.offset_step / 2
        corner_x = non_fan_mid_x + delta
    # Ensure the V1 channel (the per-line vertical run on the SOURCE's
    # OUTER side, between C1 and C2) sits at least SECTION_ROUTE_CLEARANCE
    # past the source section's right edge.  Without this, when the source
    # station is AT the section's right edge (e.g. an exit port on the
    # right side), corner_x's closest line lands ~curve_radius past the
    # edge, which reads as flush in renders.  When the source is already
    # offset from the section (e.g. a junction at sx = bbox_right +
    # JUNCTION_MARGIN), this shift is zero.  Uniform across lines, so the
    # per-line delta stagger and the corner_x - r_wrap == sx cancellation
    # below are preserved by also shifting lx.
    corner_x = _v1_corner_x(ctx, src, sx, corner_x)
    # Lead-in extends r_wrap LEFT of the corner so the first corner
    # gets the SAME concentric radius as the other three corners.  With
    # the virtual-fan corner_x above, every line lands lx == sx
    # exactly (corner_x - r_wrap = sx + curve_radius + (n-1)*step/2 +
    # delta - r_wrap, and r_wrap = curve_radius + (n-1-i)*step, delta
    # = ((n-1)/2 - i)*step => they cancel to sx for every i).  When the
    # source clearance bump is non-zero, that cancellation places lx at
    # sx + bump; pin lx back to sx so the route starts at the source
    # station/port and the extra clearance manifests as a longer
    # horizontal lead-in
    # before C1 rather than a gap at the start of the path.
    lx = sx
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (lx, sy + src_off),
            (corner_x, sy + src_off),
            (corner_x, hy),
            (vx, hy),
            (vx, ty + tgt_off),
            (tx, ty + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        # Handedness-aware radii: C1 and C2 are right turns (the bundle's
        # outer line is OUTSIDE the turn) so use the larger outside-of-turn
        # radius r_wrap.  C3 and C4 are left turns (outer line is INSIDE
        # the turn) so use the smaller inside-of-turn radius r_inside.
        # Without this, C3's outer-line radius (r_wrap) clamps mid-curve
        # because the post-C3 segment is too short, producing the visible
        # "outer line wider than inner" asymmetry the user reported.
        curve_radii=[r_wrap, r_wrap, r_inside, r_inside],
        offsets_applied=True,
    )


def _has_bypass_sibling_to_same_entry(
    edge: Edge,
    entry_port: Station,
    ctx: _RoutingCtx,
) -> bool:
    """Detect whether a sibling merge trunk's bypass shares the V_up gap.

    Mirrors :func:`_has_around_section_sibling` (which lives on the
    trunk side and answers "is there an around-route sharing my gap?").
    Used by :func:`_route_around_section_below` to decide whether the
    V_up channel shares its gap with a bypass bundle (in which case
    the around-route is bundle index 1 in the symmetric layout) or
    has the gap to itself (bundle index 0 of 1).
    """
    if entry_port is None:
        return False
    ep_id = entry_port.id
    # Walk back from the entry port through the merge-junction graph
    # to find the merge junction this entry_port serves.
    for mj_id, mapped_ep in ctx.merge.entry_port_for.items():
        if mapped_ep != ep_id:
            continue
        # mj_id is a merge junction whose entry_port is ours.  Check
        # whether the trunk source feeding it routes via bypass.
        trunk_src = ctx.merge.trunk_source.get(mj_id)
        if trunk_src is None or trunk_src == edge.source:
            continue
        return True
    return False


def _corridor_descent_x(
    ctx: _RoutingCtx, ep_col: int, ep_row: int, delta: float
) -> float | None:
    """X of the inter-column channel just LEFT of the target column.

    The corridor descends the clear gap between ``ep_col - 1`` and
    ``ep_col`` measured at the *target* row, so a wide row-span section in a
    different row does not collapse the gap.  Returns ``None`` when there is no
    column to the left (degenerate; caller falls back to the around-below loop).
    """
    if ep_col <= 0:
        return None
    gap_left, gap_right = column_gap_edges(ctx.graph, ep_col - 1, ep_col, row=ep_row)
    if gap_right <= gap_left:
        return None
    # +delta (not -delta): the L->D corner into this channel is concentric
    # only when vx + r is constant across the bundle.  r_inner shrinks for
    # the +delta (rightmost) line, so that line must sit at the LARGER vx;
    # the opposite sign delaminates the descent corner.
    return (gap_left + gap_right) / 2 + delta


def _fan_left_entry_descent_x(
    ctx: _RoutingCtx, tgt_col: int, n_outer: int, delta: float
) -> float | None:
    """Shared descent-channel X for a junction fan's LEFT-entry targets.

    When one junction fans the same lines to two LEFT-entry sections
    stacked in the same column - one reached by :func:`_route_left_entry_wrap`
    (the spine), the other by :func:`_route_inter_row_gap_corridor` (the QC
    feed) - both bundles must descend the SAME vertical channel so they
    overlay as one clean bundle rather than smearing a few px apart.

    Anchor the channel to the column's LEFT edge (the leftmost section left
    edge across all rows of *tgt_col*) so both handlers, whose individual
    targets sit at slightly different x, agree on one channel.  The
    per-line ``delta`` stagger is preserved.  Returns ``None`` when the
    column has no measurable left edge.
    """
    col_left = col_left_edge(ctx.graph, tgt_col, default=0.0)
    if col_left <= 0.0:
        return None
    return _left_entry_descent_x(ctx, col_left, n_outer, delta)


def _fan_has_corridor_sibling(junction_id: str, ctx: _RoutingCtx) -> bool:
    """True if *junction_id* fans an edge routed via the inter-row-gap corridor.

    Used so a sibling :func:`_route_left_entry_wrap` spine aligns its descent
    channel with the corridor feeder's.  A corridor feeder is a
    downward cross-row edge into a LEFT-entry section (merge junction or
    direct port) for which :func:`_corridor_is_viable` holds.
    """
    graph = ctx.graph
    for edge in graph.edges_from(junction_id):
        tgt = graph.stations.get(edge.target)
        if tgt is None:
            continue
        ep_id = ctx.merge.entry_port_for.get(edge.target)
        ep = graph.stations.get(ep_id) if ep_id else tgt
        if ep is not None and _corridor_is_viable(ctx, graph.stations[junction_id], ep):
            return True
    return False


def _corridor_is_viable(ctx: _RoutingCtx, src: Station, entry_port: Station) -> bool:
    """Whether the inter-row-gap + inter-column-channel corridor exists.

    Used to route a downward cross-row merge feeder through the clear
    corridor instead of the canvas-bottom loop
    (:func:`_route_around_section_below`).  Requires:

    * a LEFT entry port (the corridor descends just left of the target);
    * the target section sits in a row strictly *below* the source's row
      (a downward cross-row feeder; same-row fan-ins U-route in the gap
      below the row and must keep the legacy handler);
    * an inter-row gap below the source row exists in the source's column;
    * a clear inter-column channel exists left of the target column.
    """
    if entry_port is None:
        return False
    ep_port = ctx.graph.ports.get(entry_port.id)
    if ep_port is None or ep_port.side != PortSide.LEFT:
        return False
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col, ep_row = _resolve_section_colrow(ctx.graph, entry_port)
    if src_row is None or ep_row is None or src_col is None or ep_col is None:
        return False
    if ep_row <= src_row:
        return False
    if _corridor_descent_x(ctx, ep_col, ep_row, 0.0) is None:
        return False
    # An inter-row gap must open below the source row within its column.
    gap_top = row_bottom_edge(ctx.graph, src_row, col=src_col)
    gap_bottom = row_top_edge(ctx.graph, src_row + 1, col=src_col)
    return gap_bottom - gap_top > EDGE_TO_BUNDLE_CLEARANCE


def _route_inter_row_gap_corridor(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route a downward cross-row LEFT-entry merge feeder via the clear
    inter-row / inter-column corridor instead of the canvas-bottom loop.

    A multi-row collector fan-in feeds the left-entry ``reporting`` section
    (row 3) from QC sources exiting on the right in rows 0 and 1.  Rather
    than dropping to the canvas bottom (below the tall ``variant_calling``
    row-span) and climbing back up (:func:`_route_around_section_below`),
    descend through the corridor that genuinely exists::

        (lx, sy)        -> H lead-in right of source
        (corner_x, sy)  ; turn down
        (corner_x, gy)  -> V down to the inter-row gap below the source row
        (vx, gy)        -> H left in that gap to the inter-column channel
        (vx, ey)        -> V down the channel to the entry Y
        (ex, ey)        -> H right into the LEFT entry port

    All feeders converge in the same inter-column channel (``vx``) just
    left of the target column, so they travel down together as one bundle
    meeting the carriage-return spine, rather than two separate loops.

    Corners: R->D (CW), D->L (CW), L->D (CCW), D->R (CCW).  The bundle is
    staggered by ``delta`` (the L-shape offset) on each leg so parallel
    lines keep concentric corners and a constant gap.
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y

    # When this corridor feeder shares a junction fan with a sibling wrap,
    # consume the unified fan position so the source-side first corner and the
    # inter-row gap stagger match the wrap exactly.  Without this the corridor
    # picks its own per-bundle (i, n) and the two same-line bundles smear a few
    # px apart instead of overlaying.
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    pos_i, pos_n = fan if fan is not None else (i, n)

    # l_shape_radii(Direction.D) already returns the outer (corners 1-2) and
    # inner (corners 3-4) radii for this bundle; reuse them rather than
    # recomputing via corner_radius.
    delta, r_outer, r_inner = l_shape_radii(
        pos_i,
        pos_n,
        vertical=Direction.D,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col, ep_row = _resolve_section_colrow(ctx.graph, entry_port)
    # Guaranteed by the _corridor_is_viable check at every call site.
    assert (
        src_col is not None
        and src_row is not None
        and ep_col is not None
        and ep_row is not None
    )

    # Inter-row gap Y just below the source row (column-restricted so a
    # tall row-span in another column doesn't push the channel down).  Use
    # the header-aware band so the leftward traverse clears the next row's
    # section-header badge, not just the bbox edge.
    gap_top = row_bottom_edge(ctx.graph, src_row, col=src_col)
    gap_bottom = row_top_edge(ctx.graph, src_row + 1, col=src_col, default=gap_top)
    if fan is not None:
        # Share the sibling wrap's inter-row band: it centres the leftward
        # traverse in the gap below the SOURCE row using the global (non
        # column-restricted) row edges, so the two bundles' H legs coincide
        # rather than smearing 3px apart.
        wrap_top = row_bottom_edge(ctx.graph, src_row, default=gap_top)
        wrap_bottom = row_top_edge(ctx.graph, src_row + 1, default=wrap_top)
        gy_base = _center_inter_row_channel(wrap_top, wrap_bottom)
    elif gap_bottom > gap_top:
        gy_base = _center_inter_row_channel(gap_top, gap_bottom)
    else:
        gy_base = gap_top + INTER_ROW_EDGE_CLEARANCE
    # Outer line sits at LARGER y in this leftward run (CW D->L corner).
    gy = gy_base + delta
    # Keep every staggered line inside the clearance band: at least
    # INTER_ROW_EDGE_CLEARANCE below the source-row bottom and clear of the
    # next row's header badge.  In a tight gap the band is narrower than the
    # bundle, so the per-line stagger collapses rather than grazing an edge.
    # Skipped for fan feeders, which share the wrap sibling's (unclamped)
    # band so the two bundles' H legs coincide.
    if fan is None and gap_bottom > gap_top:
        gy = min(
            max(gy, gap_top + INTER_ROW_EDGE_CLEARANCE),
            gap_bottom - INTER_ROW_HEADER_CLEARANCE,
        )

    # Inter-column descent channel left of the target column.  For a fan
    # feeder, anchor it to the target COLUMN's left edge (shared with the
    # sibling wrap) so the two bundles descend the same channel; otherwise
    # use the inter-column gap midpoint.
    vx = None
    if fan is not None and ep_col is not None:
        vx = _fan_left_entry_descent_x(ctx, ep_col, pos_n, delta)
    if vx is None:
        vx = _corridor_descent_x(ctx, ep_col, ep_row, delta)
    assert vx is not None

    # H lead-in right of the source, clear of the source section's edge.
    # When the source is a sectionless junction, fall back to its own X as
    # the reference edge (mirrors :func:`_route_left_entry_wrap`) so a fan
    # feeder gets the SAME source-side clearance as its sibling wrap.
    corner_x = sx + ctx.curve_radius + (pos_n - 1) * ctx.offset_step / 2 + delta
    corner_x = _v1_corner_x(ctx, src, sx, corner_x)

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, sy + src_off),
            (corner_x, sy + src_off),
            (corner_x, gy),
            (vx, gy),
            (vx, ey + tgt_off),
            (ex, ey + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        # Corridor turns R->D->L->D->R (handedness CW, CW, CCW, CCW).  The
        # bundle's outer line is OUTSIDE corners 1-2 (larger radius r_outer)
        # but INSIDE corners 3-4 (smaller radius r_inner), so each corner's
        # arcs share a center and the bundle holds even spacing through the
        # descent.  Using r_outer at all four corners delaminates the L->D and
        # D->R turns.  Mirrors :func:`_route_left_entry_wrap`.
        curve_radii=[r_outer, r_outer, r_inner, r_inner],
        offsets_applied=True,
    )


def _route_around_section_below(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route to a LEFT entry port by going AROUND BELOW the target section.

    Used when a standard L-shape or :func:`_route_left_entry_wrap` would
    have its horizontal segment cross an intervening section's bbox.
    Routes via 4 corners in a clockwise R-D-L-U-R loop that descends
    past the target row's bottom, runs leftward under everything, rises
    in the inter-section gap to the entry Y, and enters the LEFT port
    from below::

        (lx, sy) -> (cx, sy)          ; H lead-in right
        (cx, sy) -> (cx, by)          ; V down past target row's bottom
        (cx, by) -> (vx, by)          ; H left past target's left edge
        (vx, by) -> (vx, ey)          ; V up to entry Y
        (vx, ey) -> (ex, ey)          ; H right into LEFT entry port

    All four corners are clockwise (R->D, D->L, L->U, U->R), so the
    outer line of the bundle stays on the OUTSIDE of every turn and
    gets the larger radius throughout.

    *tgt* is the L-shape's nominal target (the edge target, often a
    merge junction).  *entry_port* is the actual endpoint of the route
    (the LEFT entry port station resolved from the merge junction or
    equal to *tgt* when the edge targets a port directly).
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y
    vertical = vertical_direction(ey - sy)

    # Match the geometry of _route_left_entry_wrap's first corner so
    # this handler composes cleanly with sibling routes from the same
    # junction (junction_fan_info pivots all outgoing edges through a
    # shared first corner; merge-branch edges are excluded from
    # junction_fan_info, so for the merge case fan is typically None).
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        fan_delta, _r_first, _ = l_shape_radii(
            ui,
            un,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        delta = fan_delta
    else:
        delta, _r_first, _r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = None
    off_for_radius, max_off_for_radius = _radius_inputs(fan, i, n, ctx.offset_step)

    # All four corners are CW (clockwise loop: R->D->L->U->R).  The outer
    # line of the bundle stays OUTSIDE every turn and gets the larger
    # radius; the inner line gets the smaller radius.
    r_outer = corner_radius(
        off_for_radius,
        max_off_for_radius,
        outside=True,
        base_radius=ctx.curve_radius,
    )

    # Bypass Y below all sections in the column range so the route
    # clears every intervening section (cross_row=True).
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    # Fallbacks if a column can't be resolved (degenerate cases).
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    by_base = bypass_bottom_y(
        ctx.graph,
        bc_src_col,
        bc_tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=True,
    )
    by = by_base + delta

    # Vertical V2 channel sits just left of the target section's bbox.
    # The outer bundle line (delta > 0 going_down) sits at LARGER y in
    # H_bottom AND at SMALLER x in V_up.  This handedness is OPPOSITE
    # to _route_left_entry_wrap's vx convention because the around-route
    # turns from leftward-H to upward-V (a CW W->N corner with center
    # NE of corner), whereas the wrap turns from leftward-H to
    # downward-V (a CCW W->S corner with center SE).  Concentric C3
    # requires the outer line on the FAR side from the arc center, so
    # the V_up x stagger uses ``-delta`` here while the wrap uses
    # ``+delta``.  Without this sign flip, the per-line C3 arcs end up
    # with different centers and visibly cross under the target
    # section's left edge.
    ep_section = (
        ctx.graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    if ep_section and ep_section.bbox_w > 0:
        section_left = ep_section.bbox_x
    else:
        section_left = ex
    n_for_outer = fan[1] if fan is not None else n

    # V_up X: position the bundle within the inter-column gap just
    # left of the target section, using the principled symmetric
    # placement.  When a sibling merge-trunk bypass shares this gap,
    # we're bundle 1 (rightmost); else we're the sole bundle.  The
    # symmetric helper handles both cases and the post-corner -delta
    # stagger preserves the around-route's V_up sign convention.
    paired_with_bypass = _has_bypass_sibling_to_same_entry(edge, entry_port, ctx)
    if ep_col is not None and ep_col > 0:
        gap_left, gap_right = column_gap_edges(ctx.graph, ep_col - 1, ep_col)
        bw = bundle_width(n_for_outer, ctx.offset_step)
        widths = [bw, bw] if paired_with_bypass else [bw]
        bundle_idx = 1 if paired_with_bypass else 0
        vx_mid = symmetric_bundle_midpoint(gap_left, gap_right, widths, bundle_idx)
        # Sanity floor: keep the V_up clear of the target section's left
        # edge (re-applying the legacy clamp) when the gap is too narrow
        # for full symmetric placement.
        max_vx_mid = _left_entry_descent_x(ctx, section_left, n_for_outer)
        vx_mid = min(vx_mid, max_vx_mid)
        vx = vx_mid - delta
    else:
        # Fallback for degenerate cases without column info: legacy
        # anchored-to-edge placement.
        vx = _left_entry_descent_x(ctx, section_left, n_for_outer, -delta)

    # First-corner X: lead-in right of source, mirroring _route_left_entry_wrap.
    if fan_mid_x is not None:
        corner_x = fan_mid_x + delta
    else:
        non_fan_mid_x = sx + ctx.curve_radius + (n - 1) * ctx.offset_step / 2
        corner_x = non_fan_mid_x + delta
    # V1 clearance from the source section's right edge, mirroring
    # _route_left_entry_wrap.  See comments there for the derivation.
    corner_x = _v1_corner_x(ctx, src, sx, corner_x)
    # Pin lx at sx so the route starts at the source station; the source
    # clearance bump manifests as a longer H lead-in before C1.  See the
    # analogous comment in _route_left_entry_wrap.
    lx = sx

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (lx, sy + src_off),
            (corner_x, sy + src_off),
            (corner_x, by),
            (vx, by),
            (vx, ey + tgt_off),
            (ex, ey + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        # All four corners are CW; the outer bundle line stays on the
        # OUTSIDE of every turn (large y at H_bottom, small x at V_up
        # given the V_up sign flip above), so every corner uses the
        # outside-of-turn radius.  C1/C2/C3 are perfectly concentric;
        # C4 shares Cx with C3 but its Cy differs per line because all
        # lines converge at the single entry port endpoint (the arcs
        # nest from inside-out without crossing).
        curve_radii=[r_outer, r_outer, r_outer, r_outer],
        offsets_applied=True,
    )


def _route_right_entry_wrap(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Route to a RIGHT entry port by wrapping around the right side.

    When the source is to the LEFT of a RIGHT entry port, the standard
    L-shape would cut horizontally through the target section.  Instead,
    drop into the inter-row gap, run horizontally past the target
    section's right edge, then drop into the RIGHT entry port::

        (sx,sy) -> (lx, sy) -> (lx, hy) -> (vx, hy) -> (vx, ty) -> (tx, ty)

    For cross-row cases, the horizontal channel runs just below the
    source row's sections (bypass style) so the line stays high and
    only drops down when it reaches the target column.

    This avoids crossing through intervening sections.

    Per-corner offset propagation is the mirror of
    :func:`_route_left_entry_wrap`: bundle going RIGHT with outer line
    (i=0, delta>0 going_down) at SMALLER y in V0 (sy+src_off) wraps
    through R-D-R-D-L (handedness CW, CCW, CW, CW), landing at the
    entry port with outer at SMALLER y again - matching the natural
    priority-based station-offset ordering.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dy = ty - sy
    vertical = vertical_direction(dy)

    # When this wrap shares a junction with mixed-direction siblings
    # (e.g. another RIGHT-entry feed routed via the inter-row gap above),
    # share the source-side first-corner X by consuming junction_fan_info
    # exactly the way _route_left_entry_wrap / _route_right_entry_via_gap_above
    # do.  Without this the wrap picks lx = sx + curve_radius on its own and
    # its V1 downturn leg splays apart from the sibling's bundled channel
    # (issue #484).
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        fan_delta, _r_first, _ = l_shape_radii(
            ui,
            un,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        delta = fan_delta
        off_for_radius, max_off_for_radius = _radius_inputs(fan, i, n, ctx.offset_step)
        r_first = corner_radius(
            off_for_radius,
            max_off_for_radius,
            outside=True,
            base_radius=ctx.curve_radius,
        )
        _, _, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
    else:
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = None

    # Detect cross-row case: use bypass-style Y just below the source
    # row's sections so the line runs horizontally under the adjacent
    # section before dropping to the target row.
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    tgt_col, tgt_row = _resolve_section_colrow(ctx.graph, tgt)

    cross_row = (
        src_row is not None
        and tgt_row is not None
        and src_row != tgt_row
        and src_col is not None
        and tgt_col is not None
    )

    if cross_row:
        assert src_col is not None and tgt_col is not None
        hy = bypass_bottom_y(
            ctx.graph, src_col, tgt_col, BYPASS_CLEARANCE, src_row=src_row
        )
        hy += delta
    else:
        # Same-row: use inter-row gap above the target section.
        hy = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
        hy += delta

    # Vertical channel X: just past the entry port in the inter-section
    # gap.  Mirror of the left-entry wrap's V2 sign convention: OUTER
    # (delta > 0 going_down) lands at SMALLER x in V2 here (west of
    # bundle midline) because the C3 (R->D, CW) corner preserves the
    # right-of-travel position established on H by C2 (D->R, CCW).
    # Apply SECTION_ROUTE_CLEARANCE so the line CLOSEST to the section's
    # right edge (delta=-max_delta in this sign convention) keeps a
    # visible gap.  Uniform shift preserves the offset stagger.
    max_delta = (n - 1) * ctx.offset_step / 2
    base_gap = ctx.curve_radius + ctx.offset_step
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    vx = tx + base_gap + extra_clearance - delta

    if fan_mid_x is not None:
        # Share the source-side first-corner (V1) channel with the junction's
        # other downturning siblings: place V1 on the fan midline staggered by
        # this line's per-line delta, then apply the same right-edge clearance
        # bump the sibling handlers use so all the downturn legs land in one
        # concentric bundle rather than splaying (#484).  The route still
        # starts at the junction (sx); the clearance manifests as a longer
        # horizontal lead-in before C1.
        v1_x = _v1_corner_x(ctx, src, sx, fan_mid_x + fan_delta)
        r_lead = r_first
    else:
        # Short horizontal lead-in so the first corner (horizontal-to-vertical)
        # gets a smooth curve instead of a sharp right angle at the junction.
        r_lead = reference_anchored_radius(0.0, ctx.curve_radius)
        v1_x = sx + r_lead

    # Bake the per-line station offset into the endpoints (mirroring
    # _route_left_entry_wrap).  The horizontal channel ``hy`` already carries
    # the per-line ``delta`` and the deconflicted band Y; if the offset were
    # left for the renderer's source/target heuristic it would shift the
    # whole horizontal leg onto a neighbouring line's channel.
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, sy + src_off),
            (v1_x, sy + src_off),
            (v1_x, hy),
            (vx, hy),
            (vx, ty + tgt_off),
            (tx, ty + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r_lead, r_first, r_first, r_second],
        offsets_applied=True,
    )


def _right_entry_gap_above_target_y(
    graph: MetroGraph, src_row: int
) -> tuple[float, float]:
    """Return ``(gap_top, gap_bottom)`` of the inter-row band below *src_row*.

    The band sits between the source row's bottom edge and the next row's
    top edge.  Computed over all columns (not column-restricted) so the
    long rightward traverse stays clear of every section in the span.
    """
    gap_top = row_bottom_edge(graph, src_row)
    gap_bottom = row_top_edge(graph, src_row + 1, default=gap_top)
    return gap_top, gap_bottom


def _right_entry_gap_above_is_clear(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    entry_port: Station,
    src_row: int,
) -> bool:
    """Whether a RIGHT-entry feed from above can use the inter-row gap.

    The route runs its long horizontal in the band just below the source
    row, then drops straight down the RIGHT side of the target column into
    the port.  Viable only when that band genuinely exists (the next row's
    top is below the source row's bottom) and the horizontal at the band's
    centre crosses no section interior between the source and the target's
    right edge.
    """
    gap_top, gap_bottom = _right_entry_gap_above_target_y(graph, src_row)
    if gap_bottom <= gap_top:
        return False
    gy = _center_inter_row_channel(gap_top, gap_bottom)

    ep_section = (
        graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    section_right = (
        ep_section.bbox_x + ep_section.bbox_w
        if ep_section and ep_section.bbox_w > 0
        else entry_port.x
    )
    # Horizontal run spans the source X out to just past the target's right
    # edge (where the descent channel sits).  Exclude the source and target
    # sections themselves; any OTHER section the band crosses kills the gap
    # route (fall back to the around-below loop).
    exclude = {sid for sid in (src.section_id, tgt.section_id) if sid is not None}
    return not _h_segment_crosses_other_section(
        graph, src.x, section_right, gy, exclude
    )


def _build_right_entry_wrap_route(
    edge: Edge,
    src: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    channel_y_base: float,
) -> RoutedPath:
    """Build a wrap route into a RIGHT entry port from its outward side.

    Shared body of :func:`_route_right_entry_via_gap_above` and
    :func:`_route_right_entry_around_below`, which differ only in the
    horizontal channel they pass.  Leads right out of the source, drops to
    ``channel_y_base`` (offset by the per-line fan ``delta``), runs right
    past the target's right edge, then turns to the entry Y and in to the
    RIGHT port from ``vx >= ex`` (its outward side), never crossing the
    section interior.
    """
    sx, sy = src.x, src.y
    ex, ey = entry_port.x, entry_port.y
    vertical = vertical_direction(ey - sy)

    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        fan_delta, _r_first, _ = l_shape_radii(
            ui,
            un,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        delta = fan_delta
    else:
        delta, _r_first, _r_second = l_shape_radii(
            i,
            n,
            vertical=vertical,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        fan_mid_x = None
    off_for_radius, max_off_for_radius = _radius_inputs(fan, i, n, ctx.offset_step)
    r_outer = corner_radius(
        off_for_radius,
        max_off_for_radius,
        outside=True,
        base_radius=ctx.curve_radius,
    )

    hy = channel_y_base + delta

    # V_down/up channel sits just RIGHT of the target section's bbox, in the
    # gap to the right of the target column; +delta keeps the outer line
    # (larger radius) on the outside of both turns into the right-side port.
    ep_section = (
        ctx.graph.sections.get(entry_port.section_id) if entry_port.section_id else None
    )
    section_right = (
        ep_section.bbox_x + ep_section.bbox_w
        if ep_section and ep_section.bbox_w > 0
        else ex
    )
    n_for_outer = fan[1] if fan is not None else n
    base_gap = ctx.curve_radius + ctx.offset_step
    max_delta = (n_for_outer - 1) * ctx.offset_step / 2
    extra_clearance = max(0.0, SECTION_ROUTE_CLEARANCE - (base_gap - max_delta))
    vx = section_right + base_gap + extra_clearance + delta

    if fan_mid_x is not None:
        corner_x = fan_mid_x + delta
    else:
        non_fan_mid_x = sx + ctx.curve_radius + (n - 1) * ctx.offset_step / 2
        corner_x = non_fan_mid_x + delta
    corner_x = _v1_corner_x(ctx, src, sx, corner_x)
    lx = sx

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (lx, sy + src_off),
            (corner_x, sy + src_off),
            (corner_x, hy),
            (vx, hy),
            (vx, ey + tgt_off),
            (ex, ey + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r_outer, r_outer, r_outer, r_outer],
        offsets_applied=True,
    )


def _route_right_entry_via_gap_above(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
    src_row: int,
) -> RoutedPath:
    """Route to a RIGHT entry port via the inter-row gap ABOVE the target row.

    Used when the source sits in a row ABOVE the target's row.  Going UNDER
    the whole target row (:func:`_route_right_entry_around_below`) would run
    the long rightward horizontal counter to the target row's flow.  Instead
    run that horizontal in the clear inter-row band just below the source
    row, then drop straight down the RIGHT side of the target column into the
    RIGHT entry port::

        (lx, sy) -> (cx, sy)        ; H lead-in right out of the source
        (cx, sy) -> (cx, gy)        ; V down into the inter-row gap
        (cx, gy) -> (vx, gy)        ; H right past the target's right edge
        (vx, gy) -> (vx, ey)        ; V down to the entry Y
        (vx, ey) -> (ex, ey)        ; H left into the RIGHT entry port

    The approach to the port arrives from ``vx >= ex`` (the port's own
    outward side), and the horizontal never crosses a section interior
    (guaranteed by :func:`_right_entry_gap_above_is_clear` at the call site).
    """
    gap_top, gap_bottom = _right_entry_gap_above_target_y(ctx.graph, src_row)
    channel_y_base = _center_inter_row_channel(gap_top, gap_bottom)
    return _build_right_entry_wrap_route(
        edge, src, entry_port, i, n, ctx, channel_y_base
    )


def _route_right_entry_around_below(
    edge: Edge,
    src: Station,
    tgt: Station,
    entry_port: Station,
    i: int,
    n: int,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route to a RIGHT entry port by going AROUND BELOW the target section.

    The mirror of :func:`_route_around_section_below`.  Used when the
    source sits to the LEFT of a RIGHT entry port across intervening
    sections, so a standard bypass would rise in the inter-column gap
    LEFT of the target and then run its final horizontal RIGHTWARD across
    the section interior to reach the right-edge port (the route would
    enter the box's far side and double back).  Instead, descend past the
    target row's bottom, run leftward-to-rightward under everything, rise
    in the gap to the RIGHT of the target box, then enter the RIGHT port
    from the right::

        (lx, sy) -> (cx, sy)        ; H lead-in right out of the source
        (cx, sy) -> (cx, by)        ; V down past the target row's bottom
        (cx, by) -> (vx, by)        ; H right past the target's right edge
        (vx, by) -> (vx, ey)        ; V up to the entry Y
        (vx, ey) -> (ex, ey)        ; H left into the RIGHT entry port

    The approach to the port arrives from ``vx >= ex`` (the port's own
    outward side), never crossing the section interior.
    """
    # Bypass Y below all sections in the column range so the route clears
    # every intervening section, including the target row.
    src_col, src_row = _resolve_section_colrow(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    channel_y_base = bypass_bottom_y(
        ctx.graph,
        bc_src_col,
        bc_tgt_col,
        BYPASS_CLEARANCE,
        src_row=src_row,
        cross_row=True,
    )
    return _build_right_entry_wrap_route(
        edge, src, entry_port, i, n, ctx, channel_y_base
    )
