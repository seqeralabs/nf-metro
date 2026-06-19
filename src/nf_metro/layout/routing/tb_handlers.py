"""Top-bottom section routing handlers: TB internal, TB L/R exit and
entry, perpendicular entry, and diagonal placement.
"""

from __future__ import annotations

from collections.abc import Callable

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    MIN_STRAIGHT_EDGE,
)
from nf_metro.layout.routing.bundle import (
    build_offset_bundle,
    build_tapered_bundle,
)
from nf_metro.layout.routing.centrelines import (
    gather_member_edges,
)
from nf_metro.layout.routing.common import (
    RoutedPath,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _max_offset_at,
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.corners import (
    reversed_offset,
)
from nf_metro.layout.routing.perp import (
    _perp_entry_crossing_x,
    _perp_riser_lateral,
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


def _sign(value: float) -> float:
    """Travel direction along an axis: +1 for non-negative, -1 for negative."""
    return 1.0 if value >= 0 else -1.0


def _route_single_corner(
    edge: Edge,
    ctx: _RoutingCtx,
    centerline: list[tuple[float, float]],
    line_ids: list[str],
    source_offset: Callable[[str], float],
    target_offset: Callable[[str], float],
    *,
    min_radius: float | None = None,
) -> RoutedPath | None:
    """Fan a one-corner TB bundle along *centerline* and return *edge*'s route.

    The shape turns a single 90-degree corner: lines fan by ``source_offset`` on
    the first (approach/drop) leg and by ``target_offset`` on the leg into the
    port or station.  Routed through :func:`build_tapered_bundle` so the corner
    anchors on the bundle's innermost-of-turn line -- no caller-supplied radius
    sign, no arc below the floor.  Each member is routed alone with the full
    bundle declared as ``bundle_offsets``.
    """
    routes = build_tapered_bundle(
        [
            (
                edge,
                edge.line_id,
                source_offset(edge.line_id),
                target_offset(edge.line_id),
            )
        ],
        centerline,
        transition_leg=1,
        base_radius=ctx.curve_radius,
        min_radius=min_radius,
        bundle_offsets=[(source_offset(lid), target_offset(lid)) for lid in line_ids],
        is_inter_section=False,
        normalize_exempt=False,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def _route_tb_lr_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route internal station -> LEFT/RIGHT exit port in a TB section.

    The line drops from the station, turns once, and runs out to the port: a
    vertical leg fanned by the station's X offset (reversed for a LEFT exit, so
    the outermost line takes the widest arc), a horizontal leg fanned by the
    port's own Y offset.  The port offset (not the station's reversed offset)
    pins the horizontal Y so the inside and outside segments share the Y at
    which the port -> junction route departs.
    """
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

    _members, line_ids, _edge_by_line = gather_member_edges(graph, edge)
    exit_right = tgt_port.side == PortSide.RIGHT
    td = _sign(tgt.y - src.y)
    hd = _sign(tgt.x - src.x)
    max_src_off = _max_offset_at(ctx, edge.source)

    def vert_x_off(line_id: str) -> float:
        off = _get_offset(ctx, edge.source, line_id)
        return off if exit_right else reversed_offset(off, max_src_off)

    def source_offset(line_id: str) -> float:
        return -td * vert_x_off(line_id)

    def target_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.target, line_id)

    return _route_single_corner(
        edge,
        ctx,
        [(src.x, src.y), (src.x, tgt.y), (tgt.x, tgt.y)],
        line_ids,
        source_offset,
        target_offset,
    )


def _route_tb_lr_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route LEFT/RIGHT entry port -> internal station in a TB section.

    The mirror of :func:`_route_tb_lr_exit`: a horizontal leg out of the port
    fanned by the port's Y offset, then a vertical drop into the station fanned
    by the station's X offset (reversed for a LEFT entry).
    """
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

    _members, line_ids, _edge_by_line = gather_member_edges(graph, edge)
    entry_right = src_port.side == PortSide.RIGHT
    hd = _sign(tgt.x - src.x)
    vd = _sign(tgt.y - src.y)
    max_tgt_off = _max_offset_at(ctx, edge.target)

    def vert_x_off(line_id: str) -> float:
        off = _get_offset(ctx, edge.target, line_id)
        return off if entry_right else reversed_offset(off, max_tgt_off)

    def source_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.source, line_id)

    def target_offset(line_id: str) -> float:
        return -vd * vert_x_off(line_id)

    return _route_single_corner(
        edge,
        ctx,
        [(src.x, src.y), (tgt.x, src.y), (tgt.x, tgt.y)],
        line_ids,
        source_offset,
        target_offset,
    )


def _perp_drop_x(edge: Edge, src_x: float, dx: float, ctx: _RoutingCtx) -> float:
    """The X at which *edge*'s line drops through a TOP/BOTTOM entry port.

    A per-line stagger fans lines sharing the port onto parallel channels; an
    aligned inter-section feeder instead pins each line to the X at which its
    approach crosses the boundary, so it drops straight through.  Shared by the
    calling edge (to pick the route shape) and its bundle-mates (to anchor the
    corner), so every line in the bundle reads the same channel.
    """
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    drop_delta = _perp_entry_drop_delta(edge, dx, ctx)
    tgt = ctx.graph.stations.get(edge.target)
    tgt_sec = ctx.graph.sections.get(tgt.section_id) if tgt and tgt.section_id else None
    if (
        abs(drop_delta) > COORD_TOLERANCE
        and tgt_sec is not None
        and tgt_sec.direction not in ("TB", "BT")
    ):
        crossing_x = _perp_entry_crossing_x(ctx, edge.source, edge.line_id, src_x)
        if crossing_x is not None:
            return crossing_x
    return src_x + src_off + drop_delta


def _route_perp_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route a TOP/BOTTOM entry port down to its internal target station.

    Three shapes: an aligned straight drop down the trunk X (when the line
    crosses the boundary with no lateral step), an L-shape (drop then turn into
    the station) when an inter-section feeder pins the drop channel, and an
    H-V-H staircase (depart the shared port, jog onto a per-line channel, drop,
    turn in) when sibling lines must fan off one shared port marker.
    """
    graph = ctx.graph
    src_port = graph.ports.get(edge.source)
    if not (
        src_port
        and src_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and not tgt.is_port
    ):
        return None

    sx, sy = src.x, src.y
    tx = tgt.x
    dx = tx - sx

    corridor_feeder = _perp_corridor_feeder(edge, src, ctx)
    if corridor_feeder is not None:
        return _route_perp_entry_from_corridor(
            edge, src, tgt, ctx, corridor_feeder.side
        )

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    drop_delta = _perp_entry_drop_delta(edge, dx, ctx)
    drop_x = _perp_drop_x(edge, sx, dx, ctx)
    pinned_crossing = abs(drop_x - (sx + src_off + drop_delta)) > COORD_TOLERANCE

    if abs(dx) < COORD_TOLERANCE and abs(drop_delta) < COORD_TOLERANCE:
        # Aligned perpendicular entry: each line drops straight at its in-section
        # trunk X offset, so a multi-line bundle stays parallel into the trunk
        # instead of one line slanting across to a Y-staggered marker.
        x = tx + _tb_x_offset(ctx, edge.target, edge.line_id, tgt.section_id)
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(x, sy), (x, tgt.y)],
            offsets_applied=True,
        )

    _members, line_ids, edge_by_line = gather_member_edges(graph, edge)
    if pinned_crossing or abs(drop_delta) < COORD_TOLERANCE:
        return _route_perp_entry_l_shape(
            edge, src, tgt, ctx, dx, line_ids, edge_by_line
        )
    return _route_perp_entry_staircase(edge, src, tgt, ctx, dx, line_ids, edge_by_line)


def _route_perp_entry_l_shape(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    dx: float,
    line_ids: list[str],
    edge_by_line: dict[str, Edge],
) -> RoutedPath | None:
    """Drop down a pinned channel, then turn into the station (V-H).

    The centreline references the port X; each line fans by its drop channel on
    the vertical leg and by its target-station Y offset on the turn-in leg.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    td = _sign(ty - sy)
    hd = _sign(tx - sx)

    def source_offset(line_id: str) -> float:
        return -td * (_perp_drop_x(edge_by_line[line_id], sx, dx, ctx) - sx)

    def target_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.target, line_id)

    return _route_single_corner(
        edge,
        ctx,
        [(sx, sy), (sx, ty), (tx, ty)],
        line_ids,
        source_offset,
        target_offset,
    )


def _route_perp_entry_staircase(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    dx: float,
    line_ids: list[str],
    edge_by_line: dict[str, Edge],
) -> RoutedPath | None:
    """Fan off a shared port marker (H-V-H): jog, drop, turn into the station.

    Lines sharing the port depart it at one X, jog onto their per-line channel,
    drop, then turn into the station.  The first (port-jog) and second (drop)
    corners both round per line, so the route is fanned by explicit per-leg
    offsets through :func:`build_offset_bundle`, anchored on this line's own path
    (always non-degenerate, since a staggered line's jog is non-zero) with the
    bundle declared relative to it.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    port_x = sx + _get_offset(ctx, edge.source, edge.line_id)
    self_drop_x = _perp_drop_x(edge, sx, dx, ctx)
    self_corner_y = ty + _get_offset(ctx, edge.target, edge.line_id)
    vd = _sign(self_corner_y - sy)
    hd = _sign(tx - self_drop_x)

    def leg_offsets(line_id: str) -> list[float]:
        drop_x = _perp_drop_x(edge_by_line[line_id], sx, dx, ctx)
        corner_y = ty + _get_offset(ctx, edge.target, line_id)
        return [0.0, vd * (self_drop_x - drop_x), hd * (corner_y - self_corner_y)]

    centerline = [
        (port_x, sy),
        (self_drop_x, sy),
        (self_drop_x, self_corner_y),
        (tx, self_corner_y),
    ]
    routes = build_offset_bundle(
        [(edge, edge.line_id, leg_offsets(edge.line_id))],
        centerline,
        ctx.curve_radius,
        bundle_offsets=[leg_offsets(line_id) for line_id in line_ids],
        is_inter_section=False,
        normalize_exempt=False,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


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
) -> RoutedPath | None:
    """Drop a corridor-fed perpendicular entry into its target station.

    This is the entry end of the up-and-over shape whose exit end is
    ``inter_section_handlers._route_perp_exit_over``; both seat their bundle on
    the per-line lateral from ``perp._perp_riser_lateral`` (see that module for
    the TOP vs BOTTOM sign convention) so the two legs stay parallel across the
    shared port.

    The up-and-over corridor lands each line at ``port_x - lateral`` (the
    reversed per-line convention shared with the aligned-entry branch).
    The drop leaves at that same per-line X so the bundle stays ordered
    across the entry port, then turns into the station at the target row's
    per-line Y, mirroring how the corridor stacks the bundle on the way in.

    The turn into the station is fanned through :func:`build_tapered_bundle`, so
    the bundle's arcs nest concentrically (a constant gap through the bend) for
    either drop direction.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    _members, line_ids, _edge_by_line = gather_member_edges(ctx.graph, edge)
    td = _sign(ty - sy)
    hd = _sign(tx - sx)

    def source_offset(line_id: str) -> float:
        lateral = _perp_riser_lateral(
            ctx, edge.source, line_id, feeder_side, tgt.section_id
        )
        return td * lateral

    def target_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.target, line_id)

    return _route_single_corner(
        edge,
        ctx,
        [(sx, sy), (sx, ty), (tx, ty)],
        line_ids,
        source_offset,
        target_offset,
        min_radius=COORD_TOLERANCE,
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


# TB-section shape dispatch.  Each handler owns one shape of an edge touching a
# TB section, keyed by its endpoints' port sides; the first that claims the edge
# wins (order is significant -- ``_route_tb_internal`` shadows the port handlers
# for a same-section internal edge).  The combinatorial space:
#
#   * internal station -> internal station (or BOTTOM exit) ... _route_tb_internal
#   * internal station -> LEFT/RIGHT exit port ............... _route_tb_lr_exit
#   * LEFT/RIGHT entry port -> internal station ............. _route_tb_lr_entry
#   * TOP/BOTTOM port -> internal station ................... _route_perp_entry
#
# Each handler keeps its own applicability guard and returns ``None`` to pass,
# so the chain stays a first-match scan (mirrors ``_INTRA_SECTION_SHAPES``).
_TB_SECTION_SHAPES = (
    _route_tb_internal,
    _route_tb_lr_exit,
    _route_tb_lr_entry,
    _route_perp_entry,
)


def _route_tb_section(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route an edge touching a TB section via the first shape that claims it.

    Each shape in :data:`_TB_SECTION_SHAPES` returns a :class:`RoutedPath` when
    it owns the edge or ``None`` to pass; ``None`` here lets ``route_edges`` fall
    through to the entry-runway and general intra-section handlers.
    """
    for shape in _TB_SECTION_SHAPES:
        result = shape(edge, src, tgt, ctx)
        if result is not None:
            return result
    return None
