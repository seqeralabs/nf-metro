"""Core edge routing: the main route_edges() dispatcher.

Routes edges as horizontal segments with 45-degree diagonal transitions.
For folded layouts, cross-row edges route through the fold edge with a
clean vertical drop. Inter-section edges use L-shaped routing with
per-line bundle offsets.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    BYPASS_NEST_STEP,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CROSS_ROW_THRESHOLD,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    FOLD_MARGIN,
    HEADER_CLEARANCE,
    JUNCTION_MARGIN,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    OFFSET_STEP,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.routing.common import (
    RoutedPath,
    adjacent_column_gap_x,
    bypass_bottom_y,
    compute_bundle_info,
    inter_column_channel_x,
    row_bottom_edge,
    row_top_edge,
)
from nf_metro.layout.routing.corners import (
    l_shape_radii,
    reversed_offset,
    tb_entry_corner,
    tb_exit_corner,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Station

# ---------------------------------------------------------------------------
# Routing context: pre-computed state shared by all handlers
# ---------------------------------------------------------------------------


@dataclass
class _RoutingCtx:
    """Pre-computed state shared by edge routing handlers."""

    graph: MetroGraph
    fold_x: float
    junction_ids: set[str]
    bottom_exit_junctions: set[str]
    bottom_exit_junction_ports: dict[str, str]
    offset_step: float
    fork_stations: set[str]
    join_stations: set[str]
    tb_sections: set[str]
    tb_right_entry: set[str]
    bundle_info: dict[tuple[str, str, str], tuple[int, int]]
    bypass_gap_idx: dict[tuple[str, str, str], tuple[int, int, int, int]]
    station_offsets: dict[tuple[str, str], float] | None
    diagonal_run: float
    curve_radius: float
    skip_edges: set[tuple[str, str, str]] = field(default_factory=set)
    junction_fan_info: dict[tuple[str, str, str], tuple[int, int]] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route_edges(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
) -> list[RoutedPath]:
    """Route all edges with smooth direction changes.

    Detects cross-row edges (large Y gap relative to X gap) and routes
    them through a vertical connector at the fold edge.
    """
    ctx = _build_routing_context(graph, diagonal_run, curve_radius, station_offsets)
    routes: list[RoutedPath] = []

    for edge in graph.edges:
        if (edge.source, edge.target, edge.line_id) in ctx.skip_edges:
            continue

        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        # Try each routing handler in priority order.
        # The first handler that returns a RoutedPath wins.
        result = _route_inter_section(edge, src, tgt, ctx)
        if result is None:
            result = _route_tb_internal(edge, src, tgt, ctx)
        if result is None:
            result = _route_tb_lr_exit(edge, src, tgt, ctx)
        if result is None:
            result = _route_tb_lr_entry(edge, src, tgt, ctx)
        if result is None:
            result = _route_perp_entry(edge, src, tgt, ctx)
        if result is None:
            result = _route_intra_section(edge, src, tgt, ctx)

        if result is not None:
            routes.append(result)

    _center_bubble_stations(routes, graph)

    return routes


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _build_routing_context(
    graph: MetroGraph,
    diagonal_run: float,
    curve_radius: float,
    station_offsets: dict[tuple[str, str], float] | None,
) -> _RoutingCtx:
    """Pre-compute all shared state for edge routing."""
    junction_ids = set(graph.junctions)

    # Fold edge: max X across all stations
    all_x = [s.x for s in graph.stations.values()]
    fold_x = max(all_x) if all_x else 0

    # Junctions fed by BOTTOM exit ports
    bottom_exit_junctions: set[str] = set()
    bottom_exit_junction_ports: dict[str, str] = {}
    for e in graph.edges:
        if e.target in junction_ids:
            port = graph.ports.get(e.source)
            if port and not port.is_entry and port.side == PortSide.BOTTOM:
                bottom_exit_junctions.add(e.target)
                bottom_exit_junction_ports[e.target] = e.source

    # Fork/join stations
    fork_targets: dict[str, set[str]] = defaultdict(set)
    join_sources: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        fork_targets[e.source].add(e.target)
        join_sources[e.target].add(e.source)
    fork_stations = {sid for sid, tgts in fork_targets.items() if len(tgts) > 1}
    join_stations = {sid for sid, srcs in join_sources.items() if len(srcs) > 1}

    # TB sections and their entry sides
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    tb_right_entry: set[str] = set()
    for port in graph.ports.values():
        if (
            port.is_entry
            and port.side == PortSide.RIGHT
            and port.section_id in tb_sections
        ):
            tb_right_entry.add(port.section_id)

    # Bundle assignments and bypass gap indices
    line_priority = {lid: i for i, lid in enumerate(graph.lines.keys())}
    bundle_info = compute_bundle_info(
        graph, junction_ids, line_priority, bottom_exit_junctions
    )
    bypass_gap_idx = _compute_bypass_gap_indices(graph, junction_ids, line_priority)
    junction_fan_info = _compute_junction_fan_info(graph, junction_ids, line_priority)

    return _RoutingCtx(
        graph=graph,
        fold_x=fold_x,
        junction_ids=junction_ids,
        bottom_exit_junctions=bottom_exit_junctions,
        bottom_exit_junction_ports=bottom_exit_junction_ports,
        offset_step=OFFSET_STEP,
        fork_stations=fork_stations,
        join_stations=join_stations,
        tb_sections=tb_sections,
        tb_right_entry=tb_right_entry,
        bundle_info=bundle_info,
        bypass_gap_idx=bypass_gap_idx,
        station_offsets=station_offsets,
        diagonal_run=diagonal_run,
        curve_radius=curve_radius,
        junction_fan_info=junction_fan_info,
    )


# ---------------------------------------------------------------------------
# Offset helpers
# ---------------------------------------------------------------------------


def _get_offset(ctx: _RoutingCtx, station_id: str, line_id: str) -> float:
    """Get the station offset for a (station, line) pair, defaulting to 0."""
    if ctx.station_offsets:
        return ctx.station_offsets.get((station_id, line_id), 0.0)
    return 0.0


def _max_offset_at(ctx: _RoutingCtx, station_id: str) -> float:
    """Get the maximum offset across all lines at a station."""
    if not ctx.station_offsets:
        return 0.0
    all_offs = [
        ctx.station_offsets.get((station_id, lid), 0.0)
        for lid in ctx.graph.station_lines(station_id)
    ]
    return max(all_offs) if all_offs else 0.0


def _tb_x_offset(
    ctx: _RoutingCtx, station_id: str, line_id: str, section_id: str
) -> float:
    """Compute the TB-aware X offset for a station.

    RIGHT-entry sections use non-reversed offsets; others use reversed.
    """
    off = _get_offset(ctx, station_id, line_id)
    if section_id in ctx.tb_right_entry:
        return off
    return reversed_offset(off, _max_offset_at(ctx, station_id))


# ---------------------------------------------------------------------------
# Handler 1: Inter-section edges (port-to-port / junction)
# ---------------------------------------------------------------------------


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

    i, n = ctx.bundle_info.get((edge.source, edge.target, edge.line_id), (0, 1))

    # Check for TB BOTTOM exit
    src_port = graph.ports.get(edge.source)
    src_is_tb_bottom = (
        src_port is not None
        and not src_port.is_entry
        and src_port.side == PortSide.BOTTOM
        and src.section_id in ctx.tb_sections
    )

    # Resolve section columns and row for bypass detection
    src_col = _resolve_section_col(graph, src, ctx.junction_ids)
    tgt_col = _resolve_section_col(graph, tgt, ctx.junction_ids)
    src_row = _resolve_section_row(graph, src, ctx.junction_ids)
    needs_bypass = (
        src_col is not None
        and tgt_col is not None
        and abs(tgt_col - src_col) > 1
        and _has_intervening_sections(graph, src_col, tgt_col, src_row)
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
        return _route_bypass(edge, src, tgt, i, src_col, tgt_col, ctx, src_row)

    # Near-vertical: junction to same-column entry with tiny horizontal
    # offset (just the junction margin).  The standard L-shape would
    # place the vertical channel on the wrong side (toward the target,
    # which is back inside the section).  Instead, route the channel
    # further into the inter-column gap (away from the target) so the
    # line continues in the junction's natural direction before dropping.
    if (
        edge.source in ctx.junction_ids
        and abs(dx) <= JUNCTION_MARGIN + COORD_TOLERANCE
        and abs(dy) > abs(dx) * 3
    ):
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            going_down=(dy > 0),
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        # Push channel away from target into the inter-column gap.
        if dx < 0:
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
    if tgt_port and tgt_port.is_entry and tgt_port.side == PortSide.RIGHT and dx > 0:
        return _route_right_entry_wrap(edge, src, tgt, i, n, ctx)

    # Standard L-shape
    return _route_l_shape(edge, src, tgt, i, n, ctx)


def _route_tb_bottom_exit(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath:
    """Vertical drop from TB BOTTOM exit with X offsets."""
    x_off = _tb_x_offset(ctx, edge.source, edge.line_id, src.section_id)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(src.x + x_off, src.y), (tgt.x + x_off, tgt.y)],
        is_inter_section=True,
        offsets_applied=True,
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
    r = ctx.curve_radius + x_off
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


def _route_bypass(
    edge: Edge,
    src: Station,
    tgt: Station,
    i: int,
    src_col: int,
    tgt_col: int,
    ctx: _RoutingCtx,
    src_row: int | None = None,
) -> RoutedPath:
    """U-shaped bypass route around intervening sections."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    graph = ctx.graph

    ekey = (edge.source, edge.target, edge.line_id)
    g1_j, g1_n, g2_j, g2_n = ctx.bypass_gap_idx.get(ekey, (0, 1, 0, 1))

    # Nest vertically only when bypass routes from DIFFERENT source
    # columns share the same gap (prevents overlap).  Same-source
    # bypasses are already separated by gap offsets.
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        nest_offset = g2_j * ctx.offset_step
    else:
        nest_idx = max(i, g2_j)
        nest_offset = nest_idx * BYPASS_NEST_STEP
    base_y = bypass_bottom_y(graph, src_col, tgt_col, BYPASS_CLEARANCE, src_row=src_row)
    by = base_y + nest_offset

    base_bypass_offset = ctx.curve_radius + ctx.offset_step
    gap1_extra = g1_j * ctx.offset_step
    gap2_extra = g2_j * ctx.offset_step

    if dx > 0:
        if fan is not None:
            # Use unified fan position for gap1 (shared first corner)
            ui, un = fan
            going_down = True  # bypass always goes down first
            delta, r_fan_first, _ = l_shape_radii(
                ui,
                un,
                going_down=going_down,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
            gap1_x = fan_mid_x + delta
        else:
            gap1_base = (
                adjacent_column_gap_x(graph, src_col, src_col + 1) - base_bypass_offset
            )
            gap1_limit = sx + ctx.curve_radius
            if gap1_base - (g1_n - 1) * ctx.offset_step < gap1_limit:
                gap1_x = gap1_limit + (g1_n - 1 - g1_j) * ctx.offset_step
            else:
                gap1_x = gap1_base - gap1_extra

        gap2_base = (
            adjacent_column_gap_x(graph, tgt_col - 1, tgt_col) + base_bypass_offset
        )
        gap2_limit = tx - ctx.curve_radius
        # When gap is too narrow, fan out from the limit toward gap center
        if gap2_base + (g2_n - 1) * ctx.offset_step > gap2_limit:
            gap2_x = gap2_limit - (g2_n - 1 - g2_j) * ctx.offset_step
        else:
            gap2_x = gap2_base + gap2_extra
    else:
        if fan is not None:
            ui, un = fan
            going_down = True
            delta, r_fan_first, _ = l_shape_radii(
                ui,
                un,
                going_down=going_down,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            fan_mid_x = sx - ctx.curve_radius - (un - 1) * ctx.offset_step / 2
            gap1_x = fan_mid_x + delta
        else:
            gap1_base = (
                adjacent_column_gap_x(graph, src_col - 1, src_col) + base_bypass_offset
            )
            gap1_limit = sx - ctx.curve_radius
            if gap1_base + (g1_n - 1) * ctx.offset_step > gap1_limit:
                gap1_x = gap1_limit - (g1_n - 1 - g1_j) * ctx.offset_step
            else:
                gap1_x = gap1_base + gap1_extra

        gap2_base = (
            adjacent_column_gap_x(graph, tgt_col, tgt_col + 1) - base_bypass_offset
        )
        gap2_limit = tx + ctx.curve_radius
        if gap2_base - (g2_n - 1) * ctx.offset_step < gap2_limit:
            gap2_x = gap2_limit + (g2_n - 1 - g2_j) * ctx.offset_step
        else:
            gap2_x = gap2_base - gap2_extra

    # Per-corner radii for concentricity.  The bypass has 4 corners:
    #   1: horiz->vert-down (CW)  -- concentric needs gap1+r = const
    #   2: vert-down->horiz (CCW) -- concentric needs gap1+r = const
    #   3: horiz->vert-up (CCW)   -- concentric needs gap2-r = const
    #   4: vert-up->horiz (CW)    -- concentric needs gap2+r = const
    # Corners 2 & 3 use the same radius (gap_extra based), but corner 4
    # needs the REVERSED gap2 offset so the leftmost line gets the
    # largest radius.
    r_mid = ctx.curve_radius + max(gap1_extra, gap2_extra)
    r_last = ctx.curve_radius + (g2_n - 1 - g2_j) * ctx.offset_step

    if fan is not None:
        r_first = r_fan_first
    else:
        r_first = r_mid

    # Apply per-line offsets directly so the renderer doesn't have to
    # guess which waypoints belong to the source vs target side.
    # (When source and target share the same base Y, the midpoint
    # heuristic in the renderer breaks.)
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
            (tx, ty + tgt_off),
        ],
        is_inter_section=True,
        curve_radii=[r_first, r_mid, r_mid, r_last],
        offsets_applied=True,
    )


def _route_l_shape(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Standard L-shape inter-section route with concentric arcs."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy
    going_down = dy > 0

    # When the junction has both L-shape and bypass siblings, use
    # unified fan-out positions so all lines share one concentric
    # first corner.
    ekey = (edge.source, edge.target, edge.line_id)
    fan = ctx.junction_fan_info.get(ekey)
    if fan is not None:
        ui, un = fan
        # First corner: unified position within the combined fan-out
        delta, r_first, _ = l_shape_radii(
            ui,
            un,
            going_down=going_down,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        # mid_x places all lines so they diverge at sx
        if dx > 0:
            mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
        else:
            mid_x = sx - ctx.curve_radius - (un - 1) * ctx.offset_step / 2
        # Second corner: from sub-bundle (only L-shape siblings turn here)
        _, _, r_second = l_shape_radii(
            i,
            n,
            going_down=going_down,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
    else:
        delta, r_first, r_second = l_shape_radii(
            i,
            n,
            going_down=going_down,
            offset_step=ctx.offset_step,
            base_radius=ctx.curve_radius,
        )
        max_r = ctx.curve_radius + (n - 1) * ctx.offset_step
        mid_x = inter_column_channel_x(
            ctx.graph, src, tgt, sx, tx, dx, max_r, ctx.offset_step
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
                going_down=going_down,
                offset_step=ctx.offset_step,
                base_radius=new_base,
            )
            _, _, r_second = l_shape_radii(
                i,
                n,
                going_down=going_down,
                offset_step=ctx.offset_step,
                base_radius=new_base,
            )
        else:
            _, r_first, r_second = l_shape_radii(
                i,
                n,
                going_down=going_down,
                offset_step=ctx.offset_step,
                base_radius=new_base,
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

    delta, r_first, r_second = l_shape_radii(
        i,
        n,
        going_down=(dy > 0),
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    # Compute Y for the horizontal channel in the inter-row gap.
    mid_y = _inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    hy = mid_y + delta

    # Horizontal lead-in: a short run so the corner from horizontal to
    # vertical gets a proper curve.  When dx is large, the lead-in
    # direction matches dx.  When dx is near-zero (source directly
    # above target), infer direction from the upstream exit port so the
    # line continues with the bundle flow before curving down.
    r_lead = ctx.curve_radius
    if abs(dx) > r_lead:
        lead_sign = 1.0 if dx > 0 else -1.0
    else:
        lead_sign = 1.0  # default rightward
        if src.id in ctx.graph.junctions:
            for je in ctx.graph.edges:
                if je.target == src.id:
                    js = ctx.graph.stations.get(je.source)
                    if js and js.is_port:
                        lead_sign = 1.0 if js.x < src.x else -1.0
                        break
    lx = sx + lead_sign * r_lead
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
            curve_radii=[r_lead, r_second],
        )
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (lx, sy), (lx, hy), (tx, hy), (tx, ty)],
        is_inter_section=True,
        curve_radii=[r_lead, r_first, r_second],
    )


def _route_right_entry_wrap(
    edge: Edge, src: Station, tgt: Station, i: int, n: int, ctx: _RoutingCtx
) -> RoutedPath:
    """Route to a RIGHT entry port by wrapping around the right side.

    When the source is to the LEFT of a RIGHT entry port, the standard
    L-shape would cut horizontally through the target section.  Instead,
    drop into the inter-row gap, run horizontally past the target
    section's right edge, then drop into the RIGHT entry port::

        (sx,sy) -> (sx, hy) -> (vx, hy) -> (vx, ty) -> (tx, ty)

    For cross-row cases, the horizontal channel runs just below the
    source row's sections (bypass style) so the line stays high and
    only drops down when it reaches the target column.

    This avoids crossing through intervening sections.
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dy = ty - sy

    delta, r_first, r_second = l_shape_radii(
        i,
        n,
        going_down=(dy > 0),
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    # Detect cross-row case: use bypass-style Y just below the source
    # row's sections so the line runs horizontally under the adjacent
    # section before dropping to the target row.
    src_row = _resolve_section_row(ctx.graph, src, ctx.junction_ids)
    tgt_row = _resolve_section_row(ctx.graph, tgt, ctx.junction_ids)
    src_col = _resolve_section_col(ctx.graph, src, ctx.junction_ids)
    tgt_col = _resolve_section_col(ctx.graph, tgt, ctx.junction_ids)

    cross_row = (
        src_row is not None
        and tgt_row is not None
        and src_row != tgt_row
        and src_col is not None
        and tgt_col is not None
    )

    if cross_row:
        hy = bypass_bottom_y(
            ctx.graph, src_col, tgt_col, BYPASS_CLEARANCE, src_row=src_row
        )
        hy += delta
    else:
        # Same-row: use inter-row gap above the target section.
        hy = _inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
        hy += delta

    # Vertical channel X: just past the entry port in the inter-section gap.
    vx = tx + ctx.curve_radius + ctx.offset_step + delta

    # Short horizontal lead-in so the first corner (horizontal-to-vertical)
    # gets a smooth curve instead of a sharp right angle at the junction.
    r_lead = ctx.curve_radius
    lx = sx + r_lead
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (lx, sy), (lx, hy), (vx, hy), (vx, ty), (tx, ty)],
        is_inter_section=True,
        curve_radii=[r_lead, r_first, r_first, r_second],
    )


def _inter_row_channel_y(
    graph: MetroGraph,
    src: Station,
    tgt: Station,
    sy: float,
    ty: float,
    dy: float,
    max_r: float,
) -> float:
    """Compute Y for a horizontal channel in an inter-row gap.

    Vertical equivalent of ``inter_column_channel_x``: places the
    channel in the inter-row gap, above the target section's header
    (number badge + label rendered above bbox_y).
    """
    # Keep the channel clear of section headers (numbered circle + label)
    # that protrude above/below bbox_y.

    # Resolve sections for junction stations (section_id is None for
    # junctions; trace through edges to find a connected port's section).
    src_sec = _resolve_section(graph, src)
    tgt_sec = _resolve_section(graph, tgt)

    if src_sec and tgt_sec and src_sec.grid_row != tgt_sec.grid_row:
        src_row = src_sec.grid_row
        tgt_row = tgt_sec.grid_row

        if dy > 0:
            # Going down: gap between bottom of source row and top of target row
            bottom = row_bottom_edge(graph, src_row, default=sy)
            top = row_top_edge(graph, tgt_row, default=ty)
            # Place above the header zone
            header_top = top - HEADER_CLEARANCE
            return (bottom + header_top) / 2
        else:
            # Going up: gap between top of source row and bottom of target row
            top = row_top_edge(graph, src_row, default=sy)
            bottom = row_bottom_edge(graph, tgt_row, default=ty)
            header_bottom = bottom + HEADER_CLEARANCE
            return (top + header_bottom) / 2

    # Fallback: place near target, clearing the header zone
    if dy > 0:
        return ty - HEADER_CLEARANCE - max_r
    else:
        return ty + HEADER_CLEARANCE + max_r


def _resolve_section(
    graph: MetroGraph,
    station: Station,
    prefer_upstream: bool = True,
):
    """Resolve a station's section, tracing through junctions if needed.

    For stations with a ``section_id``, returns that section directly.
    For junctions (``section_id is None``), traces edges to find a
    connected port's section.

    When *prefer_upstream* is True (default), incoming edges are checked
    first so the junction resolves to the upstream section.  When False,
    both directions are scanned in a single pass with no preference.
    """
    if station.section_id:
        return graph.sections.get(station.section_id)

    if prefer_upstream:
        # Check incoming edges first (upstream preference)
        for e in graph.edges:
            if e.target == station.id:
                other = graph.stations.get(e.source)
                if other and other.section_id:
                    sec = graph.sections.get(other.section_id)
                    if sec:
                        return sec
        # Fall back to outgoing edges
        for e in graph.edges:
            if e.source == station.id:
                other = graph.stations.get(e.target)
                if other and other.section_id:
                    sec = graph.sections.get(other.section_id)
                    if sec:
                        return sec
    else:
        # Scan both directions in one pass (no preference)
        for e in graph.edges:
            other_id = None
            if e.source == station.id:
                other_id = e.target
            elif e.target == station.id:
                other_id = e.source
            if other_id:
                other = graph.stations.get(other_id)
                if other and other.section_id:
                    sec = graph.sections.get(other.section_id)
                    if sec:
                        return sec
    return None


# ---------------------------------------------------------------------------
# Handler 2: TB section internal edges
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Handler 3: TB section LEFT/RIGHT exit
# ---------------------------------------------------------------------------


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

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    max_src_off = _max_offset_at(ctx, edge.source)

    vert_x_off, horiz_y_off, r = tb_exit_corner(
        src_off,
        max_src_off,
        exit_right=(tgt_port.side == PortSide.RIGHT),
        base_radius=ctx.curve_radius,
    )
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


# ---------------------------------------------------------------------------
# Handler 4: TB section LEFT/RIGHT entry
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Handler 5: Perpendicular (TOP/BOTTOM) port entry to internal station
# ---------------------------------------------------------------------------


def _route_perp_entry(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route TOP/BOTTOM port -> internal station with upstream merging."""
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

    src_off = _get_offset(ctx, edge.source, edge.line_id)
    tgt_off = _get_offset(ctx, edge.target, edge.line_id)

    # Try to merge with upstream inter-section edge
    upstream_st = _find_upstream_for_merge(edge, src, ctx)

    if upstream_st is not None:
        return _route_perp_entry_merged(
            edge, src, tgt, upstream_st, src_off, tgt_off, ctx
        )

    if abs(dx) < COORD_TOLERANCE:
        # Nearly same X: straight vertical drop
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx + src_off, sy), (tx, ty + tgt_off)],
            offsets_applied=True,
        )

    # L-shape: vertical drop then horizontal to station
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx + src_off, sy),
            (sx + src_off, ty + tgt_off),
            (tx, ty + tgt_off),
        ],
        offsets_applied=True,
        curve_radii=[ctx.curve_radius + src_off],
    )


def _find_upstream_for_merge(
    edge: Edge, src: Station, ctx: _RoutingCtx
) -> Station | None:
    """Find an upstream station to merge with for combined L-shape routing.

    Returns the upstream station if merging is appropriate, or None.
    Adds the upstream edge to skip_edges when merging.
    """
    if not ctx.station_offsets:
        return None

    graph = ctx.graph
    for e2 in graph.edges:
        if e2.target != edge.source or e2.line_id != edge.line_id:
            continue
        u = graph.stations.get(e2.source)
        if not u:
            continue
        # Don't merge with TB BOTTOM exits
        u_port = graph.ports.get(e2.source)
        if (
            u_port
            and not u_port.is_entry
            and u_port.side == PortSide.BOTTOM
            and u.section_id in ctx.tb_sections
        ):
            continue
        # Only merge when upstream is at the same Y as the entry port
        if abs(u.y - src.y) > COORD_TOLERANCE:
            continue
        ctx.skip_edges.add((e2.source, e2.target, e2.line_id))
        return u

    return None


def _route_perp_entry_merged(
    edge: Edge,
    src: Station,
    tgt: Station,
    upstream_st: Station,
    src_off: float,
    tgt_off: float,
    ctx: _RoutingCtx,
) -> RoutedPath:
    """Route a combined inter-section + perpendicular entry as one L-shape."""
    graph = ctx.graph
    tx, ty = tgt.x, tgt.y
    up_y_off = _get_offset(ctx, upstream_st.id, edge.line_id)

    if abs(upstream_st.x - src.x) < COORD_TOLERANCE:
        # Same X: 4-point combined route through inter-column channel
        mid_x = inter_column_channel_x(
            graph,
            upstream_st,
            tgt,
            upstream_st.x,
            tgt.x,
            tgt.x - upstream_st.x,
            ctx.curve_radius,
            ctx.offset_step,
        )
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[
                (upstream_st.x, upstream_st.y + up_y_off),
                (mid_x + src_off, upstream_st.y + up_y_off),
                (mid_x + src_off, ty + tgt_off),
                (tx, ty + tgt_off),
            ],
            offsets_applied=True,
            curve_radii=[ctx.curve_radius, ctx.curve_radius + src_off],
        )

    # Different X (cross-column entry): 3-point L-shape
    max_tgt_off = _max_offset_at(ctx, edge.target)
    rev_tgt_off = reversed_offset(tgt_off, max_tgt_off)
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (upstream_st.x, upstream_st.y + up_y_off),
            (tx + rev_tgt_off, upstream_st.y + up_y_off),
            (tx + rev_tgt_off, ty + tgt_off),
        ],
        offsets_applied=True,
        curve_radii=[ctx.curve_radius + rev_tgt_off],
    )


# ---------------------------------------------------------------------------
# Handler 6: Intra-section edges (diagonal transitions, folds, straights)
# ---------------------------------------------------------------------------


def _route_intra_section(
    edge: Edge, src: Station, tgt: Station, ctx: _RoutingCtx
) -> RoutedPath | None:
    """Route intra-section edges: diagonals, fold routing, straight lines."""
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    dx = tx - sx
    dy = ty - sy

    # Cross-row fold edge (skip for intra-section RL edges)
    same_section = src.section_id and src.section_id == tgt.section_id
    if dx <= 0 and abs(dy) > CROSS_ROW_THRESHOLD and not same_section:
        fold_right = ctx.fold_x + FOLD_MARGIN
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (fold_right, sy), (fold_right, ty), (tx, ty)],
        )

    # Same track: straight line
    if abs(sy - ty) < COORD_TOLERANCE_FINE:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (tx, ty)],
        )

    # Near-zero X gap: straight line
    if abs(dx) < COORD_TOLERANCE:
        return RoutedPath(
            edge=edge,
            line_id=edge.line_id,
            points=[(sx, sy), (tx, ty)],
        )

    # Different tracks: horizontal -> diagonal -> horizontal
    return _route_diagonal(edge, src, tgt, ctx)


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
    if src_min + tgt_min + ctx.diagonal_run > abs(dx):
        src_min = min_straight
        tgt_min = min_straight

    diag_start_x, diag_end_x = _compute_diagonal_placement(
        sx,
        tx,
        ctx.diagonal_run,
        src_min,
        tgt_min,
        edge.source in ctx.fork_stations,
        edge.target in ctx.join_stations,
    )

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (diag_start_x, sy), (diag_end_x, ty), (tx, ty)],
    )


# ---------------------------------------------------------------------------
# Post-processing: centre bubble stations on their flat segments
# ---------------------------------------------------------------------------


@dataclass
class _BubbleCtx:
    """Pre-computed indexes for bubble-centering logic."""

    # Fork/join adjacency from the full edge list
    all_sources: dict[str, set[str]]
    all_targets: dict[str, set[str]]
    # 4-point diagonal routes indexed by station
    incoming: dict[str, list[RoutedPath]]
    outgoing: dict[str, list[RoutedPath]]
    # 2-point flat routes indexed by station
    flat_incoming: dict[str, list[RoutedPath]]
    flat_outgoing: dict[str, list[RoutedPath]]
    # Physically distinct diagonal convergence/divergence points
    diag_in_sources: dict[str, set[str]]
    diag_out_targets: dict[str, set[str]]
    # Snapshot of station X before any moves
    original_x: dict[str, float]


def _build_bubble_ctx(routes: list[RoutedPath], graph: MetroGraph) -> _BubbleCtx:
    """Build indexes for bubble-station centering."""
    all_sources: dict[str, set[str]] = defaultdict(set)
    all_targets: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        all_targets[edge.source].add(edge.target)
        all_sources[edge.target].add(edge.source)

    incoming: dict[str, list[RoutedPath]] = defaultdict(list)
    outgoing: dict[str, list[RoutedPath]] = defaultdict(list)
    flat_incoming: dict[str, list[RoutedPath]] = defaultdict(list)
    flat_outgoing: dict[str, list[RoutedPath]] = defaultdict(list)
    diag_in_sources: dict[str, set[str]] = defaultdict(set)
    diag_out_targets: dict[str, set[str]] = defaultdict(set)

    for rp in routes:
        if len(rp.points) == 2:
            flat_incoming[rp.edge.target].append(rp)
            flat_outgoing[rp.edge.source].append(rp)
            continue
        if len(rp.points) != 4:
            continue
        if abs(rp.points[1][1] - rp.points[2][1]) < COORD_TOLERANCE_FINE:
            continue
        incoming[rp.edge.target].append(rp)
        outgoing[rp.edge.source].append(rp)
        diag_in_sources[rp.edge.target].add(rp.edge.source)
        diag_out_targets[rp.edge.source].add(rp.edge.target)

    original_x = {sid: s.x for sid, s in graph.stations.items() if not s.is_port}

    return _BubbleCtx(
        all_sources=all_sources,
        all_targets=all_targets,
        incoming=incoming,
        outgoing=outgoing,
        flat_incoming=flat_incoming,
        flat_outgoing=flat_outgoing,
        diag_in_sources=diag_in_sources,
        diag_out_targets=diag_out_targets,
        original_x=original_x,
    )


def _collect_centering_candidates(
    graph: MetroGraph, ctx: _BubbleCtx
) -> dict[str, tuple]:
    """First pass: shift simple diagonals and collect station-move candidates.

    For stations with a single diagonal on each side and no bundle
    conflicts, shifts both diagonals to equalise the flat runs.
    For more complex cases (shared bundles, flat+diagonal mixes),
    collects a station-move candidate for the second pass.
    """
    station_move_candidates: dict[str, tuple] = {}

    def _is_internal(sid: str) -> bool:
        st = graph.stations.get(sid)
        return st is not None and not st.is_port and not st.is_hidden

    for sid, station in graph.stations.items():
        if station.is_port or station.is_hidden:
            continue

        in_routes = ctx.incoming.get(sid, [])
        out_routes = ctx.outgoing.get(sid, [])
        flat_in = ctx.flat_incoming.get(sid, [])
        flat_out = ctx.flat_outgoing.get(sid, [])

        is_fork_join = (
            len(ctx.all_targets.get(sid, set())) > 1
            or len(ctx.all_sources.get(sid, set())) > 1
        )

        # Determine which routes bound the station's flat segment.
        in_rp = None
        out_rp = None
        flat_in_rp = None
        flat_out_rp = None

        # Count physically distinct edges (unique source-target pairs).
        n_unique_in = len(set((rp.edge.source, rp.edge.target) for rp in in_routes))
        n_unique_out = len(set((rp.edge.source, rp.edge.target) for rp in out_routes))
        n_unique_flat_in = len(set((rp.edge.source, rp.edge.target) for rp in flat_in))
        n_unique_flat_out = len(
            set((rp.edge.source, rp.edge.target) for rp in flat_out)
        )

        multi_diag = False
        if not is_fork_join and (
            (n_unique_in + n_unique_flat_in) >= 1
            and n_unique_out >= 1
            and (
                n_unique_in > 1
                or n_unique_out > 1
                or (n_unique_in >= 1 and n_unique_flat_in >= 1)
            )
        ):
            in_rp = in_routes[0] if in_routes else None
            flat_in_rp = flat_in[0] if (not in_routes and flat_in) else None
            out_rp = out_routes[0]
            multi_diag = True
        elif is_fork_join:
            continue
        elif n_unique_in == 1 and n_unique_out == 1:
            in_rp = in_routes[0]
            out_rp = out_routes[0]
        elif n_unique_in == 0 and n_unique_flat_in == 1 and n_unique_out == 1:
            flat_in_rp = flat_in[0]
            out_rp = out_routes[0]
        elif n_unique_in == 1 and n_unique_out == 0 and n_unique_flat_out == 1:
            in_rp = in_routes[0]
            flat_out_rp = flat_out[0]
        else:
            continue

        # Check bundle convergence/divergence at neighbours.
        shared_source = False
        shared_target = False
        if out_rp:
            out_tgt = graph.stations.get(out_rp.edge.target)
            if len(ctx.diag_in_sources.get(out_rp.edge.target, set())) > 1 and not (
                out_tgt and out_tgt.is_port
            ):
                shared_target = True
        if in_rp:
            in_src = graph.stations.get(in_rp.edge.source)
            if len(ctx.diag_out_targets.get(in_rp.edge.source, set())) > 1 and not (
                in_src and in_src.is_port
            ):
                shared_source = True

        # Determine X extent of the flat segment at station Y.
        if multi_diag:
            in_xs = [r.points[2][0] for r in in_routes]
            in_xs += [r.points[0][0] for r in flat_in]
            out_xs = [r.points[1][0] for r in out_routes]
            in_diag_end_x = max(in_xs) if in_xs else station.x
            out_diag_start_x = min(out_xs) if out_xs else station.x
        elif in_rp:
            in_diag_end_x = in_rp.points[2][0]
        else:
            in_diag_end_x = flat_in_rp.points[0][0]

        if not multi_diag:
            if out_rp:
                out_diag_start_x = out_rp.points[1][0]
            else:
                out_diag_start_x = flat_out_rp.points[-1][0]

        in_flat = station.x - in_diag_end_x
        out_flat = out_diag_start_x - station.x

        if abs(in_flat) < 1 or abs(out_flat) < 1:
            continue
        if abs(in_flat - out_flat) < 1:
            continue

        has_flat_side = flat_in_rp is not None or flat_out_rp is not None

        # Guard: skip when a flat connection goes to/from an internal station.
        if has_flat_side or multi_diag:
            flat_to_internal = False
            if flat_in_rp and _is_internal(flat_in_rp.edge.source):
                flat_to_internal = True
            if flat_out_rp and _is_internal(flat_out_rp.edge.target):
                flat_to_internal = True
            if multi_diag:
                for r in flat_in:
                    if _is_internal(r.edge.source):
                        flat_to_internal = True
                for r in flat_out:
                    if _is_internal(r.edge.target):
                        flat_to_internal = True
            if flat_to_internal:
                continue

        if shared_source or shared_target or has_flat_side or multi_diag:
            new_x = (in_diag_end_x + out_diag_start_x) / 2
            station_move_candidates[sid] = (
                new_x,
                in_routes,
                flat_in,
                out_routes,
                flat_out,
            )
            continue

        # Simple case: shift both diagonals to equalise the flats.
        shift = (in_flat - out_flat) / 2

        if abs(shift) > min(abs(in_flat), abs(out_flat)):
            continue

        # Guard: don't shift in convergence/divergence bundles.
        if out_rp and len(ctx.diag_in_sources.get(out_rp.edge.target, set())) > 1:
            continue
        if in_rp and len(ctx.diag_out_targets.get(in_rp.edge.source, set())) > 1:
            continue

        for rp in in_routes:
            rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
            rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])
        for rp in out_routes:
            rp.points[1] = (rp.points[1][0] + shift, rp.points[1][1])
            rp.points[2] = (rp.points[2][0] + shift, rp.points[2][1])

    return station_move_candidates


def _apply_station_moves(
    graph: MetroGraph,
    candidates: dict[str, tuple],
    original_x: dict[str, float],
) -> None:
    """Second pass: apply station-move candidates with companion consensus.

    Only moves a station when all column companions (visible stations at
    the same original X in the same section) are also candidates.  This
    preserves column alignment when only some stations want to centre.
    """
    for sid, (
        new_x,
        in_routes,
        flat_in,
        out_routes,
        flat_out,
    ) in candidates.items():
        station = graph.stations[sid]
        if abs(new_x - station.x) > 0.5:
            ox = original_x.get(sid, station.x)
            companions = []
            for other_sid, other_ox in original_x.items():
                if other_sid == sid:
                    continue
                if abs(other_ox - ox) > 1:
                    continue
                other = graph.stations.get(other_sid)
                if not other or other.is_port or other.is_hidden:
                    continue
                if other.section_id != station.section_id:
                    continue
                if abs(other.y - station.y) > 1:
                    companions.append(other_sid)
            if companions:
                if any(c not in candidates for c in companions):
                    continue

        station.x = new_x
        for r in in_routes:
            r.points[-1] = (new_x, r.points[-1][1])
        for r in flat_in:
            r.points[-1] = (new_x, r.points[-1][1])
        for r in out_routes:
            r.points[0] = (new_x, r.points[0][1])
        for r in flat_out:
            r.points[0] = (new_x, r.points[0][1])


def _align_uncentered_siblings(
    routes: list[RoutedPath],
    graph: MetroGraph,
    original_x: dict[str, float],
) -> None:
    """Post-pass: drag unmoved stations to match their centered siblings.

    Groups stations by (section, original_x).  Only drags unmoved stations
    when a clear majority (>50%) of the group already moved to the same X.
    """
    col_groups: dict[tuple[str | None, float], list[str]] = defaultdict(list)
    for sid, s in graph.stations.items():
        if s.is_port or s.is_hidden:
            continue
        ox = original_x.get(sid)
        if ox is None:
            continue
        col_groups[(s.section_id, round(ox, 1))].append(sid)

    routes_by_src: dict[str, list[RoutedPath]] = defaultdict(list)
    routes_by_tgt: dict[str, list[RoutedPath]] = defaultdict(list)
    for rp in routes:
        routes_by_src[rp.edge.source].append(rp)
        routes_by_tgt[rp.edge.target].append(rp)

    for group in col_groups.values():
        if len(group) < 3:
            continue
        moved = [
            sid for sid in group if abs(graph.stations[sid].x - original_x[sid]) > 0.5
        ]
        unmoved = [
            sid for sid in group if abs(graph.stations[sid].x - original_x[sid]) <= 0.5
        ]
        if not moved or not unmoved:
            continue
        if len(moved) <= len(unmoved):
            continue
        moved_xs = [graph.stations[sid].x for sid in moved]
        if max(moved_xs) - min(moved_xs) > 1.0:
            continue
        target_x = sum(moved_xs) / len(moved_xs)
        for sid in unmoved:
            old_x = graph.stations[sid].x
            graph.stations[sid].x = target_x
            for rp in routes_by_src.get(sid, []):
                if abs(rp.points[0][0] - old_x) < 0.5:
                    rp.points[0] = (target_x, rp.points[0][1])
            for rp in routes_by_tgt.get(sid, []):
                if abs(rp.points[-1][0] - old_x) < 0.5:
                    rp.points[-1] = (target_x, rp.points[-1][1])


def _center_bubble_stations(routes: list[RoutedPath], graph: MetroGraph) -> None:
    """Shift diagonals so bubble stations sit centred on their flat segments.

    A "bubble station" branches off the trunk at a different Y, with a
    diagonal on each side.  The fork/join bias in ``_route_diagonal``
    keeps diagonals symmetric at the shared station but leaves the bubble
    station off-centre.  This pass detects such stations and shifts both
    adjacent diagonals by the same amount to equalise the flat runs.

    Runs in three phases:

    1. **Candidate collection** - identifies stations needing centering;
       shifts simple diagonals directly, collects complex cases as
       station-move candidates.
    2. **Station moves** - applies moves only when all column companions
       also want to move (preserving column alignment).
    3. **Sibling alignment** - drags remaining unmoved stations to match
       the majority of their centered column group.
    """
    ctx = _build_bubble_ctx(routes, graph)
    candidates = _collect_centering_candidates(graph, ctx)
    _apply_station_moves(graph, candidates, ctx.original_x)
    _align_uncentered_siblings(routes, graph, ctx.original_x)


# ---------------------------------------------------------------------------
# Utility functions (unchanged)
# ---------------------------------------------------------------------------


def _resolve_section_col(
    graph: MetroGraph,
    station: Station,
    junction_ids: set[str],
) -> int | None:
    """Resolve the grid column for a port or junction station."""
    sec = _resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_col >= 0:
        return sec.grid_col
    return None


def _resolve_section_row(
    graph: MetroGraph,
    station: Station,
    junction_ids: set[str],
) -> int | None:
    """Resolve the grid row for a port or junction station."""
    sec = _resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_row >= 0:
        return sec.grid_row
    return None


def _has_intervening_sections(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    src_row: int | None = None,
) -> bool:
    """Check if any same-row sections exist in columns strictly between src and tgt."""
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
    for s in graph.sections.values():
        if s.bbox_w > 0 and lo < s.grid_col < hi:
            if src_row is None or s.grid_row == src_row:
                return True
    return False


def _compute_bypass_gap_indices(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int, int, int]]:
    """Assign per-gap indices for bypass routes sharing physical gaps.

    Bypass routes from different source columns can share the same
    physical gap (e.g., routes from cols 1->4 and 2->4 both use the
    gap between cols 3 and 4 for their gap2 vertical).  This function
    groups bypass routes by their gap1 and gap2 column pairs and
    assigns per-gap indices so each route gets a unique X offset.

    Returns
    -------
    dict mapping (source_id, target_id, line_id) to
    (gap1_idx, gap1_count, gap2_idx, gap2_count).
    """
    EdgeKey = tuple[str, str, str]
    bypass_edges: list[tuple[EdgeKey, int, int, float]] = []

    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        is_inter = (src.is_port or edge.source in junction_ids) and (
            tgt.is_port or edge.target in junction_ids
        )
        if not is_inter:
            continue

        src_col = _resolve_section_col(graph, src, junction_ids)
        tgt_col = _resolve_section_col(graph, tgt, junction_ids)
        src_row = _resolve_section_row(graph, src, junction_ids)
        if (
            src_col is None
            or tgt_col is None
            or abs(tgt_col - src_col) <= 1
            or not _has_intervening_sections(graph, src_col, tgt_col, src_row)
        ):
            continue

        dx = tgt.x - src.x
        ekey: EdgeKey = (edge.source, edge.target, edge.line_id)
        bypass_edges.append((ekey, src_col, tgt_col, dx))

    gap1_groups: dict[tuple[int, int], list[tuple[EdgeKey, int]]] = defaultdict(list)
    gap2_groups: dict[tuple[int, int], list[tuple[EdgeKey, int, str]]] = defaultdict(
        list
    )

    for ekey, src_col, tgt_col, dx in bypass_edges:
        line_id = ekey[2]
        if dx > 0:
            gap1_pair = (src_col, src_col + 1)
            gap2_pair = (tgt_col - 1, tgt_col)
        else:
            gap1_pair = (src_col - 1, src_col)
            gap2_pair = (tgt_col, tgt_col + 1)
        gap1_groups[gap1_pair].append((ekey, src_col))
        gap2_groups[gap2_pair].append((ekey, src_col, line_id))

    gap1_idx: dict[EdgeKey, tuple[int, int]] = {}
    gap2_idx: dict[EdgeKey, tuple[int, int]] = {}

    for group in gap1_groups.values():
        group.sort(key=lambda x: x[1])
        n = len(group)
        for j, (ek, _) in enumerate(group):
            gap1_idx[ek] = (j, n)

    lp = line_priority or {}
    for group in gap2_groups.values():
        # Sort by line priority so the lowest-offset line (highest
        # priority) gets the outermost vertical channel.  This
        # prevents crossings when lines converge at an entry port
        # from different source columns.
        group.sort(key=lambda x: lp.get(x[2], 0))
        n = len(group)
        for j, (ek, _sc, _lid) in enumerate(group):
            gap2_idx[ek] = (j, n)

    result: dict[EdgeKey, tuple[int, int, int, int]] = {}
    all_keys = set(gap1_idx) | set(gap2_idx)
    for ek in all_keys:
        g1_j, g1_n = gap1_idx.get(ek, (0, 1))
        g2_j, g2_n = gap2_idx.get(ek, (0, 1))
        result[ek] = (g1_j, g1_n, g2_j, g2_n)

    return result


def _compute_junction_fan_info(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int],
) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Unified fan-out positions for junctions with mixed L-shape/bypass edges.

    When a junction fans out to both adjacent-column targets (routed as
    L-shapes) and distant targets (routed as bypasses), all edges of
    the SAME line share a single vertical channel so they travel
    together through the first corner and only diverge afterward.

    Assigns per-LINE positions (not per-edge), so test->preprocess and
    test->normalization share the same (i, n) and thus the same
    vertical channel X.
    """
    result: dict[tuple[str, str, str], tuple[int, int]] = {}

    for jid in junction_ids:
        jst = graph.stations.get(jid)
        if not jst:
            continue
        src_col = _resolve_section_col(graph, jst, junction_ids)
        if src_col is None:
            continue
        src_row = _resolve_section_row(graph, jst, junction_ids)

        # Collect all outgoing inter-section edges
        outgoing: list[Edge] = []
        has_lshape = False
        has_bypass = False
        for edge in graph.edges:
            if edge.source != jid:
                continue
            tgt = graph.stations.get(edge.target)
            if not tgt or not (tgt.is_port or edge.target in junction_ids):
                continue
            tgt_col = _resolve_section_col(graph, tgt, junction_ids)
            if tgt_col is None:
                continue
            is_bypass = abs(tgt_col - src_col) > 1 and _has_intervening_sections(
                graph, src_col, tgt_col, src_row
            )
            if is_bypass:
                has_bypass = True
            else:
                has_lshape = True
            outgoing.append(edge)

        if not outgoing or not has_lshape or not has_bypass:
            continue

        # Assign one position per unique line_id (sorted by priority).
        # All edges of the same line share that position.
        line_ids = sorted(
            {e.line_id for e in outgoing},
            key=lambda lid: line_priority.get(lid, 0),
        )
        line_pos = {lid: i for i, lid in enumerate(line_ids)}
        n = len(line_ids)

        for edge in outgoing:
            result[(edge.source, edge.target, edge.line_id)] = (
                line_pos[edge.line_id],
                n,
            )

    return result
