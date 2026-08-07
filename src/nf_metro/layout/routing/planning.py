"""Shared route-system planning preparation before path emission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from nf_metro.layout.route_plan import (
    EmissionMemberId,
    RouteSystemDisposition,
    RouteSystemId,
)
from nf_metro.layout.routing import exit_turns as exit_turn_routing
from nf_metro.layout.routing.context import _RoutingCtx
from nf_metro.layout.routing.convergences import (
    ConvergencePlanExecution,
    build_convergence_plan_execution,
    empty_convergence_plan_execution,
    preliminary_member_gap_claims,
    restrict_convergence_execution,
    settle_global_convergence_execution,
    settle_preliminary_convergence_execution,
)
from nf_metro.layout.routing.exit_turns import ExitTurnExecution
from nf_metro.layout.routing.inter_section_handlers import (
    classify_inter_section_family,
)
from nf_metro.layout.routing.member_geometry import (
    MemberGeometryExecution,
    build_member_geometry_execution,
    empty_member_geometry_execution,
)
from nf_metro.layout.routing.system_emission import (
    RouteSystemEmissionExecution,
    build_route_system_emission_execution,
    classify_route_system_dispositions,
)
from nf_metro.parser.model import MetroGraph


@dataclass(frozen=True, slots=True)
class RoutePlanningExecution:
    """Final no-emission planning state consumed by routing and diagnostics."""

    exit_turns: ExitTurnExecution
    convergences: ConvergencePlanExecution
    member_geometry: MemberGeometryExecution
    route_systems: RouteSystemEmissionExecution | None
    planned_system_ids: frozenset[RouteSystemId]


def _allocation_eligible_system_ids(
    preliminary_planned_ids: frozenset[RouteSystemId],
    member_failure_ids: frozenset[RouteSystemId],
) -> frozenset[RouteSystemId]:
    """Remove member-failed systems before shared geometry allocation."""
    return preliminary_planned_ids - member_failure_ids


def prepare_route_system_planning(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    *,
    include_convergence_resources: bool,
    reservation_ids_by_member: Mapping[EmissionMemberId, tuple[str, ...]] | None = None,
) -> RoutePlanningExecution:
    """Run the canonical planning phases without emitting production paths.

    Compatibility context is established immediately after preliminary atomic
    disposition.  Only planned systems then contribute convergence claims and
    member geometry to final shared allocation.  Resource publication happens
    after final disposition and follows ``include_convergence_resources``.
    """
    exit_turns = exit_turn_routing.build_exit_turn_execution(graph, ctx)
    ctx.exit_turns = exit_turns.query
    scaffold = exit_turns.scaffold
    if scaffold is None:
        empty_members = empty_member_geometry_execution()
        empty_convergences = empty_convergence_plan_execution()
        ctx.convergences = empty_convergences.query
        ctx.route_systems = None
        return RoutePlanningExecution(
            exit_turns,
            empty_convergences,
            empty_members,
            None,
            frozenset(),
        )

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
    )
    ctx.convergences = convergences.query
    preliminary = classify_route_system_dispositions(
        scaffold,
        exit_turn_plans=exit_turns.plans,
        fan_plans=graph.fan_plans,
        convergence_plans=convergences.plans,
    )
    preliminary_compatibility_ids = frozenset(
        decision.system_id
        for decision in preliminary
        if decision.disposition is RouteSystemDisposition.COMPATIBILITY
    )
    preliminary_planned_ids = frozenset(
        decision.system_id
        for decision in preliminary
        if decision.disposition is RouteSystemDisposition.PLANNED
    )
    ctx.compatibility_edges = frozenset(
        (edge.source, edge.target, edge.line_id)
        for edge in scaffold.edge_order
        if scaffold.system_for_edge(edge) in preliminary_compatibility_ids
    )
    convergences = settle_preliminary_convergence_execution(
        convergences,
        graph,
        ctx,
        exit_turn_plans=exit_turns.plans,
        planned_system_ids=preliminary_planned_ids,
    )
    ctx.convergences = convergences.query
    member_geometry = build_member_geometry_execution(
        graph,
        ctx,
        scaffold,
        family_by_edge=family_by_edge,
        compatibility_system_ids=preliminary_compatibility_ids,
        preliminary_gap_claims=preliminary_member_gap_claims(
            convergences,
            graph,
            preliminary_planned_ids,
            exit_turns.plans if reservation_ids_by_member is not None else (),
        ),
        reservation_ids_by_member=reservation_ids_by_member,
    )
    allocation_planned_ids = _allocation_eligible_system_ids(
        preliminary_planned_ids,
        frozenset(member_geometry.failure_reasons),
    )
    convergences = settle_global_convergence_execution(
        convergences,
        graph,
        ctx,
        exit_turn_plans=exit_turns.plans,
        member_geometry=member_geometry,
        planned_system_ids=allocation_planned_ids,
        include_resources=False,
    )
    ctx.convergences = convergences.query
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
    compatibility_system_ids = frozenset(
        system.system_id
        for system in route_systems.systems
        if system.disposition is RouteSystemDisposition.COMPATIBILITY
    )
    ctx.route_systems = route_systems
    ctx.compatibility_edges = frozenset(
        (member.edge.source, member.edge.target, member.edge.line_id)
        for system in route_systems.systems
        if system.disposition is RouteSystemDisposition.COMPATIBILITY
        for member in system.members
    )
    convergences = restrict_convergence_execution(
        convergences,
        graph,
        planned_system_ids=planned_system_ids,
        compatibility_system_ids=compatibility_system_ids,
        include_resources=include_convergence_resources,
    )
    exit_turns = exit_turns.restrict_to_systems(planned_system_ids)
    ctx.exit_turns = exit_turns.query
    ctx.convergences = convergences.query.restrict_to_systems(planned_system_ids)
    return RoutePlanningExecution(
        exit_turns,
        convergences,
        member_geometry,
        route_systems,
        planned_system_ids,
    )
