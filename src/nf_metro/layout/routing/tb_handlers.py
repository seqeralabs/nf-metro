"""Top-bottom section routing handlers: TB internal, TB L/R exit and
entry, perpendicular entry, and diagonal placement.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    MIN_STRAIGHT_EDGE,
)
from nf_metro.layout.routing.common import (
    RoutedPath,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _max_offset_at,
    _perp_entry_crossing_x,
    _perp_riser_lateral,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import (
    concentric_corner_radius,
    concentric_corner_radius_at,
    reference_anchored_radius,
    tb_entry_corner,
    tb_exit_corner,
)
from nf_metro.parser.model import (
    Edge,
    Port,
    PortSide,
    Station,
)


def _route_tb_internal(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route internal edges within TB sections as vertical drops."""
    graph = ctx.graph
    src_sec = src.section_id
    tgt_sec = tgt.section_id

    tgt_exit_port = graph.ports.get(edge.target)
    tgt_is_bottom_exit = (
        tgt_exit_port is not None
        and not tgt_exit_port.is_entry
        and tgt_exit_port.side == PortSide.BOTTOM
    )
    if not (
        src_sec
        and src_sec == tgt_sec
        and src_sec in ctx.tb_sections
        and not src.is_port
        and (not tgt.is_port or tgt_is_bottom_exit)
    ):
        return None

    x_src = _tb_x_offset(ctx, edge.source, edge.line_id, src_sec)
    x_tgt = _tb_x_offset(ctx, edge.target, edge.line_id, src_sec)

    sx = src.x + x_src
    sy = src.y
    tx = tgt.x + x_tgt
    ty = tgt.y
    dx = tx - sx

    # Different X tracks: route with vertical runs + 45-degree diagonal
    if abs(dx) >= COORD_TOLERANCE:
        return _route_tb_diagonal(edge, sx, sy, tx, ty, ctx)

    # Same track: straight vertical drop
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (tx, ty)],
        offsets_applied=True,
    )


def _compute_diagonal_placement(
    run_src: float,
    run_tgt: float,
    diagonal_run: float,
    src_min_straight: float,
    tgt_min_straight: float,
    is_fork: bool,
    is_join: bool,
) -> tuple[float, float]:
    """Compute diagonal start/end on the run axis.

    Shared by ``_route_diagonal`` (horizontal run axis) and
    ``_route_tb_diagonal`` (vertical run axis).  The caller maps the
    result back to (x, y) coordinates.

    Returns (diag_start, diag_end) in run-axis coordinates.
    """
    delta = run_tgt - run_src
    sign = 1.0 if delta > 0 else -1.0
    half_diag = diagonal_run / 2

    # Bias diagonal toward fork/join stations
    if is_fork:
        mid = run_src + sign * (src_min_straight + half_diag)
    elif is_join:
        mid = run_tgt - sign * (tgt_min_straight + half_diag)
    else:
        mid = (run_src + run_tgt) / 2

    diag_start = mid - sign * half_diag
    diag_end = mid + sign * half_diag

    # Clamp to ensure minimum straight runs at endpoints
    if sign > 0:
        diag_start = max(diag_start, run_src + src_min_straight)
        diag_end = min(diag_end, run_tgt - tgt_min_straight)
        if diag_end < diag_start:
            midpoint = (diag_start + diag_end) / 2
            diag_start = diag_end = midpoint
    else:
        diag_start = min(diag_start, run_src - src_min_straight)
        diag_end = max(diag_end, run_tgt + tgt_min_straight)
        if diag_end > diag_start:
            midpoint = (diag_start + diag_end) / 2
            diag_start = diag_end = midpoint

    return diag_start, diag_end


def _route_tb_diagonal(
    edge: Edge,
    sx: float,
    sy: float,
    tx: float,
    ty: float,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route TB edges with vertical runs and a 45-degree diagonal transition."""
    diag_start_y, diag_end_y = _compute_diagonal_placement(
        sy,
        ty,
        ctx.diagonal_run,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_EDGE,
        edge.source in ctx.fork_stations,
        edge.target in ctx.join_stations,
    )

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (sx, diag_start_y), (tx, diag_end_y), (tx, ty)],
        offsets_applied=True,
    )


def _route_tb_lr_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route internal station -> LEFT/RIGHT exit port in a TB section."""
    graph = ctx.graph
    tgt_port = graph.ports.get(edge.target)
    tgt_is_lr_exit = (
        tgt_port is not None
        and not tgt_port.is_entry
        and tgt_port.side in (PortSide.LEFT, PortSide.RIGHT)
    )
    if not (
        tgt_is_lr_exit
        and not src.is_port
        and src.section_id in ctx.tb_sections
        and src.section_id == tgt.section_id
    ):
        return None
    assert tgt_port is not None

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    max_src_off = _max_offset_at(ctx, edge.source)

    vert_x_off, _horiz_y_off, r = tb_exit_corner(
        src_off,
        max_src_off,
        exit_right=(tgt_port.side == PortSide.RIGHT),
        base_radius=ctx.curve_radius,
    )
    # The horizontal approach must arrive at the exit port at the SAME Y the
    # outgoing port -> junction route departs (``port.y + port_offset``), or
    # the line steps by a bundle offset exactly at the section boundary
    # (issue #484).  ``tb_exit_corner``'s ``horiz_y_off`` is the source
    # station's reversed offset, which only coincides with the port offset
    # when every line at the source also exits through this port; otherwise
    # the leg lands off the port centre.  Use the port's own offset directly
    # so the inside and outside segments share a Y.
    horiz_y_off = _get_offset(ctx, edge.target, edge.line_id)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (src.x + vert_x_off, src.y),
            (src.x + vert_x_off, tgt.y + horiz_y_off),
            (tgt.x, tgt.y + horiz_y_off),
        ],
        offsets_applied=True,
        curve_radii=[r],
    )


def _route_tb_lr_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route LEFT/RIGHT entry port -> internal station in a TB section."""
    graph = ctx.graph
    src_port = graph.ports.get(edge.source)
    if not (
        src_port
        and src_port.side in (PortSide.LEFT, PortSide.RIGHT)
        and src_port.is_entry
        and not tgt.is_port
        and src.section_id in ctx.tb_sections
    ):
        return None

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    max_tgt_off = _max_offset_at(ctx, edge.target)

    vert_x_off, r = tb_entry_corner(
        tgt_off,
        max_tgt_off,
        entry_right=(src_port.side == PortSide.RIGHT),
        base_radius=ctx.curve_radius,
    )
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (src.x, src.y + src_off),
            (tgt.x + vert_x_off, src.y + src_off),
            (tgt.x + vert_x_off, tgt.y),
        ],
        offsets_applied=True,
        curve_radii=[r],
    )


def _route_perp_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route a TOP/BOTTOM entry port down to its internal target station."""
    graph = ctx.graph
    src_port = graph.ports.get(edge.source)
    if not (
        src_port
        and src_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and not tgt.is_port
    ):
        return None

    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx

    corridor_feeder = _perp_corridor_feeder(edge, src, ctx)
    if corridor_feeder is not None:
        return _route_perp_entry_from_corridor(
            edge, src, tgt, ctx, corridor_feeder.side
        )

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    # When distinct lines share this port and target with per-line offset zero,
    # hold them on parallel drop channels so they don't overlay; the stagger
    # order tracks the target-side tgt_off so the drop->turn corner preserves
    # bundle order.  A fanned port already carries the separation in src_off, so
    # the stagger is zero.
    drop_delta = _perp_entry_drop_delta(edge, dx, ctx)
    drop_x = sx + src_off + drop_delta

    # That centred stagger re-fans the lines off the port marker, but the
    # inter-section approach into an LR/RL section lands each line on a
    # reference-on-marker channel; converging at the marker between the two
    # leaves a lateral reversal on the boundary.  When such a bundled feeder
    # pins the channel, drop straight through it instead.  A TB/BT continuation
    # is flow-aligned on its trunk X offset (which the approach already
    # matches), and a fanned port has no stagger to reverse, so neither applies.
    tgt_sec = ctx.graph.sections.get(tgt.section_id) if tgt.section_id else None
    crossing_x = None
    if (
        abs(drop_delta) > COORD_TOLERANCE
        and tgt_sec is not None
        and tgt_sec.direction not in ("TB", "BT")
    ):
        crossing_x = _perp_entry_crossing_x(ctx, edge.source, edge.line_id, sx)
        if crossing_x is not None:
            drop_x = crossing_x
            drop_delta = drop_x - (sx + src_off)

    if abs(dx) < COORD_TOLERANCE and abs(drop_delta) < COORD_TOLERANCE:
        # Aligned perpendicular entry into the trunk: each line drops straight
        # at its in-section trunk X offset and continues down, so a multi-line
        # bundle stays parallel into the trunk instead of one line slanting
        # across to a Y-staggered marker.
        x = tx + _tb_x_offset(ctx, edge.target, edge.line_id, tgt.section_id)
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(x, sy), (x, ty)],
            offsets_applied=True,
        )

    # L-shape: vertical drop then horizontal to station.  A pinned crossing X
    # drops straight through the boundary; otherwise a per-line stagger fans out
    # from the shared port marker so the lines converge only there.
    if crossing_x is not None or abs(drop_delta) < COORD_TOLERANCE:
        pts = [
            (drop_x, sy),
            (drop_x, ty + tgt_off),
            (tx, ty + tgt_off),
        ]
        radii = [
            concentric_corner_radius_at(
                pts[0], pts[1], pts[2], drop_x - sx, ctx.curve_radius
            )
        ]
    else:
        pts = [
            (sx + src_off, sy),
            (drop_x, sy),
            (drop_x, ty + tgt_off),
            (tx, ty + tgt_off),
        ]
        radii = [
            reference_anchored_radius(0.0, ctx.curve_radius),
            concentric_corner_radius_at(
                pts[1], pts[2], pts[3], drop_x - sx, ctx.curve_radius
            ),
        ]
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=pts,
        offsets_applied=True,
        curve_radii=radii,
    )


def _perp_corridor_feeder(
    edge: Edge, entry_st: Station, ctx: _RoutingCtx
) -> Port | None:
    """Return the perpendicular exit port feeding this entry over the corridor.

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
    for feed in ctx.graph.edges_to(edge.source):
        port = ctx.graph.ports.get(feed.source)
        feeder_st = ctx.graph.stations.get(feed.source)
        if (
            port is not None
            and not port.is_entry
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and feeder_st is not None
            and abs(feeder_st.y - entry_st.y) <= COORD_TOLERANCE
        ):
            return port
    return None


def _route_perp_entry_from_corridor(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    feeder_side: PortSide,
) -> RoutedPath:
    """Drop a corridor-fed perpendicular entry into its target station.

    The up-and-over corridor lands each line at ``port_x - lateral`` (the
    reversed per-line convention shared with the aligned-entry branch).
    The drop leaves at that same per-line X so the bundle stays ordered
    across the entry port, then turns into the station at the target row's
    per-line Y, mirroring how the corridor stacks the bundle on the way in.

    The turn into the station is sized by :func:`concentric_corner_radius`
    from the two travel vectors, so the bundle's arcs share a centre (a
    constant gap through the bend) for either drop direction.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    lateral = _perp_riser_lateral(
        ctx, edge.source, edge.line_id, feeder_side, tgt.section_id
    )
    drop_x = sx - lateral
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)
    corner_y = ty + tgt_off
    turn_in = (0.0, 1.0 if corner_y >= sy else -1.0)
    turn_out = (1.0 if tx >= drop_x else -1.0, 0.0)
    radius = concentric_corner_radius(
        turn_in, turn_out, -lateral, ctx.curve_radius, min_radius=COORD_TOLERANCE
    )
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(drop_x, sy), (drop_x, corner_y), (tx, corner_y)],
        offsets_applied=True,
        curve_radii=[radius],
    )


def _perp_entry_drop_delta(edge: Edge, dx: float, ctx: _RoutingCtx) -> float:
    """X stagger for this line on a multi-line perpendicular-port drop.

    Lines sharing a TOP/BOTTOM port carry per-line offset zero at the port,
    so without a stagger the parallel drops to their target collapse onto
    one X.  Order the channels by each line's target-side Y offset and pick
    the sign from the drop->turn handedness so the corner preserves bundle
    order (the lower line ends inside a westbound turn, outside an eastbound
    one).
    """
    siblings = sorted(
        {
            e.line_id
            for e in ctx.graph.edges
            if e.source == edge.source and e.target == edge.target
        },
        key=lambda lid: (_get_offset(ctx, edge.target, lid), lid),
    )
    n = len(siblings)
    if n < 2:
        return 0.0
    # When the port-side bundle offsets already fan the siblings into distinct
    # slots, the drop inherits that separation and an extra stagger only
    # doubles it (a tight bundle splays apart on the way in).  Stagger only
    # when the lines would otherwise collapse onto one drop X - i.e. their
    # source-port offsets are effectively uniform.
    src_offs = [_get_offset(ctx, edge.source, lid) for lid in siblings]
    if max(src_offs) - min(src_offs) >= (n - 1) * ctx.offset_step - COORD_TOLERANCE:
        return 0.0
    i = siblings.index(edge.line_id)
    # Order channels by target-side Y offset so the drop->turn corner keeps
    # bundle order: a larger tgt_off (lower) arrives on the south side of the
    # turn, which maps to the larger X on a westbound (D->L) descent and the
    # smaller X on an eastbound (D->R) one.
    centred = i - (n - 1) / 2
    sign = 1.0 if dx < 0 else -1.0
    return sign * centred * ctx.offset_step
