"""Top-bottom section routing handlers: TB internal, TB L/R exit and
entry, perpendicular entry, and diagonal placement.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import NamedTuple

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    DEFAULT_LINE_WIDTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    TB_PILL_EDGE_OFFSET,
)
from nf_metro.layout.geometry import (
    AxisFrame,
    axis_split,
    diagonal_centreline,
    flow_entry_side,
    lanes_run_along_x,
    section_bbox_span,
    segment_intersects_bbox,
    single_corner_centreline,
    station_lane_coord,
)
from nf_metro.layout.labels import label_beside_pill, label_glyph_ink_bbox
from nf_metro.layout.routing.bundle import (
    build_tapered_bundle,
)
from nf_metro.layout.routing.centrelines import (
    gather_member_edges,
)
from nf_metro.layout.routing.co_row import CoRowFeeder, DiagonalWindow
from nf_metro.layout.routing.common import (
    OffsetRegime,
    RoutedPath,
    trailing_perp_side,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _RoutingCtx,
    _section_lane_frame,
    _tb_x_offset,
)
from nf_metro.layout.routing.perp import (
    _perp_entry_crossing_x,
    _perp_riser_lateral,
)
from nf_metro.layout.routing.perp_columns import perp_corridor_feeder
from nf_metro.parser.model import (
    Edge,
    PortSide,
    Section,
    Station,
)


def _tb_trunk_endpoints(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    tgt: Station,
    section: Section,
) -> tuple[float, float]:
    """Source and target X for an intra TB edge, holding continuations straight.

    A vertical-flow section stores its offsets in arrival order and draws the
    rotation ``x - offset`` (:func:`_tb_x_offset`), so a line keeps one lane the
    length of its trunk by construction: source and target read the same
    per-station rotation with no reflection or seam compensation.
    """
    src_x = src.x + _tb_x_offset(ctx, edge.source, edge.line_id, section.id)
    tgt_x = tgt.x + _tb_x_offset(ctx, edge.target, edge.line_id, section.id)
    return src_x, tgt_x


def _route_tb_internal(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route internal edges within TB sections as vertical drops."""
    section = _section_link_owner(edge, src, tgt, ctx)
    if section is None:
        return None
    return _section_link_route(edge, src, tgt, ctx, section)


def _section_link_owner(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> Section | None:
    """The vertical-flow section whose internal link *edge* is, or ``None``.

    An internal station feeding another internal station, or the section's
    trailing exit port, of one vertical-flow section.
    """
    graph = ctx.graph
    src_sec = src.section_id
    tgt_sec = tgt.section_id

    tgt_exit_port = graph.ports.get(edge.target)
    tgt_is_trailing_exit = (
        tgt_exit_port is not None
        and not tgt_exit_port.is_entry
        and src_sec is not None
        and tgt_exit_port.side == trailing_perp_side(graph.sections[src_sec].direction)
    )
    if not (
        src_sec
        and src_sec == tgt_sec
        and src_sec in ctx.tb_sections
        and not src.is_port
        and (not tgt.is_port or tgt_is_trailing_exit)
    ):
        return None
    return graph.sections[src_sec]


def _section_link_route(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx, section: Section
) -> RoutedPath:
    """Route an edge between two internal stations of vertical-flow *section*."""
    sx, tx, own = _section_link_diagonal(edge, src, tgt, section, ctx)

    # Different X tracks: route with vertical runs + 45-degree diagonal
    if own is not None:
        group = ctx.co_row_diagonal_groups.get((edge.source, edge.target, edge.line_id))
        window = group.window if group is not None else (own.start, own.end)
        return _route_tb_diagonal(
            edge, (sx, src.y), (tx, tgt.y), window, section.direction
        )

    # Same track: straight vertical drop
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, src.y), (tx, tgt.y)],
        offset_regime=OffsetRegime.BAKED,
    )


def _section_link_diagonal(
    edge: Edge, src: Station, tgt: Station, section: Section, ctx: _RoutingCtx
) -> tuple[float, float, DiagonalWindow | None]:
    """A vertical-flow link's lanes and, unless they share an X, its own window."""
    sx, tx = _tb_trunk_endpoints(ctx, edge, src, tgt, section)
    if abs(tx - sx) < COORD_TOLERANCE:
        return sx, tx, None
    # A feeder descending collinear with the merge's column but landing on an
    # outboard slot is displaced by a continuation that owns the trunk; bend
    # its diagonal at the source so it vacates the column immediately instead
    # of running down the trunk-owner's lane and overlapping it (issue #1012).
    bias_to_source = (
        edge.target in ctx.join_stations
        and abs(sx - tgt.x) < COORD_TOLERANCE
        and abs(tx - tgt.x) >= COORD_TOLERANCE
    )
    start, end = _compute_diagonal_placement(
        src.y,
        tgt.y,
        ctx.diagonal_run,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_EDGE,
        bias_to_source or edge.source in ctx.fork_stations,
        edge.target in ctx.join_stations and not bias_to_source,
    )
    return sx, tx, DiagonalWindow(start, end, MIN_STRAIGHT_EDGE, MIN_STRAIGHT_EDGE)


def section_link_co_row_feeder(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> CoRowFeeder | None:
    """*edge* as a co-row feeder, when :func:`_section_link_route` draws a diagonal."""
    section = _section_link_owner(edge, src, tgt, ctx)
    if section is None:
        return None
    sx, tx, own = _section_link_diagonal(edge, src, tgt, section, ctx)
    if own is None:
        return None
    return CoRowFeeder(
        key=(edge.source, edge.target, edge.line_id),
        source_run=src.y,
        window=own,
        source_lateral=sx,
        target_lateral=tx,
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

    Shared by ``_diagonal_window`` (horizontal run axis) and
    ``_section_link_diagonal`` (vertical run axis).  The caller maps the
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
    source: tuple[float, float],
    target: tuple[float, float],
    window: tuple[float, float],
    direction: str,
) -> RoutedPath:
    """Route TB edges with vertical runs and a 45-degree diagonal transition.

    The flow-axis sibling of ``_route_diagonal``: *window* is the diagonal's
    flow-axis span (Y for TB), from :func:`_section_link_diagonal` or a co-row
    group, laid out by :func:`~nf_metro.layout.geometry.diagonal_centreline`,
    which maps the flow-axis run back to vertical legs for a vertical-flow
    *direction*.
    """
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=diagonal_centreline(direction, source, target, *window),
        offset_regime=OffsetRegime.BAKED,
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
    vertical leg fanned by the station's in-section column X offset (via
    :func:`_tb_x_offset`, so the drop continues the column without crossing
    lines at the feeder station), a horizontal leg fanned by the port's own Y
    offset.  The exit-port Y order is the reverse of the column order, so the
    drop -> turn corner nests concentrically.
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
    td = _sign(tgt.y - src.y)
    hd = _sign(tgt.x - src.x)

    def vert_x_off(line_id: str) -> float:
        # Drop along the same interior column the trunk rides, so the
        # drop->turn corner nests concentrically against the exit-port Y fan.
        return _tb_x_offset(ctx, edge.source, line_id, src.section_id)

    def source_offset(line_id: str) -> float:
        return -td * vert_x_off(line_id)

    def target_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.target, line_id)

    return _route_single_corner(
        edge,
        ctx,
        single_corner_centreline(
            graph.sections[src.section_id].direction,
            (src.x, src.y),
            (tgt.x, tgt.y),
            flow_first=True,
        ),
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
    by the station's X offset (the rotation ``x - off``).
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
    hd = _sign(tgt.x - src.x)
    vd = _sign(tgt.y - src.y)

    def vert_x_off(line_id: str) -> float:
        return _tb_x_offset(ctx, edge.target, line_id, tgt.section_id)

    def source_offset(line_id: str) -> float:
        return hd * _get_offset(ctx, edge.source, line_id)

    def target_offset(line_id: str) -> float:
        return -vd * vert_x_off(line_id)

    return _route_single_corner(
        edge,
        ctx,
        single_corner_centreline(
            graph.sections[src.section_id].direction,
            (src.x, src.y),
            (tgt.x, tgt.y),
            flow_first=False,
        ),
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
    tgt = ctx.graph.station_for_edge_target(edge)
    tgt_sec = ctx.graph.sections.get(tgt.section_id) if tgt.section_id else None
    if tgt_sec is not None and tgt_sec.direction not in ("TB", "BT"):
        crossing_x = _perp_entry_crossing_x(ctx, edge.source, edge.line_id, src_x)
        if crossing_x is not None:
            return crossing_x
    src_off = _get_offset(ctx, edge.source, edge.line_id)
    drop_delta = _perp_entry_drop_delta(edge, dx, ctx)
    return src_x + src_off + drop_delta


def _route_perp_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route a TOP/BOTTOM entry port down to its internal target station.

    Two shapes: an aligned straight drop down the trunk X (when the line
    crosses the boundary with no lateral step), and an L-shape (drop then turn
    into the station) for every other perpendicular entry.
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

    corridor_feeder = perp_corridor_feeder(graph, edge.source)
    if corridor_feeder is not None:
        return _route_perp_entry_from_corridor(
            edge, src, tgt, ctx, corridor_feeder.side
        )

    drop_delta = _perp_entry_drop_delta(edge, dx, ctx)

    if abs(dx) < COORD_TOLERANCE and abs(drop_delta) < COORD_TOLERANCE:
        # Aligned perpendicular entry: each line drops straight at its in-section
        # trunk lane, so a multi-line bundle stays parallel into the trunk
        # instead of one line slanting across to a Y-staggered marker.
        x = tx + _tb_x_offset(ctx, edge.target, edge.line_id, tgt.section_id)
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(x, sy), (x, tgt.y)],
            offset_regime=OffsetRegime.BAKED,
        )

    tgt_sec = graph.sections.get(tgt.section_id or "")
    if (
        tgt_sec is not None
        and lanes_run_along_x(tgt_sec.direction)
        and src_port.side == flow_entry_side(tgt_sec.direction)
    ):
        route = _route_flow_side_lane_change(edge, src, tgt, ctx, tgt_sec)
        if route is not None:
            return route

    _members, line_ids, edge_by_line = gather_member_edges(graph, edge)
    return _route_perp_entry_l_shape(edge, src, tgt, ctx, dx, line_ids, edge_by_line)


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

    The drop is vertical-first because the entry port is TOP/BOTTOM, regardless
    of the target section's flow axis -- so this is a direct V-H build, not the
    flow-relative ``single_corner_centreline`` (which would invert the leg order
    for a horizontal-flow target).
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


def entry_runway_placement(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    section: Section,
) -> tuple[float, float] | None:
    """Flow-axis start and end of an entry runway's lane-change diagonal.

    The runway runs straight along one of two tracks (the port's or the
    target's) and crosses to the other on the diagonal, so a bypassed station
    this line does not carry sitting on the straight run's track would be raked
    through its marker.  ``None`` when there is nothing to bypass, no room for
    the diagonal before the first bypassed station, or both tracks carry a
    non-consumer.  Placement is kept in terms of distance from the source, so
    either flow sense along either axis reads the same.
    """
    graph = ctx.graph
    flow_axis, _lane_axis = AxisFrame.axes_for_direction(section.direction)
    sp, sq = axis_split(flow_axis, (src.x, src.y))
    tp, tq = axis_split(flow_axis, (tgt.x, tgt.y))
    lo, hi = min(sp, tp), max(sp, tp)
    between: list[float] = []
    target_track_blockers: list[float] = []
    source_track_blocked = False
    for sid in section.station_ids:
        if sid == edge.target or sid in section.port_ids:
            continue
        st = graph.stations.get(sid)
        if not st or st.is_port:
            continue
        p, q = axis_split(flow_axis, (st.x, st.y))
        if not lo < p < hi:
            continue
        between.append(p)
        if edge.line_id not in graph.station_lines(sid):
            if abs(q - tq) < COORD_TOLERANCE_FINE:
                target_track_blockers.append(p)
            elif abs(q - sq) < COORD_TOLERANCE_FINE:
                source_track_blocked = True

    if not between:
        return None

    if not target_track_blockers:
        # Target track is clear: compress the diagonal in the entry region and
        # run straight along the target track past the bypassed stations.
        nearest_src = min(between, key=lambda p: abs(p - sp))
        src_min = ctx.curve_radius + MIN_STRAIGHT_PORT
        if abs(nearest_src - sp) < src_min + ctx.diagonal_run:
            return None
        return _compute_diagonal_placement(
            sp,
            nearest_src,
            ctx.diagonal_run,
            src_min,
            0.0,
            is_fork=edge.source in ctx.fork_stations,
            is_join=False,
        )
    if source_track_blocked:
        return None
    # Target track carries a non-consumer but the source (entry) track is
    # clear: keep the straight run on the source track and cross into the
    # target only after the last bypassed station, so the crossing clears it.
    tgt_min = ctx.curve_radius + MIN_STRAIGHT_PORT
    diag_start, diag_end = _compute_diagonal_placement(
        sp, tp, ctx.diagonal_run, 0.0, tgt_min, is_fork=False, is_join=True
    )
    last_blocker = max(target_track_blockers, key=lambda p: abs(p - sp))
    if abs(diag_start - sp) <= abs(last_blocker - sp):
        return None
    return diag_start, diag_end


def _route_flow_side_lane_change(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx, section: Section
) -> RoutedPath | None:
    """Change lanes along the flow axis from a vertical section's flow-start port.

    The port on a vertical section's flow-start edge is not perpendicular to
    it, so a line entering there keeps travelling along the flow axis: it
    leaves the port on the lane its feeder lands on, crosses to the target's
    lane on a diagonal (inside the entry region when it bypasses stations,
    just inside the port otherwise), and reaches the station on that lane.

    The lines sharing the edge cross on parallel diagonals, which the diagonal
    spread pass separates by translating each along the flow axis.  ``None``
    when the run from the port to the station is too short for that spread,
    leaving the bundle to the perpendicular turn-in.  Also ``None`` when every
    lane change it could make would cross a line sharing the port twice
    (:func:`_crossed_twice`, :func:`_tangles_with_departure`).

    A single line whose own line also reaches the target through the chain it
    skips runs with that chain instead (:func:`_same_line_chain_route`).
    """
    flow_axis, _lane_axis = AxisFrame.axes_for_direction(section.direction)
    sp, sq = axis_split(flow_axis, (src.x, src.y))
    tp, tq = axis_split(flow_axis, (tgt.x, tgt.y))
    _members, line_ids, _edge_by_line = gather_member_edges(ctx.graph, edge)
    lateral = abs(tq - sq)
    if lateral < COORD_TOLERANCE:
        return None
    spread = _diagonal_spread(len(line_ids), ctx.offset_step, ctx.diagonal_run, lateral)
    end_straights = 2 * MIN_STRAIGHT_EDGE
    slack = abs(tp - sp) - ctx.diagonal_run - end_straights
    if slack < spread:
        return None

    placement = entry_runway_placement(
        edge, src, tgt, ctx, section
    ) or _compute_diagonal_placement(
        sp,
        tp,
        ctx.diagonal_run,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_EDGE,
        is_fork=True,
        is_join=False,
    )
    frame = _section_lane_frame(ctx.graph, section, ctx.positive_fan)
    src_lane, tgt_lane = (
        _lane_coord(ctx, frame, station, edge.line_id) for station in (src, tgt)
    )
    points = diagonal_centreline(
        section.direction, (src_lane, src.y), (tgt_lane, tgt.y), *placement
    )
    departures = _bundle_mate_departures(src, tgt, ctx, section, line_ids)
    plain_holds = not _crossed_twice(
        departures, abs(placement[0] - sp), src_lane, tgt_lane
    )
    if len(line_ids) == 1:
        chain = _same_line_chain_route(edge, src, tgt, ctx, section, src_lane)
        footprints = (
            _passed_station_footprints(src, tgt, ctx, section) if chain is None else []
        )
        if chain is not None:
            points = chain
        elif not (plain_holds and _clears(points, footprints)):
            cleared = _clear_lane_change(
                src, tgt, ctx, section, src_lane, tgt_lane, footprints, departures
            )
            if cleared is not None:
                points = cleared
            elif not plain_holds:
                return None
    elif not plain_holds:
        return None
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=points,
        offset_regime=OffsetRegime.BAKED,
    )


class _Departure(NamedTuple):
    """A line sharing the port that leaves its lane for another station's.

    Lanes are lane-axis coordinates; distances are flow-axis distances from
    the port.
    """

    port_lane: float
    leave_lane: float
    arrive_lane: float
    leave_at: float
    arrive_at: float


def _bundle_mate_departures(
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    section: Section,
    line_ids: list[str],
) -> list[_Departure]:
    """Lane changes of the port's other lines that leave short of *tgt*.

    Every line at port *src* outside *line_ids* that, from the port or from a
    station nearer the port than *tgt*, runs to a station of *section* on
    another lane.  The section's flow runs along ``y`` (lanes along ``x``).
    """
    graph = ctx.graph
    sp = src.y
    reach = abs(tgt.y - sp)
    mates = set(graph.station_lines(src.id)) - set(line_ids)
    frame = _section_lane_frame(graph, section, ctx.positive_fan)

    def lane(station: Station, line_id: str) -> float:
        return _lane_coord(ctx, frame, station, line_id)

    departures: list[_Departure] = []
    for sid in section.station_ids:
        leave = graph.stations[sid]
        if (leave.is_port and sid != src.id) or abs(leave.y - sp) >= reach:
            continue
        for out in graph.edges_from(sid):
            if out.line_id not in mates:
                continue
            arrive = graph.stations[out.target]
            if arrive.section_id != section.id:
                continue
            leave_lane = lane(leave, out.line_id)
            arrive_lane = lane(arrive, out.line_id)
            if abs(arrive_lane - leave_lane) < COORD_TOLERANCE:
                continue
            departures.append(
                _Departure(
                    lane(src, out.line_id),
                    leave_lane,
                    arrive_lane,
                    abs(leave.y - sp),
                    abs(arrive.y - sp),
                )
            )
    return departures


def _crossed_twice(
    departures: list[_Departure], start: float, from_lane: float, to_lane: float
) -> bool:
    """Whether a lane change off the port's lane meets one line twice.

    The change starts *start* from the port and steps from *from_lane* over
    the port lanes between it and *to_lane*.  A line it steps over that has not
    yet left, and later leaves the same way for a lane beyond *to_lane*,
    crosses it a second time.
    """
    sense = _sign(to_lane - from_lane)
    lo, hi = sorted((from_lane, to_lane))
    return any(
        lo < d.port_lane < hi
        and _sign(d.arrive_lane - d.leave_lane) == sense
        and (d.arrive_lane - to_lane) * sense > 0
        and start < d.arrive_at
        for d in departures
    )


def _tangles_with_departure(
    departures: list[_Departure],
    span: tuple[float, float],
    from_lane: float,
    to_lane: float,
) -> bool:
    """Whether a lane change over flow distances *span* overlaps a departure.

    Both diagonals cover overlapping lanes while both are under way, so with
    different slopes they cross and cross back.
    """
    lo, hi = sorted((from_lane, to_lane))
    return any(
        min(d.leave_lane, d.arrive_lane) < hi
        and max(d.leave_lane, d.arrive_lane) > lo
        and d.leave_at < span[1]
        and d.arrive_at > span[0]
        for d in departures
    )


def _same_line_chain_route(
    edge: Edge,
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    section: Section,
    src_lane: float,
) -> list[tuple[float, float]] | None:
    """A bypass drawn along the chain its own line also takes to *tgt*, or ``None``.

    When the bypass's line also reaches *tgt* through stations it skips, from
    one standing on the lane the line enters on, the line arrives at *tgt*
    twice.  The bypass then climbs that lane to the station and follows the
    chain's own routes into *tgt*, so the two read as one track rather than
    splitting at the port and rejoining at the target.  Stations passed on the
    climb must carry the line or keep clear of the lane.  The section's flow
    runs along ``y``.
    """
    graph = ctx.graph
    sp = src.y
    frame = _section_lane_frame(graph, section, ctx.positive_fan)

    def chain_into(current: Station) -> list[Edge] | None:
        for chain in graph.edges_to(current.id):
            if chain.line_id != edge.line_id or chain.source == edge.source:
                continue
            via = graph.stations[chain.source]
            if via.is_port or via.section_id != section.id:
                continue
            if not 0.0 < (via.y - sp) / (current.y - sp) < 1.0:
                continue
            via_lane = _lane_coord(ctx, frame, via, edge.line_id)
            if abs(via_lane - src_lane) < COORD_TOLERANCE:
                return [chain]
            upstream = chain_into(via)
            if upstream is not None:
                return [*upstream, chain]
        return None

    chain = chain_into(tgt)
    if chain is None:
        return None
    first = graph.stations[chain[0].source]
    passed = _passed_station_footprints(
        src, first, ctx, section, skip_line=edge.line_id
    )
    if not _clears([(src_lane, sp), (src_lane, first.y)], passed):
        return None
    points = [(src_lane, sp)]
    for link in chain:
        routed = _section_link_route(
            link, graph.stations[link.source], graph.stations[link.target], ctx, section
        )
        points.extend(routed.points[1:])
    return [
        p
        for k, p in enumerate(points)
        if k in (0, len(points) - 1)
        or not (
            abs(points[k - 1][0] - p[0]) < COORD_TOLERANCE_FINE
            and abs(points[k + 1][0] - p[0]) < COORD_TOLERANCE_FINE
        )
    ]


def _diagonal_spread(n: int, step: float, run: float, lateral: float) -> float:
    """Flow-axis room *n* parallel diagonals need to separate by *step*.

    Each diagonal covers *run* along the flow axis and *lateral* across it;
    translating the lines along the flow axis opens a *step* of perpendicular
    clearance between neighbours at the cost of this much extra run.
    """
    return (n - 1) * step * (math.hypot(run, lateral) - run) / lateral


_Box = tuple[float, float, float, float]


def _passed_station_footprints(
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    section: Section,
    *,
    skip_line: str | None = None,
) -> list[_Box]:
    """Boxes a flow-side entry into a vertical section must keep clear of.

    Each station of *section* strictly between the port and the target along
    the flow axis contributes its pill and its label; the target contributes
    its label only.  Crossing a passed station's pill reads as stopping there,
    even on the line's own lane.  Labels are seated after routing, so the box
    assumes the preferred, low-x side of the pill.  Stations carrying
    *skip_line* contribute nothing.  The section's flow runs along ``y`` (lanes
    along ``x``).
    """
    graph = ctx.graph
    frame = _section_lane_frame(graph, section, ctx.positive_fan)
    lo, hi = sorted((src.y, tgt.y))
    half_stroke = DEFAULT_LINE_WIDTH / 2
    boxes: list[_Box] = []
    for sid in section.station_ids:
        st = graph.stations[sid]
        if st.is_port or st.is_hidden:
            continue
        if skip_line is not None and skip_line in graph.station_lines(sid):
            continue
        passed = lo < st.y < hi
        if sid != tgt.id and not passed:
            continue
        lanes = _lane_coords(ctx, frame, st) or [st.x]
        pill_left = min(lanes) - TB_PILL_EDGE_OFFSET
        x0, y0, x1, y1 = label_glyph_ink_bbox(
            label_beside_pill(st, pill_left, left=True)
        )
        if passed:
            x1 = max(lanes) + TB_PILL_EDGE_OFFSET
            y0 = min(y0, st.y - TB_PILL_EDGE_OFFSET)
            y1 = max(y1, st.y + TB_PILL_EDGE_OFFSET)
        boxes.append(
            (x0 - half_stroke, y0 - half_stroke, x1 + half_stroke, y1 + half_stroke)
        )
    return boxes


def _lane_coord(
    ctx: _RoutingCtx, frame: AxisFrame, station: Station, line_id: str
) -> float:
    """Lane-axis coordinate of *line_id* at *station*, read in *frame*."""
    return station_lane_coord(frame, station, _get_offset(ctx, station.id, line_id))


def _lane_coords(ctx: _RoutingCtx, frame: AxisFrame, station: Station) -> list[float]:
    """Lane-axis coordinates of the lines *station* carries, read in *frame*."""
    return [
        _lane_coord(ctx, frame, station, lid)
        for lid in ctx.graph.station_lines(station.id)
    ]


def _clears(points: list[tuple[float, float]], boxes: list[_Box]) -> bool:
    """Whether no segment of the polyline *points* touches any of *boxes*."""
    return not any(
        segment_intersects_bbox(a[0], a[1], b[0], b[1], box)
        for a, b in zip(points, points[1:])
        for box in boxes
    )


def _clear_lane_change(
    src: Station,
    tgt: Station,
    ctx: _RoutingCtx,
    section: Section,
    src_lane: float,
    tgt_lane: float,
    footprints: list[_Box],
    departures: list[_Departure],
) -> list[tuple[float, float]] | None:
    """A flow-side entry that passes beside the stations it skips, or ``None``.

    Tried in order: stay on the port's lane past them and cross to the target's
    lane just before it; otherwise step out just inside the port to a lane
    beside the footprints and inside the section box, pass them there, and
    cross to the target's lane just before it.  Side lanes that cross another
    line's lane at the port come last, then nearer the target's lane first.
    A route is skipped where a line sharing the port would cross it twice:
    stepped over before it leaves the same way (:func:`_crossed_twice`), or
    met by the step back while that line is itself changing lanes
    (:func:`_tangles_with_departure`).

    The section's flow runs along ``y`` (lanes along ``x``): the route's
    flow-axis coordinates are the stations' ``y`` and its lanes are ``x``.
    """
    sp, tp = src.y, tgt.y
    end_min = ctx.curve_radius + MIN_STRAIGHT_PORT
    late = _compute_diagonal_placement(
        sp, tp, ctx.diagonal_run, 0.0, end_min, is_fork=False, is_join=True
    )
    route = diagonal_centreline(
        section.direction, (src_lane, sp), (tgt_lane, tp), *late
    )
    if not _crossed_twice(departures, abs(late[0] - sp), src_lane, tgt_lane) and (
        _clears(route, footprints)
    ):
        return route

    early = _compute_diagonal_placement(
        sp, tp, ctx.diagonal_run, end_min, 0.0, is_fork=True, is_join=False
    )
    room_for_two_turns = 2 * ctx.curve_radius
    if (late[0] - early[1]) * math.copysign(1.0, tp - sp) < room_for_two_turns:
        return None
    frame = _section_lane_frame(ctx.graph, section, ctx.positive_fan)
    port_lanes = _lane_coords(ctx, frame, src)

    def preference(lane: float) -> tuple[bool, float, float]:
        crosses_port_lane = any(
            min(lane, src_lane) < other < max(lane, src_lane) for other in port_lanes
        )
        return crosses_port_lane, abs(lane - tgt_lane), abs(lane - src_lane)

    candidates = _side_lane_candidates(footprints, ctx.offset_step, src_lane, early)
    _flow_axis, lane_axis = AxisFrame.axes_for_direction(section.direction)
    box_lo, box_hi = section_bbox_span(section, lane_axis)
    inside_margin = ctx.offset_step
    inside = (box_lo + inside_margin, box_hi - inside_margin)
    for lane in sorted(candidates, key=preference):
        if not inside[0] <= lane <= inside[1]:
            continue
        if min(abs(lane - tgt_lane), abs(lane - src_lane)) < COORD_TOLERANCE:
            continue
        if _crossed_twice(departures, abs(early[0] - sp), src_lane, lane):
            continue
        step_back = (abs(late[0] - sp), abs(late[1] - sp))
        if _tangles_with_departure(departures, step_back, lane, tgt_lane):
            continue
        route = [
            (src_lane, sp),
            (src_lane, early[0]),
            (lane, early[1]),
            (lane, late[0]),
            (tgt_lane, late[1]),
            (tgt_lane, tp),
        ]
        if _clears(route, footprints):
            return route
    return None


def _side_lane_candidates(
    footprints: list[_Box], gap: float, src_lane: float, early: tuple[float, float]
) -> list[float]:
    """Lanes *gap* beside each footprint, plus those whose first step clears it.

    *early* is the flow-axis span of the step off the port's lane.  A lane just
    beside a box can still have that step cut the box's corner, so each box
    corner within the step's span also yields the lane at which the step passes
    that corner *gap* clear.
    """
    lanes: list[float] = []
    for x0, y0, x1, y1 in footprints:
        for edge in (x0 - gap, x1 + gap):
            lanes.append(edge)
            for corner in (y0, y1):
                t = (corner - early[0]) / (early[1] - early[0])
                if 0.0 < t < 1.0:
                    lanes.append(src_lane + (edge - src_lane) / t)
    return lanes


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
        lateral = _perp_riser_lateral(ctx, edge.source, line_id, feeder_side)
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
