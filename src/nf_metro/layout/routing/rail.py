"""Dedicated edge router for opt-in rail mode.

In rail mode each metro line runs along a single fixed horizontal rail Y
(see ``layout/rail_mode.py``).  An edge therefore connects two points that
both sit on *this line's* rail, so the route is a straight horizontal run at
that rail Y from the source X to the target X.  The station pills (drawn by
the renderer) bridge across rails; the rails themselves never converge.

A line that uses a station meets that station's pill at its own rail Y.  A
line that does not use a station simply passes straight along its rail; if
that rail falls between two rails the station spans, the rail visually
crosses the pill (acceptable for v1 - see the feature notes).
"""

from __future__ import annotations

__all__ = ["route_rail_edges"]

from collections import defaultdict

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_RUN,
    MIN_STRAIGHT_EDGE,
    OFFSET_STEP,
    RAIL_TERMINUS_FAN_LEAD,
    X_SPACING,
)
from nf_metro.layout.routing.bundle import build_offset_bundle, build_tapered_bundle
from nf_metro.layout.routing.common import OffsetRegime, RoutedPath
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Station


def _line_rail_y(graph: MetroGraph, station_id: str, line_id: str) -> float:
    """Return the Y at which *line_id* meets *station_id* in rail mode.

    A single-rail station sits at its ``y``.  A multi-rail (spanning) station
    stores its rail range on ``rail_top_y``/``rail_bottom_y``; the line meets
    the pill at the rail Y for that line, which is recoverable from the
    station's served lines and the even rail spacing.  When the rail span is
    unknown the station's own ``y`` is used (single-rail fallback).
    """
    st = graph.stations.get(station_id)
    if st is None:
        return 0.0

    # Ports carry an averaged Y; resolve to the line's own rail in the port's
    # section so inter-section legs stay on the correct rail per line.
    port = graph.ports.get(station_id)
    if port is not None:
        section_rails = graph._rail_y.get(port.section_id, {})
        if line_id in section_rails:
            return section_rails[line_id]

    if st.rail_top_y is None or st.rail_bottom_y is None:
        return st.y

    # Each served line's rail Y is recorded directly in rail_used_ys, parallel
    # to the station's served-line order, so look it up rather than assuming
    # the used rails evenly fill the span (a station may span a rail it does
    # not use, which would break an interpolated estimate).
    served = graph.station_lines_ordered(station_id)
    if line_id in served and len(st.rail_used_ys) == len(served):
        return st.rail_used_ys[served.index(line_id)]
    return st.y


def _off_track_drop_order(
    graph: MetroGraph,
    feeder: Station,
    on_rail: Station,
) -> list[str]:
    """Lines bundled through one off-track elbow, in left-to-right drop order.

    Several lines feeding (or fed by) the same consumer would otherwise drop on
    the same X and merge into one fat vertical leg.  Each drops on its own X, one
    OFFSET_STEP apart and centred on the feeder, so the bundle stays parallel.

    The order is chosen so the bundle nests through the elbow's single corner
    without twisting: the line on the inside of the turn takes the inside drop X.
    For the baseline corner -- consumer to the right of the feeder, feeder above
    the rails (drop down, turn right) -- the lowest rail (largest Y) drops
    leftmost.  A mirrored corner (consumer to the left, or feeder below the
    rails) flips one of the turn's two axes, so the drop order reverses; without
    that the bundle crosses itself through the bend.
    """
    sib_rails: list[tuple[float, str]] = []
    for e in graph.edges:
        if {e.source, e.target} != {feeder.id, on_rail.id}:
            continue
        sib_rails.append((_line_rail_y(graph, on_rail.id, e.line_id), e.line_id))
    sib_rails.sort(reverse=True)
    order = [lid for _y, lid in sib_rails]
    if sib_rails:
        consumer_left = on_rail.x < feeder.x
        feeder_below = feeder.y > sib_rails[0][0]
        if consumer_left != feeder_below:
            order.reverse()
    return order


def _drop_stagger(order: list[str], line_id: str) -> float:
    """Signed lateral offset of *line_id*'s drop from the feeder centre."""
    n = len(order)
    if n <= 1 or line_id not in order:
        return 0.0
    return (order.index(line_id) - (n - 1) / 2.0) * OFFSET_STEP


def _route_off_track_elbow(
    graph: MetroGraph,
    edge: Edge,
    feeder: Station,
    on_rail: Station,
    off_src: bool,
) -> RoutedPath:
    """Route one off-track feeder line as a drop -> turn-onto-rail elbow.

    Routed through :func:`build_tapered_bundle` with this line as the lone member
    and the full sibling fan declared as ``bundle_offsets``, so the corner anchors
    on the bundle's innermost-of-turn line and no arc falls below the floor.  The
    centreline's flat leg sits at this line's own rail Y, so its rail-leg offset
    is zero and the staggered drop is the only fan on the turning leg.
    """
    rail_y = _line_rail_y(graph, on_rail.id, edge.line_id)
    order = _off_track_drop_order(graph, feeder, on_rail)
    # The staggered drop displaces the vertical leg in X; the builder's normal
    # flips that X by the leg's travel direction, so pre-sign the stagger by the
    # direction the vertical leg runs in the centreline below.  Feeding in
    # (``off_src``) the leg runs feeder -> rail; feeding out it runs rail ->
    # feeder, so the sign inverts.
    drop_to_rail = 1.0 if rail_y >= feeder.y else -1.0
    vert_dir = drop_to_rail if off_src else -drop_to_rail

    def drop_off(line_id: str) -> float:
        return -vert_dir * _drop_stagger(order, line_id)

    siblings = [
        (drop_off(lid), _line_rail_y(graph, on_rail.id, lid) - rail_y) for lid in order
    ]
    drop = drop_off(edge.line_id)
    if off_src:
        centerline = [(feeder.x, feeder.y), (feeder.x, rail_y), (on_rail.x, rail_y)]
        member = (edge, edge.line_id, drop, 0.0)
        bundle = siblings
    else:
        centerline = [(on_rail.x, rail_y), (feeder.x, rail_y), (feeder.x, feeder.y)]
        member = (edge, edge.line_id, 0.0, drop)
        bundle = [(rail_off, sib_drop) for sib_drop, rail_off in siblings]

    routes = build_tapered_bundle(
        [member],
        centerline,
        transition_leg=1,
        base_radius=CURVE_RADIUS,
        bundle_offsets=bundle,
        is_inter_section=False,
        normalize_exempt=False,
    )
    return routes[0]


def _diagonal_placement(
    sx: float,
    tx: float,
    sy: float,
    ty: float,
    is_fork: bool,
    is_join: bool,
) -> tuple[float, float]:
    """X coordinates of a 45-degree diagonal joining two rails along an X run.

    The diagonal climbs/falls between rail Ys at 45 degrees, so its horizontal
    span equals the vertical rail separation ``|ty - sy|`` (clamped to the
    available run so it never inverts).  The flat lead-in/out keeps a minimum
    straight stub at each end; the diagonal is biased toward the fan's shared
    convergence point (the fork source or the join target) so a rail eases off
    the fan early and runs flat the rest of the column.
    """
    from nf_metro.layout.routing.core import _compute_diagonal_placement

    # The diagonal's horizontal span equals the vertical rail separation (a true
    # 45 degrees), clamped to DIAGONAL_RUN.  A station that both forks and joins
    # has no single convergence point to bias toward, so it centres.
    diag = min(abs(ty - sy), DIAGONAL_RUN)
    return _compute_diagonal_placement(
        sx,
        tx,
        diagonal_run=diag,
        src_min_straight=MIN_STRAIGHT_EDGE,
        tgt_min_straight=MIN_STRAIGHT_EDGE,
        is_fork=is_fork and not is_join,
        is_join=is_join and not is_fork,
    )


def _route_inter_section_connector(
    graph: MetroGraph,
    group: list[Edge],
    exit_port: Port,
    entry_port: Port,
) -> list[RoutedPath] | None:
    """Route a port-to-port inter-section edge bundle as a clean corridor.

    Whole-graph rail mode stacks sections vertically, so a connecting edge
    leaves the upstream section by its RIGHT exit port and enters the
    downstream section by its LEFT entry port -- two points on opposite sides
    of the stack.  A straight rail run between them backtracks into the section
    interiors (a dangling stub) and crosses the sections' own rails.  Instead
    route the bundle out past the upstream right edge, down the right margin,
    across the inter-section gap, down the left margin, and in to the entry
    port: every vertical leg sits outside both section boxes and the cross leg
    sits in the empty band between them, so nothing slices an interior rail.

    Returns one route per edge, or ``None`` for a port pairing this corridor
    does not model (anything but a RIGHT exit feeding a LEFT entry), leaving
    the caller's straight-rail routing in place.
    """
    if exit_port.side is not PortSide.RIGHT or entry_port.side is not PortSide.LEFT:
        return None
    up = graph.sections.get(exit_port.section_id)
    down = graph.sections.get(entry_port.section_id)
    if up is None or down is None:
        return None

    rails = [
        (
            e,
            _line_rail_y(graph, e.source, e.line_id),
            _line_rail_y(graph, e.target, e.line_id),
        )
        for e in group
    ]
    center_ye = sum(ye for _e, ye, _yn in rails) / len(rails)
    center_yn = sum(yn for _e, _ye, yn in rails) / len(rails)
    exit_offsets = {e.line_id: ye - center_ye for e, ye, _yn in rails}
    entry_offsets = {e.line_id: yn - center_yn for e, _ye, yn in rails}
    half_width = max(
        (abs(o) for o in (*exit_offsets.values(), *entry_offsets.values())), default=0.0
    )

    # Corridor verticals clear the widest section edge by the bundle half-width
    # plus a fixed margin; the cross leg runs through the vertical gap between
    # the two stacked sections.
    clear = half_width + X_SPACING / 2.0
    corridor_r = max(up.bbox_x + up.bbox_w, down.bbox_x + down.bbox_w) + clear
    corridor_l = min(up.bbox_x, down.bbox_x) - clear
    upper, lower = sorted((up, down), key=lambda s: s.bbox_y)
    gap_y = (upper.bbox_y + upper.bbox_h + lower.bbox_y) / 2.0

    centerline = [
        (exit_port.x, center_ye),
        (corridor_r, center_ye),
        (corridor_r, gap_y),
        (corridor_l, gap_y),
        (corridor_l, center_yn),
        (entry_port.x, center_yn),
    ]

    # Each line is a rigid parallel offset through the corridor (exit-rail
    # spacing), tapering to its entry-rail offset only on the final lead-in, so
    # the bundle keeps its order and never flips.
    def leg_offsets(line_id: str) -> list[float]:
        eo = exit_offsets[line_id]
        return [eo, eo, eo, eo, entry_offsets[line_id]]

    members = [(e, e.line_id, leg_offsets(e.line_id)) for e, _ye, _yn in rails]
    # Self-contained baked route (like the off-track elbow): not part of the
    # normal router's trunk-slot regime, so it declares no TrunkSlot.
    return build_offset_bundle(
        members,
        centerline,
        base_radius=CURVE_RADIUS,
        is_inter_section=False,
    )


def route_rail_edges(
    graph: MetroGraph,
    edges: list[Edge] | None = None,
) -> list[RoutedPath]:
    """Route edges as straight horizontal runs along their line's rail.

    The two endpoints of an edge are on the same line, hence the same rail,
    so each route is two points at a common Y.  When the endpoints' resolved
    rail Ys differ slightly (e.g. a port at a section's mid rail vs. a pill's
    own rail), a short vertical jog at the source X joins them so the run
    stays axis-aligned rather than diagonal.

    When *edges* is None every edge in the graph is routed (whole-graph rail
    mode).  In per-section rail mode the caller passes just that section's
    internal edges so the normal router handles the rest.
    """
    edge_list = list(edges) if edges is not None else list(graph.edges)
    routes: list[RoutedPath] = []

    # Port-to-port edges joining two sections share an inter-section corridor;
    # gather each such bundle and route it as one connector so co-travelling
    # lines keep their order.  (Per-section rail mode never passes these edges.)
    connectors: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    for edge in edge_list:
        sp = graph.ports.get(edge.source)
        tp = graph.ports.get(edge.target)
        if sp is not None and tp is not None and sp.section_id != tp.section_id:
            connectors[(edge.source, edge.target)].append(edge)
    routed_connector: set[tuple[str, str, str]] = set()
    for (sid, tid), group in connectors.items():
        conn = _route_inter_section_connector(
            graph, group, graph.ports[sid], graph.ports[tid]
        )
        if conn is not None:
            routes.extend(conn)
            routed_connector.update((e.source, e.target, e.line_id) for e in group)

    for edge in edge_list:
        if (edge.source, edge.target, edge.line_id) in routed_connector:
            continue
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue

        # An off-track endpoint sits off the rails: drop straight down onto the
        # consumer's rail (a clean perpendicular crossing reads better than a
        # steep diagonal merge), turn once, and run flat into the consumer.
        off_src = src.off_track and not tgt.off_track
        off_tgt = tgt.off_track and not src.off_track
        if off_src or off_tgt:
            feeder = src if off_src else tgt
            on_rail = tgt if off_src else src
            routes.append(_route_off_track_elbow(graph, edge, feeder, on_rail, off_src))
            continue

        y_src = _line_rail_y(graph, edge.source, edge.line_id)
        y_tgt = _line_rail_y(graph, edge.target, edge.line_id)

        # Endpoints within a line-stroke of each other are the same rail for
        # drawing purposes: route straight rather than easing a sub-stroke
        # diagonal.  This catches a terminus-bundle slot that lands a fraction
        # off its line's rail (the bundle packs lines tighter than the rail
        # pitch), which would otherwise jitter the line as it enters the
        # terminus.  Real rail transitions are a full pitch (or a combo
        # sub-rail) apart, well above this tolerance.
        if abs(y_src - y_tgt) < 2.0:
            points = [(src.x, y_src), (tgt.x, y_src)]
        else:
            # The endpoints sit on different rails: this is a fan-out (the
            # source is a shared convergence point, e.g. the CRAM input) or a
            # fan-in (the target is a shared collector, e.g. the VCF output).
            # Ease between the two rails with a 45-degree diagonal transition -
            # the metro-map convention used by the normal router - rather than
            # a square right-angle jog.  Bias the diagonal toward whichever
            # endpoint is the shared convergence point so the rail leaves/joins
            # the fan on a diagonal and runs flat the rest of the way.
            is_fork = len(graph.edges_from(edge.source)) > 1
            is_join = len(graph.edges_to(edge.target)) > 1
            # A blank terminus converges its lines to a point; give the fan a
            # short flat lead-in at the source (or lead-out at the target) along
            # the convergence Y so the bundle reads as entering/leaving the
            # terminus level (next to its marker) before fanning to the rails.
            dx = tgt.x - src.x
            sign = 1.0 if dx >= 0 else -1.0
            lead_src = RAIL_TERMINUS_FAN_LEAD if (src.is_blank_terminus) else 0.0
            lead_tgt = RAIL_TERMINUS_FAN_LEAD if (tgt.is_blank_terminus) else 0.0
            fan_sx = src.x + sign * lead_src
            fan_tx = tgt.x - sign * lead_tgt
            diag_start_x, diag_end_x = _diagonal_placement(
                fan_sx, fan_tx, y_src, y_tgt, is_fork, is_join
            )
            points = [(src.x, y_src)]
            if lead_src:
                points.append((fan_sx, y_src))
            points.append((diag_start_x, y_src))
            points.append((diag_end_x, y_tgt))
            if lead_tgt:
                points.append((fan_tx, y_tgt))
            points.append((tgt.x, y_tgt))

        routes.append(
            RoutedPath(
                edge=edge,
                line_id=edge.line_id,
                points=points,
                offset_regime=OffsetRegime.BAKED,
            )
        )
    return routes
