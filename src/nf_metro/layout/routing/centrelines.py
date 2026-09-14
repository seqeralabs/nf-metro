"""Centreline templates for inter-section routes.

A handler describes only the bundle's **centreline** -- the axis-aligned
polyline the bundle's centre follows -- plus the co-travelling lines.
:func:`~nf_metro.layout.routing.bundle.build_concentric_bundle` then fans every
line as a rigid parallel offset of that centreline with concentric corners.  No
handler assembles per-line ``points`` or ``curve_radii`` by hand, so a bundle
can neither flip (the lines keep a constant side-of-travel order) nor pinch
(every corner radius is derived from the turn geometry).

Each builder gathers the bundle for an edge with :func:`gather_bundle`, lays
out the centreline from the handler's named geometry, and returns the single
:class:`RoutedPath` for the calling edge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from nf_metro.layout.constants import COORD_TOLERANCE, MIN_STRAIGHT_EDGE
from nf_metro.layout.fan_geometry import symmetric_lane_offsets
from nf_metro.layout.geometry import (
    axis_point,
    axis_split,
)
from nf_metro.layout.routing.bundle import (
    build_concentric_bundle,
    build_offset_bundle,
    build_tapered_bundle,
)
from nf_metro.layout.routing.common import (
    Direction,
    OffsetRegime,
    RoutedPath,
    horizontal_direction,
    segment_direction,
)
from nf_metro.layout.routing.context import (
    _get_offset,
    _RoutingCtx,
    lane_is_clear_in_corridor,
)
from nf_metro.layout.routing.orientation import direction_axis
from nf_metro.parser.model import Edge, MetroGraph, Station

_Vec = tuple[float, float]
_Member = tuple[Edge, str, float]
_TaperedMember = tuple[Edge, str, float, float]


def route_lane_transition(
    edge: Edge,
    p_src: _Vec,
    p_tgt: _Vec,
    *,
    source_offset: float,
    target_offset: float,
    run_direction: Direction,
    source_runway: float,
    target_runway: float,
    diagonal_run: float,
    place_at_source: bool,
    is_inter_section: bool,
) -> RoutedPath:
    """Realise a planned lane hand-off along either cardinal flow axis."""
    primary_axis = direction_axis(run_direction).value
    source_primary, source_secondary = axis_split(primary_axis, p_src)
    target_primary, target_secondary = axis_split(primary_axis, p_tgt)
    source_point = axis_point(
        primary_axis, source_primary, source_secondary + source_offset
    )
    target_point = axis_point(
        primary_axis, target_primary, target_secondary + target_offset
    )
    sign = run_direction.sign
    lateral_delta = target_secondary + target_offset - source_secondary - source_offset
    available_run = (target_primary - source_primary) * sign
    if (
        source_runway <= 0
        or target_runway <= 0
        or diagonal_run <= 0
        or abs(abs(lateral_delta) - diagonal_run) > COORD_TOLERANCE
        or available_run
        < source_runway + diagonal_run + target_runway - COORD_TOLERANCE
    ):
        raise ValueError("lane-transition template inputs are inconsistent")
    if place_at_source:
        diagonal_start = source_primary + sign * source_runway
        diagonal_end = diagonal_start + sign * diagonal_run
    else:
        diagonal_end = target_primary - sign * target_runway
        diagonal_start = diagonal_end - sign * diagonal_run
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            source_point,
            axis_point(
                primary_axis,
                diagonal_start,
                source_secondary + source_offset,
            ),
            axis_point(
                primary_axis,
                diagonal_end,
                target_secondary + target_offset,
            ),
            target_point,
        ],
        is_inter_section=is_inter_section,
        offset_regime=OffsetRegime.BAKED,
        normalize_exempt=True,
    )


def gather_member_edges(
    graph: MetroGraph, edge: Edge
) -> tuple[list[Edge], list[str], dict[str, Edge]]:
    """The bundle of edges co-travelling ``edge.source -> edge.target``.

    Returns ``(member_edges, line_ids, edge_by_line)``: every edge sharing this
    edge's source and target, the distinct line ids in first-seen order, and a
    ``line_id -> edge`` map.  The membership query carries no offset convention,
    so callers that need a per-line offset other than :func:`_get_offset` (e.g.
    a perpendicular riser lateral or a TB trunk X) keep that logic at the call
    site and feed these line ids to it.
    """
    member_edges = [e for e in graph.edges_to(edge.target) if e.source == edge.source]
    line_ids = list(dict.fromkeys(e.line_id for e in member_edges))
    edge_by_line = {e.line_id: e for e in member_edges}
    return member_edges, line_ids, edge_by_line


def fan_offsets(n: int, step: float) -> list[float]:
    """The signed offsets of an *n*-line bundle centred on its own mean.

    Line ``j`` sits at ``(j - (n-1)/2) * step``, so the set is symmetric about
    zero with half-width ``(n-1)/2 * step``.  A handler that routes a bundle's
    lines one at a time passes this as ``bundle_offsets`` so the builder can
    anchor each corner on the innermost line without seeing the siblings.
    """
    return list(symmetric_lane_offsets(n, step))


def gather_bundle(ctx: _RoutingCtx, edge: Edge) -> tuple[list[_Member], float, float]:
    """Collect the bundle of lines co-travelling ``edge.source -> edge.target``.

    Returns ``(members, src_center, tgt_center)``.  ``members`` is one
    ``(edge, line_id, signed_offset)`` per line, with ``signed_offset`` the
    line's station-offset displacement from the bundle's source-side mean -- so
    the bundle is centred on its source mean and a single rigid offset
    reproduces each line's fan position on every leg.  ``src_center`` /
    ``tgt_center`` are the mean source / target offsets: the centreline's own
    displacement from the raw port coordinate on each side.

    The source-only view of :func:`gather_tapered_bundle`, for callers that fan
    one rigid offset on every leg.
    """
    members, src_center, tgt_center = gather_tapered_bundle(ctx, edge)
    return [(e, lid, src) for e, lid, src, _tgt in members], src_center, tgt_center


def gather_tapered_bundle(
    ctx: _RoutingCtx, edge: Edge
) -> tuple[list[_TaperedMember], float, float]:
    """Collect a co-travelling bundle as a *tapering* one.

    Like :func:`gather_bundle`, but each member is ``(edge, line_id,
    src_offset, tgt_offset)``: the line's displacement from the bundle's
    source-side mean *and* from its target-side mean.  When the two spreads
    differ the bundle tapers, and feeding these members to
    :func:`build_tapered_bundle` lands each line on its own offset at both ends
    rather than baking the source spread onto the target endpoints.  When the
    spreads match it is rigid and the result equals :func:`gather_bundle`'s.
    ``src_center`` / ``tgt_center`` are the mean source / target offsets to
    centre the centreline on each side.
    """
    _member_edges, line_ids, edge_by_line = gather_member_edges(ctx.graph, edge)

    src_offs = {lid: _get_offset(ctx, edge.source, lid) for lid in line_ids}
    tgt_offs = {lid: _get_offset(ctx, edge.target, lid) for lid in line_ids}
    src_center = sum(src_offs.values()) / len(src_offs)
    tgt_center = sum(tgt_offs.values()) / len(tgt_offs)
    members = [
        (edge_by_line[lid], lid, src_offs[lid] - src_center, tgt_offs[lid] - tgt_center)
        for lid in line_ids
    ]
    return members, src_center, tgt_center


def route_along(
    edge: Edge,
    members: list[_Member],
    centerline: list[_Vec],
    *,
    base_radius: float,
    min_radius: float | None = None,
    bundle_offsets: Sequence[float] | None = None,
    normalize_exempt: bool = True,
) -> RoutedPath | None:
    """Fan *members* along *centerline* and return the route for *edge*.

    The single seam between a handler's named geometry and
    :func:`build_concentric_bundle`: the handler builds the centreline, this
    fans the bundle and picks out the calling edge's line.  A handler that
    routes its siblings one at a time passes the full bundle's offsets as
    *bundle_offsets* so the lone member anchors against the whole spread.
    """
    routes = build_concentric_bundle(
        members,
        centerline,
        base_radius=base_radius,
        min_radius=min_radius,
        bundle_offsets=bundle_offsets,
        normalize_exempt=normalize_exempt,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def route_offset(
    edge: Edge,
    members: list[tuple[Edge, str, list[float]]],
    centerline: list[_Vec],
    *,
    base_radius: float,
    min_radius: float | None = None,
    bundle_offsets: Sequence[Sequence[float]] | None = None,
    normalize_exempt: bool = True,
) -> RoutedPath | None:
    """Fan per-leg-offset *members* along *centerline* and return *edge*'s line.

    The offset-bundle analogue of :func:`route_along`: the seam between a
    handler's centreline and :func:`build_offset_bundle` for a staircase that
    fans by a different amount on more than two legs.  A handler routing its
    siblings one at a time passes the full bundle's per-leg offsets as
    *bundle_offsets* so the lone member anchors against the whole spread.
    """
    routes = build_offset_bundle(
        members,
        centerline,
        base_radius,
        min_radius=min_radius,
        bundle_offsets=bundle_offsets,
        normalize_exempt=normalize_exempt,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def route_vhvh_offset(
    edge: Edge,
    members: Sequence[_TaperedMember],
    *,
    source: _Vec,
    launch_y: float,
    corridor_x: float,
    target: _Vec,
    source_offsets: Mapping[str, float],
    target_offsets: Mapping[str, float],
    line_order: Sequence[str],
    base_radius: float,
) -> RoutedPath | None:
    """Route a vertical-horizontal-vertical-horizontal offset bundle.

    This is the standard entry-wrap shape for a vertically-fed source whose
    targets use RIGHT ports.  The first leg continues each source lane to
    ``launch_y``; the middle vertical leg follows ``corridor_x``; the final leg
    lands each target lane.  Per-leg offsets let both endpoint fans retain
    their own ordering while :func:`route_offset` owns all corner radii.
    """
    sx, sy = source
    tx, ty = target
    centerline = [
        (sx, sy),
        (sx, launch_y),
        (corridor_x, launch_y),
        (corridor_x, ty),
        (tx, ty),
    ]

    def leg_offsets(line_id: str) -> list[float]:
        source_offset = source_offsets[line_id]
        target_offset = target_offsets.get(line_id, 0.0)
        return [source_offset, source_offset, source_offset, -target_offset]

    return route_offset(
        edge,
        [
            (member_edge, line_id, leg_offsets(line_id))
            for member_edge, line_id, _source_offset, _target_offset in members
        ],
        centerline,
        base_radius=base_radius,
        bundle_offsets=[leg_offsets(line_id) for line_id in line_order],
    )


def route_tapered(
    edge: Edge,
    members: list[_TaperedMember],
    centerline: list[_Vec],
    *,
    transition_leg: int,
    base_radius: float,
    min_radius: float | None = None,
    normalize_exempt: bool = False,
) -> RoutedPath | None:
    """Fan a bundle and return the route for *edge*, tapering when it must.

    Each member carries a source and target offset.  When every line's two
    offsets match the bundle is rigid: it is routed through :func:`route_along`
    and left un-exempt so the post-routing passes can bundle it with any
    gap-mates (channels into different targets that share one inter-column gap
    collapse into one concentric bundle there, not here) -- unless the caller
    passes *normalize_exempt* to opt a wrap loop out, whose outward-side port
    approach a normalize restack would misread as a backtrack.

    When the spreads differ the bundle tapers, and a single rigid offset cannot
    land each line on its true offset at both ends.  Then it is built with
    :func:`build_tapered_bundle` -- the offset switches at *transition_leg* --
    and always marked ``normalize_exempt``, since a normalize restack would
    re-size its transition corner as if it were wholesale and pinch the bundle.
    """
    if all(abs(src - tgt) <= COORD_TOLERANCE for _e, _lid, src, tgt in members):
        return route_along(
            edge,
            [(e, lid, src) for e, lid, src, _tgt in members],
            centerline,
            base_radius=base_radius,
            min_radius=min_radius,
            normalize_exempt=normalize_exempt,
        )
    routes = build_tapered_bundle(
        members,
        centerline,
        transition_leg,
        base_radius=base_radius,
        min_radius=min_radius,
        normalize_exempt=True,
    )
    return next((r for r in routes if r.line_id == edge.line_id), None)


def route_tapered_anchored(
    member: _TaperedMember,
    centerline: list[_Vec],
    *,
    transition_leg: int,
    base_radius: float,
    src_bundle_offsets: Sequence[float],
    tgt_bundle_offsets: Sequence[float],
    min_radius: float | None = None,
    normalize_exempt: bool = True,
) -> RoutedPath:
    """Route one tapering *member* anchored on two independent channel fans.

    :func:`route_tapered` derives the corner anchors from the members it routes,
    so the source-region and target-region corners share one fan.  A bundle
    whose two ends are *separately* fanned -- the source-region legs by one
    channel's line count and the target-region legs by another's, the two paired
    asymmetrically so neither channel's spread pulls the other's per-corner
    anchor -- cannot describe its anchors that way.  This routes a single member
    (``(edge, line_id, src_offset, tgt_offset)``) and assembles that paired
    ``bundle_offsets`` from the two fans: every source-channel offset paired with
    *member*'s own target offset, and *member*'s own source offset paired with
    every target-channel offset.  The builder then anchors the source-region
    corners on the source channel's innermost-of-turn line and the target-region
    corners on the target channel's, so a tapering loop whose two channels carry
    different line counts nests correctly at both ends.
    """
    _e, _lid, src_off, tgt_off = member
    bundle = [(s, tgt_off) for s in src_bundle_offsets] + [
        (src_off, t) for t in tgt_bundle_offsets
    ]
    routes = build_tapered_bundle(
        [member],
        centerline,
        transition_leg,
        base_radius=base_radius,
        min_radius=min_radius,
        bundle_offsets=bundle,
        normalize_exempt=normalize_exempt,
    )
    return routes[0]


def route_hvh_tapered(
    ctx: _RoutingCtx,
    edge: Edge,
    src: Station,
    tgt: Station,
    channel_x: float,
    *,
    base_radius: float,
    min_radius: float | None = None,
    fit_segment: bool = False,
) -> RoutedPath | None:
    """Route a horizontal -> vertical -> horizontal bundle through *channel_x*.

    The shared template for an inter-section L-shape: gather the co-travelling
    bundle, lay its centreline out of the source port, down the channel at
    *channel_x*, and into the target port, and taper each line to its own offset
    at both ends (the vertical leg is the transition).  With *fit_segment* the
    base radius shrinks to fit a vertical leg shorter than its two corners.
    Opposing horizontal legs form a half-turn, so the target screen-normal is
    opposite the path-normal used to carry bundle order through the corners.
    """
    members, src_center, tgt_center = gather_tapered_bundle(ctx, edge)
    sy_c = src.y + src_center
    ty_c = tgt.y + tgt_center
    if fit_segment:
        seg = abs(ty_c - sy_c)
        if seg > 0 and 2 * base_radius > seg:
            base_radius = seg / 2
    centerline = [
        (src.x, sy_c),
        (channel_x, sy_c),
        (channel_x, ty_c),
        (tgt.x, ty_c),
    ]
    if (channel_x - src.x) * (tgt.x - channel_x) < 0:
        members = [
            (member_edge, line_id, source_offset, -target_offset)
            for member_edge, line_id, source_offset, target_offset in members
        ]
    reversed_route = horizontal_direction(tgt.x - src.x) is Direction.L
    transition_leg = 1
    if reversed_route:
        centerline.reverse()
        members = [
            (member_edge, line_id, target_offset, source_offset)
            for member_edge, line_id, source_offset, target_offset in members
        ]
        transition_leg = 2
    route = route_tapered(
        edge,
        members,
        centerline,
        transition_leg=transition_leg,
        base_radius=base_radius,
        min_radius=min_radius,
    )
    if route is not None and reversed_route:
        route.points.reverse()
        if route.curve_radii is not None:
            route.curve_radii.reverse()
    return route


def _lane_change_step(
    edge: Edge,
    ctx: _RoutingCtx,
    p_src: _Vec,
    p_tgt: _Vec,
) -> RoutedPath | None:
    """One line's lane change across a straight connector, drawn as a step.

    Ends that hold the line on different lanes have to climb the difference
    somewhere.  Spread across the whole run it reads as neither a turn nor a
    level run, and carries a chevron pointing off-axis; drawn against the target
    port it is the 45-degree hand-off :func:`route_lane_transition` states.

    ``None`` leaves the caller to draw one slope: a run into a merge ends where
    the convergence planner re-seats it rather than at the coordinate routed
    here, so the target runway is not this route's to spend, and a diagonal or
    collapsed raw segment has no single direction to align the step with.
    """
    run_direction = segment_direction(p_src, p_tgt)
    source_offset = _get_offset(ctx, edge.source, edge.line_id)
    target_offset = _get_offset(ctx, edge.target, edge.line_id)
    diagonal_run = abs(p_tgt[1] + target_offset - p_src[1] - source_offset)
    run = abs(p_tgt[0] - p_src[0])
    if (
        diagonal_run <= COORD_TOLERANCE
        or run + COORD_TOLERANCE < 2 * MIN_STRAIGHT_EDGE + diagonal_run
        or edge.target not in ctx.graph.ports
        or run_direction is None
    ):
        return None
    return route_lane_transition(
        edge,
        p_src,
        p_tgt,
        source_offset=source_offset,
        target_offset=target_offset,
        run_direction=run_direction,
        source_runway=MIN_STRAIGHT_EDGE,
        target_runway=MIN_STRAIGHT_EDGE,
        diagonal_run=diagonal_run,
        place_at_source=_place_hand_off_at_source(
            ctx, edge, p_src, p_tgt, run_direction, source_offset, target_offset
        ),
        is_inter_section=True,
    )


def _place_hand_off_at_source(
    ctx: _RoutingCtx,
    edge: Edge,
    p_src: _Vec,
    p_tgt: _Vec,
    run_direction: Direction,
    source_offset: float,
    target_offset: float,
) -> bool:
    """Where along a straight connector's run its lane hand-off should sit.

    A hand-off drawn against the target port holds the source lane across the
    whole run, so it reads as a turn against the port -- the wanted default.  But
    that source lane is carried straight into the port's shared approach, and if
    another line already occupies that lane there the two run flush.  When that
    happens the line must reach its target lane at the source end instead,
    provided the target lane is itself clear across the run; flipping into an
    occupied target lane would only relocate the clash.

    With no offset regime every lane collapses to its station coordinate, so the
    two corridors resolve identically and this keeps the default placement.
    """
    offsets = ctx.station_offsets or {}
    primary_axis = direction_axis(run_direction).value
    _, source_secondary = axis_split(primary_axis, p_src)
    _, target_secondary = axis_split(primary_axis, p_tgt)
    source_lane_corridor = (
        (edge.source, source_offset),
        (edge.target, source_secondary + source_offset - target_secondary),
    )
    if lane_is_clear_in_corridor(
        ctx.graph, offsets, edge.line_id, source_lane_corridor
    ):
        return False
    target_lane_corridor = (
        (edge.source, target_secondary + target_offset - source_secondary),
        (edge.target, target_offset),
    )
    return lane_is_clear_in_corridor(
        ctx.graph, offsets, edge.line_id, target_lane_corridor
    )


def route_straight(
    edge: Edge,
    ctx: _RoutingCtx,
    p_src: _Vec,
    p_tgt: _Vec,
    *,
    base_radius: float,
    normalize_exempt: bool = False,
) -> RoutedPath | None:
    """Straight connector as a two-vertex centreline.

    The bundle fans perpendicular to the run.  A straight trunk segment must
    keep its bundle on the same screen side as the rest of the line, so the
    centreline is laid out in the canonical travel direction (left-to-right or
    top-to-bottom) and the emitted points reversed back to source-first if the
    edge runs the other way -- otherwise the perpendicular normal would flip the
    fan on a right-to-left or serpentine segment.  A bundle whose two ends sit at
    different ranks would need a diagonal centreline (which
    :func:`build_concentric_bundle` forbids), so each line takes the step
    :func:`_lane_change_step` draws, falling back to a direct segment whose
    per-line offsets the renderer applies where no step fits.
    """
    members, src_center, tgt_center = gather_bundle(ctx, edge)
    src_pt = (p_src[0], p_src[1] + src_center)
    tgt_pt = (p_tgt[0], p_tgt[1] + tgt_center)
    dx = tgt_pt[0] - src_pt[0]
    dy = tgt_pt[1] - src_pt[1]
    if abs(dx) > COORD_TOLERANCE and abs(dy) > COORD_TOLERANCE:
        return _lane_change_step(edge, ctx, p_src, p_tgt) or RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[p_src, p_tgt],
            is_inter_section=True,
            normalize_exempt=normalize_exempt,
        )

    reverse = dx < -COORD_TOLERANCE or dy < -COORD_TOLERANCE
    centerline = [tgt_pt, src_pt] if reverse else [src_pt, tgt_pt]
    route = route_along(
        edge,
        members,
        centerline,
        base_radius=base_radius,
        normalize_exempt=normalize_exempt,
    )
    if route is not None and reverse:
        route.points = list(reversed(route.points))
    return route
