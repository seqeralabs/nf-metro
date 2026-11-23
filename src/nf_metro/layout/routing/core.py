"""Core edge routing: the main route_edges() coordinator.

Routes edges as horizontal segments with 45-degree diagonal transitions.
The per-handler families and post-routing passes live in sibling modules
(context, *_handlers, normalize, postprocess) and are re-exported here as the
module's public routing surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_RUN,
)
from nf_metro.layout.route_plan import RouteSystemId
from nf_metro.layout.route_reservations import reservation_ids_by_claimant_member
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
from nf_metro.layout.routing.families import RouteFamilyId
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
    classify_inter_section_family,
)
from nf_metro.layout.routing.intra_handlers import (  # noqa: F401
    _is_side_branch_ascent,
    _route_diagonal,
    _route_entry_runway,
    _route_intra_section,
)
from nf_metro.layout.routing.normalize import (  # noqa: F401
    _align_merge_fed_confluence_to_band,
    _band_order_crossings,
    _bundle_divergent_distinct_descents,
    _bundle_divergent_distinct_traverses,
    _clamp_inter_row_band_top,
    _clear_channel_x_in_band,
    _clear_merge_trunk_opposite_arm,
    _coincide_fanout_opening_descents,
    _coincide_merge_fanout_pivots,
    _coincide_same_line_fanout_traverses,
    _coincide_same_line_tracks,
    _coincident_trunk_slots,
    _collect_htrunks,
    _distinct_line_order,
    _dogleg_off_exempt_trunks,
    _final_port_approach,
    _gap_channel_base,
    _group_channel_trunks,
    _h_segment_crosses_other_section,
    _hold_runs_in_corridor_clearance,
    _HTrunk,
    _inter_row_gap_band,
    _materialize_gap_slots,
    _materialize_trunk_slots,
    _nest_bypass_above_over_top_wrap,
    _plan_trunk_band,
    _reconcile_port_peeloff_risers,
    _rederive_semantic_end_corners,
    _restack_channel,
    _restack_htrunk,
    _restack_trunk_band,
    _separate_declared_opposing_gap_bundles,
    _separate_fused_cotravelling_runs,
    _separate_opposing_inter_row_trunks,
    _set_vchannel_x,
    _settle_same_destination_approach_bundles,
    _stagger_convergent_distinct_lines,
    _suboptimal_trunk_bands,
    _unify_coincident_corner_radii,
    _VChannel,
)
from nf_metro.layout.routing.planning import prepare_route_system_planning
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
from nf_metro.layout.routing.reserved_bands import build_reserved_corridors
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
from nf_metro.layout.settlement_demand import BoundaryClearanceRequirement
from nf_metro.parser.model import (
    Edge,
    LineSpread,
    MetroGraph,
)

if TYPE_CHECKING:
    from nf_metro.layout.route_plan import (
        RouteMemberGeometryPlan,
        RouteObservation,
        RoutePlan,
        RoutePlanObserver,
    )
    from nf_metro.layout.route_reservations import ReservationCoordinateTranslation


def _published_boundary_clearance_owner_ids(
    inherited_owner_ids: frozenset[str],
    planned_system_ids: frozenset[RouteSystemId],
    requirements: tuple[BoundaryClearanceRequirement, ...],
) -> frozenset[str]:
    """Publish granted owners that survive this planning generation."""
    planned_owner_ids = frozenset(str(system_id) for system_id in planned_system_ids)
    new_owner_ids = frozenset(requirement.owner_id for requirement in requirements)
    return (inherited_owner_ids & planned_owner_ids) | new_owner_ids


def _route_classified_edge(
    edge: Edge,
    ctx: _RoutingCtx,
    *,
    observer: RoutePlanObserver | None = None,
    family_id: RouteFamilyId | None = None,
) -> RoutedPath | None:
    """Build one preclassified inter-section member or a local edge."""
    source, target = ctx.graph.edge_endpoints(edge)
    selected_family = family_id or classify_inter_section_family(
        edge, source, target, ctx
    )
    if selected_family is not None:
        return _route_inter_section(
            edge,
            source,
            target,
            ctx,
            observer=observer,
            planned_family_id=selected_family,
        )
    for handler in (_route_tb_section, _route_entry_runway, _route_intra_section):
        route = handler(edge, source, target, ctx)
        if route is None:
            continue
        return route
    return None


def _route_edges(  # noqa: C901
    graph: MetroGraph,
    diagonal_run: float,
    curve_radius: float,
    station_offsets: dict[tuple[str, str], float] | None,
    *,
    observe_plan: bool,
    offset_step: float | None = None,
    reservations: RoutePlan | None = None,
    reservation_translations: tuple[ReservationCoordinateTranslation, ...] = (),
    validate_final_route_frames: bool = True,
    allow_convergence_clearance_requirements: bool = False,
) -> tuple[list[RoutedPath], dict[str, float], RoutePlan | None]:
    """Route all edges, returning the paths and the bubble-centring moves.

    Shared body behind :func:`route_edges` (pure) and
    :func:`route_edges_centred` (applies the moves).  The ``moves`` are the
    per-station X-targets the bubble-centring pass produced as ``{station_id:
    x}`` requests; the route points are adjusted in place either way.
    """
    observer: RoutePlanObserver | None
    if graph.line_spread is LineSpread.RAILS:
        from nf_metro.layout.route_plan import (
            build_route_plan_observer,
            build_route_semantic_scaffold,
        )
        from nf_metro.layout.routing.rail import route_rail_edges
        from nf_metro.layout.routing.system_emission import (
            build_route_system_emission_execution,
            validate_route_system_emission,
        )

        fan_execution = graph.fan_plan_execution
        scaffold = fan_execution.scaffold if fan_execution is not None else None
        if scaffold is None:
            scaffold = build_route_semantic_scaffold(
                graph,
                coupled_connector_groups=tuple(
                    plan.connector_ids for plan in graph.fan_plans if plan.connector_ids
                ),
            )
        rail_execution = (
            None
            if scaffold is None
            else build_route_system_emission_execution(
                scaffold,
                exit_turn_plans=(),
                fan_plans=(),
                convergence_plans=(),
                family_by_edge={
                    edge: RouteFamilyId.RAIL_INTER_SECTION
                    for edge in scaffold.edge_order
                },
            )
        )
        rail_graph_routes = route_rail_edges(graph)
        if rail_execution is not None:
            for route in rail_graph_routes:
                rail_execution.attribute_route(route)
            validate_route_system_emission(rail_graph_routes, rail_execution)
        observer = None
        if observe_plan:
            observer = build_route_plan_observer(
                graph,
                None,
                scaffold=scaffold,
                route_systems=rail_execution,
            )
        if observer is not None:
            observer.record_rail_routes(rail_graph_routes)
        plan = observer.finish(rail_graph_routes) if observer is not None else None
        if plan is not None:
            from nf_metro.layout.routing.system_emission import (
                validate_published_route_attribution,
            )

            validate_published_route_attribution(rail_graph_routes, plan)
        return (
            rail_graph_routes,
            {},
            plan,
        )

    # Per-section rail mode: route each rail section's own edges with the
    # dedicated rail router (straight rails, no bundling) and let the normal
    # router handle every other edge.  An edge belongs to a rail section when
    # both endpoints sit in it and at most one of them is a boundary port: a
    # port-to-station leg carries the fan between the section's single gateway
    # and the rails, so the rail router owns it too; a port-to-port leg crosses
    # between sections and stays with the normal router.
    rail_routes: list[RoutedPath] = []
    rail_internal: set[tuple[str, str, str]] = set()
    if graph.has_rail_sections:
        from nf_metro.layout.routing.rail import route_rail_edges

        rail_edges = []
        for edge in graph.edges:
            src, tgt = graph.edge_endpoints(edge)
            if src.is_port and tgt.is_port:
                continue
            if (
                src.section_id == tgt.section_id
                and src.section_id is not None
                and graph.is_rail_section(src.section_id)
            ):
                rail_edges.append(edge)
                rail_internal.add((edge.source, edge.target, edge.line_id))
        rail_routes = route_rail_edges(graph, rail_edges, station_offsets)

    ctx = _build_routing_context(
        graph,
        diagonal_run,
        curve_radius,
        station_offsets,
        offset_step=offset_step,
        validate_final_route_frames=validate_final_route_frames,
        reserved_bands=(
            None
            if reservations is None
            else build_reserved_corridors(graph, reservations, reservation_translations)
        ),
        prior_exit_turn_dispositions=(
            None if reservations is None else dict(reservations.exit_turn_dispositions)
        ),
    )
    from nf_metro.layout.route_plan import build_route_plan_observer
    from nf_metro.layout.routing.member_geometry import (
        fresh_member_route,
        validate_member_geometry_emission,
    )

    reservation_ids_by_member = (
        None
        if reservations is None
        else reservation_ids_by_claimant_member(reservations.reservations)
    )
    boundary_clearance_owner_ids = (
        frozenset()
        if reservations is None
        else frozenset(reservations.boundary_clearance_owner_ids)
    )
    planning = prepare_route_system_planning(
        graph,
        ctx,
        include_convergence_resources=observe_plan,
        reservation_ids_by_member=reservation_ids_by_member,
        allow_convergence_clearance_requirements=(
            allow_convergence_clearance_requirements
        ),
        granted_clearance_owner_ids=boundary_clearance_owner_ids,
    )
    execution = planning.exit_turns
    member_geometry = planning.member_geometry
    convergence_execution = planning.convergences
    system_execution = planning.route_systems
    planned_system_ids = planning.planned_system_ids
    observer = None
    if observe_plan:
        planned_owner_ids = frozenset(
            str(system_id) for system_id in planned_system_ids
        )
        published_member_geometry = tuple(
            plan
            for plan in member_geometry.plans
            if plan.system_id in planned_system_ids
        )
        published_member_requirements = tuple(
            requirement
            for requirement in member_geometry.clearance_requirements
            if requirement.owner_id in planned_owner_ids
        )
        published_owner_ids = _published_boundary_clearance_owner_ids(
            boundary_clearance_owner_ids,
            planned_system_ids,
            published_member_requirements,
        )
        observer = build_route_plan_observer(
            graph,
            ctx,
            scaffold=execution.scaffold,
            exit_turn_plans=execution.plans,
            exit_turn_references=execution.references,
            exit_turn_demands=execution.demands,
            exit_turn_diagnostics=execution.diagnostics,
            convergence_plans=convergence_execution.plans,
            convergence_references=tuple(
                reference
                for reference in convergence_execution.references
                if reference.system_id in planned_system_ids
            ),
            convergence_demands=tuple(
                demand
                for demand in convergence_execution.demands
                if demand.system_id in planned_system_ids
            ),
            convergence_diagnostics=convergence_execution.diagnostics,
            boundary_clearance_requirements=(
                *convergence_execution.clearance_requirements,
                *published_member_requirements,
            ),
            boundary_clearance_owner_ids=published_owner_ids,
            member_geometry_plans=published_member_geometry,
            exit_turn_dispositions=planning.exit_turn_dispositions,
        )
    # Route into the context's own list so handlers can read the routes settled
    # so far (a wrap clearing an already-placed sibling channel); it grows as
    # edges route and is what every post-loop pass consumes.
    routes: list[RoutedPath] = ctx.built_routes
    routes.extend(rail_routes)

    def emit_edge(
        edge: Edge,
        planned_family_id: RouteFamilyId | None = None,
        geometry_plan: RouteMemberGeometryPlan | None = None,
    ) -> None:
        if (
            edge.source,
            edge.target,
            edge.line_id,
        ) in ctx.skip_edges:
            if observer is not None:
                observer.record_merge_skip(
                    (edge.source, edge.target, edge.line_id),
                    observer.covering_edge((edge.source, edge.target, edge.line_id)),
                )
            return
        planned_covering_edge = (
            ctx.convergences.covering_edge_for_edge(edge)
            if ctx.convergences is not None
            else None
        )
        if planned_covering_edge is not None:
            if observer is not None:
                observer.record_merge_skip(
                    (edge.source, edge.target, edge.line_id),
                    (
                        planned_covering_edge.source,
                        planned_covering_edge.target,
                        planned_covering_edge.line_id,
                    ),
                )
            return
        if (edge.source, edge.target, edge.line_id) in rail_internal:
            return

        edge_key = (edge.source, edge.target, edge.line_id)

        result = (
            fresh_member_route(geometry_plan, edge)
            if geometry_plan is not None
            else _route_classified_edge(
                edge,
                ctx,
                observer=observer,
                family_id=planned_family_id,
            )
        )
        if geometry_plan is not None and observer is not None:
            observer.record_dispatch(edge_key, geometry_plan.family_id)

        if result is not None:
            member_geometry.apply_semantic_corner_template(result)
            if system_execution is not None:
                system_execution.attribute_route(result)
            routes.append(result)

    emitted_systems: set[RouteSystemId] = set()
    next_system_rank = 0
    for edge in graph.edges:
        system = (
            system_execution.system_for_edge(edge)
            if system_execution is not None
            else None
        )
        if system is None:
            emit_edge(edge)
            continue
        system_key = system.system_id
        if system_key in emitted_systems:
            continue
        assert system_execution is not None
        expected = system_execution.systems[next_system_rank]
        if system.system_id != expected.system_id:
            # Edge order and the semantic component order are derived
            # independently; this is the only place they are tied together.
            raise RuntimeError(
                f"route system at emission rank {next_system_rank} is "
                f"{system.system_id}, but canonical order names "
                f"{expected.system_id}"
            )
        for member in system.members:
            member_edge = ctx.edge_by_key[
                (member.edge.source, member.edge.target, member.edge.line_id)
            ]
            family_id = member.family_id
            emit_edge(
                member_edge,
                family_id,
                (member.geometry_plan),
            )
        emitted_systems.add(system_key)
        next_system_rank += 1

    if system_execution is not None and next_system_rank != len(
        system_execution.systems
    ):
        raise RuntimeError(
            f"canonical route-system emission covered {next_system_rank} of "
            f"{len(system_execution.systems)} systems"
        )

    from nf_metro.layout.routing.exit_turns import (
        assert_exit_turn_snapshot,
        snapshot_exit_turn_segments,
        validate_exit_turn_plans,
    )

    emitted_exit_turn_plans = (
        tuple(
            plan
            for plan in ctx.exit_turns.plans
            if plan.system_id in planned_system_ids
        )
        if ctx.exit_turns is not None
        else ()
    )
    planned_segments = snapshot_exit_turn_segments(routes, emitted_exit_turn_plans)
    moves = _center_bubble_stations(routes, graph)
    assert_exit_turn_snapshot(routes, planned_segments, "bubble centring")
    _spread_diagonal_bundles(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "diagonal spreading")
    _materialize_gap_slots(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "gap-slot materialization")
    _materialize_trunk_slots(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "trunk-slot materialization")
    # Counter-running flows that entered one inter-row gap from opposite rows
    # sit in different dip groups the trunk-slot pass never compares, so they
    # both centre on the gap and fold over each other; split them onto their
    # own direction-specific bands before the downstream coincidence passes read
    # the settled channels.
    _separate_opposing_inter_row_trunks(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "opposing-trunk separation")
    # Re-stack peel-off risers against the settled trunk depths, so each rises
    # on the concentric slot its post-repack depth earns.
    _reconcile_port_peeloff_risers(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "port-peeloff reconciliation")
    # Peel-off reconciliation can transpose riser order, so symmetric
    # divergences must be joined against those final columns.
    _separate_declared_opposing_gap_bundles(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "opposing-gap separation")
    # A merge fan-out's branches leave one fork and turn off its lead-out
    # through a first corner each; fuse those corners onto one shared pivot
    # column so the fork opens as one stroke, before the same-line coincidence
    # pass reads the settled channels.
    _coincide_merge_fanout_pivots(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "merge-fanout pivoting")
    # Coincidence runs after the trunk/gap channels are finalised: it snaps
    # same-line tracks onto a reference read from that final geometry (the
    # port-side track, the source-side track, the merge trunk's descent, and
    # the fan-out junction handoff tail), so a single line reads as one stroke.
    _coincide_same_line_tracks(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "same-line coincidence")
    # Settle every fan-out's opening-descent column in one pass: fuse each line's
    # same-source descents onto the source-nearest track and nest the distinct
    # lines one step apart until each turns off.  Runs after the coincidence pass
    # so a perpendicular drop already resolved onto the junction column stays
    # clear of an L-shaped sibling diverging to another column.
    _coincide_fanout_opening_descents(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "fanout opening coincidence")
    _coincide_same_line_fanout_traverses(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "fanout traverse coincidence")
    # Distinct lines fanning out share the corridor they turn onto; nest their
    # traverses one step apart so the bundle holds a constant width until each
    # line peels off, rather than running on independently-sized bands.
    _bundle_divergent_distinct_traverses(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "fanout traverse bundling")
    # Distinct-line counterpart: spread any two different lines whose final port
    # descents were forced onto one channel (a shared gap left of a wide target).
    _stagger_convergent_distinct_lines(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "convergence staggering")
    # A same-row over-top wrap to a RIGHT entry is pinned deep in the inter-row
    # gap by the target's header clearance; lift any longer-haul cross-row bypass
    # sharing that gap above the wrap's peak so the local wrap nests beneath it.
    _nest_bypass_above_over_top_wrap(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "bypass nesting")
    _clear_bypass_v_label_strikes(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "bypass label clearance")
    # A merge fan-out's down-trunk and an opposite up-arm to a second merge can
    # settle onto one column over a shared Y span, folding the line back over
    # itself.  Slide the down-trunk's descent column a curve radius past the
    # up-arm through the concentric channel machinery so the two clear.  Reads
    # the settled columns, so it runs after the channel-settling passes.
    _clear_merge_trunk_opposite_arm(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "merge-arm clearance")
    # A reserved corridor band says how much room a corridor is left, not which
    # lane in it the corridor takes, so runs held in one band settle without
    # seeing each other and two distinct lines can close to less than the
    # nesting step. Restore that step before corridor containment closes the
    # final reservation geometry.
    _separate_fused_cotravelling_runs(routes, ctx, station_offsets=ctx.station_offsets)
    assert_exit_turn_snapshot(routes, planned_segments, "co-travelling separation")
    # The separation pass rewrites only the band Y, so a merge-fed convergence
    # whose band it reorders is left with its flanking descent-X and port-Y on
    # the old order; re-nest them onto the band so the bundle stays concentric.
    _align_merge_fed_confluence_to_band(routes, ctx)
    assert_exit_turn_snapshot(routes, planned_segments, "merge confluence alignment")
    # Planned same-line corner cohorts are immutable here.  This final pass is
    # limited to wholly unowned legacy cohorts.
    # Every pass above sizes a channel from the grid edges it has to hand, which
    # over-states the obstruction wherever a section spans a boundary or sits
    # outside the corridor's run.  Close that difference last, against the
    # blockers the corridor actually has, so the geometry this pass leaves is
    # what the reservation raised over it measures.
    settled_approach_segments = _settle_same_destination_approach_bundles(routes, ctx)
    _hold_runs_in_corridor_clearance(
        routes, ctx, fixed_segment_keys=settled_approach_segments
    )
    assert_exit_turn_snapshot(routes, planned_segments, "corridor clearance holding")
    assert_exit_turn_snapshot(
        routes, planned_segments, "same-destination approach settlement"
    )
    _unify_coincident_corner_radii(routes)
    assert_exit_turn_snapshot(routes, planned_segments, "corner-radius unification")
    _rederive_semantic_end_corners(routes, ctx.curve_radius, ctx.station_offsets or {})
    assert_exit_turn_snapshot(routes, planned_segments, "semantic corner derivation")
    covered_edges = (
        system_execution.covered_edges()
        if system_execution is not None
        else frozenset()
    )

    if system_execution is not None:
        from nf_metro.layout.routing.system_emission import (
            RouteSystemGeometryOwner,
            validate_route_system_emission,
        )

        validate_route_system_emission(routes, system_execution)

    validate_exit_turn_plans(
        graph,
        routes,
        emitted_exit_turn_plans,
        ctx.station_offsets or {},
        convergence_execution.query.clearance_requirement_system_ids,
    )
    from nf_metro.layout.fan_plans import validate_fan_route_emissions

    validate_fan_route_emissions(
        graph,
        routes,
        ctx.station_offsets if validate_final_route_frames else None,
        planned_system_ids=frozenset(
            system.system_id
            for system in system_execution.systems
            if system.geometry_owner is RouteSystemGeometryOwner.FAN
        )
        if system_execution is not None
        else frozenset(),
        covered_edges=covered_edges,
    )
    from nf_metro.layout.routing.convergences import validate_convergence_plans

    if validate_final_route_frames:
        validate_convergence_plans(routes, convergence_execution)
    validate_member_geometry_emission(routes, member_geometry)

    plan = observer.finish(routes) if observer is not None else None
    if plan is not None:
        from nf_metro.layout.routing.system_emission import (
            validate_published_route_attribution,
        )

        validate_published_route_attribution(routes, plan)
    return routes, moves, plan


def route_edges(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
    *,
    offset_step: float | None = None,
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
    routes, _moves, _plan = _route_edges(
        graph,
        diagonal_run,
        curve_radius,
        station_offsets,
        observe_plan=False,
        offset_step=offset_step,
    )
    return routes


def route_edges_for_placement_guards(
    graph: MetroGraph,
    station_offsets: dict[tuple[str, str], float],
) -> list[RoutedPath]:
    """Route transient placement geometry for Pass C bisection guards.

    Fan and convergence endpoint frames become final after Stage 6.16. The
    earlier Pass C checkpoints need routed paths for their own geometric
    guards, so they retain semantic emission validation without asserting
    those later frames.
    """
    routes, _moves, _plan = _route_edges(
        graph,
        DIAGONAL_RUN,
        CURVE_RADIUS,
        station_offsets,
        observe_plan=False,
        validate_final_route_frames=False,
    )
    return routes


def observe_route_edges(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
    *,
    offset_step: float | None = None,
    allow_convergence_clearance_requirements: bool = False,
) -> RouteObservation:
    """Route once and return the context-local semantic observation."""
    from nf_metro.layout.route_plan import RouteObservation

    routes, _moves, plan = _route_edges(
        graph,
        diagonal_run,
        curve_radius,
        station_offsets,
        observe_plan=True,
        offset_step=offset_step,
        allow_convergence_clearance_requirements=(
            allow_convergence_clearance_requirements
        ),
    )
    assert plan is not None
    return RouteObservation(routes, plan)


def _settle_station_moves(graph: MetroGraph, moves: dict[str, float]) -> None:
    for sid, x in moves.items():
        graph.stations[sid].x = x


def route_edges_centred(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
    *,
    offset_step: float | None = None,
    reservations: RoutePlan | None = None,
    reservation_translations: tuple[ReservationCoordinateTranslation, ...] = (),
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
    routes, moves, _plan = _route_edges(
        graph,
        diagonal_run,
        curve_radius,
        station_offsets,
        observe_plan=False,
        offset_step=offset_step,
        reservations=reservations,
        reservation_translations=reservation_translations,
    )
    _settle_station_moves(graph, moves)
    return routes


def observe_route_edges_centred(
    graph: MetroGraph,
    diagonal_run: float = DIAGONAL_RUN,
    curve_radius: float = CURVE_RADIUS,
    station_offsets: dict[tuple[str, str], float] | None = None,
    *,
    offset_step: float | None = None,
    reservations: RoutePlan | None = None,
    reservation_translations: tuple[ReservationCoordinateTranslation, ...] = (),
    allow_convergence_clearance_requirements: bool = False,
) -> RouteObservation:
    """Route drawn geometry and return its context-local semantic observation."""
    from nf_metro.layout.route_plan import RouteObservation

    routes, moves, plan = _route_edges(
        graph,
        diagonal_run,
        curve_radius,
        station_offsets,
        observe_plan=True,
        offset_step=offset_step,
        reservations=reservations,
        reservation_translations=reservation_translations,
        allow_convergence_clearance_requirements=(
            allow_convergence_clearance_requirements
        ),
    )
    _settle_station_moves(graph, moves)
    assert plan is not None
    return RouteObservation(routes, plan)
