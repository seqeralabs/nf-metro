"""Core edge routing: the main route_edges() dispatcher.

Routes edges as horizontal segments with 45-degree diagonal transitions.
For folded layouts, cross-row edges route through the fold edge with a
clean vertical drop. Inter-section edges use L-shaped routing with
per-line bundle offsets.
"""

from __future__ import annotations

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
    FOLD_MARGIN,
    HEADER_CLEARANCE,
    JUNCTION_MARGIN,
    MERGE_ROUTE_MARGIN,
    MIN_STATION_FLAT_LENGTH,
    MIN_STRAIGHT_EDGE,
    MIN_STRAIGHT_PORT,
    OFFSET_STEP,
    STATION_MOVE_TOLERANCE,
)
from nf_metro.layout.labels import label_text_width
from nf_metro.layout.routing.common import (
    RoutedPath,
    adjacent_column_gap_x,
    bypass_bottom_y,
    col_left_edge,
    col_right_edge,
    compute_bundle_info,
    inter_column_channel_x,
    row_bottom_edge,
    row_top_edge,
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

    for mjid in junctions:
        mst = graph.stations.get(mjid)
        if not mst:
            continue
        tgt_col = _resolve_section_col(graph, mst, junction_ids)
        if tgt_col is None:
            continue

        # Resolve entry port (successor of merge junction)
        for e in graph.edges:
            if e.source == mjid:
                ep = graph.ports.get(e.target)
                if ep and ep.is_entry:
                    entry_port_for[mjid] = e.target
                    break

        # Find farthest bypass predecessor (trunk carrier)
        farthest_source: str | None = None
        farthest_span = 0
        deepest_by = 0.0
        for edge in graph.edges:
            if edge.target != mjid:
                continue
            pred = graph.stations.get(edge.source)
            if not pred:
                continue
            pred_col = _resolve_section_col(graph, pred, junction_ids)
            pred_row = _resolve_section_row(graph, pred, junction_ids)
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
                if by > deepest_by:
                    deepest_by = by
                if span > farthest_span:
                    farthest_span = span
                    farthest_source = edge.source
        if farthest_source and deepest_by > 0:
            trunk_source[mjid] = farthest_source
            trunk_by[mjid] = deepest_by

    # Classify edges into skip (not routed) and index_exclude
    # (routed as branches but excluded from gap indexing)
    skip_edges: set[_EdgeKey] = set()
    index_exclude: set[_EdgeKey] = set()

    for mjid, trunk_src in trunk_source.items():
        m_col = _resolve_section_col(
            graph,
            graph.stations.get(mjid),  # type: ignore[arg-type]
            junction_ids,
        )

        # Check for adjacent JUNCTION predecessors whose stubs need
        # the merge -> entry edge to cross the full gap.  Adjacent
        # PORT predecessors get redirected, so merge -> entry is
        # redundant for them.
        has_adjacent_junction_pred = False
        if m_col is not None:
            for e2 in graph.edges:
                if e2.target != mjid or e2.source == trunk_src:
                    continue
                p = graph.stations.get(e2.source)
                if not p or e2.source not in junction_ids:
                    continue
                p_col = _resolve_section_col(graph, p, junction_ids)
                if p_col is not None and abs(m_col - p_col) <= 1:
                    has_adjacent_junction_pred = True
                    break

        for edge in graph.edges:
            # merge -> entry: skip unless adjacent junction pred needs it
            if edge.source == mjid and not has_adjacent_junction_pred:
                ep = graph.ports.get(edge.target)
                if ep and ep.is_entry:
                    skip_edges.add((edge.source, edge.target, edge.line_id))
            # Non-trunk bypass junction -> merge: exclude from indexing
            # (truncated branches shouldn't occupy bundle slots)
            if (
                edge.target == mjid
                and edge.source != trunk_src
                and edge.source in junction_ids
                and m_col is not None
            ):
                src_col = _resolve_section_col(
                    graph,
                    graph.stations.get(edge.source),  # type: ignore[arg-type]
                    junction_ids,
                )
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
        # Merge dispatch: trunk gets full bypass to entry port,
        # branches get truncated descent to trunk level.
        if edge.target in ctx.merge.trunk_source:
            if ctx.merge.trunk_source[edge.target] == edge.source:
                return _route_merge_trunk(
                    edge, src, tgt, i, src_col, tgt_col, ctx, src_row
                )
            return _route_merge_branch(edge, src, ctx, src_col, tgt_col)
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
    tgt_col: int,
) -> RoutedPath:
    """Truncated L-shape descent from a junction to the trunk level.

    Routes a 4-point path: horizontal lead-in, curve down, vertical
    drop, curve into trunk direction.  The lead-in is positioned at
    MERGE_ROUTE_MARGIN from the source section edge.
    """
    sx, sy = src.x, src.y
    dx = ctx.graph.stations[edge.target].x - sx
    trunk_dir = 1.0 if dx > 0 else -1.0
    src_off = _get_offset(ctx, edge.source, edge.line_id)

    # Trunk bypass Y level (branches drop to meet it)
    by = ctx.merge.trunk_by.get(edge.target, sy)

    # Position descent at MERGE_ROUTE_MARGIN from section edge
    if dx > 0:
        lead_x = col_right_edge(ctx.graph, src_col) + MERGE_ROUTE_MARGIN
    else:
        lead_x = col_left_edge(ctx.graph, src_col) - MERGE_ROUTE_MARGIN
    # Clamp to at least curve_radius from the junction
    min_lead = sx + trunk_dir * ctx.curve_radius
    if trunk_dir > 0:
        lead_x = max(lead_x, min_lead)
    else:
        lead_x = min(lead_x, min_lead)
    tail_x = lead_x + trunk_dir * ctx.curve_radius * 2

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
    entry.
    """
    ep_id = ctx.merge.entry_port_for.get(edge.target)
    ep = ctx.graph.stations.get(ep_id) if ep_id else None
    effective_tx = ep.x if ep else tgt.x
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
) -> RoutedPath:
    """U-shaped bypass route around intervening sections.

    When *effective_tx* is provided, it overrides the target X for
    gap2 limit computation (used by merge trunks to reach the entry
    port instead of the merge junction).
    """
    sx, sy = src.x, src.y
    tx, ty = tgt.x, tgt.y
    if effective_tx is None:
        effective_tx = tx
    dx = tx - sx
    going_right = dx > 0
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
    tgt_row = _resolve_section_row(graph, tgt, ctx.junction_ids)
    cross_row = src_row is not None and tgt_row is not None and src_row != tgt_row
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
    gap1_going_down = base_y > sy
    gap2_going_down = ty > base_y

    # Radii and per-line deltas via the same l_shape_radii logic used
    # for all other concentric corners.
    delta1, delta2, r1, _, r3, r4 = bypass_radii(
        g1_j,
        g1_n,
        g2_j,
        g2_n,
        going_right=going_right,
        offset_step=ctx.offset_step,
        base_radius=ctx.curve_radius,
        gap1_going_down=gap1_going_down,
        gap2_going_down=gap2_going_down,
    )
    by = base_y + nest_offset

    # Override r2 so all trunk horizontals begin at the same X
    # (gap1_x + r2 = constant across all lines in the bundle).
    r2 = corner_radius(
        nest_offset,
        (g2_n - 1) * ctx.offset_step,
        outside=gap1_going_down,
        base_radius=ctx.curve_radius,
    )

    # Gap channel centers and per-line positions.
    base_bypass_offset = ctx.curve_radius + ctx.offset_step
    half_g1 = (g1_n - 1) * ctx.offset_step / 2
    half_g2 = (g2_n - 1) * ctx.offset_step / 2

    if going_right:
        if fan is not None:
            # Corner 1 uses unified fan indices for a shared first corner
            # with L-shape siblings.  Override delta1 and r1.
            ui, un = fan
            fan_delta, r1, _ = l_shape_radii(
                ui,
                un,
                going_down=gap1_going_down,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            fan_mid_x = sx + ctx.curve_radius + (un - 1) * ctx.offset_step / 2
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = (
                adjacent_column_gap_x(graph, src_col, src_col + 1) - base_bypass_offset
            )
            gap1_limit = sx + ctx.curve_radius
            if gap1_base - (g1_n - 1) * ctx.offset_step < gap1_limit:
                gap1_mid = gap1_limit + half_g1
            else:
                gap1_mid = gap1_base - half_g1
            gap1_x = gap1_mid + delta1

        gap2_base = (
            adjacent_column_gap_x(graph, tgt_col - 1, tgt_col) + base_bypass_offset
        )
        gap2_limit = effective_tx - ctx.curve_radius
        if gap2_base + (g2_n - 1) * ctx.offset_step > gap2_limit:
            gap2_mid = gap2_limit - half_g2
        else:
            gap2_mid = gap2_base + half_g2
        gap2_x = gap2_mid + delta2
    else:
        if fan is not None:
            ui, un = fan
            fan_delta, r1, _ = l_shape_radii(
                ui,
                un,
                going_down=gap1_going_down,
                offset_step=ctx.offset_step,
                base_radius=ctx.curve_radius,
            )
            fan_mid_x = sx - ctx.curve_radius - (un - 1) * ctx.offset_step / 2
            gap1_x = fan_mid_x + fan_delta
        else:
            gap1_base = (
                adjacent_column_gap_x(graph, src_col - 1, src_col) + base_bypass_offset
            )
            gap1_limit = sx - ctx.curve_radius
            if gap1_base + (g1_n - 1) * ctx.offset_step > gap1_limit:
                gap1_mid = gap1_limit - half_g1
            else:
                gap1_mid = gap1_base + half_g1
            gap1_x = gap1_mid + delta1

        gap2_base = (
            adjacent_column_gap_x(graph, tgt_col, tgt_col + 1) - base_bypass_offset
        )
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
    port_ids = set(section.entry_ports) | set(section.exit_ports)
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
    skip_edges: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Unified fan-out positions for junctions with mixed L-shape/bypass edges.

    When a junction fans out to both adjacent-column targets (routed as
    L-shapes) and distant targets (routed as bypasses), all edges of
    the SAME line share a single vertical channel so they travel
    together through the first corner and only diverge afterward.

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
        src_col = _resolve_section_col(graph, jst, junction_ids)
        if src_col is None:
            continue
        src_row = _resolve_section_row(graph, jst, junction_ids)

        # Collect all outgoing inter-section edges (excluding skipped)
        outgoing: list[Edge] = []
        has_lshape = False
        has_bypass = False
        for edge in graph.edges:
            if edge.source != jid:
                continue
            if (edge.source, edge.target, edge.line_id) in _skip:
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
