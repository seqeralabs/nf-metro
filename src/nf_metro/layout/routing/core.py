"""Core edge routing: the main route_edges() dispatcher.

Routes edges as horizontal segments with 45-degree diagonal transitions.
The per-handler families and post-routing passes live in sibling modules
(context, *_handlers, normalize, postprocess) and are re-exported here for
backward-compatible ``routing.core`` imports.
"""

from __future__ import annotations

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_RUN,
)
from nf_metro.layout.routing.common import (
    RoutedPath,
)
from nf_metro.layout.routing.context import (  # noqa: F401
    _build_routing_context,
    _classify_merge_edges,
    _compute_bypass_gap_indices,
    _compute_junction_fan_info,
    _compute_section_trunk_ys,
    _EdgeKey,
    _get_offset,
    _has_intervening_sections,
    _max_offset_at,
    _MergeRouting,
    _resolve_section_col,
    _resolve_section_colrow,
    _resolve_section_row,
    _RoutingCtx,
    _tb_x_offset,
    compute_junction_fan_info,
)
from nf_metro.layout.routing.inter_section_handlers import (  # noqa: F401
    _build_right_entry_wrap_route,
    _corridor_descent_x,
    _corridor_is_viable,
    _fan_has_corridor_sibling,
    _fan_left_entry_descent_x,
    _has_around_section_sibling,
    _has_bypass_sibling_to_same_entry,
    _left_entry_descent_x,
    _right_entry_gap_above_is_clear,
    _right_entry_gap_above_target_y,
    _route_around_section_below,
    _route_bottom_exit_junction,
    _route_bypass,
    _route_inter_row_gap_corridor,
    _route_inter_section,
    _route_l_shape,
    _route_left_entry_wrap,
    _route_left_exit_left_entry_drop,
    _route_merge_branch,
    _route_merge_trunk,
    _route_right_entry_around_below,
    _route_right_entry_via_gap_above,
    _route_right_entry_wrap,
    _route_tb_bottom_exit,
    _route_top_entry_l_shape,
    _v1_corner_x,
)
from nf_metro.layout.routing.intra_handlers import (  # noqa: F401
    _is_side_branch_ascent,
    _route_diagonal,
    _route_entry_runway,
    _route_intra_section,
)
from nf_metro.layout.routing.normalize import (  # noqa: F401
    _band_order_crossings,
    _build_gap_intervals,
    _clamp_inter_row_band_top,
    _clear_channel_x_in_band,
    _coincide_convergent_port_approaches,
    _coincide_divergent_fanout_descents,
    _coincident_trunk_slots,
    _collect_htrunks,
    _collect_vchannels,
    _distinct_line_order,
    _dogleg_off_exempt_trunks,
    _final_port_approach,
    _gap_channel_base,
    _group_channel_trunks,
    _h_segment_crosses_other_section,
    _has_other_row_section_in_col_range,
    _HTrunk,
    _inter_row_gap_band,
    _join_fanout_upstream_tails,
    _normalize_bypass_trunks,
    _normalize_gap_channels,
    _plan_trunk_band,
    _port_peeloff_tail,
    _reorder_convergence_peeloff,
    _restack_channel,
    _restack_htrunk,
    _restack_trunk_band,
    _set_vchannel_x,
    _suboptimal_trunk_bands,
    _VChannel,
)
from nf_metro.layout.routing.postprocess import (  # noqa: F401
    _align_uncentered_siblings,
    _apply_diagonal_spread,
    _apply_station_moves,
    _BubbleCtx,
    _build_bubble_ctx,
    _center_bubble_stations,
    _clear_bypass_v_label_strikes,
    _collect_centering_candidates,
    _is_diagonal_route,
    _spread_diagonal_bundles,
    _StationMoveCandidate,
)
from nf_metro.layout.routing.tb_handlers import (  # noqa: F401
    _compute_diagonal_placement,
    _perp_entry_drop_delta,
    _route_perp_entry,
    _route_tb_diagonal,
    _route_tb_internal,
    _route_tb_lr_entry,
    _route_tb_lr_exit,
    _route_tb_section,
)
from nf_metro.parser.model import (
    LineSpread,
    MetroGraph,
)


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
    if graph.line_spread is LineSpread.RAILS:
        from nf_metro.layout.routing.rail import route_rail_edges

        return route_rail_edges(graph)

    # Per-section rail mode: route each rail section's internal edges with the
    # dedicated rail router (straight rails, no bundling) and let the normal
    # router handle every other edge.  An edge is "internal" to a rail section
    # when both endpoints are non-port stations of that section.
    rail_routes: list[RoutedPath] = []
    rail_internal: set[tuple[str, str, str]] = set()
    if graph.has_rail_sections:
        from nf_metro.layout.routing.rail import route_rail_edges

        rail_edges = []
        for edge in graph.edges:
            src = graph.stations.get(edge.source)
            tgt = graph.stations.get(edge.target)
            if src is None or tgt is None or src.is_port or tgt.is_port:
                continue
            if (
                src.section_id == tgt.section_id
                and src.section_id is not None
                and graph.is_rail_section(src.section_id)
            ):
                rail_edges.append(edge)
                rail_internal.add((edge.source, edge.target, edge.line_id))
        rail_routes = route_rail_edges(graph, rail_edges)

    ctx = _build_routing_context(graph, diagonal_run, curve_radius, station_offsets)
    routes: list[RoutedPath] = list(rail_routes)

    for edge in graph.edges:
        if (edge.source, edge.target, edge.line_id) in ctx.skip_edges:
            continue
        if (edge.source, edge.target, edge.line_id) in rail_internal:
            continue

        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue

        # Try each routing handler in priority order.
        # The first handler that returns a RoutedPath wins.
        result = _route_inter_section(edge, src, tgt, ctx)
        if result is None:
            result = _route_tb_section(edge, src, tgt, ctx)
        if result is None:
            result = _route_entry_runway(edge, src, tgt, ctx)
        if result is None:
            result = _route_intra_section(edge, src, tgt, ctx)

        if result is not None:
            routes.append(result)

    _center_bubble_stations(routes, graph)
    _spread_diagonal_bundles(routes, ctx)
    _normalize_gap_channels(routes, ctx)
    _normalize_bypass_trunks(routes, ctx)
    _reorder_convergence_peeloff(routes, ctx)
    _coincide_convergent_port_approaches(routes)
    _coincide_divergent_fanout_descents(routes)
    _join_fanout_upstream_tails(routes, ctx)
    _clear_bypass_v_label_strikes(routes, ctx)

    return routes
