"""Core edge routing: the main route_edges() dispatcher.

Routes edges as horizontal segments with 45-degree diagonal transitions.
For folded layouts, cross-row edges route through the fold edge with a
clean vertical drop. Inter-section edges use L-shaped routing with
per-line bundle offsets.
"""

from __future__ import annotations

import functools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from nf_metro.layout.constants import (
    BYPASS_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CROSS_ROW_THRESHOLD,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    EDGE_TO_BUNDLE_CLEARANCE,
    FOLD_MARGIN,
    ICON_TERMINUS_FORK_LEAD,
    INTER_ROW_HEADER_CLEARANCE,
    JUNCTION_MARGIN,
    MERGE_ROUTE_MARGIN,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    OFFSET_STEP,
    SECTION_ROUTE_CLEARANCE,
    STATION_MOVE_TOLERANCE,
)
from nf_metro.layout.labels import label_text_width
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
    compute_bundle_info,
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
from nf_metro.layout.routing.corners import (
    bypass_radii,
    corner_radius,
    l_shape_radii,
    reversed_offset,
    tb_entry_corner,
    tb_exit_corner,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Station

# ---------------------------------------------------------------------------
# Routing context: pre-computed state shared by all handlers
# ---------------------------------------------------------------------------


_EdgeKey = tuple[str, str, str]


@dataclass
class _MergeRouting:
    """Pre-computed merge junction routing decisions.

    Consolidates trunk/branch classification, edge skipping, and
    index exclusion so routing handlers can dispatch cleanly.
    """

    junctions: set[str]
    """IDs of merge junction stations."""

    trunk_source: dict[str, str]
    """merge_id -> source station that carries the full bypass trunk."""

    trunk_by: dict[str, float]
    """merge_id -> bypass Y level for the trunk (including nest offset)."""

    entry_port_for: dict[str, str]
    """merge_id -> entry port station ID (pre-resolved)."""

    skip_edges: set[_EdgeKey]
    """Edges not routed at all (trunk covers them)."""

    index_exclude: set[_EdgeKey]
    """Edges excluded from gap/fan indexing but still routed."""


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
    bundle_info: dict[_EdgeKey, tuple[int, int]]
    bypass_gap_idx: dict[_EdgeKey, tuple[int, int, int, int]]
    station_offsets: dict[tuple[str, str], float] | None
    diagonal_run: float
    curve_radius: float
    skip_edges: set[_EdgeKey] = field(default_factory=set)
    junction_fan_info: dict[_EdgeKey, tuple[int, int]] = field(default_factory=dict)
    section_trunk_y: dict[str, float] = field(default_factory=dict)
    merge: _MergeRouting = field(
        default_factory=lambda: _MergeRouting(
            junctions=set(),
            trunk_source={},
            trunk_by={},
            entry_port_for={},
            skip_edges=set(),
            index_exclude=set(),
        )
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
            result = _route_entry_runway(edge, src, tgt, ctx)
        if result is None:
            result = _route_intra_section(edge, src, tgt, ctx)

        if result is not None:
            routes.append(result)

    _center_bubble_stations(routes, graph)
    _spread_diagonal_bundles(routes, ctx)
    _normalize_gap_channels(routes, ctx)
    _join_fanout_upstream_tails(routes, ctx)

    return routes


# ---------------------------------------------------------------------------
# Merge junction classification
# ---------------------------------------------------------------------------


def _classify_merge_edges(
    graph: MetroGraph,
    junction_ids: set[str],
    join_sources: dict[str, set[str]],
    fork_targets: dict[str, set[str]],
) -> _MergeRouting:
    """Classify merge junction edges into trunk, branch, skip, and exclude.

    A merge junction has N>1 predecessors converging on one entry port.
    The farthest bypass predecessor becomes the "trunk" (full U-shape).
    Closer bypass predecessors are "branches" (truncated descents).
    """
    # Detect merge junctions
    junctions: set[str] = set()
    for jid in junction_ids:
        preds = join_sources.get(jid, set())
        succs = fork_targets.get(jid, set())
        if len(preds) > 1 and len(succs) == 1:
            succ_id = next(iter(succs))
            succ_port = graph.ports.get(succ_id)
            if succ_port and succ_port.is_entry:
                junctions.add(jid)

    trunk_source: dict[str, str] = {}
    trunk_by: dict[str, float] = {}
    entry_port_for: dict[str, str] = {}

    def _col_for_id(sid: str) -> int | None:
        st = graph.stations.get(sid)
        if st is None:
            return None
        return _resolve_section_col(graph, st)

    for mjid in junctions:
        mst = graph.stations.get(mjid)
        if not mst:
            continue
        tgt_col = _resolve_section_col(graph, mst)
        if tgt_col is None:
            continue

        # Resolve entry port (successor of merge junction)
        for e in graph.edges_from(mjid):
            ep = graph.ports.get(e.target)
            if ep and ep.is_entry:
                entry_port_for[mjid] = e.target
                break

        # Find farthest bypass predecessor (trunk carrier).  Branches
        # must land on the trunk's own bypass_bottom_y -- the value the
        # trunk's route actually uses -- not the deepest across all
        # preds (which can disagree when the cap-at-midpoint guard in
        # bypass_bottom_y fires for the trunk's span but not a closer
        # branch's).
        farthest_source: str | None = None
        farthest_span = 0
        trunk_pred_by = 0.0
        for edge in graph.edges_to(mjid):
            pred = graph.stations.get(edge.source)
            if not pred:
                continue
            pred_col = _resolve_section_col(graph, pred)
            pred_row = _resolve_section_row(graph, pred)
            if (
                pred_col is not None
                and abs(tgt_col - pred_col) > 1
                and _has_intervening_sections(graph, pred_col, tgt_col, pred_row)
            ):
                span = abs(tgt_col - pred_col)
                by = bypass_bottom_y(
                    graph,
                    pred_col,
                    tgt_col,
                    BYPASS_CLEARANCE,
                    src_row=pred_row,
                )
                if span > farthest_span:
                    farthest_span = span
                    farthest_source = edge.source
                    trunk_pred_by = by
        if farthest_source and trunk_pred_by > 0:
            trunk_source[mjid] = farthest_source
            trunk_by[mjid] = trunk_pred_by

    # Classify edges into skip (not routed) and index_exclude
    # (routed as branches but excluded from gap indexing)
    skip_edges: set[_EdgeKey] = set()
    index_exclude: set[_EdgeKey] = set()

    for mjid, trunk_src in trunk_source.items():
        m_col = _col_for_id(mjid)

        # Check for adjacent JUNCTION predecessors whose stubs need
        # the merge -> entry edge to cross the full gap.  Adjacent
        # PORT predecessors get redirected, so merge -> entry is
        # redundant for them.
        has_adjacent_junction_pred = False
        if m_col is not None:
            for e2 in graph.edges_to(mjid):
                if e2.source == trunk_src or e2.source not in junction_ids:
                    continue
                p_col = _col_for_id(e2.source)
                if p_col is not None and abs(m_col - p_col) <= 1:
                    has_adjacent_junction_pred = True
                    break

        # merge -> entry: skip unless adjacent junction pred needs it
        if not has_adjacent_junction_pred:
            for edge in graph.edges_from(mjid):
                ep = graph.ports.get(edge.target)
                if ep and ep.is_entry:
                    skip_edges.add((edge.source, edge.target, edge.line_id))
        # Non-trunk bypass junction -> merge: exclude from indexing
        # (truncated branches shouldn't occupy bundle slots)
        if m_col is not None:
            for edge in graph.edges_to(mjid):
                if edge.source == trunk_src or edge.source not in junction_ids:
                    continue
                src_col = _col_for_id(edge.source)
                if src_col is not None and abs(m_col - src_col) > 1:
                    index_exclude.add((edge.source, edge.target, edge.line_id))

    return _MergeRouting(
        junctions=junctions,
        trunk_source=trunk_source,
        trunk_by=trunk_by,
        entry_port_for=entry_port_for,
        skip_edges=skip_edges,
        index_exclude=index_exclude,
    )


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
    junction_ids = graph.junction_ids

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

    # Merge routing classification
    merge = _classify_merge_edges(graph, junction_ids, join_sources, fork_targets)

    # Section trunk Ys: the dominant on-track Y per LR/RL section, used
    # to detect side-branch ascents (a below-trunk station feeding the
    # trunk bundle).  Routing such edges with a late diagonal keeps the
    # side-branch line on its own track until the section boundary.
    section_trunk_y = _compute_section_trunk_ys(graph)

    # Bundle assignments and bypass gap indices
    line_priority = {lid: i for i, lid in enumerate(graph.lines.keys())}
    bundle_info = compute_bundle_info(
        graph, junction_ids, line_priority, bottom_exit_junctions
    )
    all_exclude = merge.skip_edges | merge.index_exclude
    bypass_gap_idx = _compute_bypass_gap_indices(
        graph, junction_ids, line_priority, skip_edges=all_exclude
    )
    junction_fan_info = _compute_junction_fan_info(
        graph, junction_ids, line_priority, skip_edges=all_exclude
    )

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
        skip_edges=merge.skip_edges,
        section_trunk_y=section_trunk_y,
        merge=merge,
    )


def _compute_section_trunk_ys(graph: MetroGraph) -> dict[str, float]:
    """Return a mapping ``section_id -> trunk_y`` for LR/RL sections.

    The trunk Y is the Y of the section's exit/entry ports, which
    coincides with the dominant on-track bundle level.  Returns an
    empty mapping for sections without horizontal ports.
    """
    result: dict[str, float] = {}
    for sec_id, section in graph.sections.items():
        if section.direction not in ("LR", "RL"):
            continue
        port_ys: list[float] = []
        for pid in list(section.entry_ports) + list(section.exit_ports):
            port = graph.ports.get(pid)
            if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_st = graph.stations.get(pid)
            if port_st is None:
                continue
            port_ys.append(port_st.y)
        if port_ys:
            # Use the median to ignore outliers; all LR ports in a section
            # typically share the same Y after alignment.
            port_ys.sort()
            result[sec_id] = port_ys[len(port_ys) // 2]
    return result


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

    # Resolve section columns and row for bypass detection
    src_col = _resolve_section_col(graph, src)
    tgt_col = _resolve_section_col(graph, tgt)
    src_row = _resolve_section_row(graph, src)
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
        # Merge dispatch: trunk gets full bypass to entry port,
        # branches get truncated descent to trunk level.
        if edge.target in ctx.merge.trunk_source:
            if ctx.merge.trunk_source[edge.target] == edge.source:
                return _route_merge_trunk(
                    edge, src, tgt, i, src_col, tgt_col, ctx, src_row
                )
            return _route_merge_branch(edge, src, ctx, src_col)
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
        and (src_col_for_special := _resolve_section_col(graph, src)) is not None
        and (tgt_col_for_special := _resolve_section_col(graph, tgt)) is not None
        and src_col_for_special == tgt_col_for_special
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
        and _resolve_section_row(graph, tgt) is not None
        and src_row != _resolve_section_row(graph, tgt)
    ):
        # When the inter-row channel _route_left_entry_wrap would use
        # lands inside an intervening section (e.g. a multi-row jump
        # past a tall middle row), route AROUND BELOW the target
        # section instead.  Otherwise use the standard wrap.  The
        # source section is excluded - the H lead-in just outside the
        # source's right edge is fine even if its Y falls within the
        # source's row.
        wrap_hy = inter_row_channel_y(graph, src, tgt, sy, ty, dy, ctx.curve_radius)
        exclude = {src.section_id} if src.section_id else set()
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
        and _resolve_section_row(graph, tgt) != src_row
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
                exclude = {src.section_id} if src.section_id else set()
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
        curve_radii=[ctx.curve_radius, ctx.curve_radius],
        offsets_applied=True,
    )


def _has_around_section_sibling(
    edge: Edge, ep: Station, ep_port, ctx: _RoutingCtx
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
        other_col = _resolve_section_col(graph, other_src)
        other_row = _resolve_section_row(graph, other_src)
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

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[
            (sx, sy + src_off),
            (corner_x, sy + src_off),
            (corner_x, step_y),
            (chan_x, step_y),
            (chan_x, ey + tgt_off),
            (ex, ey + tgt_off),
        ],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r, r, r, r],
        offsets_applied=True,
    )


def _should_step_descent(
    graph: MetroGraph,
    src: Station,
    ep: Station,
    ep_port,
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
        r_lead = ctx.curve_radius
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
    mid_y = inter_row_channel_y(ctx.graph, src, tgt, sy, ty, dy, ctx.curve_radius)
    hy = mid_y + delta

    # Horizontal lead-in: a short run so the corner from horizontal to
    # vertical gets a proper curve.  When dx is large, the lead-in
    # direction matches dx.  When dx is near-zero (source directly
    # above target), infer direction from the upstream exit port so the
    # line continues with the bundle flow before curving down.
    r_lead = ctx.curve_radius
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
    lx = sx + lead.sign * r_lead
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
            curve_radii=[r_lead, r_second],
        )
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (lx, sy), (lx, hy), (tx, hy), (tx, ty)],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r_lead, r_first, r_second],
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
    src_row = _resolve_section_row(ctx.graph, src)
    ep_row = _resolve_section_row(ctx.graph, entry_port)
    src_col = _resolve_section_col(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    if None in (src_row, ep_row, src_col, ep_col):
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

    src_row = _resolve_section_row(ctx.graph, src)
    src_col = _resolve_section_col(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    ep_row = _resolve_section_row(ctx.graph, entry_port)

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
        gy_base = gap_top + EDGE_TO_BUNDLE_CLEARANCE
    # Outer line sits at LARGER y in this leftward run (CW D->L corner).
    gy = gy_base + delta
    # Keep every staggered line inside the clearance band: at least
    # EDGE_TO_BUNDLE_CLEARANCE below the source-row bottom and clear of the
    # next row's header badge.  In a tight gap the band is narrower than the
    # bundle, so the per-line stagger collapses rather than grazing an edge.
    # Skipped for fan feeders, which share the wrap sibling's (unclamped)
    # band so the two bundles' H legs coincide.
    if fan is None and gap_bottom > gap_top:
        gy = min(
            max(gy, gap_top + EDGE_TO_BUNDLE_CLEARANCE),
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
    src_col = _resolve_section_col(ctx.graph, src)
    ep_col = _resolve_section_col(ctx.graph, entry_port)
    # Fallbacks if a column can't be resolved (degenerate cases).
    bc_src_col = src_col if src_col is not None else 0
    bc_tgt_col = ep_col if ep_col is not None else bc_src_col
    by_base = bypass_bottom_y(
        ctx.graph,
        bc_src_col,
        bc_tgt_col,
        BYPASS_CLEARANCE,
        src_row=_resolve_section_row(ctx.graph, src),
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

    delta, r_first, r_second = l_shape_radii(
        i,
        n,
        vertical=vertical,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
    )

    # Detect cross-row case: use bypass-style Y just below the source
    # row's sections so the line runs horizontally under the adjacent
    # section before dropping to the target row.
    src_row = _resolve_section_row(ctx.graph, src)
    tgt_row = _resolve_section_row(ctx.graph, tgt)
    src_col = _resolve_section_col(ctx.graph, src)
    tgt_col = _resolve_section_col(ctx.graph, tgt)

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

    # Short horizontal lead-in so the first corner (horizontal-to-vertical)
    # gets a smooth curve instead of a sharp right angle at the junction.
    r_lead = ctx.curve_radius
    lx = sx + r_lead
    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (lx, sy), (lx, hy), (vx, hy), (vx, ty), (tx, ty)],
        is_inter_section=True,
        normalize_exempt=True,
        curve_radii=[r_lead, r_first, r_first, r_second],
    )


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
        curve_radii=[
            corner_radius(
                tgt_off,
                max_tgt_off,
                outside=False,
                base_radius=ctx.curve_radius,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Handler 6: Entry-port runway (flow-side entry to deep-layer target)
# ---------------------------------------------------------------------------


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

    # Same Y means target is on the trunk track -- no runway needed.
    if abs(sy - ty) < COORD_TOLERANCE_FINE:
        return None

    # Find the earliest internal station between entry port and target.
    port_ids = section.port_ids
    first_x: float | None = None
    for sid in section.station_ids:
        if sid == edge.target or sid in port_ids:
            continue
        st = graph.stations.get(sid)
        if not st or st.is_port:
            continue
        if section.direction == "LR" and sx < st.x < tx:
            if first_x is None or st.x < first_x:
                first_x = st.x
        elif section.direction == "RL" and tx < st.x < sx:
            if first_x is None or st.x > first_x:
                first_x = st.x

    if first_x is None:
        return None  # No intervening stations -- normal routing is fine.

    # Compute diagonal within the entry-to-first-station region.
    # The source side needs a normal straight run; the runway side
    # needs no clearance since the horizontal run continues past it.
    src_min = ctx.curve_radius + MIN_STRAIGHT_PORT
    tgt_min = 0.0

    room = abs(first_x - sx)
    if room < src_min + ctx.diagonal_run:
        return None  # Too tight -- fall through to default handler.

    diag_start_x, diag_end_x = _compute_diagonal_placement(
        sx,
        first_x,
        ctx.diagonal_run,
        src_min,
        tgt_min,
        edge.source in ctx.fork_stations,
        False,
    )

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (diag_start_x, sy), (diag_end_x, ty), (tx, ty)],
    )


# ---------------------------------------------------------------------------
# Handler 7: Intra-section edges (diagonal transitions, folds, straights)
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
    if src_min + tgt_min + ctx.diagonal_run > abs(dx):
        src_min = min_straight
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
    if tgt.is_hidden and edge.target.startswith("__bypass_"):
        is_fork_flag = False
        is_join_flag = True
        tgt_min = v_flat_half
        if src_min + tgt_min + ctx.diagonal_run > abs(dx):
            tgt_min = MIN_STRAIGHT_EDGE
    elif src.is_hidden and edge.source.startswith("__bypass_"):
        is_fork_flag = True
        is_join_flag = False
        src_min = v_flat_half
        if src_min + tgt_min + ctx.diagonal_run > abs(dx):
            src_min = MIN_STRAIGHT_EDGE

    diag_start_x, diag_end_x = _compute_diagonal_placement(
        sx,
        tx,
        ctx.diagonal_run,
        src_min,
        tgt_min,
        is_fork_flag,
        is_join_flag,
    )

    return RoutedPath(
        edge=edge,
        line_id=edge.line_id,
        points=[(sx, sy), (diag_start_x, sy), (diag_end_x, ty), (tx, ty)],
    )


# ---------------------------------------------------------------------------
# Post-processing: spread bundled diagonal routes
# ---------------------------------------------------------------------------


def _is_diagonal_route(rp: RoutedPath) -> bool:
    """True if *rp* is a 4-point diagonal (horizontal-diagonal-horizontal).

    L-shapes also have 4 points with different Y at indices 1-2, but their
    middle points share the same X (vertical segment).  A true diagonal
    changes both X and Y between points 1 and 2.
    """
    if len(rp.points) != 4:
        return False
    dx = abs(rp.points[1][0] - rp.points[2][0])
    dy = abs(rp.points[1][1] - rp.points[2][1])
    return dx >= COORD_TOLERANCE and dy >= COORD_TOLERANCE_FINE


def _spread_diagonal_bundles(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Translate diagonal start/end X per-line so bundled diagonals spread apart.

    For L-shapes the ``delta`` from :func:`l_shape_radii` translates each
    line's vertical channel X, giving perpendicular separation.  Diagonals
    lack this: all lines share the same ``diag_start_x`` / ``diag_end_x``,
    so the only separation is the Y offset (~2.1 px perpendicular on a 45-
    degree line).  This post-pass adds a complementary X translation derived
    from the per-line Y offset so that bundled diagonals are parallel but
    horizontally spread.
    """
    if ctx.station_offsets is None:
        return

    # Collect diagonal routes grouped by shared fork / join station.
    fork_groups: dict[str, list[RoutedPath]] = defaultdict(list)
    join_groups: dict[str, list[RoutedPath]] = defaultdict(list)

    for rp in routes:
        if not _is_diagonal_route(rp):
            continue
        # Skip bypass V hops: the two legs (P -> V and V -> T) are
        # spread independently and the V-side MIN_STRAIGHT_EDGE bound
        # forces asymmetric clamping, producing a visible kink at V.
        # Bypass V routes are short and the perpendicular separation
        # from per-line Y offsets alone is sufficient for visibility.
        if rp.edge.source.startswith("__bypass_") or rp.edge.target.startswith(
            "__bypass_"
        ):
            continue
        if rp.edge.source in ctx.fork_stations:
            fork_groups[rp.edge.source].append(rp)
        if rp.edge.target in ctx.join_stations:
            join_groups[rp.edge.target].append(rp)

    # Track routes already spread so we don't double-shift a route that
    # appears in both a fork and a join group.
    spread: set[tuple[str, str, str]] = set()

    def _edge_key(rp: RoutedPath) -> tuple[str, str, str]:
        return (rp.edge.source, rp.edge.target, rp.line_id)

    for station_id, group in list(fork_groups.items()) + list(join_groups.items()):
        unseen = [rp for rp in group if _edge_key(rp) not in spread]
        if len(unseen) < 2:
            continue
        # Sub-group by diagonal direction (up vs down) so the scale
        # factor and sign are correct for each route.
        by_dir: dict[bool, list[RoutedPath]] = defaultdict(list)
        for rp in unseen:
            by_dir[rp.points[2][1] >= rp.points[1][1]].append(rp)
        for subgroup in by_dir.values():
            if len(subgroup) >= 2:
                _apply_diagonal_spread(subgroup, station_id, ctx=ctx)
        spread.update(_edge_key(rp) for rp in unseen)


def _apply_diagonal_spread(
    group: list[RoutedPath],
    station_id: str,
    *,
    ctx: _RoutingCtx,
) -> None:
    """Compute and apply per-line X deltas to a diagonal sub-group.

    All routes in *group* share the same diagonal direction (up or down).
    The delta translates both diagonal waypoints (indices 1 and 2) so
    the diagonal segments are parallel but horizontally spread.
    """
    offsets = [ctx.station_offsets.get((station_id, rp.line_id), 0.0) for rp in group]
    center = sum(offsets) / len(offsets)

    rep = group[0]
    dx = rep.points[2][0] - rep.points[1][0]
    dy = rep.points[2][1] - rep.points[1][1]
    sign = 1.0 if dx >= 0 else -1.0
    down_sign = -1.0 if dy > 0 else 1.0

    # On a diagonal at angle theta, Y-only offset gives reduced
    # perpendicular separation (OFFSET_STEP * cos(theta)).  This scale
    # restores the full OFFSET_STEP: (hypot - |dx|) / |dy|.
    # For 45 degrees: sqrt(2) - 1 ~ 0.414.
    hypot = math.hypot(dx, dy)
    abs_dy = abs(dy)
    spread_scale = (hypot - abs(dx)) / abs_dy if abs_dy > COORD_TOLERANCE_FINE else 0.0

    for rp, offset in zip(group, offsets):
        delta = down_sign * (offset - center) * spread_scale * sign

        # Clamp so the horizontal runs don't collapse below minimum.
        bound_src = rp.points[0][0] + sign * MIN_STRAIGHT_EDGE
        bound_tgt = rp.points[3][0] - sign * MIN_STRAIGHT_EDGE
        overshoot = max(
            sign * (bound_src - (rp.points[1][0] + delta)),
            sign * ((rp.points[2][0] + delta) - bound_tgt),
        )
        if overshoot > 0 and abs(delta) > COORD_TOLERANCE_FINE:
            delta *= max(0.0, 1.0 - overshoot / abs(delta))

        rp.points[1] = (rp.points[1][0] + delta, rp.points[1][1])
        rp.points[2] = (rp.points[2][0] + delta, rp.points[2][1])


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
    # True fan-out divergence hubs (matches engine._divergence_target_ys):
    # >= 2 outbound real-station targets at distinct Ys, with at least one
    # above and one below the station's own Y.
    divergence_anchors: set[str]


def _build_bubble_ctx(routes: list[RoutedPath], graph: MetroGraph) -> _BubbleCtx:
    """Build indexes for bubble-station centering."""
    # Imported here to avoid a top-level cycle (engine does not depend on
    # routing, so this one-way import is safe).
    from nf_metro.layout.engine import _divergence_target_ys

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
        if not _is_diagonal_route(rp):
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
        divergence_anchors=_divergence_target_ys(graph),
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

    def _is_chain_predecessor(sid: str) -> bool:
        """Internal upstream station that acts as a flat-chain predecessor.

        When a station being considered for centring has a flat-side
        connection coming FROM ``sid``, this predicate decides whether
        ``sid`` should block centring.  Normal internal stations do
        block it.  A true fan-out divergence hub (matching
        ``engine._divergence_target_ys``: >= 2 outbound real-station
        targets at distinct Ys, with at least one above and one below
        the hub's own Y) is exempt: its flat-side connection to one
        branch is incidental (induced by grid snapping the hub onto
        that branch's track), not a topological chain.  Without this
        exemption the branch's column would fail to centre.

        Exemption applies only to the upstream/source side of a flat
        connection.  Downstream chain predecessors (an anchor sitting
        as the target of a flat connection from the station being
        centred) reflect a natural same-Y chain, not a snap artefact,
        and are still treated as chain-internal.
        """
        if not _is_internal(sid):
            return False
        return sid not in ctx.divergence_anchors

    for sid, station in graph.stations.items():
        if station.is_port:
            continue
        if station.is_hidden and not sid.startswith("__bypass_"):
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

        # Guard: skip when a flat connection goes to/from an internal
        # chain station.  Upstream sources may be fork-hub-exempted (a
        # snap-induced flat from a true divergence anchor does not
        # represent a real chain).  Downstream targets are checked
        # strictly: a same-Y predecessor->successor pair on a downstream
        # internal station is a natural chain regardless of whether the
        # successor happens to be a divergence anchor.
        if has_flat_side or multi_diag:
            flat_to_internal = False
            if flat_in_rp and _is_chain_predecessor(flat_in_rp.edge.source):
                flat_to_internal = True
            if flat_out_rp and _is_internal(flat_out_rp.edge.target):
                flat_to_internal = True
            if multi_diag:
                for r in flat_in:
                    if _is_chain_predecessor(r.edge.source):
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

        # Guard: don't shift in convergence/divergence bundles.  Bypass
        # V helpers have no marker so the convergence-guard doesn't apply.
        is_bypass_v = sid.startswith("__bypass_")
        if not is_bypass_v:
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
        # Hidden bypass V helpers have no marker, so column alignment
        # with visible companions isn't a visible concern - centre them
        # without requiring companion consensus.
        skip_companion_check = sid.startswith("__bypass_")
        if not skip_companion_check and abs(new_x - station.x) > STATION_MOVE_TOLERANCE:
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
            sid
            for sid in group
            if abs(graph.stations[sid].x - original_x[sid]) > STATION_MOVE_TOLERANCE
        ]
        unmoved = [
            sid
            for sid in group
            if abs(graph.stations[sid].x - original_x[sid]) <= STATION_MOVE_TOLERANCE
        ]
        if not moved:
            continue

        moved_xs = [graph.stations[sid].x for sid in moved]
        if max(moved_xs) - min(moved_xs) > 1.0:
            # Moved stations disagree on target X.  Find the majority
            # position and treat outliers as needing alignment too.
            rounded = [round(x, 1) for x in moved_xs]
            ((majority_x, majority_count),) = Counter(rounded).most_common(1)
            if majority_count <= len(moved) / 2:
                continue  # no clear majority, skip
            outliers = [
                sid
                for sid, x in zip(moved, moved_xs)
                if abs(round(x, 1) - majority_x) > 1.0
            ]
            if not outliers:
                continue
            unmoved = unmoved + outliers
            target_x = majority_x
        else:
            if not unmoved:
                continue
            if len(moved) <= len(unmoved):
                continue
            target_x = sum(moved_xs) / len(moved_xs)

        for sid in unmoved:
            old_x = graph.stations[sid].x
            graph.stations[sid].x = target_x
            for rp in routes_by_src.get(sid, []):
                if abs(rp.points[0][0] - old_x) < STATION_MOVE_TOLERANCE:
                    rp.points[0] = (target_x, rp.points[0][1])
            for rp in routes_by_tgt.get(sid, []):
                if abs(rp.points[-1][0] - old_x) < STATION_MOVE_TOLERANCE:
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


def _resolve_section_col(graph: MetroGraph, station: Station) -> int | None:
    """Resolve the grid column for a port or junction station."""
    sec = resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_col >= 0:
        return sec.grid_col
    return None


def _resolve_section_row(graph: MetroGraph, station: Station) -> int | None:
    """Resolve the grid row for a port or junction station."""
    sec = resolve_section(graph, station, prefer_upstream=False)
    if sec and sec.grid_row >= 0:
        return sec.grid_row
    return None


@dataclass
class _VChannel:
    """One vertical channel segment of a routed inter-section path.

    Records the route, the segment's start index in ``route.points`` (so
    ``points[idx]`` and ``points[idx+1]`` are the channel endpoints), its
    current x, vertical span and direction, plus the indices of any
    flanking corners in ``route.curve_radii`` and whether each corner is
    on the OUTSIDE of its turn for this line (recomputed after re-stack).
    """

    route: RoutedPath
    idx: int
    x: float
    y_lo: float
    y_hi: float
    down: bool


def _collect_vchannels(routes: list[RoutedPath]) -> list[_VChannel]:
    """Find every vertical channel segment in inter-section routes."""
    out: list[_VChannel] = []
    for rp in routes:
        if not rp.is_inter_section or rp.normalize_exempt:
            continue
        pts = rp.points
        for k in range(len(pts) - 1):
            x0, y0 = pts[k]
            x1, y1 = pts[k + 1]
            if abs(x1 - x0) < COORD_TOLERANCE and abs(y1 - y0) > COORD_TOLERANCE:
                out.append(
                    _VChannel(
                        route=rp,
                        idx=k,
                        x=x0,
                        y_lo=min(y0, y1),
                        y_hi=max(y0, y1),
                        down=y1 > y0,
                    )
                )
    return out


def _build_gap_intervals(
    graph: MetroGraph,
) -> dict[int | None, list[tuple[int, float, float]]]:
    """Per-row list of ``(lo_col, gap_left, gap_right)`` for adjacent columns.

    The row key is the grid row; a single combined ``None`` entry is also
    produced (row-agnostic union) as a fallback for channels whose row
    can't be matched precisely.
    """
    cols = sorted({s.grid_col for s in graph.sections.values() if s.bbox_w > 0})
    rows = sorted({s.grid_row for s in graph.sections.values() if s.bbox_w > 0})
    intervals: dict[int | None, list[tuple[int, float, float]]] = {}
    for row in list(rows) + [None]:
        per_row: list[tuple[int, float, float]] = []
        for lo, hi in zip(cols, cols[1:]):
            if hi != lo + 1:
                continue
            left, right = column_gap_edges(graph, lo, hi, row=row)
            if right > left:
                per_row.append((lo, left, right))
        intervals[row] = per_row
    return intervals


def _normalize_gap_channels(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Re-bundle inter-section vertical channels sharing a gap + direction.

    Post-routing pass that enforces the uniform inter-section gap geometry
    regardless of which handler placed each leg:

    * All same-direction channels sharing one inter-column gap collapse
      into ONE concentric bundle, ``OFFSET_STEP`` apart, centred.
    * A downward bundle and an upward bundle sharing a gap are held
      ``BUNDLE_TO_BUNDLE_CLEARANCE`` (B) apart, centred as a group.
    * A lone bundle centres in its gap with at least
      ``EDGE_TO_BUNDLE_CLEARANCE`` (A) from each bounding section edge.

    Only channels whose current x already lands inside a real inter-column
    gap are touched, so wrap / around-section legs that deliberately sit
    outside the immediate gap are left alone.  Corner radii flanking each
    re-stacked channel are recomputed so the bundle stays concentric.
    """
    graph = ctx.graph
    step = ctx.offset_step
    channels = _collect_vchannels(routes)
    if not channels:
        return
    gap_intervals = _build_gap_intervals(graph)

    # Per-row vertical band (top/bottom Y) so a channel can be matched to
    # the row whose gap it actually travels in, not merely the first row
    # whose x-interval brackets it.  Two channels in the same column gap
    # but different grid rows (e.g. a row-0 fan and a row-1 bypass) must
    # NOT be merged into one bundle: each centres on its own row's gap.
    row_bands: dict[int, tuple[float, float]] = {}
    for s in graph.sections.values():
        if s.bbox_h <= 0:
            continue
        for r in range(s.grid_row, s.grid_row + max(1, s.grid_row_span)):
            top, bot = row_bands.get(r, (s.bbox_y, s.bbox_y + s.bbox_h))
            row_bands[r] = (min(top, s.bbox_y), max(bot, s.bbox_y + s.bbox_h))

    def _find_gap(ch: _VChannel) -> tuple[int, int | None, float, float] | None:
        """Match a channel to ``(lo_col, row, gap_left, gap_right)``.

        Prefer the row whose x-interval brackets the channel AND whose
        vertical band the channel overlaps; fall back to any bracketing
        row, then to the row-agnostic union.

        A channel that vertically crosses several rows must clear sections
        in ALL of them, so its gap is narrowed to the intersection of every
        crossed row's gap in the same column.  Otherwise a fan climbing out
        of a row whose section edge sits further out than a sibling row's
        would centre in the wider sibling gap and step back behind its source
        section edge (#386).
        """
        x = ch.x
        overlap_match: tuple[int, int | None, float, float] | None = None
        bracket_match: tuple[int, int | None, float, float] | None = None
        for row in gap_intervals:
            if row is None:
                continue
            for lo, left, right in gap_intervals[row]:
                if not (left - COORD_TOLERANCE <= x <= right + COORD_TOLERANCE):
                    continue
                if bracket_match is None:
                    bracket_match = (lo, row, left, right)
                band = row_bands.get(row)
                if band is not None and ch.y_lo < band[1] and band[0] < ch.y_hi:
                    if overlap_match is None:
                        overlap_match = (lo, row, left, right)
        match = overlap_match or bracket_match
        if match is not None:
            lo, row, left, right = match
            for r, band in row_bands.items():
                if not (ch.y_lo < band[1] and band[0] < ch.y_hi):
                    continue
                for rlo, rleft, rright in gap_intervals.get(r, []):
                    if rlo == lo:
                        left = max(left, rleft)
                        right = min(right, rright)
            return (lo, row, left, right)
        for lo, left, right in gap_intervals.get(None, []):
            if left - COORD_TOLERANCE <= x <= right + COORD_TOLERANCE:
                return (lo, None, left, right)
        return None

    # Bucket channels per (gap lo_col, row, direction).  A channel is only
    # a candidate when its x lands strictly inside the gap interior (so a
    # near-vertical drop hugging a section edge is left untouched).
    buckets: dict[tuple[int, int | None, bool], list[_VChannel]] = defaultdict(list)
    gap_bounds: dict[tuple[int, int | None], tuple[float, float]] = {}
    for ch in channels:
        gap = _find_gap(ch)
        if gap is None:
            continue
        lo, row, left, right = gap
        if not (left + COORD_TOLERANCE < ch.x < right - COORD_TOLERANCE):
            # x sits on / outside a section edge: not a clean gap channel.
            if not (left <= ch.x <= right):
                continue
        # Bundles sharing a (gap, row) are laid out together in one x-range,
        # so the shared bound must clear every member's crossed rows: narrow
        # to the intersection rather than letting the last channel win.
        prev = gap_bounds.get((lo, row))
        if prev is not None:
            left = max(left, prev[0])
            right = min(right, prev[1])
        gap_bounds[(lo, row)] = (left, right)
        buckets[(lo, row, ch.down)].append(ch)

    # Within a (gap, direction) bucket, split into corridors by vertical
    # overlap: only channels whose y-spans overlap share a true corridor
    # (independent vertical runs at different heights must NOT be merged).
    def _corridors(chans: list[_VChannel]) -> list[list[_VChannel]]:
        chans = sorted(chans, key=lambda c: (c.y_lo, c.y_hi))
        groups: list[list[_VChannel]] = []
        for ch in chans:
            placed = False
            for g in groups:
                if any(
                    ch.y_lo < o.y_hi - COORD_TOLERANCE
                    and o.y_lo < ch.y_hi - COORD_TOLERANCE
                    for o in g
                ):
                    g.append(ch)
                    placed = True
                    break
            if not placed:
                groups.append([ch])
        return groups

    # Assemble bundles per (gap, row): one per corridor, both directions,
    # laid out together so a down/up pair sharing a gap is B-separated.
    by_gap: dict[tuple[int, int | None], list[tuple[bool, list[_VChannel]]]]
    by_gap = defaultdict(list)
    for (lo, row, down), chans in buckets.items():
        for corridor in _corridors(chans):
            by_gap[(lo, row)].append((down, corridor))

    # Intrusion guard (row-agnostic): a re-stacked channel must never land
    # inside any section's bbox.
    def _intrudes(x: float, y_lo: float, y_hi: float) -> bool:
        for s in graph.sections.values():
            if s.bbox_w <= 0:
                continue
            sx_l = s.bbox_x
            sx_r = s.bbox_x + s.bbox_w
            if sx_l - COORD_TOLERANCE < x < sx_r + COORD_TOLERANCE:
                sy_t = s.bbox_y
                sy_b = s.bbox_y + s.bbox_h
                if y_lo < sy_b and sy_t < y_hi:
                    return True
        return False

    for (lo, _row), bundles in by_gap.items():
        # Stable left-to-right order: by current bundle centre.
        bundles.sort(key=lambda b: sum(c.x for c in b[1]) / len(b[1]))
        # Distinct-line count per bundle drives the bundle width and the
        # per-line slotting: multiple segments sharing one line_id (a fan
        # whose line feeds several targets) overlay at a single x rather
        # than each claiming an OFFSET_STEP slot.
        line_orders = [_distinct_line_order(c) for _, c in bundles]
        # Skip a lone bundle carrying a single distinct line: nothing to
        # re-bundle and centring risks disturbing wrap geometry.
        if len(bundles) == 1 and len(line_orders[0]) <= 1:
            continue
        gap_left, gap_right = gap_bounds[(lo, _row)]
        widths = [max(0, len(o) - 1) * step for o in line_orders]
        # A lone bundle centres on the true gap midpoint (symmetric
        # clearance both sides) rather than flooring one edge at A, which
        # would push the bundle off-centre when the gap is sized tighter
        # than 2A + width.  Multi-bundle gaps keep the symmetric A/B
        # layout from symmetric_bundle_midpoint.
        lone = len(bundles) == 1
        for bi, (down, chans) in enumerate(bundles):
            order = line_orders[bi]
            if lone:
                mid = (gap_left + gap_right) / 2
            else:
                mid = symmetric_bundle_midpoint(gap_left, gap_right, widths, bi)
            n = len(order)
            # line_id -> (slot index, x); every segment of that line overlays
            # at its single slot rather than claiming an OFFSET_STEP each.
            line_slot = {
                lid: (i, mid + (i - (n - 1) / 2) * step) for i, lid in enumerate(order)
            }
            targets = [(ch, line_slot[ch.route.line_id]) for ch in chans]
            # Intrusion guard: if any target x would land inside a section
            # bbox (e.g. the gap bounds came from another row), leave this
            # bundle untouched rather than route through a section.
            if any(_intrudes(nx, ch.y_lo, ch.y_hi) for ch, (_li, nx) in targets):
                continue
            for ch, (li, nx) in targets:
                _restack_channel(ch, nx, li, n, step, ctx.curve_radius)


def _join_fanout_upstream_tails(routes: list[RoutedPath], ctx: _RoutingCtx) -> None:
    """Snap each fan-out junction's upstream tail onto its downstream start.

    At a *fan-out* junction (single upstream source, one or more
    inter-section targets), the incoming ``port -> junction`` route and
    the outgoing ``junction -> target`` route are two separate
    :class:`RoutedPath`\\ s.  Their handoff points at the junction don't
    coincide: the downstream route carries the per-line bundle offset
    (and, for L-shape fans, a curve lead-in that starts a ``curve_radius``
    past the junction), while the upstream route ends at the bare junction
    coordinate.  The mismatch renders as a seam / notch where the two
    segments meet end-to-end instead of one continuous flowing line.

    This pass extends the upstream route's final, horizontal segment so
    it ends at the X of the paired (same ``line_id``) downstream route's
    first waypoint -- closing the horizontal "bite" at the apex that
    otherwise shows as a notch (the downstream L-shape lead-in starts a
    ``curve_radius`` PAST the junction, leaving a gap along the line's
    travel direction between the upstream tail end and the downstream
    curve start).

    The upstream tail's Y is kept unchanged: when the downstream start
    carries a per-line bundle ``offset`` (the inner concentric-corner
    member), the residual PERPENDICULAR offset between the extended
    upstream end and the downstream start is sub-line-width and hidden
    under the stroke.  Lifting the upstream Y to match would either tilt
    the approach or step it, reintroducing a visible kink at the apex, so
    only the X is extended.  Only the upstream tail is moved; the
    downstream geometry is left untouched.

    Gated to genuine single-upstream-source fan-out junctions.  Merge
    junctions (>1 distinct upstream source) are excluded so their trunk
    routing, which intentionally lands branches on a shared bypass Y, is
    never perturbed.
    """
    from nf_metro.layout.routing.invariants import (
        _fanout_route_maps,
        fanout_junctions,
    )

    fanouts = fanout_junctions(ctx.graph)
    if not fanouts:
        return

    upstream, downstream = _fanout_route_maps(routes, fanouts)
    for (jid, line_id), up in upstream.items():
        down = downstream.get((jid, line_id))
        if down is None or len(up.points) < 2:
            continue
        p_prev, p_last = up.points[-2], up.points[-1]
        # Only a genuinely-horizontal final segment is extended; extend
        # its X to the downstream start X, keeping the upstream Y so the
        # approach into the bend stays horizontal.
        if abs(p_prev[1] - p_last[1]) <= COORD_TOLERANCE_FINE:
            up.points[-1] = (down.points[0][0], p_last[1])


def _distinct_line_order(chans: list[_VChannel]) -> list[str]:
    """Left-to-right order of the distinct lines in one gap-bundle corridor.

    Channels sharing a ``line_id`` collapse to a single slot, so the order
    is over distinct lines.  The ordering minimises crossings between each
    line's vertical leg and the others' horizontal lead-outs.

    A line's vertical leg spans the gap from the shared trunk level (near
    the junction) down to its deepest turn-off; each channel segment turns
    off horizontally at its deep endpoint (``y_hi``).  For a DOWN bundle
    that lead-out extends RIGHTWARD toward the target; for an UP bundle the
    lead-in extends LEFTWARD from the source.  When line A sits LEFT of B:

    * DOWN: B's (right-placed, deeper) vertical crosses each A lead-out that
      turns off shallower than B's deepest point.
    * UP: A's (left-placed, deeper) vertical crosses each B lead-in (which
      extends left under A) that attaches shallower than A's deepest point.

    The pairwise comparator picks, for each pair, the side incurring fewer
    crossings; ties keep the incoming x order.  This places a deep bypass
    before a shallow neighbour (variant_calling: qc before main) yet still
    puts a shallow long-reach line before a deeper multi-target fan when
    that strictly reduces crossings (genomeassembly: hic before assemblies),
    and mirrors the rule for UP bundles (subworkflows: the deeper
    preprocess_reporting sits to the RIGHT).
    """
    down = chans[0].down if chans else True

    # Per line: the deep turn-off depths of each segment (always y_hi), the
    # deepest reach, and a representative x for stable tie-breaking.
    turns: dict[str, list[float]] = defaultdict(list)
    deepest: dict[str, float] = {}
    rep_x: dict[str, float] = {}
    for ch in chans:
        lid = ch.route.line_id
        turns[lid].append(ch.y_hi)
        deepest[lid] = max(deepest.get(lid, ch.y_hi), ch.y_hi)
        rep_x[lid] = min(rep_x.get(lid, ch.x), ch.x)

    def crossings_if_left(a: str, b: str) -> int:
        # Number of crossings when a is placed LEFT of b.
        if down:
            # b's deeper vertical crosses a's shallower right-going lead-outs.
            return sum(1 for t in turns[a] if t < deepest[b] - COORD_TOLERANCE)
        # UP: a's deeper vertical crosses b's shallower left-going lead-ins.
        return sum(1 for t in turns[b] if t < deepest[a] - COORD_TOLERANCE)

    def cmp(a: str, b: str) -> int:
        ca = crossings_if_left(a, b)  # a left of b
        cb = crossings_if_left(b, a)  # b left of a
        if ca != cb:
            return -1 if ca < cb else 1
        if rep_x[a] != rep_x[b]:
            return -1 if rep_x[a] < rep_x[b] else 1
        return -1 if a < b else (1 if a > b else 0)

    return sorted(turns, key=functools.cmp_to_key(cmp))


def _restack_channel(
    ch: _VChannel,
    new_x: float,
    i: int,
    n: int,
    step: float,
    base_radius: float,
) -> None:
    """Move one vertical channel to *new_x* and recompute its corner radii.

    Shifts the channel's two endpoints (which share x) to *new_x*; the
    flanking horizontal segments stretch.  The re-stacked channel behaves
    exactly like line *i* of an *n*-line standard L-shape, so its two
    flanking corner radii come straight from :func:`l_shape_radii`, which
    encodes the concentric (outermost-line-largest-on-the-outside)
    geometry for both the down- and up-going cases.

    ``l_shape_radii`` assigns ``i = 0`` to the rightmost (DOWN) / leftmost
    (UP) line; the bundle here is ordered left-to-right with ``i`` growing
    rightward, so the index is mapped accordingly.
    """
    rp = ch.route
    pts = rp.points
    k = ch.idx
    pts[k] = (new_x, pts[k][1])
    pts[k + 1] = (new_x, pts[k + 1][1])

    if rp.curve_radii is None:
        return
    vertical = Direction.D if ch.down else Direction.U
    # Map left-to-right index to l_shape_radii's convention.
    li = (n - 1 - i) if ch.down else i
    _, r_first, r_second = l_shape_radii(
        li, n, vertical=vertical, offset_step=step, base_radius=base_radius
    )
    # Lead corner radius lives at curve_radii[k-1]; trail at curve_radii[k].
    if 0 <= k - 1 < len(rp.curve_radii):
        rp.curve_radii[k - 1] = r_first
    if k < len(rp.curve_radii) and k + 2 < len(pts):
        rp.curve_radii[k] = r_second


def _gap_channel_base(
    graph: MetroGraph,
    lo: int,
    row: int | None,
    n: int,
    offset_step: float,
) -> float:
    """Centred midline x for a bundle of *n* lines in gap ``(lo, lo+1)``.

    This is only the initial placement during routing; the post-routing
    :func:`_normalize_gap_channels` pass re-stacks every inter-section
    channel into its final centred / B-separated position, so the value
    here just needs to land the channel in the right gap.
    """
    gap_left, gap_right = column_gap_edges(graph, lo, lo + 1, row=row)
    return symmetric_bundle_midpoint(
        gap_left, gap_right, [max(0, n - 1) * offset_step], 0
    )


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


def _has_other_row_section_in_col_range(
    graph: MetroGraph,
    src_col: int,
    tgt_col: int,
    src_row: int,
) -> bool:
    """Check if a section in a row OTHER than *src_row* sits anywhere in the
    column range ``[min(src_col, tgt_col), max(src_col, tgt_col)]``.

    Used by :func:`_route_merge_trunk` to decide whether the standard
    same-row bypass channel would visually collide with another row's
    section title text.  When no such other-row section exists in the
    column range, the standard channel sits in empty inter-row space
    and there is nothing to push the trunk further down for - so the
    historical ``cross_row=False`` placement is preferred.
    """
    lo, hi = min(src_col, tgt_col), max(src_col, tgt_col)
    for s in graph.sections.values():
        if s.bbox_w <= 0 or s.grid_row == src_row:
            continue
        if lo <= s.grid_col <= hi:
            return True
    return False


def _h_segment_crosses_other_section(
    graph: MetroGraph,
    x1: float,
    x2: float,
    y: float,
    exclude_section_ids: set[str],
    margin: float = 0.0,
) -> bool:
    """Return True if a horizontal segment at *y* crosses any section interior.

    Sections listed in *exclude_section_ids* are skipped entirely.  All
    other sections are tested against the segment's open interior.  The
    horizontal segment runs from ``min(x1, x2)`` to ``max(x1, x2)``.

    A section is "crossed" when the segment overlaps its bbox's open
    interior - i.e. the segment penetrates the section rather than just
    grazing its boundary.  ``y`` is considered inside when it falls
    within ``[bbox_y - margin, bbox_y + bbox_h + margin]``.
    """
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    for s in graph.sections.values():
        if s.bbox_w <= 0:
            continue
        if s.id in exclude_section_ids:
            continue
        # Strict X interior overlap: segment must enter past the bbox
        # left edge AND not end before reaching past the right edge.
        right = s.bbox_x + s.bbox_w
        if hi_x <= s.bbox_x or lo_x >= right:
            continue
        # Y inside bbox (with optional margin so headers/footers count).
        if s.bbox_y - margin <= y <= s.bbox_y + s.bbox_h + margin:
            return True
    return False


def _compute_bypass_gap_indices(
    graph: MetroGraph,
    junction_ids: set[str],
    line_priority: dict[str, int] | None = None,
    skip_edges: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int, int, int]]:
    """Assign per-gap indices for bypass routes sharing physical gaps.

    Bypass routes from different source columns can share the same
    physical gap (e.g., routes from cols 1->4 and 2->4 both use the
    gap between cols 3 and 4 for their gap2 vertical).  This function
    groups bypass routes by their gap1 and gap2 column pairs and
    assigns per-gap indices so each route gets a unique X offset.

    Edges in *skip_edges* are excluded so truncated merge branches
    don't create gaps in the bundle.

    Returns
    -------
    dict mapping (source_id, target_id, line_id) to
    (gap1_idx, gap1_count, gap2_idx, gap2_count).
    """
    _skip = skip_edges or set()
    EdgeKey = tuple[str, str, str]
    bypass_edges: list[tuple[EdgeKey, int, int, float]] = []

    for edge in graph.edges:
        ekey: EdgeKey = (edge.source, edge.target, edge.line_id)
        if ekey in _skip:
            continue

        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        is_inter = (src.is_port or edge.source in junction_ids) and (
            tgt.is_port or edge.target in junction_ids
        )
        if not is_inter:
            continue

        src_col = _resolve_section_col(graph, src)
        tgt_col = _resolve_section_col(graph, tgt)
        src_row = _resolve_section_row(graph, src)
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
    skip_edges: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Unified fan-out positions for junctions with mixed-direction edges.

    When a junction fans out to targets that would otherwise be routed
    by DIFFERENT inter-section handlers (L-shape, bypass, LEFT/RIGHT
    entry wrap), all edges of the SAME line share a single vertical
    channel so they travel together through the first corner and only
    diverge afterward.

    Assigns per-LINE positions (not per-edge), so test->preprocess and
    test->normalization share the same (i, n) and thus the same
    vertical channel X.

    Edges in *skip_edges* are excluded so that truncated merge branches
    don't create gaps in the bundle.
    """
    _skip = skip_edges or set()
    result: dict[tuple[str, str, str], tuple[int, int]] = {}

    for jid in junction_ids:
        jst = graph.stations.get(jid)
        if not jst:
            continue
        src_col = _resolve_section_col(graph, jst)
        if src_col is None:
            continue
        src_row = _resolve_section_row(graph, jst)

        # Classify each outgoing edge into one of: L-shape (adjacent col,
        # no intervening sections), bypass (column gap with intervening),
        # or wrap (LEFT/RIGHT entry port reached from the opposite side,
        # going via the inter-row channel).  A junction qualifies for a
        # unified shared-first-corner fan when its outgoing edges use at
        # least two of these three handlers, since each handler would
        # otherwise pick a different source-side channel X.
        #
        # Category detection uses ALL outgoing inter-section edges
        # (including merge-branch edges that get skipped from the
        # per-line position assignment).  Otherwise a junction whose
        # bypass siblings are all routed as merge branches would lose
        # its bypass category and the wrap-only / L-only fallback would
        # leave the wrap routes free to pick a different corner X than
        # the bypass routes.
        outgoing: list[Edge] = []
        has_lshape = False
        has_bypass = False
        has_wrap = False
        for edge in graph.edges_from(jid):
            tgt = graph.stations.get(edge.target)
            if not tgt or not (tgt.is_port or edge.target in junction_ids):
                continue
            tgt_col = _resolve_section_col(graph, tgt)
            if tgt_col is None:
                continue
            tgt_row = _resolve_section_row(graph, tgt)
            tgt_port = graph.ports.get(edge.target)
            is_bypass = abs(tgt_col - src_col) > 1 and _has_intervening_sections(
                graph, src_col, tgt_col, src_row
            )
            dx_edge = tgt.x - jst.x
            is_wrap = (
                tgt_port is not None
                and tgt_port.is_entry
                and not is_bypass
                and src_row is not None
                and tgt_row is not None
                and src_row != tgt_row
                and (
                    (tgt_port.side == PortSide.RIGHT and dx_edge > 0)
                    or (tgt_port.side == PortSide.LEFT and dx_edge < 0)
                )
            )
            if is_bypass:
                has_bypass = True
            elif is_wrap:
                has_wrap = True
            else:
                has_lshape = True
            if (edge.source, edge.target, edge.line_id) not in _skip:
                outgoing.append(edge)

        # Unified fan-out only when handlers would otherwise pick
        # different source-side channel Xs:
        #   * lshape + bypass: the legacy condition.
        #   * wrap + anything else: extends the same idea to wrap routes.
        # Pure lshape-only or pure bypass-only junctions don't need the
        # shared first corner - the single active handler picks one
        # corridor on its own.  Widening this predicate further (e.g.
        # `category_count >= 2`) is a no-op for pure-X junctions but
        # was observed to perturb simple gallery fixtures, so keep the
        # condition explicit.
        needs_unified = (has_lshape and has_bypass) or (
            has_wrap and (has_lshape or has_bypass)
        )
        if not outgoing or not needs_unified:
            continue

        # Assign one position per unique line_id (sorted by priority).
        # All edges of the same line share that position, including
        # edges previously SKIPPED for the bundle.  This makes a merge
        # branch route share the same source-side fan position as a
        # sibling wrap/L-shape route, so all of them pivot through the
        # same first corner X (their geometries diverge afterward).
        all_outgoing = [
            e
            for e in graph.edges_from(jid)
            if (es := graph.stations.get(e.target)) is not None
            and (es.is_port or e.target in junction_ids)
        ]
        line_ids = sorted(
            {e.line_id for e in all_outgoing},
            key=lambda lid: line_priority.get(lid, 0),
        )
        line_pos = {lid: i for i, lid in enumerate(line_ids)}
        n = len(line_ids)

        for edge in all_outgoing:
            if edge.line_id in line_pos:
                result[(edge.source, edge.target, edge.line_id)] = (
                    line_pos[edge.line_id],
                    n,
                )

    return result
