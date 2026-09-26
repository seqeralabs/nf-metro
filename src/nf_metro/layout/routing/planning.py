"""Shared route-system planning preparation before path emission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.route_plan import (
    ConvergencePlanId,
    EmissionMemberId,
    ExitTurnPlanId,
    RoutePlan,
    RouteSystemDisposition,
    RouteSystemId,
)
from nf_metro.layout.routing import exit_turns as exit_turn_routing
from nf_metro.layout.routing.context import _RoutingCtx
from nf_metro.layout.routing.convergences import (
    ConvergenceInvariantError,
    ConvergencePlanExecution,
    apply_convergence_corridor_grants,
    build_convergence_plan_execution,
    convergence_corridor_requests,
    empty_convergence_plan_execution,
    preliminary_member_gap_claims,
    restrict_convergence_execution,
    settle_global_convergence_execution,
    settle_preliminary_convergence_execution,
)
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortLedger,
    CorridorCohortPlan,
    CorridorScalarGrant,
    CorridorScalarRequest,
    build_corridor_cohort_ledger,
)
from nf_metro.layout.routing.exit_turns import ExitTurnExecution
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.inter_section_handlers import (
    classify_inter_section_family,
)
from nf_metro.layout.routing.member_geometry import (
    MemberGeometryExecution,
    build_member_geometry_execution,
    empty_member_geometry_execution,
    settle_member_geometry_corner_cohorts,
)
from nf_metro.layout.routing.system_emission import (
    RouteSystemEmissionExecution,
    RouteSystemGeometryOwner,
    build_route_system_emission_execution,
    classify_route_system_dispositions,
)
from nf_metro.layout.settlement_demand import BoundaryClearanceRequirementKind
from nf_metro.parser.model import MetroGraph
from nf_metro.parser.route_topology import ResolvedEdge


@dataclass(frozen=True, slots=True)
class RoutePlanningExecution:
    """Final no-emission planning state consumed by routing and diagnostics."""

    exit_turns: ExitTurnExecution
    convergences: ConvergencePlanExecution
    member_geometry: MemberGeometryExecution
    route_systems: RouteSystemEmissionExecution | None
    planned_system_ids: frozenset[RouteSystemId]
    exit_turn_dispositions: tuple[tuple[ExitTurnPlanId, str | None], ...] = ()
    """Every plan's frozen verdict, including systems whose record is restricted.

    Settlement re-routes across moved geometry, so a fresh planning pass can
    reach a different verdict on a plan sitting near a tolerance boundary.
    Replay reads the verdict from here, which is why it is captured before the
    published record is narrowed to planned systems."""
    corridor_cohort_ledger: CorridorCohortLedger | None = None


def _allocation_eligible_system_ids(
    preliminary_planned_ids: frozenset[RouteSystemId],
    member_failure_ids: frozenset[RouteSystemId],
) -> frozenset[RouteSystemId]:
    """Remove member-failed systems before shared geometry allocation."""
    return preliminary_planned_ids - member_failure_ids


def _apply_corridor_grants(
    convergences: ConvergencePlanExecution,
    requests: tuple[CorridorScalarRequest, ...],
    member_geometry: MemberGeometryExecution,
    *,
    compiled: bool,
) -> ConvergencePlanExecution:
    """Publish the convergence plans the corridor-cohort compile granted.

    The compile runs only on a pass that may publish clearance requirements, so
    requests on any other pass are exposed without being solved.  On a
    *compiled* pass a request goes ungranted only when the compile published an
    aperture requirement instead of a plan; any other omission is a wiring
    defect.  A component the compile kept on legacy geometry as a compatibility
    outcome owns none of its coordinates, so neither its requests nor any grant
    for them reach the applier.
    """
    if not requests:
        return convergences
    cohorts = member_geometry.corridor_cohorts
    if cohorts is None:
        aperture_pending = any(
            requirement.kind
            is BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
            for requirement in member_geometry.clearance_requirements
        )
        if compiled and not aperture_pending:
            raise ConvergenceInvariantError(
                "corridor-cohort compile omitted convergence corridor requests"
            )
        return convergences
    compatibility_ids = _compatibility_scalar_ids(cohorts)
    return apply_convergence_corridor_grants(
        convergences,
        tuple(
            request
            for request in requests
            if request.variable.variable_id not in compatibility_ids
        ),
        _owned_scalar_grants(cohorts),
    )


def _compatibility_scalar_ids(cohorts: CorridorCohortPlan) -> frozenset[str]:
    return frozenset(
        variable_id
        for component in cohorts.components
        if component.compatibility is not None
        for variable_id in component.compatibility.scalar_variable_ids
    )


def _owned_scalar_grants(
    cohorts: CorridorCohortPlan,
) -> tuple[CorridorScalarGrant, ...]:
    """The grants outside every component kept on legacy geometry."""
    compatibility_ids = _compatibility_scalar_ids(cohorts)
    return tuple(
        grant
        for grant in cohorts.scalar_grants
        if grant.variable_id not in compatibility_ids
    )


def _granted_trunk_coordinates(
    member_geometry: MemberGeometryExecution,
) -> dict[ConvergencePlanId, float]:
    """The trunk coordinate every owned corridor grant decided, by plan.

    A grant that leaves its trunk where it stands owns that coordinate as much
    as one that moves it.
    """
    cohorts = member_geometry.corridor_cohorts
    if cohorts is None:
        return {}
    return {
        ConvergencePlanId(grant.owner_id): grant.coordinate
        for grant in _owned_scalar_grants(cohorts)
    }


def _assert_grants_survive_settlement(
    granted: Mapping[ConvergencePlanId, float],
    settled: ConvergencePlanExecution,
) -> None:
    """Refuse a settlement that re-seated a trunk its corridor grant placed.

    The published grant names the coordinate it decided, so a trunk settled
    anywhere else would publish provenance for geometry the render does not
    draw.
    """
    plans = {plan.id: plan for plan in settled.plans}
    for plan_id, coordinate in granted.items():
        plan = plans.get(plan_id)
        axis = None if plan is None else plan.trunk_axis
        if axis is None or abs(axis.coordinate - coordinate) > COORD_TOLERANCE_FINE:
            raise ConvergenceInvariantError(
                f"convergence {plan_id} was re-settled off the {coordinate:g} its "
                "corridor grant decided"
            )


def _with_settled_exit_turns(
    execution: MemberGeometryExecution,
    allocation: MemberGeometryExecution,
    pending_member_ids: frozenset[EmissionMemberId],
    ctx: _RoutingCtx,
) -> MemberGeometryExecution:
    """Apply each allocated source axis to the fully normalized member path."""
    from nf_metro.layout.routing.common import Direction, RoutedPath
    from nf_metro.layout.routing.exit_turns import planned_exit_turn_corner_offsets
    from nf_metro.layout.routing.normalize import _reseat_concentric_flanking

    plans = []
    for plan in execution.plans:
        settled = ctx.settled_exit_turns.get(
            (plan.edge.source, plan.edge.target, plan.edge.line_id)
        )
        rank = plan.exit_turn_segment_rank
        if plan.member_id not in pending_member_ids or settled is None or rank is None:
            plans.append(plan)
            continue
        if plan.curve_radii is None or ctx.exit_turns is None:
            raise RuntimeError(
                f"settled exit turn {plan.id} has no explicit corner geometry"
            )
        membership = ctx.exit_turns.membership_for_edge(plan.edge)
        if membership is None:
            raise RuntimeError(f"settled exit turn {plan.id} lost its plan membership")
        corner_offsets = planned_exit_turn_corner_offsets(membership)
        if corner_offsets is None:
            raise RuntimeError(
                f"settled exit turn {plan.id} has no standard corner offsets"
            )
        curve_radii = list(plan.curve_radii)
        route = RoutedPath(
            ctx.edge_by_key[(plan.edge.source, plan.edge.target, plan.edge.line_id)],
            plan.edge.line_id,
            list(plan.points),
            curve_radii=curve_radii,
            concentric_corner_offsets_by_segment=dict(
                plan.concentric_corner_offsets_by_segment
            ),
            concentric_corner_bases_by_segment=dict(
                plan.concentric_corner_bases_by_segment
            ),
        )
        existing_offsets = route.concentric_corner_offsets_by_segment.get(rank)
        existing_bases = route.concentric_corner_bases_by_segment.get(rank)
        offset_out = (
            existing_offsets[1]
            if existing_offsets is not None and existing_offsets[1] is not None
            else 0.0
        )
        base_radius_out = (
            existing_bases[1]
            if existing_bases is not None and existing_bases[1] is not None
            else (curve_radii[rank] if rank < len(curve_radii) else ctx.curve_radius)
        )
        axis = 0 if settled.run_direction in {Direction.R, Direction.L} else 1
        lead = list(route.points[rank - 1])
        lead[axis] = settled.launch_coordinate
        route.points[rank - 1] = (lead[0], lead[1])
        _reseat_concentric_flanking(
            route,
            rank,
            settled.axis_coordinate,
            axis=axis,
            offset_in=corner_offsets[0],
            offset_out=offset_out,
            base_radius=ctx.curve_radius,
            base_radius_out=base_radius_out,
        )
        points = route.points
        gap_channels = tuple(
            replace(
                channel,
                start=points[channel.segment_rank],
                end=points[channel.segment_rank + 1],
            )
            for channel in plan.gap_channels
        )
        plans.append(
            replace(
                plan,
                points=tuple(points),
                curve_radii=tuple(curve_radii),
                gap_channels=gap_channels,
                concentric_corner_offsets_by_segment=tuple(
                    sorted(route.concentric_corner_offsets_by_segment.items())
                ),
                concentric_corner_bases_by_segment=tuple(
                    sorted(route.concentric_corner_bases_by_segment.items())
                ),
            )
        )
    frozen_plans = tuple(plans)
    semantic_corner_templates = dict(execution._semantic_corner_templates)
    semantic_corner_templates.update(
        {
            plan.edge: (
                plan.curve_radii,
                plan.concentric_corner_offsets_by_segment,
                plan.concentric_corner_bases_by_segment,
            )
            for plan in frozen_plans
        }
    )
    return MemberGeometryExecution(
        frozen_plans,
        execution.failure_reasons,
        MappingProxyType({plan.edge: plan for plan in frozen_plans}),
        allocation.settled_exit_turns,
        MappingProxyType(semantic_corner_templates),
        execution.clearance_requirements,
        execution.corridor_cohorts,
    )


def prepare_route_system_planning(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    *,
    include_convergence_resources: bool,
    reservation_ids_by_member: Mapping[EmissionMemberId, tuple[str, ...]] | None = None,
    allow_convergence_clearance_requirements: bool = False,
    granted_clearance_owner_ids: frozenset[str] = frozenset(),
    prior_plan: RoutePlan | None = None,
) -> RoutePlanningExecution:
    """Run the canonical planning phases without emitting production paths.

    Compatibility context is established immediately after preliminary atomic
    disposition.  Only planned systems then contribute convergence claims and
    member geometry to final shared allocation.  Resource publication happens
    after final disposition and follows ``include_convergence_resources``.
    """
    station_offsets = ctx.station_offsets
    initial_station_offsets = dict(station_offsets or {})
    provisional_exit_turns = exit_turn_routing.build_exit_turn_execution(
        graph,
        ctx,
        adopt_prior_dispositions=False,
    )
    scaffold = provisional_exit_turns.scaffold
    if scaffold is None:
        empty_members = empty_member_geometry_execution()
        empty_convergences = empty_convergence_plan_execution()
        ctx.convergences = empty_convergences.query
        ctx.route_systems = None
        return RoutePlanningExecution(
            provisional_exit_turns,
            empty_convergences,
            empty_members,
            None,
            frozenset(),
            tuple(
                (plan.id, plan.legacy_reason) for plan in provisional_exit_turns.plans
            ),
        )

    # Corridor-cohort intent reads a prior semantic ledger, and only once the
    # general convergence clearance it depends on has settled: a pending GENERAL
    # requirement means the boxes the cohort would measure against are about to
    # move, so the ledger waits a generation rather than freezing intent over
    # geometry the next translation invalidates.
    pending_general_clearance = prior_plan is not None and any(
        requirement.kind is BoundaryClearanceRequirementKind.GENERAL
        for requirement in prior_plan.boundary_clearance_requirements
    )
    corridor_cohort_ledger: CorridorCohortLedger | None = (
        None
        if prior_plan is None or pending_general_clearance
        else prior_plan.corridor_cohort_ledger
    )
    if (
        prior_plan is not None
        and not pending_general_clearance
        and corridor_cohort_ledger is None
    ):
        corridor_cohort_ledger = build_corridor_cohort_ledger(
            graph,
            scaffold,
            prior_plan,
            station_offsets=station_offsets or {},
            curve_radius=ctx.curve_radius,
        )

    def prepare_member_geometry(
        exit_turns: ExitTurnExecution,
        pending_plan_ids: frozenset[ExitTurnPlanId],
        settled_plan_ids: frozenset[ExitTurnPlanId] = frozenset(),
        *,
        compile_corridor_cohorts: bool = True,
    ) -> tuple[
        Mapping[ResolvedEdge, RouteFamilyId],
        ConvergencePlanExecution,
        frozenset[RouteSystemId],
        MemberGeometryExecution,
    ]:
        ctx.exit_turns = exit_turns.query
        family_by_edge = MappingProxyType(
            {
                edge: family
                for edge in scaffold.edge_order
                if (
                    family := classify_inter_section_family(
                        ctx.edge_by_key[(edge.source, edge.target, edge.line_id)],
                        graph.stations[edge.source],
                        graph.stations[edge.target],
                        ctx,
                    )
                )
                is not None
            }
        )
        convergences = build_convergence_plan_execution(
            graph,
            ctx,
            scaffold,
            exit_turn_plans=exit_turns.plans,
            fan_plans=graph.fan_plans,
            member_geometry=empty_member_geometry_execution(),
            include_resources=False,
            allow_clearance_requirements=(allow_convergence_clearance_requirements),
        )
        ctx.convergences = convergences.query
        preliminary = classify_route_system_dispositions(
            scaffold,
            exit_turn_plans=exit_turns.plans,
            fan_plans=graph.fan_plans,
            convergence_plans=convergences.plans,
        )
        complete_path_system_ids = frozenset(
            decision.system_id
            for decision in preliminary
            if decision.geometry_owner is RouteSystemGeometryOwner.MEMBER_GEOMETRY
            and decision.superseded_verdicts
        )
        planned_ids = frozenset(scaffold.ordered_system_ids)
        convergences = settle_preliminary_convergence_execution(
            convergences,
            graph,
            ctx,
            exit_turn_plans=exit_turns.plans,
            planned_system_ids=planned_ids,
        )
        ctx.convergences = convergences.query
        cohort_ledger = corridor_cohort_ledger if compile_corridor_cohorts else None
        corridor_targets, corridor_scalar_requests = (
            convergence_corridor_requests(convergences.plans, graph, ctx)
            if cohort_ledger is not None
            else ((), ())
        )
        member_geometry = build_member_geometry_execution(
            graph,
            ctx,
            scaffold,
            family_by_edge=family_by_edge,
            convergence_plans=convergences.plans,
            complete_path_system_ids=complete_path_system_ids,
            preliminary_gap_claims=preliminary_member_gap_claims(
                convergences,
                graph,
                planned_ids,
            ),
            reservation_ids_by_member=reservation_ids_by_member,
            pending_exit_turn_plan_ids=pending_plan_ids,
            settled_exit_turn_plan_ids=settled_plan_ids,
            allow_clearance_requirements=allow_convergence_clearance_requirements,
            granted_clearance_owner_ids=granted_clearance_owner_ids,
            corridor_cohort_ledger=cohort_ledger,
            corridor_targets=corridor_targets,
            corridor_scalar_requests=corridor_scalar_requests,
        )
        convergences = _apply_corridor_grants(
            convergences,
            corridor_scalar_requests,
            member_geometry,
            compiled=allow_convergence_clearance_requirements,
        )
        ctx.convergences = convergences.query
        return family_by_edge, convergences, planned_ids, member_geometry

    allocation_exit_turns, pending_plan_ids = (
        exit_turn_routing.promote_pending_gap_allocation(provisional_exit_turns)
    )
    if pending_plan_ids:
        # Only this execution's settled exit turns survive into the re-plan
        # below, which compiles the corridor cohorts against them.
        _, _, _, allocation_geometry = prepare_member_geometry(
            allocation_exit_turns, pending_plan_ids, compile_corridor_cohorts=False
        )
        ctx.settled_exit_turns = allocation_geometry.settled_exit_turns
        if station_offsets is not None:
            station_offsets.clear()
            station_offsets.update(initial_station_offsets)
        ctx.station_offsets = station_offsets
        exit_turns = exit_turn_routing.build_exit_turn_execution(
            graph,
            ctx,
            adopt_prior_dispositions=False,
        )
        unresolved = tuple(
            plan
            for plan in exit_turns.plans
            if plan.legacy_reason == exit_turn_routing.GAP_ALLOCATION_PENDING
        )
        if unresolved:
            exit_turns = exit_turn_routing.decline_unsettled_gap_allocation(exit_turns)
        family_by_edge, convergences, preliminary_planned_ids, member_geometry = (
            prepare_member_geometry(
                exit_turns,
                frozenset(),
                pending_plan_ids,
            )
        )
        pending_member_ids = frozenset(
            member_id
            for plan in provisional_exit_turns.plans
            if plan.id in pending_plan_ids
            for member_id in plan.member_ids
        )
        member_geometry = _with_settled_exit_turns(
            member_geometry,
            allocation_geometry,
            pending_member_ids,
            ctx,
        )
    elif ctx.prior_exit_turn_dispositions is not None:
        exit_turns = exit_turn_routing.build_exit_turn_execution(graph, ctx)
        family_by_edge, convergences, preliminary_planned_ids, member_geometry = (
            prepare_member_geometry(exit_turns, pending_plan_ids)
        )
    else:
        exit_turns = provisional_exit_turns
        family_by_edge, convergences, preliminary_planned_ids, member_geometry = (
            prepare_member_geometry(exit_turns, pending_plan_ids)
        )
    if not pending_plan_ids and member_geometry.settled_exit_turns:
        allocation_geometry = member_geometry
        settled_plan_ids = frozenset(
            membership.plan.id
            for source, target, line_id in member_geometry.settled_exit_turns
            if (
                membership := exit_turns.query.membership_for_edge(
                    ctx.edge_by_key[(source, target, line_id)]
                )
            )
            is not None
        )
        settled_member_ids = frozenset(
            membership.member_id
            for source, target, line_id in member_geometry.settled_exit_turns
            if (
                membership := exit_turns.query.membership_for_edge(
                    ctx.edge_by_key[(source, target, line_id)]
                )
            )
            is not None
        )
        ctx.settled_exit_turns = member_geometry.settled_exit_turns
        if station_offsets is not None:
            station_offsets.clear()
            station_offsets.update(initial_station_offsets)
        ctx.station_offsets = station_offsets
        exit_turns = exit_turn_routing.build_exit_turn_execution(
            graph,
            ctx,
            adopt_prior_dispositions=False,
        )
        family_by_edge, convergences, preliminary_planned_ids, member_geometry = (
            prepare_member_geometry(
                exit_turns,
                frozenset(),
                settled_plan_ids,
            )
        )
        member_geometry = _with_settled_exit_turns(
            member_geometry,
            allocation_geometry,
            settled_member_ids,
            ctx,
        )
    allocation_planned_ids = _allocation_eligible_system_ids(
        preliminary_planned_ids,
        frozenset(member_geometry.failure_reasons),
    )
    granted_trunks = _granted_trunk_coordinates(member_geometry)
    convergences = settle_global_convergence_execution(
        convergences,
        graph,
        ctx,
        exit_turn_plans=exit_turns.plans,
        member_geometry=member_geometry,
        planned_system_ids=allocation_planned_ids,
        include_resources=False,
        allow_clearance_requirements=allow_convergence_clearance_requirements,
    )
    _assert_grants_survive_settlement(granted_trunks, convergences)
    ctx.convergences = convergences.query
    member_geometry = settle_member_geometry_corner_cohorts(
        member_geometry,
        scaffold,
        family_by_edge,
        ctx,
    )
    route_systems = build_route_system_emission_execution(
        scaffold,
        exit_turn_plans=exit_turns.plans,
        fan_plans=graph.fan_plans,
        convergence_plans=convergences.plans,
        reservation_ids_by_member=reservation_ids_by_member,
        family_by_edge=family_by_edge,
        member_geometry_plans=member_geometry.plans,
        member_geometry_failures=member_geometry.failure_reasons,
        require_member_geometry=True,
    )
    planned_system_ids = frozenset(
        system.system_id
        for system in route_systems.systems
        if system.disposition is RouteSystemDisposition.PLANNED
    )
    ctx.route_systems = route_systems
    convergences = restrict_convergence_execution(
        convergences,
        graph,
        planned_system_ids=planned_system_ids,
        include_resources=include_convergence_resources,
    )
    exit_turn_dispositions = tuple(
        (plan.id, plan.legacy_reason) for plan in exit_turns.plans
    )
    # Member templates consume exit-turn axes even when another planner owns the
    # complete system. Publishing only system-owned records would hide those
    # coordinates from the templates that must freeze them.
    emission_exit_turns = exit_turns.query
    exit_turns = exit_turns.restrict_to_systems(planned_system_ids)
    ctx.exit_turns = emission_exit_turns
    ctx.convergences = convergences.query.restrict_to_systems(planned_system_ids)
    return RoutePlanningExecution(
        exit_turns,
        convergences,
        member_geometry,
        route_systems,
        planned_system_ids,
        exit_turn_dispositions,
        corridor_cohort_ledger,
    )
