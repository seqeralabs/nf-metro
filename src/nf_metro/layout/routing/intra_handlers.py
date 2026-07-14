"""Intra-section routing: entry runway, shape dispatch, and in-section diagonals.

``_route_intra_section`` is an ordered dispatch over the distinct shapes an
edge inside (or leaving) a section can take -- a fold gutter, a perpendicular
exit, two straight cases -- with the horizontal-diagonal-horizontal run as the
fall-through.  ``_route_entry_runway`` is a sibling shape kept as its own
first-match handler in ``core.py`` because it claims an edge before the
section's internal handlers run.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CROSS_ROW_THRESHOLD,
    CURVE_RADIUS,
    FOLD_MARGIN,
    ICON_TERMINUS_FORK_LEAD,
    LABEL_BBOX_MARGIN,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.geometry import (
    diagonal_centreline,
    segment_intersects_bbox,
    single_corner_centreline,
)
from nf_metro.layout.labels import (
    label_text_width,
)
from nf_metro.layout.routing.bundle import (
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
    _RoutingCtx,
    _tb_x_offset,
)
from nf_metro.layout.routing.perp import (
    _perp_riser_lateral,
)
from nf_metro.layout.routing.tb_handlers import (
    _compute_diagonal_placement,
)
from nf_metro.parser.model import (
    Edge,
    PortSide,
    Section,
    Station,
    is_bypass_v,
)


def _route_entry_runway(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route flow-side entry port -> deep internal station with a runway.

    When a line enters a section but its first internal station is deeper
    than the earliest layer, a plain diagonal would hide the fact that
    the line bypasses the early-layer stations.  Instead, compress the
    diagonal into the entry region and extend a horizontal runway past
    the bypassed stations to the target.
    """
    graph = ctx.graph
    port = graph.ports.get(edge.source)
    if not port or not port.is_entry:
        return None

    section = graph.sections.get(tgt.section_id or "")
    if not section:
        return None

    # Only handle flow-side entries (LEFT for LR, RIGHT for RL).
    # TB/BT and perpendicular entries are handled by earlier handlers.
    if section.direction == "LR" and port.side != PortSide.LEFT:
        return None
    if section.direction == "RL" and port.side != PortSide.RIGHT:
        return None
    if section.direction not in ("LR", "RL"):
        return None

    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y

    # Same Y means target is on the trunk track: a plain straight run suffices,
    # unless a same-row non-consumer sits between the port and the target, which
    # the straight run would rake.  Bow over such a blocker instead.
    if abs(sy - ty) < COORD_TOLERANCE_FINE:
        return _route_entry_bow(edge, src, tgt, ctx, section)

    # Gather the internal stations bypassed between the entry port and the
    # target.  The runway runs flat along one of the two rows (source Y or
    # target Y) and diagonals across to the other, so a bypassed station this
    # line does not carry sitting on the flat-run row would be raked through
    # its marker.  Record which rows carry such a non-consumer.  Flow direction
    # (LR/RL) only sets which end of the x-span is the source, so ordering is
    # kept in terms of distance from the source rather than a direction literal.
    line = edge.line_id
    port_ids = section.port_ids
    lo, hi = min(sx, tx), max(sx, tx)
    between_xs: list[float] = []
    ty_blocker_xs: list[float] = []
    sy_blocked = False
    for sid in section.station_ids:
        if sid == edge.target or sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if not st or st.is_port or not (lo < st.x < hi):
            continue
        between_xs.append(st.x)
        if line not in graph.station_lines(sid):
            if abs(st.y - ty) < COORD_TOLERANCE_FINE:
                ty_blocker_xs.append(st.x)
            elif abs(st.y - sy) < COORD_TOLERANCE_FINE:
                sy_blocked = True

    if not between_xs:
        return None  # No intervening stations -- normal routing is fine.

    if not ty_blocker_xs:
        # Target row is clear: compress the diagonal in the entry region and
        # run the flat runway along the target Y past the bypassed stations.
        nearest_src = min(between_xs, key=lambda x: abs(x - sx))
        src_min = ctx.curve_radius + MIN_STRAIGHT_PORT
        if abs(nearest_src - sx) < src_min + ctx.diagonal_run:
            return None  # Too tight -- fall through to default handler.
        diag_start_x, diag_end_x = _compute_diagonal_placement(
            sx,
            nearest_src,
            ctx.diagonal_run,
            src_min,
            0.0,
            is_fork=edge.source in ctx.fork_stations,
            is_join=False,
        )
    elif sy_blocked:
        return None  # Both rows carry a non-consumer -- no safe runway.
    else:
        # Target row carries a non-consumer but the source (entry) row is
        # clear: keep the flat run on the source Y and drop into the target
        # only after the last bypassed station, so the descent clears its
        # marker.
        tgt_min = ctx.curve_radius + MIN_STRAIGHT_PORT
        diag_start_x, diag_end_x = _compute_diagonal_placement(
            sx, tx, ctx.diagonal_run, 0.0, tgt_min, is_fork=False, is_join=True
        )
        last_blocker = max(ty_blocker_xs, key=lambda x: abs(x - sx))
        if abs(diag_start_x - sx) <= abs(last_blocker - sx):
            return None  # Descent would clip a blocker -- let the guard warn.

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=diagonal_centreline(
            section.direction, (sx, sy), (tx, ty), diag_start_x, diag_end_x
        ),
    )


def _marker_bbox(st: Station) -> tuple[float, float, float, float]:
    """Conservative axis-aligned marker footprint centred on the station."""
    r = STATION_RADIUS_APPROX
    return (st.x - r, st.y - r, st.x + r, st.y + r)


def _bow_clears_markers(
    points: list[tuple[float, float]],
    section_stations: list[Station],
) -> bool:
    """Whether every bow segment stays clear of each station marker.

    Verifies the arced-over blockers (which the apex must pass outside of) and
    every other real station in the bow's x-span, so the detour trades one
    crossing for none.
    """
    footprints = [_marker_bbox(st) for st in section_stations]
    for k in range(len(points) - 1):
        x1, y1 = points[k]
        x2, y2 = points[k + 1]
        for bbox in footprints:
            if segment_intersects_bbox(x1, y1, x2, y2, bbox):
                return False
    return True


def _route_entry_bow(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx, section: Section
) -> RoutedPath | None:
    """Bow a trunk-row entry run over a same-row non-consumer station.

    The entry port lands on the target's trunk track, so a straight run would
    pass through any non-carrying station seated on that row between them.  When
    one or more such blockers exist, arc the line off the trunk (up or down,
    whichever side stays clear), run flat past the blockers, and drop back onto
    the trunk before the target.  Returns ``None`` when there is no blocker or
    no side leaves a verifiable clear detour, so routing falls back to the plain
    straight run and the crossing guard warns if it truly cannot be avoided.
    """
    graph = ctx.graph
    line = edge.line_id
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    lo, hi = min(sx, tx), max(sx, tx)

    port_ids = section.port_ids
    section_stations: list[Station] = []
    blockers: list[Station] = []
    for sid in section.station_ids:
        if sid == edge.target or sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if not st or st.is_port:
            continue
        section_stations.append(st)
        if (
            lo < st.x < hi
            and abs(st.y - sy) < COORD_TOLERANCE_FINE
            and line not in graph.station_lines(sid)
        ):
            blockers.append(st)
    if not blockers:
        return None

    clearance = STATION_RADIUS_APPROX + CURVE_RADIUS
    over_start = min(st.x for st in blockers) - clearance
    over_end = max(st.x for st in blockers) + clearance
    height = STATION_RADIUS_APPROX + CURVE_RADIUS
    min_straight = ctx.curve_radius + MIN_STRAIGHT_PORT

    lead_end = over_start - height
    tail_start = over_end + height
    if lead_end - sx < min_straight or tx - tail_start < min_straight:
        return None
    if over_end - over_start < 2 * ctx.curve_radius:
        return None

    for sign in _bow_side_order(section_stations, blockers, sy, over_start, over_end):
        apex = sy + sign * height
        points = [
            (sx, sy),
            (lead_end, sy),
            (over_start, apex),
            (over_end, apex),
            (tail_start, sy),
            (tx, ty),
        ]
        if _bow_clears_markers(points, section_stations):
            return RoutedPath(edge=edge, line_id=edge.line_id, points=points)
    return None


def _bow_side_order(
    section_stations: list[Station],
    blockers: list[Station],
    trunk_y: float,
    over_start: float,
    over_end: float,
) -> list[int]:
    """Order the bow's two candidate sides, roomier one first.

    Returns bow-direction signs (``-1`` up, ``+1`` down) sorted by how far the
    nearest off-trunk marker in the arc's x-span sits from the trunk, so the
    detour prefers the side with more vertical headroom.
    """
    blocker_ids = {st.id for st in blockers}
    up_gap = down_gap = float("inf")
    for st in section_stations:
        if st.id in blocker_ids:
            continue
        if not (
            over_start - STATION_RADIUS_APPROX < st.x < over_end + STATION_RADIUS_APPROX
        ):
            continue
        dy = st.y - trunk_y
        if dy < -COORD_TOLERANCE_FINE:
            up_gap = min(up_gap, -dy)
        elif dy > COORD_TOLERANCE_FINE:
            down_gap = min(down_gap, dy)
    return [-1, 1] if up_gap >= down_gap else [1, -1]


def _route_fold_edge(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Cross-row fold edge: out to the fold gutter, down the row gap, back in.

    A backward, cross-row edge between two sections (the target sits on a lower
    row to the left) routes out past the right edge of the fold column, drops in
    the gutter, and runs back in -- rather than cutting diagonally across the
    intervening rows.  Intra-section RL edges are excluded (they share a section
    and run within it).
    """
    same_section = bool(src.section_id and src.section_id == tgt.section_id)
    dy = tgt.y - src.y
    if not (tgt.x - src.x <= 0 and abs(dy) > CROSS_ROW_THRESHOLD and not same_section):
        return None
    fold_right = ctx.fold_x + FOLD_MARGIN
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (src.x, src.y),
            (fold_right, src.y),
            (fold_right, tgt.y),
            (tgt.x, tgt.y),
        ],
    )


def _route_perp_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Internal station -> perpendicular (TOP/BOTTOM) exit port on an LR/RL section.

    The line runs along the trunk to the exit X, then turns once and leaves
    vertically.  The corner sits past the trailing station so the line bends
    after the marker rather than through it.  A single line is a plain 3-point
    L; co-travelling lines fan the bend through the bundle builder.
    """
    tgt_port = ctx.graph.ports.get(tgt.id)
    src_section = ctx.graph.sections.get(src.section_id) if src.section_id else None
    if not (
        not src.is_port
        and tgt_port is not None
        and not tgt_port.is_entry
        and tgt_port.side in (PortSide.TOP, PortSide.BOTTOM)
        and src_section is not None
        and src_section.direction in ("LR", "RL")
    ):
        return None
    sibling_count = sum(
        1 for e in ctx.graph.edges_from(edge.source) if e.target == edge.target
    )
    if sibling_count <= 1:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=single_corner_centreline(
                src_section.direction, (src.x, src.y), (tgt.x, tgt.y), flow_first=True
            ),
        )
    return _route_perp_exit_bundle(
        edge, src, tgt, tgt_port.side, ctx, src_section.direction
    )


def _aligned_tb_drop_target(
    ctx: _RoutingCtx, exit_port_id: str, exit_x: float
) -> tuple[str, str] | None:
    """The column-aligned vertical-flow entry this perp exit drops straight into.

    Returns ``(entry_port_id, section_id)`` when *exit_port_id* feeds a
    TOP/BOTTOM entry of a vertical-flow section sharing its X, or ``None``.  The
    descent then continues that section's lane through the port instead of the
    reflected riser the cross-column up-and-over uses.
    """
    for e in ctx.graph.edges_from(exit_port_id):
        entry_port = ctx.graph.ports.get(e.target)
        entry_st = ctx.graph.station_for_edge_target(e)
        if (
            entry_port is not None
            and entry_port.is_entry
            and entry_port.side in (PortSide.TOP, PortSide.BOTTOM)
            and entry_port.section_id in ctx.tb_sections
            and abs(entry_st.x - exit_x) <= COORD_TOLERANCE
        ):
            return e.target, entry_port.section_id
    return None


def _route_perp_exit_bundle(
    edge: Edge,
    src: Station,
    tgt: Station,
    side: PortSide,
    ctx: _RoutingCtx,
    direction: str,
) -> RoutedPath | None:
    """Fan a co-travelling perpendicular-exit bundle along one turning centreline.

    The centreline runs the trunk to the exit X, turns once, and leaves
    vertically::

        (sx, sy) -> (tx, sy) -> (tx, ty)

    Each line is a perpendicular offset of it: the trunk run carries the line's
    source-side render Y and the vertical leg its exit-trunk X (via
    :func:`~nf_metro.layout.routing.perp._perp_riser_lateral`, the shared TOP/BOTTOM
    convention).  :func:`build_tapered_bundle` anchors the bend on the bundle's
    innermost-of-turn line, so the per-line gap stays constant through the corner
    and no inside-of-turn arc pinches below the floor radius.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    is_top = side == PortSide.TOP
    _member_edges, line_ids, _edge_by_line = gather_member_edges(ctx.graph, edge)

    # The trunk run turns +/-x to the exit X; on an RL section the builder's
    # right-hand normal flips the run's per-line render Y, so the source offset
    # carries that leg's travel sign.
    hsign = 1.0 if tx >= sx else -1.0

    # A column-aligned drop into a vertical-flow section continues that section's
    # rotation lane straight through the port, so the leave seats on the target's
    # lane rather than the perp riser's reflection (which the up-and-over needs).
    tb_drop = _aligned_tb_drop_target(ctx, edge.target, tx)

    def source_offset(line_id: str) -> float:
        return _get_offset(ctx, edge.source, line_id) * hsign

    def exit_offset(line_id: str) -> float:
        # The vertical leave seats each line on the exit trunk's per-line X; the
        # right-hand normal reverses a BOTTOM (descending) leg, so the lateral is
        # negated there to cancel it back.
        if tb_drop is not None:
            entry_id, tb_sec = tb_drop
            d = _tb_x_offset(ctx, entry_id, line_id, tb_sec)
        else:
            d = _perp_riser_lateral(ctx, edge.target, line_id, side, src.section_id)
        return d if is_top else -d

    routes = build_tapered_bundle(
        [(edge, edge.line_id, source_offset(edge.line_id), exit_offset(edge.line_id))],
        single_corner_centreline(direction, (sx, sy), (tx, ty), flow_first=True),
        transition_leg=1,
        base_radius=ctx.curve_radius,
        bundle_offsets=[(source_offset(lid), exit_offset(lid)) for lid in line_ids],
        is_inter_section=False,
        normalize_exempt=False,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def _route_same_track_straight(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Endpoints on one track (shared Y): a straight segment."""
    if abs(src.y - tgt.y) >= COORD_TOLERANCE_FINE:
        return None
    return RoutedPath(
        edge=edge, line_id=edge.line_id, points=[(src.x, src.y), (tgt.x, tgt.y)]
    )


def _route_near_zero_gap_straight(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Endpoints all but stacked (near-zero X gap): a straight segment."""
    if abs(tgt.x - src.x) >= COORD_TOLERANCE:
        return None
    return RoutedPath(
        edge=edge, line_id=edge.line_id, points=[(src.x, src.y), (tgt.x, tgt.y)]
    )


# Intra-section shape dispatch.  The first shape that claims the edge owns the
# route; order is significant (earlier shapes shadow later ones).  The
# horizontal-diagonal-horizontal run in ``_route_diagonal`` is the fall-through
# when no shape claims the edge.
_INTRA_SECTION_SHAPES = (
    _route_fold_edge,
    _route_perp_exit,
    _route_same_track_straight,
    _route_near_zero_gap_straight,
)


def _route_intra_section(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route an intra-section edge via the first shape that claims it.

    Each shape in :data:`_INTRA_SECTION_SHAPES` returns a :class:`RoutedPath`
    when it owns the edge or ``None`` to pass; :func:`_route_diagonal` is the
    fall-through.
    """
    for shape in _INTRA_SECTION_SHAPES:
        result = shape(edge, src, tgt, ctx)
        if result is not None:
            return result
    return _route_diagonal(edge, src, tgt, ctx)


def _is_side_branch_ascent(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> bool:
    """Return True for a side-branch edge climbing to the section trunk.

    A side-branch ascent edge starts at a non-port internal station
    sitting off the section's trunk Y, with target either an exit port
    of the same section or another internal station on the trunk Y.
    Visually we want the line to stay on the side-branch track until
    just before the target, so the bundle ordering inside the section
    stays meaningful (the side-branch line does not appear to merge
    with the main trunk bundle mid-section).

    Only fires for non-port sources with a single outgoing line set
    that exits the section via a shared exit port or feeds the trunk
    bundle's join station.
    """
    if src.is_port or src.section_id is None:
        return False
    sec_id = src.section_id
    trunk_y = ctx.section_trunk_y.get(sec_id)
    if trunk_y is None:
        return False
    trunk_tol = ctx.offset_step * 2
    if abs(src.y - trunk_y) < trunk_tol:
        return False  # source is on the trunk; not a side branch
    if abs(tgt.y - trunk_y) >= trunk_tol:
        return False  # target is not on the trunk bundle
    tgt_port = ctx.graph.ports.get(edge.target)
    same_sec = tgt.section_id == sec_id and not tgt.is_port
    is_exit_port = (
        tgt_port is not None and not tgt_port.is_entry and tgt_port.section_id == sec_id
    )
    if not (same_sec or is_exit_port):
        return False
    # Multi-line trunks may momentarily dip below trunk Y; only single-line
    # exits count as a side branch slot.
    if len(ctx.graph.station_lines(edge.source)) > 1:
        return False
    return True


def _route_diagonal(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath:
    """Route with horizontal runs and a 45-degree diagonal transition."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx

    # Minimum straight track at endpoints
    if src.is_port or tgt.is_port:
        min_straight = ctx.curve_radius + MIN_STRAIGHT_PORT
    else:
        min_straight = MIN_STRAIGHT_EDGE

    # Extend straight run past labels at fork/join stations, but only
    # when there is enough horizontal room.  If label clearance would
    # collapse the diagonal to a near-vertical line, fall back to the
    # base min_straight so a proper diagonal can still be drawn.
    src_min = min_straight
    tgt_min = min_straight
    if edge.source in ctx.fork_stations and src.label.strip():
        src_min = max(min_straight, label_text_width(src.label) / 2)
    if edge.target in ctx.join_stations and tgt.label.strip():
        tgt_min = max(min_straight, label_text_width(tgt.label) / 2)
    # File-input stations fan out to several downstream stations; the
    # diagonal should start past the icon so the line visually leaves
    # the file before forking.  Without this clamp the diagonal can
    # start inside the icon's drawn area (MIN_STRAIGHT_EDGE = 10 px,
    # icon extends to ~station.x + 20 + 14).
    if src.is_terminus and edge.source in ctx.fork_stations:
        src_min = max(src_min, ICON_TERMINUS_FORK_LEAD)
    if tgt.is_terminus and edge.target in ctx.join_stations:
        tgt_min = max(tgt_min, ICON_TERMINUS_FORK_LEAD)
    # A downward off-track output normally drops on the same side as its
    # producer's name label, so the descent turns down past the label's far edge
    # to stay clear of the text.  When producer and output are both fanned branch
    # stations (a symmetric fork's dead-end output, half-grid on the outer side),
    # the producer's label sits on the opposite (trunk) side instead, so the drop
    # runs clear without the clearance -- forcing it would collapse the diagonal
    # into a banned right angle.
    co_fanned = (
        edge.source in ctx.graph.half_grid_station_ids
        and edge.target in ctx.graph.half_grid_station_ids
    )
    drop_label_clearance = 0.0
    if (
        tgt.off_track
        and ty > sy + COORD_TOLERANCE_FINE
        and src.label.strip()
        and not co_fanned
    ):
        drop_label_clearance = label_text_width(src.label) / 2 + LABEL_BBOX_MARGIN
        src_min = max(src_min, drop_label_clearance)
    if src_min + tgt_min + ctx.diagonal_run > abs(dx):
        src_min = max(min_straight, drop_label_clearance)
        tgt_min = min_straight

    # Side-branch ascents: override the fork bias so the diagonal lands
    # near the target.  The side-branch line then stays on its own track
    # from the source until the section boundary, instead of merging
    # with the main trunk bundle mid-section.
    is_side_branch = _is_side_branch_ascent(edge, src, tgt, ctx)
    is_fork_flag = edge.source in ctx.fork_stations and not is_side_branch
    is_join_flag = edge.target in ctx.join_stations or is_side_branch

    # Bypass-V edges: bias the diagonal toward V on both halves with
    # equal V-side flat reservations so V sits at the centre of the
    # straight segment of the bypass loop.
    v_flat_half = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    diagonal_run = ctx.diagonal_run
    tgt_is_bypass_v = tgt.is_hidden and is_bypass_v(edge.target)
    src_is_bypass_v = src.is_hidden and is_bypass_v(edge.source)
    is_bypass_edge = tgt_is_bypass_v or src_is_bypass_v
    if tgt_is_bypass_v:
        is_fork_flag = False
        is_join_flag = True
        tgt_min = v_flat_half
        if src_min + tgt_min + diagonal_run > abs(dx):
            tgt_min = MIN_STRAIGHT_EDGE
    elif src_is_bypass_v:
        is_fork_flag = True
        is_join_flag = False
        src_min = v_flat_half
        if src_min + tgt_min + diagonal_run > abs(dx):
            src_min = MIN_STRAIGHT_EDGE

    # A multi-line bypass bundle stepping between two horizontal runs (leaving
    # the trunk row for a lower exit-port row around an in-line terminal) carries
    # a vertical per-line offset onto the diagonal, where adjacent lines separate
    # by only OFFSET_STEP * cos(theta); past 45 degrees that falls below the
    # channel floor and the strokes fuse.  Flatten toward 45 degrees (diagonal
    # run >= vertical drop) within the horizontal room the endpoint straights
    # leave, so the bundle keeps distinct slots.  Scoped to bypass V helpers:
    # they have no rendered marker, so widening the diagonal cannot rake a
    # station label the way a fan-out to a real target would.
    drop = abs(ty - sy)
    if is_bypass_edge and drop > diagonal_run:
        _, line_ids, _ = gather_member_edges(ctx.graph, edge)
        if len(line_ids) > 1:
            room = abs(dx) - src_min - tgt_min
            diagonal_run = max(diagonal_run, min(drop, room))

    diag_start_x, diag_end_x = _compute_diagonal_placement(
        sx,
        tx,
        diagonal_run,
        src_min,
        tgt_min,
        is_fork_flag,
        is_join_flag,
    )

    section = ctx.graph.sections.get(src.section_id or "")
    direction = section.direction if section else "LR"
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=diagonal_centreline(
            direction, (sx, sy), (tx, ty), diag_start_x, diag_end_x
        ),
    )
