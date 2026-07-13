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
    _gap_above_target_y,
    _has_around_section_sibling,
    _has_bypass_sibling_to_same_entry,
    _left_entry_descent_x,
    _right_entry_gap_above_is_clear,
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
    _bundle_divergent_distinct_descents,
    _bundle_divergent_distinct_traverses,
    _clamp_inter_row_band_top,
    _clear_channel_x_in_band,
    _coincide_fanout_opening_descents,
    _coincide_merge_fanout_pivots,
    _coincide_same_line_tracks,
    _coincident_trunk_slots,
    _collect_htrunks,
    _distinct_line_order,
    _dogleg_off_exempt_trunks,
    _final_port_approach,
    _gap_channel_base,
    _group_channel_trunks,
    _h_segment_crosses_other_section,
    _HTrunk,
    _inter_row_gap_band,
    _materialize_gap_slots,
    _materialize_trunk_slots,
    _nest_bypass_above_over_top_wrap,
    _plan_trunk_band,
    _reconcile_port_peeloff_risers,
    _restack_channel,
    _restack_htrunk,
    _restack_trunk_band,
    _round_junction_perp_peeloff,
    _set_vchannel_x,
    _stagger_convergent_distinct_lines,
    _suboptimal_trunk_bands,
    _unify_coincident_corner_radii,
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


def _route_edges(
    graph: MetroGraph,
    diagonal_run: float,
    curve_radius: float,
    station_offsets: dict[tuple[str, str], float] | None,
) -> tuple[list[RoutedPath], dict[str, float]]:
    """Route all edges, returning the paths and the bubble-centring moves.

    Shared body behind :func:`route_edges` (pure) and
    :func:`route_edges_centred` (applies the moves).  The ``moves`` are the
    per-station X-targets the bubble-centring pass produced as ``{station_id:
    x}`` requests; the route points are adjusted in place either way.
    """
    if graph.line_spread is LineSpread.RAILS:
        from nf_metro.layout.routing.rail import route_rail_edges

        return route_rail_edges(graph), {}

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
            src, tgt = graph.edge_endpoints(edge)
            if src.is_port or tgt.is_port:
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
    # Route into the context's own list so handlers can read the routes settled
    # so far (a wrap clearing an already-placed sibling channel); it grows as
    # edges route and is what every post-loop pass consumes.
    routes: list[RoutedPath] = ctx.built_routes
    routes.extend(rail_routes)

    for edge in graph.edges:
        if (edge.source, edge.target, edge.line_id) in ctx.skip_edges:
            continue
        if (edge.source, edge.target, edge.line_id) in rail_internal:
            continue

        src, tgt = graph.edge_endpoints(edge)

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

    moves = _center_bubble_stations(routes, graph)
    _spread_diagonal_bundles(routes, ctx)
    _materialize_gap_slots(routes, ctx)
    _materialize_trunk_slots(routes, ctx)
    # Re-stack peel-off risers against the settled trunk depths, so each rises
    # on the concentric slot its post-repack depth earns.
    _reconcile_port_peeloff_risers(routes, ctx)
    # A merge fan-out's branches leave one fork and turn off its lead-out
    # through a first corner each; fuse those corners onto one shared pivot
    # column so the fork opens as one stroke, before the same-line coincidence
    # pass reads the settled channels.
    _coincide_merge_fanout_pivots(routes, ctx)
    # Coincidence runs after the trunk/gap channels are finalised: it snaps
    # same-line tracks onto a reference read from that final geometry (the
    # port-side track, the source-side track, the merge trunk's descent, and
    # the fan-out junction handoff tail), so a single line reads as one stroke.
    _coincide_same_line_tracks(routes, ctx)
    # Settle every fan-out's opening-descent column in one pass: fuse each line's
    # same-source descents onto the source-nearest track and nest the distinct
    # lines one step apart until each turns off.  Runs after the coincidence pass
    # so a perpendicular drop already resolved onto the junction column stays
    # clear of an L-shaped sibling diverging to another column.
    _coincide_fanout_opening_descents(routes, ctx)
    # Distinct lines fanning out share the corridor they turn onto; nest their
    # traverses one step apart so the bundle holds a constant width until each
    # line peels off, rather than running on independently-sized bands.
    _bundle_divergent_distinct_traverses(routes, ctx)
    # A perpendicular branch dropped directly off a horizontal fan-out junction
    # trunk peels off at a hard 90; give its departure a lead-in so the corner
    # curves. Runs after coincidence settles the drop's port column.
    _round_junction_perp_peeloff(routes, ctx)
    # Distinct-line counterpart: spread any two different lines whose final port
    # descents were forced onto one channel (a shared gap left of a wide target).
    _stagger_convergent_distinct_lines(routes, ctx)
    # A same-row over-top wrap to a RIGHT entry is pinned deep in the inter-row
    # gap by the target's header clearance; lift any longer-haul cross-row bypass
    # sharing that gap above the wrap's peak so the local wrap nests beneath it.
    _nest_bypass_above_over_top_wrap(routes, ctx)
    _clear_bypass_v_label_strikes(routes, ctx)
    # Same-line legs a coincidence pass fused onto one channel each kept their
    # handler's corner radius; unify every turn they share so the fused stroke
    # draws one arc rather than concentric duplicates.
    _unify_coincident_corner_radii(routes)

    return routes, moves


def route_edges(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
) -> list[RoutedPath]:
    """Route all edges with smooth direction changes.

    Detects cross-row edges (large Y gap relative to X gap) and routes
    them through a vertical connector at the fold edge.

    Routing is pure with respect to placement: it never moves stations.  The
    bubble-centring pass emits its per-station X-targets as move requests,
    which this entry point discards; :func:`route_edges_centred` is the variant
    that applies them.  Callers get a route they can inspect without perturbing
    ``graph.stations``.
    """
    routes, _moves = _route_edges(graph, diagonal_run, curve_radius, station_offsets)
    return routes


def route_edges_centred(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
) -> list[RoutedPath]:
    """Route, then settle the bubble-centred markers onto ``graph.stations``.

    The drawn variant of :func:`route_edges`: it applies the centring move
    requests so any reader of marker / label geometry after routing (the SVG
    render, the label-overlap spacing search, the render-output strike guards)
    sees the markers on their centred flats.  Unlike :func:`route_edges` this
    is *not* placement-pure -- it is the single named home for that mutation.

    Inside a ``_restoring_layout_geometry`` scope the move is undone on exit,
    so a probe can inspect the drawn geometry without perturbing the settled
    layout.  Bisection / placement guards that must read the *un-centred*
    placement geometry call :func:`route_edges` directly instead.
    """
    routes, moves = _route_edges(graph, diagonal_run, curve_radius, station_offsets)
    for sid, x in moves.items():
        graph.stations[sid].x = x
    return routes
