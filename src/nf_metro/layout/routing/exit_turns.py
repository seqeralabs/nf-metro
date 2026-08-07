"""Whole-group source-turn planning before inter-section route emission."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    MIN_STRAIGHT_EDGE,
)
from nf_metro.layout.route_plan import (
    CoordinateRegime,
    DemandAxis,
    DemandId,
    DemandKind,
    EmissionMemberId,
    EmissionRole,
    ExitLaneOrderSource,
    ExitLaneTransition,
    ExitLaneTransitionPlacement,
    ExitSourceLane,
    ExitTurnAssignment,
    ExitTurnAxis,
    ExitTurnAxisId,
    ExitTurnDisposition,
    ExitTurnPlan,
    ExitTurnPlanId,
    GridSpan,
    KeepOutClass,
    ResolvedEndpointGroup,
    RoutePlan,
    RoutePlanDiagnostic,
    RoutePlanProvenance,
    RouteSemanticScaffold,
    RouteSystemDisposition,
    RouteSystemId,
    SharedReference,
    SharedReferenceId,
    SharedReferenceKind,
    SymbolicDemand,
    _member_roles,
    _ordered_unique,
    _plan_provenance,
    build_route_semantic_scaffold,
    grid_span_for_sections,
    reservation_decision_refs,
    turn_handedness,
)
from nf_metro.layout.routing.centrelines import (
    gather_tapered_bundle,
    route_lane_transition,
)
from nf_metro.layout.routing.common import (
    Direction,
    OffsetRegime,
    RoutedPath,
    apply_route_offsets,
    horizontal_direction,
    vertical_direction,
)
from nf_metro.layout.routing.context import _RoutingCtx, _tb_x_offset
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.inter_section_handlers import (
    _build_inter_facts,
    _l_shape_fan_source_turn,
    _l_shape_mid_x,
    _merge_branch_lead_x,
    _merge_entry_route_kind,
    _MergeEntryRoute,
    _tb_bottom_exit_around_stack_geometry,
    _tb_bottom_exit_geometry,
    _wrap_fan_geometry,
    classify_inter_section_family,
)
from nf_metro.layout.routing.normalize import _reseat_concentric_flanking
from nf_metro.layout.routing.offsets import (
    LinearEntryFrameOwnership,
    capture_linear_entry_frame_ownership,
    conflicting_linear_entry_frame_assignments,
    validate_linear_entry_frame_ownership,
)
from nf_metro.layout.routing.orientation import (
    direction_axis,
    get_point_coordinate,
    lateral_axis,
    lateral_order_sign,
)
from nf_metro.layout.routing.perp import _perp_entry_crossing_x
from nf_metro.parser.model import Edge, MetroGraph, PortSide
from nf_metro.parser.route_topology import (
    ConnectorId,
    EndpointGroup,
    EndpointGroupId,
    ResolvedEdge,
    semantic_route_id,
)

PLANNED_EXIT_FAMILIES = frozenset(
    {
        RouteFamilyId.SAME_Y_STRAIGHT,
        RouteFamilyId.STANDARD_L_SHAPE,
        RouteFamilyId.MERGE_BRANCH,
        RouteFamilyId.MERGE_ENTRY,
        RouteFamilyId.RIGHT_ENTRY_CROSS_ROW_WRAP,
        RouteFamilyId.TOP_ENTRY_L_SHAPE,
        RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
        RouteFamilyId.TB_BOTTOM_EXIT,
    }
)

_EdgeKey = tuple[str, str, str]


class ExitTurnInvariantError(RuntimeError):
    """A planned source turn was not emitted exactly as committed."""


@dataclass(frozen=True, slots=True)
class _ExitTurnGeometryState:
    family_id: str | None
    axis_id: str | None
    segment_rank: int | None
    lead_in_start: tuple[float, float] | None
    segment_start: tuple[float, float] | None
    segment_end: tuple[float, float] | None
    segment_radii: tuple[tuple[int, float], ...] | None
    transition_plan_id: str | None
    transition_points: tuple[tuple[float, float], ...] | None
    transition_radii: tuple[float, ...] | None
    transition_regime: OffsetRegime | None


@dataclass(frozen=True, slots=True)
class _ExitTurnSnapshot:
    geometry: Mapping[tuple[str, ...], _ExitTurnGeometryState]
    plans_by_id: Mapping[str, ExitTurnPlan]


@dataclass(frozen=True, slots=True)
class _Membership:
    plan: ExitTurnPlan
    member_id: EmissionMemberId
    assignment: ExitTurnAssignment | None
    axis: ExitTurnAxis | None


@dataclass(frozen=True, slots=True)
class _TransitionMembership:
    plan: ExitTurnPlan
    transition: ExitLaneTransition


@dataclass(frozen=True, slots=True)
class ExitTurnPlanQuery:
    """Read-only lookup used by production dispatch and invariants."""

    plans: tuple[ExitTurnPlan, ...]
    _by_edge: Mapping[_EdgeKey, _Membership]
    _transition_by_edge: Mapping[_EdgeKey, _TransitionMembership]

    def membership_for_edge(self, edge: Edge | ResolvedEdge) -> _Membership | None:
        return self._by_edge.get((edge.source, edge.target, edge.line_id))

    def transition_for_edge(
        self, edge: Edge | ResolvedEdge
    ) -> _TransitionMembership | None:
        return self._transition_by_edge.get((edge.source, edge.target, edge.line_id))

    def restrict_to_systems(
        self, system_ids: frozenset[RouteSystemId]
    ) -> ExitTurnPlanQuery:
        plans = tuple(plan for plan in self.plans if plan.system_id in system_ids)
        return ExitTurnPlanQuery(
            plans,
            MappingProxyType(
                {
                    key: membership
                    for key, membership in self._by_edge.items()
                    if membership.plan.system_id in system_ids
                }
            ),
            MappingProxyType(
                {
                    key: membership
                    for key, membership in self._transition_by_edge.items()
                    if membership.plan.system_id in system_ids
                }
            ),
        )


def route_planned_lane_transition(
    edge: Edge,
    ctx: _RoutingCtx,
    *,
    is_inter_section: bool,
) -> RoutedPath | None:
    """Realise a lane hand-off committed by the exit-turn planner."""
    membership = (
        ctx.exit_turns.transition_for_edge(edge) if ctx.exit_turns is not None else None
    )
    if membership is None:
        return None
    transition = membership.transition
    route = route_lane_transition(
        edge,
        transition.source_point,
        transition.target_point,
        source_offset=transition.source_offset,
        target_offset=transition.target_offset,
        run_direction=transition.run_direction,
        source_runway=transition.source_runway,
        target_runway=transition.target_runway,
        diagonal_run=transition.diagonal_run,
        place_at_source=(transition.placement is ExitLaneTransitionPlacement.SOURCE),
        is_inter_section=is_inter_section,
    )
    route.exit_lane_transition_plan_id = str(membership.plan.id)
    return route


@dataclass(frozen=True, slots=True)
class ExitTurnExecution:
    """Immutable planning output shared by ordinary and observed routing."""

    scaffold: RouteSemanticScaffold | None
    plans: tuple[ExitTurnPlan, ...]
    references: tuple[SharedReference, ...]
    demands: tuple[SymbolicDemand, ...]
    diagnostics: tuple[RoutePlanDiagnostic, ...]
    query: ExitTurnPlanQuery

    def restrict_to_systems(
        self, system_ids: frozenset[RouteSystemId]
    ) -> ExitTurnExecution:
        plans = tuple(plan for plan in self.plans if plan.system_id in system_ids)
        reference_ids = {
            plan.reference_id for plan in plans if plan.reference_id is not None
        }
        demand_ids = {demand_id for plan in plans for demand_id in plan.demand_ids}
        diagnostic_member_ids = {
            plan.member_ids[0]
            for plan in plans
            if plan.member_ids and plan.legacy_reason is not None
        }
        return ExitTurnExecution(
            self.scaffold,
            plans,
            tuple(item for item in self.references if item.id in reference_ids),
            tuple(item for item in self.demands if item.id in demand_ids),
            tuple(
                item
                for item in self.diagnostics
                if item.member_id in diagnostic_member_ids
            ),
            self.query.restrict_to_systems(system_ids),
        )


@dataclass(frozen=True, slots=True)
class _AssignmentSeed:
    edge: ResolvedEdge
    member_id: EmissionMemberId
    connector_ids: tuple[ConnectorId, ...]
    entry_group_id: EndpointGroupId
    family_id: RouteFamilyId
    lane_rank: int
    roles: tuple[EmissionRole, ...]
    run_direction: Direction | None
    turn_direction: Direction | None
    launch_coordinate: float | None
    minimum_runway: float | None
    fixed_axis: float | None


@dataclass(frozen=True, slots=True)
class _ClassifiedMember:
    edge: ResolvedEdge
    member_id: EmissionMemberId
    connector_ids: tuple[ConnectorId, ...]
    entry_group_id: EndpointGroupId
    family_id: RouteFamilyId
    lane_rank: int
    run_direction: Direction | None
    turn_direction: Direction | None
    launch_coordinate: float | None
    minimum_runway: float | None
    fixed_axis: float | None


@dataclass(frozen=True, slots=True)
class _AssignmentClassification:
    seeds: tuple[_AssignmentSeed, ...]
    unclassified_member_ids: tuple[EmissionMemberId, ...]
    legacy_reason: str | None


@dataclass(frozen=True, slots=True)
class _AxisPlan:
    axes: tuple[ExitTurnAxis, ...]
    axis_by_member: Mapping[EmissionMemberId, ExitTurnAxis]
    minimum_runway: float
    legacy_reason: str | None


@dataclass(frozen=True, slots=True)
class _SourceTurnRequirement:
    """Pre-emission source-turn inputs selected by a production subshape."""

    run_direction: Direction | None
    turn_direction: Direction | None
    launch_coordinate: float | None
    minimum_runway: float | None
    fixed_axis: float | None
    legacy_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _LaneOwnership:
    station_ids_by_line: Mapping[str, tuple[str, ...]]
    transitions: tuple[ExitLaneTransition, ...]
    legacy_reason: str | None


@dataclass(frozen=True, slots=True)
class _PlannerIndexes:
    member_edges_by_exit_group: Mapping[EndpointGroupId, tuple[ResolvedEdge, ...]]
    member_ids_by_system: Mapping[RouteSystemId, tuple[EmissionMemberId, ...]]
    endpoint_by_id: Mapping[EndpointGroupId, ResolvedEndpointGroup]


@dataclass(frozen=True, slots=True)
class _BuiltGroupPlan:
    plan: ExitTurnPlan
    reference: SharedReference | None
    demands: tuple[SymbolicDemand, ...]
    diagnostic: RoutePlanDiagnostic | None
    assignments_by_edge: Mapping[ResolvedEdge, ExitTurnAssignment]


@dataclass(frozen=True, slots=True)
class _CompatibilityChannelClaim:
    line_id: str
    axis: DemandAxis
    coordinate: float
    cross_lo: float
    cross_hi: float


def _edge_key(edge: ResolvedEdge) -> _EdgeKey:
    return edge.source, edge.target, edge.line_id


def _graph_edge(edge_by_key: Mapping[_EdgeKey, Edge], edge: ResolvedEdge) -> Edge:
    return edge_by_key[_edge_key(edge)]


def _connector_span(
    graph: MetroGraph,
    scaffold: RouteSemanticScaffold,
    connector_ids: tuple[ConnectorId, ...],
) -> GridSpan:
    return grid_span_for_sections(
        graph,
        (
            section_id
            for connector_id in connector_ids
            for connector in (scaffold.query.connector(connector_id),)
            for section_id in (connector.source_section, connector.target_section)
        ),
    )


def _build_planner_indexes(scaffold: RouteSemanticScaffold) -> _PlannerIndexes:
    edges_by_exit_group: defaultdict[EndpointGroupId, list[ResolvedEdge]] = defaultdict(
        list
    )
    member_ids_by_system: defaultdict[RouteSystemId, list[EmissionMemberId]] = (
        defaultdict(list)
    )
    for edge in scaffold.edge_order:
        connector_ids = _ordered_unique(
            ref.connector_id for ref in scaffold.refs_by_edge[edge]
        )
        member_ids_by_system[scaffold.system_for(connector_ids)].append(
            scaffold.member_id_by_edge[edge]
        )
        for exit_group_id in _ordered_unique(
            scaffold.query.connector(connector_id).exit_group_id
            for connector_id in connector_ids
        ):
            edges_by_exit_group[exit_group_id].append(edge)
    return _PlannerIndexes(
        MappingProxyType(
            {group_id: tuple(edges) for group_id, edges in edges_by_exit_group.items()}
        ),
        MappingProxyType(
            {
                system_id: tuple(member_ids)
                for system_id, member_ids in member_ids_by_system.items()
            }
        ),
        MappingProxyType(
            {item.id: item for item in scaffold.resolution.endpoint_groups}
        ),
    )


def _source_lane_order(
    ctx: _RoutingCtx,
    source_id: str,
    exit_port_id: str,
    run_direction: Direction,
    line_ids: tuple[str, ...],
    section_id: str,
) -> tuple[tuple[str, float], ...] | None:
    if ctx.station_offsets is None:
        return None
    graph_rank = {line_id: rank for rank, line_id in enumerate(ctx.graph.lines)}
    values = []
    for line_id in line_ids:
        key = (source_id, line_id)
        port_key = (exit_port_id, line_id)
        if key not in ctx.station_offsets and port_key not in ctx.station_offsets:
            return None
        offset = (
            ctx.station_offsets[key]
            if key in ctx.station_offsets
            else ctx.station_offsets[port_key]
        )
        lateral_coordinate = (
            ctx.graph.stations[source_id].y + offset
            if run_direction in {Direction.R, Direction.L}
            else ctx.graph.stations[source_id].x
            + _tb_x_offset(ctx, source_id, line_id, section_id)
        )
        values.append((line_id, offset, lateral_coordinate))
    if len({coordinate for _line, _offset, coordinate in values}) != len(values):
        return None
    return tuple(
        (line_id, offset)
        for line_id, offset, _coordinate in sorted(
            values,
            key=lambda item: (
                item[2] * lateral_order_sign(run_direction),
                graph_rank.get(item[0], len(graph_rank)),
            ),
        )
    )


def _source_turn_requirement(
    edge: Edge,
    family_id: RouteFamilyId,
    source_run_direction: Direction,
    ctx: _RoutingCtx,
    exit_port_id: str | None = None,
) -> _SourceTurnRequirement:
    src = ctx.graph.stations[edge.source]
    tgt = ctx.graph.stations[edge.target]
    if family_id is RouteFamilyId.TB_BOTTOM_EXIT:
        if source_run_direction not in {Direction.U, Direction.D}:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:nonvertical-tb-exit",
            )
        geometry = _tb_bottom_exit_geometry(edge, src, tgt, ctx)
        if geometry.turn_direction is None:
            return _SourceTurnRequirement(
                geometry.run_direction,
                None,
                None,
                None,
                None,
            )
        assert (
            geometry.launch_coordinate is not None
            and geometry.axis_coordinate is not None
        )
        return _SourceTurnRequirement(
            geometry.run_direction,
            geometry.turn_direction,
            geometry.launch_coordinate,
            abs(geometry.axis_coordinate - geometry.launch_coordinate),
            geometry.axis_coordinate,
        )
    if family_id is RouteFamilyId.SAME_Y_STRAIGHT:
        if source_run_direction not in {Direction.R, Direction.L}:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:vertical-source-horizontal-straight",
            )
        if abs(tgt.x - src.x) <= COORD_TOLERANCE:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:degenerate-horizontal-straight",
            )
        actual_run = horizontal_direction(tgt.x - src.x)
        if actual_run is not source_run_direction:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:opposed-horizontal-straight",
            )
        return _SourceTurnRequirement(
            actual_run,
            None,
            None,
            None,
            None,
        )
    if family_id in {
        RouteFamilyId.TOP_ENTRY_L_SHAPE,
        RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
    }:
        target_port = ctx.graph.ports.get(edge.target)
        expected_side = (
            PortSide.TOP
            if family_id is RouteFamilyId.TOP_ENTRY_L_SHAPE
            else PortSide.BOTTOM
        )
        if (
            edge.source in ctx.graph.junctions
            and target_port is not None
            and target_port.side is expected_side
            and abs(
                ctx.graph.stations[edge.source].x - ctx.graph.stations[edge.target].x
            )
            <= COORD_TOLERANCE
        ):
            axis = ctx.graph.stations[edge.target].x
            feeder_x = (
                ctx.graph.stations[exit_port_id].x
                if exit_port_id is not None
                else src.x
            )
            run = horizontal_direction(axis - feeder_x)
            # The drop peels off the trunk one corner radius before the turn
            # column, or at the feeder itself when that is the nearer of the
            # two (``_perp_entry_junction_straight_drop``).  Asking a whole
            # radius of the shorter launch is what refuses a turn column too
            # close to its feeder to open a full corner.
            lead_in = min(ctx.curve_radius, abs(axis - feeder_x))
            return _SourceTurnRequirement(
                run,
                vertical_direction(tgt.y - src.y),
                axis - run.sign * lead_in,
                ctx.curve_radius,
                axis,
            )
        return _SourceTurnRequirement(
            None,
            None,
            None,
            None,
            None,
            "unsupported-subshape:unaligned-perpendicular-entry",
        )
    if family_id is RouteFamilyId.MERGE_BRANCH:
        facts = _build_inter_facts(edge, src, tgt, ctx)
        assert facts.src_col is not None
        axis = _merge_branch_lead_x(src, ctx, facts.src_col) + (
            ctx.station_offsets or {}
        ).get((edge.target, edge.line_id), 0.0)
        source_offset = (ctx.station_offsets or {}).get(
            (edge.source, edge.line_id), 0.0
        )
        delta = ctx.merge.trunk_by.get(edge.target, src.y) - (src.y + source_offset)
        return _SourceTurnRequirement(
            horizontal_direction(axis - src.x),
            vertical_direction(delta),
            src.x,
            ctx.curve_radius,
            axis,
        )
    if family_id is RouteFamilyId.MERGE_ENTRY:
        facts = _build_inter_facts(edge, src, tgt, ctx)
        entry_port = facts.merge_ep
        assert entry_port is not None
        kind = _merge_entry_route_kind(facts)
        if kind is _MergeEntryRoute.STRAIGHT:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:merge-entry-straight",
            )
        if kind is not _MergeEntryRoute.L_SHAPE:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                f"unsupported-subshape:merge-entry-{kind.value}",
            )
        fan = ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
        if fan is not None:
            fan_geometry = _l_shape_fan_source_turn(edge, src, entry_port, fan, ctx)
            return _SourceTurnRequirement(
                fan_geometry.run_direction,
                fan_geometry.turn_direction,
                fan_geometry.launch_x,
                abs(fan_geometry.axis_x - fan_geometry.launch_x),
                fan_geometry.axis_x,
            )
        axis = _l_shape_mid_x(edge, src, entry_port, facts.n, ctx)
        return _SourceTurnRequirement(
            horizontal_direction(axis - src.x),
            vertical_direction(entry_port.y - src.y),
            src.x,
            ctx.curve_radius,
            axis,
        )
    if family_id is RouteFamilyId.RIGHT_ENTRY_CROSS_ROW_WRAP:
        facts = _build_inter_facts(edge, src, tgt, ctx)
        if facts.is_left_exit:
            return _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                "unsupported-subshape:left-exit-right-entry-step",
            )
        turn = vertical_direction(tgt.y - src.y)
        _fan, _size, _delta, axis = _wrap_fan_geometry(
            ctx,
            edge,
            src,
            facts.i,
            facts.n,
            turn,
        )
        return _SourceTurnRequirement(
            horizontal_direction(axis - src.x),
            turn,
            src.x,
            abs(axis - src.x),
            axis,
        )
    turn_delta = tgt.y - src.y
    if abs(turn_delta) <= COORD_TOLERANCE:
        return _SourceTurnRequirement(
            None, None, None, None, None, "missing-source-turn"
        )
    fan = ctx.junction_fan_info.get((edge.source, edge.target, edge.line_id))
    if fan is not None:
        fan_geometry = _l_shape_fan_source_turn(edge, src, tgt, fan, ctx)
        return _SourceTurnRequirement(
            fan_geometry.run_direction,
            fan_geometry.turn_direction,
            fan_geometry.launch_x,
            abs(fan_geometry.axis_x - fan_geometry.launch_x),
            fan_geometry.axis_x,
        )
    members, _source_center, _target_center = gather_tapered_bundle(ctx, edge)
    target_offset = next(
        target
        for _member_edge, line_id, _source, target in members
        if line_id == edge.line_id
    )
    turn_direction = vertical_direction(turn_delta)
    centreline_axis = _l_shape_mid_x(edge, src, tgt, len(members), ctx)
    emitted_axis = centreline_axis + lateral_order_sign(turn_direction) * target_offset
    return _SourceTurnRequirement(
        horizontal_direction(emitted_axis - src.x),
        turn_direction,
        src.x,
        abs(emitted_axis - src.x),
        None,
    )


def _fixed_axis(
    edge: Edge,
    family_id: RouteFamilyId,
    ctx: _RoutingCtx,
) -> float | None:
    source = ctx.graph.stations[edge.source]
    target = ctx.graph.stations[edge.target]
    source_port = ctx.graph.ports.get(edge.source)
    source_run = (
        Direction.U
        if source_port is not None and source_port.side is PortSide.TOP
        else Direction.D
        if source_port is not None and source_port.side is PortSide.BOTTOM
        else horizontal_direction(target.x - source.x)
    )
    return _source_turn_requirement(
        edge,
        family_id,
        source_run,
        ctx,
    ).fixed_axis


def _roles(
    graph: MetroGraph,
    edge: ResolvedEdge,
    family_id: RouteFamilyId,
    *,
    continuation: bool,
) -> tuple[EmissionRole, ...]:
    roles = set(_member_roles(graph, edge, family_id))
    roles.add(EmissionRole.CONTINUATION if continuation else EmissionRole.PEEL_OFF)
    return tuple(role for role in EmissionRole if role in roles)


def _slot_is_available(
    graph: MetroGraph,
    offsets: Mapping[tuple[str, str], float],
    station_id: str,
    line_id: str,
    desired: float,
) -> bool:
    return not any(
        other_line != line_id
        and abs(offsets.get((station_id, other_line), 0.0) - desired) <= COORD_TOLERANCE
        for other_line in graph.station_lines(station_id)
    )


def _lane_transition(
    graph: MetroGraph,
    edge: ResolvedEdge,
    claimant_member_ids: tuple[EmissionMemberId, ...],
    source_offset: float,
    target_offset: float,
    source_lane_offset: float,
    target_lane_offset: float,
    run_direction: Direction,
    placement: ExitLaneTransitionPlacement,
) -> ExitLaneTransition | None:
    source = graph.stations[edge.source]
    target = graph.stations[edge.target]
    primary_delta = (
        target.x - source.x
        if direction_axis(run_direction) is DemandAxis.X
        else target.y - source.y
    )
    source_lateral = (
        source.y + source_offset
        if direction_axis(run_direction) is DemandAxis.X
        else source.x + source_offset
    )
    target_lateral = (
        target.y + target_offset
        if direction_axis(run_direction) is DemandAxis.X
        else target.x + target_offset
    )
    diagonal = abs(target_lateral - source_lateral)
    if diagonal <= COORD_TOLERANCE:
        return None
    if primary_delta * run_direction.sign < 2 * MIN_STRAIGHT_EDGE + diagonal:
        return None
    return ExitLaneTransition(
        edge,
        claimant_member_ids,
        (source.x, source.y),
        (target.x, target.y),
        source_offset,
        target_offset,
        source_lane_offset,
        target_lane_offset,
        run_direction,
        placement,
        diagonal,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_EDGE,
    )


def _lane_transition_order_is_preserved(
    transitions: Iterable[ExitLaneTransition],
) -> bool:
    groups: dict[
        tuple[str, str, Direction],
        list[tuple[float, float]],
    ] = defaultdict(list)
    for transition in transitions:
        if direction_axis(transition.run_direction) is DemandAxis.X:
            source_lateral = transition.source_point[1] + transition.source_offset
            target_lateral = transition.target_point[1] + transition.target_offset
        else:
            source_lateral = transition.source_point[0] + transition.source_offset
            target_lateral = transition.target_point[0] + transition.target_offset
        groups[
            (
                transition.edge.source,
                transition.edge.target,
                transition.run_direction,
            )
        ].append((source_lateral, target_lateral))
    return all(
        (source_a - source_b) * (target_a - target_b) >= 0
        for values in groups.values()
        for index, (source_a, target_a) in enumerate(values)
        for source_b, target_b in values[index + 1 :]
    )


def _source_lane_ownership(
    graph: MetroGraph,
    offsets: Mapping[tuple[str, str], float],
    exit_port_id: str,
    source_id: str,
    line_id: str,
    claimant_member_ids: tuple[EmissionMemberId, ...],
    desired: float,
    run_direction: Direction,
    ctx: _RoutingCtx,
) -> tuple[tuple[str, ...], tuple[ExitLaneTransition, ...], str | None]:
    stations = [exit_port_id]
    if source_id != exit_port_id:
        stations.append(source_id)
    section_id = graph.ports[exit_port_id].section_id
    exit_station = graph.stations[exit_port_id]
    candidates = [
        edge.source
        for edge in graph.edges_to(exit_port_id)
        if edge.line_id == line_id
        and graph.stations[edge.source].section_id == section_id
        and (
            abs(graph.stations[edge.source].y - exit_station.y) <= COORD_TOLERANCE
            if direction_axis(run_direction) is DemandAxis.X
            else abs(graph.stations[edge.source].x - exit_station.x) <= COORD_TOLERANCE
        )
    ]
    mismatched = [
        station_id
        for station_id in candidates
        if abs(offsets.get((station_id, line_id), 0.0) - desired) > COORD_TOLERANCE
    ]
    if not mismatched:
        return tuple(stations), (), None
    if len(candidates) != 1:
        return (), (), "ambiguous-source-lane-boundary"
    candidate = candidates[0]
    source_lane_offset = offsets.get((candidate, line_id), 0.0)
    target_lane_offset = desired
    source_offset = source_lane_offset
    target_offset = target_lane_offset
    if direction_axis(run_direction) is DemandAxis.Y:
        source_ctx = replace(ctx, station_offsets=dict(offsets))
        target_offsets = dict(offsets)
        target_offsets[exit_port_id, line_id] = desired
        target_ctx = replace(ctx, station_offsets=target_offsets)
        source_offset = _tb_x_offset(
            source_ctx,
            candidate,
            line_id,
            graph.stations[candidate].section_id,
        )
        target_offset = _tb_x_offset(
            target_ctx,
            exit_port_id,
            line_id,
            graph.stations[exit_port_id].section_id,
        )
    transition = _lane_transition(
        graph,
        ResolvedEdge(candidate, exit_port_id, line_id),
        claimant_member_ids,
        source_offset,
        target_offset,
        source_lane_offset,
        target_lane_offset,
        run_direction,
        ExitLaneTransitionPlacement.TARGET,
    )
    if transition is None:
        return (), (), "source-lane-transition-has-no-runway"
    return tuple(stations), (transition,), None


def _continuation_lane_ownership(
    graph: MetroGraph,
    offsets: Mapping[tuple[str, str], float],
    source_id: str,
    entry_id: str,
    line_id: str,
    claimant_member_id: EmissionMemberId,
    desired: float,
    run_direction: Direction,
    family_id: RouteFamilyId,
    ctx: _RoutingCtx,
) -> tuple[tuple[str, ...], tuple[ExitLaneTransition, ...], str | None]:
    if family_id is RouteFamilyId.TB_BOTTOM_EXIT:
        source_offsets = dict(offsets)
        source_offsets[source_id, line_id] = desired
        seam_ctx = replace(ctx, station_offsets=source_offsets, built_routes=[])
        resolved_edge = ResolvedEdge(source_id, entry_id, line_id)
        edge = _graph_edge(ctx.edge_by_key, resolved_edge)
        source, target = graph.edge_endpoints(edge)
        source_crossing = _tb_bottom_exit_geometry(
            edge,
            source,
            target,
            seam_ctx,
        ).points[-1][0]
        target_crossing = _perp_entry_crossing_x(
            seam_ctx,
            entry_id,
            line_id,
            target.x,
        )
        if (
            target_crossing is None
            or abs(source_crossing - target_crossing) > COORD_TOLERANCE
        ):
            return (), (), "unresolved-perpendicular-entry-seam"
        return (), (), None

    stations = []
    transitions = []
    if not _slot_is_available(graph, offsets, entry_id, line_id, desired):
        source_lane_offset = desired
        target_lane_offset = offsets.get((entry_id, line_id), 0.0)
        source_offset = source_lane_offset
        target_offset = target_lane_offset
        if direction_axis(run_direction) is DemandAxis.Y:
            source_offsets = dict(offsets)
            source_offsets[source_id, line_id] = desired
            source_offset = _tb_x_offset(
                replace(ctx, station_offsets=source_offsets),
                source_id,
                line_id,
                graph.stations[source_id].section_id,
            )
            target_offset = _tb_x_offset(
                replace(ctx, station_offsets=dict(offsets)),
                entry_id,
                line_id,
                graph.stations[entry_id].section_id,
            )
        transition = _lane_transition(
            graph,
            ResolvedEdge(source_id, entry_id, line_id),
            (claimant_member_id,),
            source_offset,
            target_offset,
            source_lane_offset,
            target_lane_offset,
            run_direction,
            ExitLaneTransitionPlacement.SOURCE,
        )
        if transition is None:
            return (), (), "continuation-transition-has-no-runway"
        return (), (transition,), None
    stations.append(entry_id)
    section_id = graph.stations[entry_id].section_id
    current = entry_id
    seen = {entry_id}
    while True:
        current_station = graph.stations[current]
        candidates = [
            edge.target
            for edge in graph.edges_from(current)
            if edge.line_id == line_id
            and edge.target not in seen
            and graph.stations[edge.target].section_id == section_id
            and (
                abs(graph.stations[edge.target].y - current_station.y)
                <= COORD_TOLERANCE
                if direction_axis(run_direction) is DemandAxis.X
                else abs(graph.stations[edge.target].x - current_station.x)
                <= COORD_TOLERANCE
            )
        ]
        if len(candidates) != 1:
            break
        candidate = candidates[0]
        candidate_port = graph.ports.get(candidate)
        if candidate_port is not None and not candidate_port.is_entry:
            break
        if not _slot_is_available(graph, offsets, candidate, line_id, desired):
            source_lane_offset = desired
            target_lane_offset = offsets.get((candidate, line_id), 0.0)
            source_offset = source_lane_offset
            target_offset = target_lane_offset
            if direction_axis(run_direction) is DemandAxis.Y:
                source_offsets = dict(offsets)
                source_offsets[current, line_id] = desired
                source_offset = _tb_x_offset(
                    replace(ctx, station_offsets=source_offsets),
                    current,
                    line_id,
                    graph.stations[current].section_id,
                )
                target_offset = _tb_x_offset(
                    replace(ctx, station_offsets=dict(offsets)),
                    candidate,
                    line_id,
                    graph.stations[candidate].section_id,
                )
            transition = _lane_transition(
                graph,
                ResolvedEdge(current, candidate, line_id),
                (claimant_member_id,),
                source_offset,
                target_offset,
                source_lane_offset,
                target_lane_offset,
                run_direction,
                ExitLaneTransitionPlacement.SOURCE,
            )
            if transition is None:
                return (), (), "continuation-transition-has-no-runway"
            transitions.append(transition)
            break
        current = candidate
        seen.add(current)
        stations.append(current)
    return tuple(stations), tuple(transitions), None


def _classify_assignment_seeds(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    exit_group: EndpointGroup,
    outbound_edges: tuple[ResolvedEdge, ...],
    lane_rank: Mapping[str, int],
    source_run_direction: Direction,
    exit_port_id: str,
) -> _AssignmentClassification:
    classified: list[_ClassifiedMember] = []
    unclassified: list[EmissionMemberId] = []
    straight_edges: list[ResolvedEdge] = []
    reason: str | None = None
    for edge in outbound_edges:
        connector_ids = _ordered_unique(
            ref.connector_id
            for ref in scaffold.refs_by_edge[edge]
            if ref.connector_id in exit_group.connector_ids
        )
        entry_group_ids = _ordered_unique(
            scaffold.query.connector(item).entry_group_id for item in connector_ids
        )
        if len(entry_group_ids) != 1:
            reason = reason or "multiple-destinations"
            unclassified.append(scaffold.member_id_by_edge[edge])
            continue
        graph_edge = _graph_edge(ctx.edge_by_key, edge)
        src, tgt = graph.edge_endpoints(graph_edge)
        family_id = classify_inter_section_family(graph_edge, src, tgt, ctx)
        if family_id is None:
            reason = reason or "missing-production-family"
            unclassified.append(scaffold.member_id_by_edge[edge])
            continue
        if family_id not in PLANNED_EXIT_FAMILIES:
            reason = reason or f"unsupported-family:{family_id.value}"
            requirement = _SourceTurnRequirement(
                None,
                None,
                None,
                None,
                None,
                f"unsupported-family:{family_id.value}",
            )
        else:
            requirement = _source_turn_requirement(
                graph_edge,
                family_id,
                source_run_direction,
                ctx,
                exit_port_id,
            )
        if (
            family_id is RouteFamilyId.MERGE_BRANCH
            and requirement.turn_direction is not None
            and requirement.run_direction is not source_run_direction
        ):
            requirement = replace(
                requirement,
                legacy_reason="opposed-source-run",
            )
        reason = reason or requirement.legacy_reason
        if requirement.turn_direction is None and requirement.legacy_reason is None:
            straight_edges.append(edge)
        elif (
            requirement.turn_direction is None
            and family_id is not RouteFamilyId.MERGE_ENTRY
        ):
            reason = reason or "missing-source-turn"
        classified.append(
            _ClassifiedMember(
                edge,
                scaffold.member_id_by_edge[edge],
                connector_ids,
                entry_group_ids[0],
                family_id,
                lane_rank[edge.line_id],
                requirement.run_direction,
                requirement.turn_direction,
                requirement.launch_coordinate,
                requirement.minimum_runway,
                requirement.fixed_axis,
            )
        )
    if len({edge.target for edge in straight_edges}) > 1:
        reason = reason or "ambiguous-continuation"
    straight_set = frozenset(straight_edges)
    seeds = tuple(
        _AssignmentSeed(
            item.edge,
            item.member_id,
            item.connector_ids,
            item.entry_group_id,
            item.family_id,
            item.lane_rank,
            _roles(
                graph,
                item.edge,
                item.family_id,
                continuation=item.edge in straight_set,
            ),
            item.run_direction,
            item.turn_direction,
            item.launch_coordinate,
            item.minimum_runway,
            item.fixed_axis,
        )
        for item in classified
    )
    return _AssignmentClassification(seeds, tuple(unclassified), reason)


def _plan_turn_axes(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    plan_id: ExitTurnPlanId,
    source_id: str,
    exit_port_id: str,
    source_run_direction: Direction,
    ordered_lanes: tuple[tuple[str, float], ...],
    seeds: tuple[_AssignmentSeed, ...],
) -> _AxisPlan:
    del graph, source_id, exit_port_id
    turning_seeds = tuple(seed for seed in seeds if seed.turn_direction is not None)
    minimum_runway = max(
        (
            seed.minimum_runway
            for seed in turning_seeds
            if seed.minimum_runway is not None
        ),
        default=ctx.curve_radius,
    )
    if not turning_seeds:
        return _AxisPlan((), MappingProxyType({}), minimum_runway, None)
    if any(
        seed.run_direction not in set(Direction)
        or seed.turn_direction not in set(Direction)
        or direction_axis(seed.run_direction)
        is not direction_axis(source_run_direction)
        or direction_axis(seed.run_direction) is direction_axis(seed.turn_direction)
        or seed.launch_coordinate is None
        or seed.minimum_runway is None
        or seed.minimum_runway <= 0
        for seed in turning_seeds
    ):
        return _AxisPlan(
            (), MappingProxyType({}), minimum_runway, "invalid-source-turn-requirement"
        )

    cohorts: dict[tuple[Direction, Direction], list[_AssignmentSeed]] = defaultdict(
        list
    )
    for seed in turning_seeds:
        assert seed.run_direction is not None and seed.turn_direction is not None
        cohorts[seed.run_direction, seed.turn_direction].append(seed)

    built_axes: list[ExitTurnAxis] = []
    axis_by_member: dict[EmissionMemberId, ExitTurnAxis] = {}
    for (run_direction, turn_direction), cohort in cohorts.items():
        ranks = tuple(sorted({seed.lane_rank for seed in cohort}))
        cohort_rank = {rank: index for index, rank in enumerate(ranks)}
        progression = lateral_order_sign(turn_direction)
        fixed_origins = tuple(
            seed.fixed_axis
            - progression * cohort_rank[seed.lane_rank] * ctx.offset_step
            for seed in cohort
            if seed.fixed_axis is not None
        )
        if fixed_origins and any(
            abs(item - fixed_origins[0]) > COORD_TOLERANCE for item in fixed_origins[1:]
        ):
            return _AxisPlan(
                (), MappingProxyType({}), minimum_runway, "fixed-axis-conflict"
            )
        if fixed_origins:
            origin = fixed_origins[0]
        else:
            required_origins = tuple(
                seed.launch_coordinate
                + run_direction.sign * seed.minimum_runway
                - progression * cohort_rank[seed.lane_rank] * ctx.offset_step
                for seed in cohort
                if seed.launch_coordinate is not None
                and seed.minimum_runway is not None
            )
            origin = (
                max(required_origins)
                if run_direction.sign > 0
                else min(required_origins)
            )
        coordinates = {
            rank: origin + progression * cohort_rank[rank] * ctx.offset_step
            for rank in ranks
        }
        insufficient_runway = tuple(
            seed
            for seed in cohort
            if seed.launch_coordinate is not None
            and seed.minimum_runway is not None
            and (coordinates[seed.lane_rank] - seed.launch_coordinate)
            * run_direction.sign
            < seed.minimum_runway - COORD_TOLERANCE
        )
        if insufficient_runway:
            reason = (
                "insufficient-structural-runway"
                if any(
                    seed.family_id
                    in {
                        RouteFamilyId.TOP_ENTRY_L_SHAPE,
                        RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
                    }
                    for seed in insufficient_runway
                )
                else "insufficient-fixed-runway"
            )
            return _AxisPlan((), MappingProxyType({}), minimum_runway, reason)
        for rank in ranks:
            lane_line = ordered_lanes[rank][0]
            rank_seeds = tuple(seed for seed in cohort if seed.lane_rank == rank)
            claimant_ids = tuple(seed.member_id for seed in rank_seeds)
            fixed_seed = next(
                (seed for seed in rank_seeds if seed.fixed_axis is not None),
                None,
            )
            coordinate = coordinates[rank]
            fixed_anchor_id = None
            fixed_anchor_offset = None
            if fixed_seed is not None:
                target_anchored = fixed_seed.family_id in {
                    RouteFamilyId.MERGE_BRANCH,
                    RouteFamilyId.TOP_ENTRY_L_SHAPE,
                    RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
                }
                fixed_anchor_id = (
                    fixed_seed.edge.target
                    if target_anchored
                    else fixed_seed.edge.source
                )
                fixed_anchor_offset = (
                    (ctx.station_offsets or {}).get(
                        (fixed_seed.edge.target, fixed_seed.edge.line_id),
                        0.0,
                    )
                    if fixed_seed.family_id is RouteFamilyId.MERGE_BRANCH
                    else 0.0
                )
            fixed_anchor_coordinate = (
                coordinate - fixed_anchor_offset
                if fixed_anchor_offset is not None
                else None
            )
            axis = ExitTurnAxis(
                ExitTurnAxisId(
                    semantic_route_id(
                        "exit-turn-axis",
                        plan_id,
                        lane_line,
                        run_direction.value,
                        turn_direction.value,
                    )
                ),
                lane_line,
                direction_axis(run_direction),
                coordinate,
                rank,
                claimant_ids,
                fixed_anchor_id,
                fixed_anchor_coordinate,
                fixed_anchor_offset,
            )
            built_axes.append(axis)
            for seed in rank_seeds:
                axis_by_member[seed.member_id] = axis
    return _AxisPlan(
        tuple(built_axes), MappingProxyType(axis_by_member), minimum_runway, None
    )


def _plan_lane_ownership(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    exit_port_id: str,
    source_id: str,
    seeds: tuple[_AssignmentSeed, ...],
    ordered_lanes: tuple[tuple[str, float], ...],
    planned_offsets: Mapping[str, float],
    source_run_direction: Direction,
) -> _LaneOwnership:
    assert ctx.station_offsets is not None
    station_ids_by_line: dict[str, list[str]] = defaultdict(list)
    transitions: list[ExitLaneTransition] = []
    for line_id, _input_offset in ordered_lanes:
        claimant_member_ids = tuple(
            seed.member_id for seed in seeds if seed.edge.line_id == line_id
        )
        stations, lane_transitions, reason = _source_lane_ownership(
            graph,
            ctx.station_offsets,
            exit_port_id,
            source_id,
            line_id,
            claimant_member_ids,
            planned_offsets[line_id],
            source_run_direction,
            ctx,
        )
        if reason is not None:
            return _LaneOwnership(MappingProxyType({}), (), reason)
        station_ids_by_line[line_id].extend(stations)
        transitions.extend(lane_transitions)
    for seed in seeds:
        if seed.turn_direction is not None:
            continue
        stations, lane_transitions, reason = _continuation_lane_ownership(
            graph,
            ctx.station_offsets,
            source_id,
            seed.edge.target,
            seed.edge.line_id,
            seed.member_id,
            planned_offsets[seed.edge.line_id],
            seed.run_direction or source_run_direction,
            seed.family_id,
            ctx,
        )
        if reason is not None:
            return _LaneOwnership(MappingProxyType({}), (), reason)
        station_ids_by_line[seed.edge.line_id].extend(stations)
        transitions.extend(lane_transitions)
    if not _lane_transition_order_is_preserved(transitions):
        return _LaneOwnership(
            MappingProxyType({}),
            (),
            "lane-transition-order-inversion",
        )
    return _LaneOwnership(
        MappingProxyType(
            {
                line_id: tuple(_ordered_unique(station_ids))
                for line_id, station_ids in station_ids_by_line.items()
            }
        ),
        tuple(transitions),
        None,
    )


def _build_group_plan(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    indexes: _PlannerIndexes,
    exit_group: EndpointGroup,
    provenance: RoutePlanProvenance,
) -> _BuiltGroupPlan:
    query = scaffold.query
    exit_port_id = query.exit_port(exit_group.id)
    divergence = next(
        (
            item
            for item in query.divergences
            if item.group.exit_group_id == exit_group.id
        ),
        None,
    )
    source_id = divergence.junction_id if divergence is not None else exit_port_id
    system_id = scaffold.system_for(exit_group.connector_ids)
    plan_id = ExitTurnPlanId(
        semantic_route_id("exit-turn-plan", system_id, exit_group.id, source_id)
    )
    member_edges = tuple(
        edge
        for edge in indexes.member_edges_by_exit_group.get(exit_group.id, ())
        if (
            edge.source == source_id
            or (
                divergence is not None
                and edge.source == exit_port_id
                and edge.target == source_id
            )
        )
    )
    member_ids = tuple(scaffold.member_id_by_edge[edge] for edge in member_edges)
    system_member_ids = indexes.member_ids_by_system.get(system_id, ())
    outbound_edges = tuple(edge for edge in member_edges if edge.source == source_id)
    reason: str | None = None
    represented_connectors = {
        ref.connector_id
        for edge in outbound_edges
        for ref in scaffold.refs_by_edge[edge]
        if ref.connector_id in exit_group.connector_ids
    }
    missing_connectors = tuple(
        item for item in exit_group.connector_ids if item not in represented_connectors
    )
    if not outbound_edges or missing_connectors:
        reason = "missing-outbound-member"

    run_direction = {
        PortSide.RIGHT: Direction.R,
        PortSide.LEFT: Direction.L,
        PortSide.TOP: Direction.U,
        PortSide.BOTTOM: Direction.D,
    }[exit_group.side]
    source_axis = direction_axis(run_direction)
    line_ids = _ordered_unique(
        edge.line_id for edge in (outbound_edges or member_edges)
    )
    if len(line_ids) == 1 and len(outbound_edges) == 1 and reason is None:
        reason = "single-member-group"
    ordered_lanes = _source_lane_order(
        ctx,
        source_id,
        exit_port_id,
        run_direction,
        line_ids,
        exit_group.section_id,
    )
    lane_order_source = ExitLaneOrderSource.STATION_OFFSETS
    if ordered_lanes is None:
        lane_order_source = ExitLaneOrderSource.GRAPH_LINE_ORDER_FALLBACK
        if reason is None:
            reason = "missing-or-ambiguous-source-order"
        graph_rank = {line_id: rank for rank, line_id in enumerate(graph.lines)}
        ordered_lanes = tuple(
            (line_id, 0.0)
            for line_id in sorted(
                line_ids,
                key=lambda line_id: graph_rank.get(line_id, len(graph_rank)),
            )
        )
    lane_rank = {line_id: rank for rank, (line_id, _offset) in enumerate(ordered_lanes)}
    base_offset = min((offset for _line, offset in ordered_lanes), default=0.0)

    classification = _classify_assignment_seeds(
        graph,
        ctx,
        scaffold,
        exit_group,
        outbound_edges,
        lane_rank,
        run_direction,
        exit_port_id,
    )
    seeds = classification.seeds
    unclassified_member_ids = classification.unclassified_member_ids
    reason = reason or classification.legacy_reason
    offsets_increase_with_rank = (
        len(ordered_lanes) < 2 or ordered_lanes[-1][1] > ordered_lanes[0][1]
    )
    planned_offsets = {
        line_id: base_offset
        + (rank if offsets_increase_with_rank else len(ordered_lanes) - rank - 1)
        * ctx.offset_step
        for rank, (line_id, _input_offset) in enumerate(ordered_lanes)
    }
    ownership = _LaneOwnership(MappingProxyType({}), (), None)
    if reason is None:
        ownership = _plan_lane_ownership(
            graph,
            ctx,
            exit_port_id,
            source_id,
            seeds,
            ordered_lanes,
            planned_offsets,
            run_direction,
        )
        reason = ownership.legacy_reason
    if reason is None:
        tentative_offsets = dict(ctx.station_offsets or {})
        for line_id, station_ids in ownership.station_ids_by_line.items():
            for station_id in station_ids:
                tentative_offsets[station_id, line_id] = planned_offsets[line_id]
        final_classification = _classify_assignment_seeds(
            graph,
            replace(ctx, station_offsets=tentative_offsets, built_routes=[]),
            scaffold,
            exit_group,
            outbound_edges,
            lane_rank,
            run_direction,
            exit_port_id,
        )
        if final_classification.legacy_reason is not None:
            reason = final_classification.legacy_reason
        elif tuple(seed.family_id for seed in final_classification.seeds) != tuple(
            seed.family_id for seed in seeds
        ):
            reason = "family-changed-after-lane-compaction"
        else:
            seeds = final_classification.seeds
    axis_plan = _AxisPlan((), MappingProxyType({}), ctx.curve_radius, None)
    if reason is None:
        assert run_direction is not None
        axis_plan = _plan_turn_axes(
            graph,
            replace(ctx, station_offsets=tentative_offsets, built_routes=[]),
            plan_id,
            source_id,
            exit_port_id,
            run_direction,
            ordered_lanes,
            seeds,
        )
        reason = axis_plan.legacy_reason
    axes = axis_plan.axes
    axis_by_member = axis_plan.axis_by_member
    minimum_runway = axis_plan.minimum_runway
    station_ids_by_line = ownership.station_ids_by_line
    lane_transitions = ownership.transitions
    if reason is not None:
        axes = ()
        axis_by_member = MappingProxyType({})
        station_ids_by_line = MappingProxyType({})
        lane_transitions = ()
    disposition = (
        ExitTurnDisposition.PLANNED if reason is None else ExitTurnDisposition.LEGACY
    )
    source_lanes = tuple(
        ExitSourceLane(
            line_id,
            rank,
            tuple(
                scaffold.member_id_by_edge[edge]
                for edge in member_edges
                if edge.line_id == line_id
            ),
            station_ids_by_line.get(line_id, ()),
            input_offset,
            planned_offsets[line_id]
            if disposition is ExitTurnDisposition.PLANNED
            else input_offset,
        )
        for rank, (line_id, input_offset) in enumerate(ordered_lanes)
    )
    assignments = tuple(
        ExitTurnAssignment(
            seed.member_id,
            seed.entry_group_id,
            indexes.endpoint_by_id[seed.entry_group_id].section_id,
            graph.sections[
                indexes.endpoint_by_id[seed.entry_group_id].section_id
            ].grid_col,
            graph.sections[
                indexes.endpoint_by_id[seed.entry_group_id].section_id
            ].grid_row,
            indexes.endpoint_by_id[seed.entry_group_id].side,
            seed.lane_rank,
            seed.family_id,
            seed.roles,
            seed.run_direction,
            seed.turn_direction,
            seed.launch_coordinate,
            seed.minimum_runway,
            turn_handedness(seed.run_direction, seed.turn_direction)
            if seed.turn_direction is not None and seed.run_direction is not None
            else None,
            (
                axis_by_member[seed.member_id].id
                if disposition is ExitTurnDisposition.PLANNED
                and seed.turn_direction is not None
                else None
            ),
        )
        for seed in seeds
    )
    span = _connector_span(graph, scaffold, exit_group.connector_ids)
    decision_refs = reservation_decision_refs(
        provenance, exit_group.connector_ids, span
    )
    reference = None
    built_demands: list[SymbolicDemand] = []
    reference_id = None
    if disposition is ExitTurnDisposition.PLANNED and axes:
        reference_id = SharedReferenceId(
            semantic_route_id("shared-reference", plan_id, "ordered-turns")
        )
        turning_claimants = tuple(
            seed.member_id for seed in seeds if seed.turn_direction is not None
        )
        cohort_sizes = defaultdict(set)
        for seed in seeds:
            if seed.turn_direction is not None and seed.run_direction is not None:
                cohort_sizes[seed.run_direction, seed.turn_direction].add(
                    seed.lane_rank
                )
        ordered_turn_span = max(
            ((len(ranks) - 1) * ctx.offset_step for ranks in cohort_sizes.values()),
            default=0.0,
        )
        reference = SharedReference(
            reference_id,
            system_id,
            SharedReferenceKind.ORDERED_TURNS,
            turning_claimants,
            CoordinateRegime.LAYOUT_CANVAS,
            decision_refs,
        )
        ordered_demand_id = DemandId(
            semantic_route_id("symbolic-demand", plan_id, "ordered-turns")
        )
        runway_demand_id = DemandId(
            semantic_route_id("symbolic-demand", plan_id, "runway")
        )
        built_demands.extend(
            (
                SymbolicDemand(
                    ordered_demand_id,
                    system_id,
                    turning_claimants,
                    DemandKind.ORDERED_TURNS,
                    source_axis if source_axis is not None else DemandAxis.X,
                    span,
                    len(axes),
                    ordered_turn_span,
                    CoordinateRegime.LAYOUT_CANVAS,
                    (reference_id,),
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    decision_refs,
                ),
                SymbolicDemand(
                    runway_demand_id,
                    system_id,
                    turning_claimants,
                    DemandKind.RUNWAY,
                    source_axis if source_axis is not None else DemandAxis.X,
                    span,
                    len(axes),
                    minimum_runway,
                    CoordinateRegime.LAYOUT_CANVAS,
                    (reference_id,),
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    decision_refs,
                ),
            )
        )
    if disposition is ExitTurnDisposition.PLANNED:
        for transition in lane_transitions:
            built_demands.append(
                SymbolicDemand(
                    DemandId(
                        semantic_route_id(
                            "symbolic-demand",
                            plan_id,
                            "lane-transition",
                            transition.edge.source,
                            transition.edge.target,
                            transition.edge.line_id,
                        )
                    ),
                    system_id,
                    transition.claimant_member_ids,
                    DemandKind.RUNWAY,
                    direction_axis(transition.run_direction),
                    span,
                    1,
                    transition.source_runway
                    + transition.diagonal_run
                    + transition.target_runway,
                    CoordinateRegime.LAYOUT_CANVAS,
                    (),
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    decision_refs,
                )
            )
    demands = tuple(built_demands)
    plan = ExitTurnPlan(
        plan_id,
        system_id,
        exit_group.id,
        exit_port_id,
        divergence.group.id if divergence is not None else None,
        source_id,
        run_direction,
        source_axis if source_axis is not None else DemandAxis.X,
        exit_group.connector_ids,
        system_member_ids,
        member_ids,
        source_lanes,
        lane_order_source,
        tuple(lane_transitions),
        axes,
        assignments,
        tuple(unclassified_member_ids),
        ctx.offset_step,
        minimum_runway,
        reference_id,
        tuple(item.id for item in demands),
        (),
        disposition,
        reason,
        decision_refs,
    )
    diagnostic = (
        RoutePlanDiagnostic(
            member_ids[0] if member_ids else None,
            "exit-turn-legacy",
            f"exit group {exit_group.id} uses legacy routing: {reason}",
            blocking=False,
        )
        if disposition is ExitTurnDisposition.LEGACY
        else None
    )
    assignment_by_edge = {
        seed.edge: assignment
        for seed, assignment in zip(seeds, assignments, strict=True)
    }
    return _BuiltGroupPlan(
        plan,
        reference,
        demands,
        diagnostic,
        MappingProxyType(assignment_by_edge),
    )


def _legacy_plan(plan: ExitTurnPlan, reason: str) -> ExitTurnPlan:
    return replace(
        plan,
        source_lanes=tuple(
            replace(
                lane,
                station_ids=(),
                planned_offset=lane.input_offset,
            )
            for lane in plan.source_lanes
        ),
        lane_transitions=(),
        axes=(),
        assignments=tuple(
            replace(assignment, axis_id=None) for assignment in plan.assignments
        ),
        reference_id=None,
        demand_ids=(),
        foreign_reference_ids=(),
        disposition=ExitTurnDisposition.LEGACY,
        legacy_reason=reason,
    )


def _index_unique_member_owners(
    plans: Iterable[ExitTurnPlan],
) -> dict[EmissionMemberId, ExitTurnPlan]:
    owners: dict[EmissionMemberId, ExitTurnPlan] = {}
    for plan in plans:
        for member_id in plan.member_ids:
            incumbent = owners.setdefault(member_id, plan)
            if incumbent.id == plan.id:
                continue
            raise ExitTurnInvariantError(
                f"{_failure(incumbent, f'member {member_id} has another owner')}; "
                f"{_failure(plan, f'member {member_id} has another owner')}"
            )
    return owners


def _adopt_prior_dispositions(
    ctx: _RoutingCtx,
    plans: Iterable[ExitTurnPlan],
    reasons: dict[ExitTurnPlanId, str],
) -> frozenset[ExitTurnPlanId]:
    """Redraw the frozen pass's exit-turn dispositions on a settlement re-route.

    The cross-plan fallback verdicts are measured on live coordinates, so a
    re-route across settled geometry can reach a different verdict than the
    pass whose plan was frozen -- wider gaps clear an overlap, or a consumed
    reserved band creates one.  Those verdicts are decisions, and settlement
    does not own decisions: the frozen reason stands, in both directions.

    Returns the plan ids where the frozen reason actually differed from this
    pass's own fresh verdict, for the caller to publish as a diagnostic.
    """
    prior = ctx.prior_exit_turn_dispositions
    if prior is None:
        return frozenset()
    planned = {
        plan.id for plan in plans if plan.disposition is ExitTurnDisposition.PLANNED
    }
    shared = planned & prior.keys()
    overridden = frozenset(
        plan_id for plan_id in shared if reasons.get(plan_id) != prior.get(plan_id)
    )
    held = {
        plan_id: reason for plan_id in shared if (reason := prior[plan_id]) is not None
    }
    for plan_id in shared:
        reasons.pop(plan_id, None)
    reasons.update(held)
    return overridden


def _cross_plan_fallback_reasons(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    plans: Iterable[ExitTurnPlan],
    assignments_by_plan: Mapping[
        ExitTurnPlanId, Mapping[ResolvedEdge, ExitTurnAssignment]
    ],
    frame_ownership: LinearEntryFrameOwnership,
) -> dict[ExitTurnPlanId, str]:
    plans = tuple(plans)
    reasons: dict[ExitTurnPlanId, str] = {}
    compatibility_channels = _compatibility_channel_claims(
        graph,
        ctx,
        plans,
        assignments_by_plan,
    )
    station_owners: defaultdict[tuple[str, str], set[ExitTurnPlanId]] = defaultdict(set)
    planned_station_offsets: dict[tuple[str, str], float] = {}
    for plan in plans:
        if plan.disposition is not ExitTurnDisposition.PLANNED:
            continue
        for lane in plan.source_lanes:
            for station_id in lane.station_ids:
                key = station_id, lane.line_id
                owners = station_owners[key]
                if owners and plan.id not in owners:
                    for owner in owners:
                        reasons[owner] = "shared-source-ownership-conflict"
                    reasons[plan.id] = "shared-source-ownership-conflict"
                owners.add(plan.id)
                planned_station_offsets[key] = lane.planned_offset
    conflicting_plan_ids = {
        plan_id
        for key in conflicting_linear_entry_frame_assignments(
            planned_station_offsets, frame_ownership
        )
        for plan_id in station_owners[key]
    }
    for plan in plans:
        if (
            plan.disposition is ExitTurnDisposition.PLANNED
            and plan.id in conflicting_plan_ids
        ):
            reasons.setdefault(plan.id, "linear-entry-frame-ownership-conflict")
    for plan in plans:
        if plan.disposition is not ExitTurnDisposition.PLANNED or plan.id in reasons:
            continue
        assignment_by_id = {item.member_id: item for item in plan.assignments}
        for axis in plan.axes:
            merge_anchored = any(
                assignment_by_id[member_id].planned_family_id
                is RouteFamilyId.MERGE_BRANCH
                for member_id in axis.claimant_member_ids
            )
            if not merge_anchored or axis.fixed_anchor_id is None:
                continue
            proposed = planned_station_offsets.get((axis.fixed_anchor_id, axis.line_id))
            if (
                proposed is not None
                and axis.fixed_anchor_offset is not None
                and abs(proposed - axis.fixed_anchor_offset) > COORD_TOLERANCE
            ):
                reasons[plan.id] = "fixed-anchor-owned-by-another-plan"
                break
    for plan in plans:
        if (
            plan.disposition is not ExitTurnDisposition.PLANNED
            or plan.id in reasons
            or not plan.axes
        ):
            continue
        axis_ranges = {
            axis.id: _planned_axis_cross_range(
                graph,
                ctx,
                plan,
                axis,
                assignments_by_plan[plan.id],
            )
            for axis in plan.axes
        }
        if any(
            plan.source_axis is channel.axis
            and axis.line_id != channel.line_id
            and abs(axis.coordinate - channel.coordinate) <= COORD_TOLERANCE
            and _ranges_overlap(
                *axis_ranges[axis.id],
                channel.cross_lo,
                channel.cross_hi,
            )
            for axis in plan.axes
            for channel in compatibility_channels
        ):
            reasons[plan.id] = "planned-axis-overlaps-compatibility-channel"
            continue
        if any(
            first.line_id != second.line_id
            and abs(first.coordinate - second.coordinate) <= COORD_TOLERANCE
            and _ranges_overlap(
                *axis_ranges[first.id],
                *axis_ranges[second.id],
            )
            for rank, first in enumerate(plan.axes)
            for second in plan.axes[rank + 1 :]
        ):
            reasons[plan.id] = "overlapping-planned-turn-axes"
    _add_station_lane_collision_fallbacks(graph, ctx, plans, reasons)
    return reasons


def _compatibility_channel_claims(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    plans: Iterable[ExitTurnPlan],
    assignments_by_plan: Mapping[
        ExitTurnPlanId, Mapping[ResolvedEdge, ExitTurnAssignment]
    ],
) -> tuple[_CompatibilityChannelClaim, ...]:
    claims: list[_CompatibilityChannelClaim] = []
    for plan in plans:
        if plan.disposition is not ExitTurnDisposition.LEGACY:
            continue
        for edge, assignment in assignments_by_plan[plan.id].items():
            if (
                assignment.planned_family_id
                is not RouteFamilyId.TB_BOTTOM_EXIT_AROUND_STACK
            ):
                continue
            graph_edge = _graph_edge(ctx.edge_by_key, edge)
            source, target = graph.edge_endpoints(graph_edge)
            geometry = _tb_bottom_exit_around_stack_geometry(
                _build_inter_facts(graph_edge, source, target, ctx)
            )
            claims.append(
                _CompatibilityChannelClaim(
                    edge.line_id,
                    DemandAxis.X,
                    geometry.channel_x,
                    geometry.channel_y_lo,
                    geometry.channel_y_hi,
                )
            )
    return tuple(claims)


def _add_station_lane_collision_fallbacks(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    plans: Iterable[ExitTurnPlan],
    reasons: dict[ExitTurnPlanId, str],
) -> None:
    claims: defaultdict[tuple[str, str], list[tuple[ExitTurnPlan, ExitSourceLane]]] = (
        defaultdict(list)
    )
    stations = set()
    for plan in plans:
        if plan.disposition is not ExitTurnDisposition.PLANNED:
            continue
        for lane in plan.source_lanes:
            for station_id in lane.station_ids:
                claims[station_id, lane.line_id].append((plan, lane))
                stations.add(station_id)

    while True:
        additions: dict[ExitTurnPlanId, str] = {}
        for station_id in stations:
            lines = set(graph.station_lines(station_id))
            lines.update(line_id for sid, line_id in claims if sid == station_id)
            final: dict[str, tuple[float, ExitTurnPlanId | None]] = {}
            for line_id in lines:
                active = tuple(
                    (plan, lane)
                    for plan, lane in claims.get((station_id, line_id), ())
                    if plan.id not in reasons
                )
                if active:
                    plan, lane = active[0]
                    final[line_id] = lane.planned_offset, plan.id
                else:
                    final[line_id] = (
                        (ctx.station_offsets or {}).get((station_id, line_id), 0.0),
                        None,
                    )
            ordered = tuple(final.items())
            for rank, (_first_line, (first_offset, first_owner)) in enumerate(ordered):
                for _second_line, (second_offset, second_owner) in ordered[rank + 1 :]:
                    if abs(first_offset - second_offset) > COORD_TOLERANCE:
                        continue
                    for owner in (first_owner, second_owner):
                        if owner is not None:
                            additions[owner] = "shared-station-lane-collision"
        additions = {
            plan_id: reason
            for plan_id, reason in additions.items()
            if plan_id not in reasons
        }
        if not additions:
            return
        reasons.update(additions)


def _ranges_overlap(
    first_lo: float,
    first_hi: float,
    second_lo: float,
    second_hi: float,
) -> bool:
    return min(first_hi, second_hi) - max(first_lo, second_lo) > COORD_TOLERANCE


def _planned_axis_cross_range(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    plan: ExitTurnPlan,
    axis: ExitTurnAxis,
    assignments: Mapping[ResolvedEdge, ExitTurnAssignment],
) -> tuple[float, float]:
    lane_by_line = {lane.line_id: lane for lane in plan.source_lanes}
    edge_by_member = {item.member_id: edge for edge, item in assignments.items()}
    assignment_by_member = {item.member_id: item for item in assignments.values()}
    values: list[float] = []
    for member_id in axis.claimant_member_ids:
        edge = edge_by_member[member_id]
        assignment = assignment_by_member[member_id]
        source = graph.stations[edge.source]
        target = graph.stations[edge.target]
        lane = lane_by_line[edge.line_id]
        target_offset = (ctx.station_offsets or {}).get(
            (edge.target, edge.line_id), 0.0
        )
        if plan.source_axis is DemandAxis.X:
            values.extend((source.y + lane.planned_offset, target.y + target_offset))
        else:
            source_offsets = dict(ctx.station_offsets or {})
            source_offsets[edge.source, edge.line_id] = lane.planned_offset
            tentative_ctx = replace(ctx, station_offsets=source_offsets)
            if assignment.planned_family_id is not RouteFamilyId.TB_BOTTOM_EXIT:
                raise ExitTurnInvariantError(
                    _failure(plan, "vertical turn axis has no production geometry")
                )
            geometry = _tb_bottom_exit_geometry(
                _graph_edge(ctx.edge_by_key, edge),
                source,
                target,
                tentative_ctx,
            )
            if geometry.bundle_offsets is None:
                values.extend((geometry.points[0][0], geometry.points[-1][0]))
            else:
                effective_offset = _tb_x_offset(
                    tentative_ctx,
                    edge.source,
                    edge.line_id,
                    source.section_id,
                )
                values.extend(
                    (source.x + effective_offset, target.x + effective_offset)
                )
    return min(values), max(values)


def _build_group_plans(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    indexes: _PlannerIndexes,
    provenance: RoutePlanProvenance,
) -> tuple[_BuiltGroupPlan, ...]:
    return tuple(
        _build_group_plan(
            graph,
            ctx,
            scaffold,
            indexes,
            exit_group,
            provenance,
        )
        for exit_group in scaffold.topology.exit_groups
    )


def _apply_cross_plan_fallbacks(
    plans: list[ExitTurnPlan],
    references: list[SharedReference],
    demands: list[SymbolicDemand],
    diagnostics: list[RoutePlanDiagnostic],
    assignments_by_plan: dict[ExitTurnPlanId, dict[ResolvedEdge, ExitTurnAssignment]],
    reasons: Mapping[ExitTurnPlanId, str],
) -> tuple[list[ExitTurnPlan], list[SharedReference], list[SymbolicDemand]]:
    if not reasons:
        return plans, references, demands
    conflicting_reference_ids = {
        plan.reference_id
        for plan in plans
        if plan.id in reasons and plan.reference_id is not None
    }
    conflicting_demand_ids = {
        demand_id
        for plan in plans
        if plan.id in reasons
        for demand_id in plan.demand_ids
    }
    plans = [
        _legacy_plan(plan, reasons[plan.id]) if plan.id in reasons else plan
        for plan in plans
    ]
    references = [
        item for item in references if item.id not in conflicting_reference_ids
    ]
    demands = [item for item in demands if item.id not in conflicting_demand_ids]
    for plan in plans:
        if plan.id not in reasons:
            continue
        assignment_by_id = {item.member_id: item for item in plan.assignments}
        assignments_by_plan[plan.id] = {
            edge: assignment_by_id[assignment.member_id]
            for edge, assignment in assignments_by_plan[plan.id].items()
        }
        diagnostics.append(
            RoutePlanDiagnostic(
                plan.member_ids[0] if plan.member_ids else None,
                "exit-turn-legacy",
                f"exit group {plan.exit_group_id} uses legacy routing: "
                f"{reasons[plan.id]}",
                blocking=False,
            )
        )
    return plans, references, demands


def build_exit_turn_execution(graph: MetroGraph, ctx: _RoutingCtx) -> ExitTurnExecution:
    """Plan every complete exit group before the first handler emits geometry."""
    fan_execution = graph.fan_plan_execution
    scaffold = fan_execution.scaffold if fan_execution is not None else None
    if scaffold is None:
        scaffold = build_route_semantic_scaffold(
            graph,
            ctx.topology,
            coupled_connector_groups=tuple(
                plan.connector_ids for plan in graph.fan_plans if plan.connector_ids
            ),
        )
    if scaffold is None:
        query = ExitTurnPlanQuery((), MappingProxyType({}), MappingProxyType({}))
        return ExitTurnExecution(None, (), (), (), (), query)
    frame_ownership = (
        capture_linear_entry_frame_ownership(graph, ctx.station_offsets)
        if ctx.station_offsets is not None
        else LinearEntryFrameOwnership(())
    )
    provenance = _plan_provenance(graph, scaffold.topology.connectors)
    indexes = _build_planner_indexes(scaffold)
    built_groups = _build_group_plans(graph, ctx, scaffold, indexes, provenance)
    plans: list[ExitTurnPlan] = []
    references: list[SharedReference] = []
    demands: list[SymbolicDemand] = []
    diagnostics: list[RoutePlanDiagnostic] = []
    assignments_by_plan: dict[
        ExitTurnPlanId, dict[ResolvedEdge, ExitTurnAssignment]
    ] = {}
    for built in built_groups:
        plans.append(built.plan)
        assignments_by_plan[built.plan.id] = dict(built.assignments_by_edge)
        if built.reference is not None:
            references.append(built.reference)
        demands.extend(built.demands)
        if built.diagnostic is not None:
            diagnostics.append(built.diagnostic)

    cross_plan_reasons = _cross_plan_fallback_reasons(
        graph,
        ctx,
        plans,
        assignments_by_plan,
        frame_ownership,
    )
    overridden_dispositions = _adopt_prior_dispositions(ctx, plans, cross_plan_reasons)
    if overridden_dispositions:
        adopted_by_id = {plan.id: plan for plan in plans}
        for plan_id in overridden_dispositions:
            adopted_plan = adopted_by_id[plan_id]
            diagnostics.append(
                RoutePlanDiagnostic(
                    adopted_plan.member_ids[0] if adopted_plan.member_ids else None,
                    "exit-turn-disposition-adopted",
                    f"exit group {adopted_plan.exit_group_id} disposition adopted "
                    "from the frozen settlement pass, overriding this pass's own "
                    "fresh cross-plan verdict",
                    blocking=False,
                )
            )
    plans, references, demands = _apply_cross_plan_fallbacks(
        plans,
        references,
        demands,
        diagnostics,
        assignments_by_plan,
        cross_plan_reasons,
    )
    if ctx.station_offsets is not None:
        trial_offsets = dict(ctx.station_offsets)
        for plan in plans:
            if plan.disposition is not ExitTurnDisposition.PLANNED:
                continue
            for lane in plan.source_lanes:
                for station_id in lane.station_ids:
                    trial_offsets[(station_id, lane.line_id)] = lane.planned_offset
        validate_linear_entry_frame_ownership(trial_offsets, frame_ownership)
        ctx.station_offsets.clear()
        ctx.station_offsets.update(trial_offsets)

    plan_by_id = {plan.id: plan for plan in plans}
    owner_by_member = _index_unique_member_owners(plans)
    membership_by_edge = {}
    for edge, member_id in scaffold.member_id_by_edge.items():
        member_owner_plan = owner_by_member.get(member_id)
        if member_owner_plan is None:
            continue
        assignment = assignments_by_plan[member_owner_plan.id].get(edge)
        axis = (
            next(
                item for item in member_owner_plan.axes if item.id == assignment.axis_id
            )
            if assignment is not None and assignment.axis_id is not None
            else None
        )
        membership_by_edge[_edge_key(edge)] = _Membership(
            plan_by_id[member_owner_plan.id], member_id, assignment, axis
        )
    transition_by_edge = {}
    for plan in plans:
        if plan.disposition is not ExitTurnDisposition.PLANNED:
            continue
        for transition in plan.lane_transitions:
            transition_key = _edge_key(transition.edge)
            if transition_key in transition_by_edge:
                raise ExitTurnInvariantError(
                    _failure(plan, "lane transition has more than one owner")
                )
            transition_by_edge[transition_key] = _TransitionMembership(plan, transition)
    query = ExitTurnPlanQuery(
        tuple(plans),
        MappingProxyType(membership_by_edge),
        MappingProxyType(transition_by_edge),
    )
    return ExitTurnExecution(
        scaffold,
        tuple(plans),
        tuple(references),
        tuple(demands),
        tuple(diagnostics),
        query,
    )


def _segment_direction(
    start: tuple[float, float], end: tuple[float, float]
) -> Direction | None:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dy) <= COORD_TOLERANCE and abs(dx) > COORD_TOLERANCE:
        return horizontal_direction(dx)
    if abs(dx) <= COORD_TOLERANCE and abs(dy) > COORD_TOLERANCE:
        return vertical_direction(dy)
    return None


def _opening_turn_segment(
    route: RoutedPath, run_direction: Direction, turn_direction: Direction
) -> int | None:
    points = route.points
    if len(points) >= 3:
        before, start, end = points[:3]
        if (
            _segment_direction(before, start) is run_direction
            and _segment_direction(start, end) is turn_direction
        ):
            return 1
    return None


def _expected_fixed_anchor_offset(
    axis: ExitTurnAxis,
    assignments: tuple[ExitTurnAssignment, ...],
    station_offsets: Mapping[tuple[str, str], float],
) -> float | None:
    families = {item.planned_family_id for item in assignments}
    if RouteFamilyId.MERGE_BRANCH in families and axis.fixed_anchor_id is not None:
        return station_offsets.get((axis.fixed_anchor_id, axis.line_id))
    return 0.0 if families else None


def _fixed_axis_matches_plan(
    axis: ExitTurnAxis,
    assignments: tuple[ExitTurnAssignment, ...],
    station_offsets: Mapping[tuple[str, str], float],
) -> bool:
    expected_offset = _expected_fixed_anchor_offset(
        axis,
        assignments,
        station_offsets,
    )
    return (
        axis.fixed_anchor_id is not None
        and axis.fixed_anchor_coordinate is not None
        and axis.fixed_anchor_offset is not None
        and expected_offset is not None
        and abs(axis.fixed_anchor_offset - expected_offset) <= COORD_TOLERANCE
        and abs(axis.coordinate - axis.fixed_anchor_coordinate - expected_offset)
        <= COORD_TOLERANCE
    )


def consume_exit_turn_route(
    route: RoutedPath,
    family_id: RouteFamilyId,
    ctx: _RoutingCtx,
) -> None:
    """Bind one emitted route to its precomputed source-turn assignment."""
    if ctx.exit_turns is None:
        return
    membership = ctx.exit_turns.membership_for_edge(route.edge)
    if membership is None or membership.plan.disposition is ExitTurnDisposition.LEGACY:
        return
    route.exit_turn_plan_id = str(membership.plan.id)
    route.exit_turn_member_id = str(membership.member_id)
    route.exit_turn_family_id = family_id.value
    assignment = membership.assignment
    if assignment is None:
        return
    if family_id is not assignment.planned_family_id:
        raise ExitTurnInvariantError(
            _failure(membership.plan, "production family changed during dispatch")
        )
    if membership.axis is None:
        return
    axis_assignments = tuple(
        item
        for item in membership.plan.assignments
        if item.axis_id == membership.axis.id
    )
    if membership.axis.fixed_anchor_id is not None:
        expected_axis = _fixed_axis(route.edge, family_id, ctx)
        if (
            expected_axis is None
            or abs(expected_axis - membership.axis.coordinate) > COORD_TOLERANCE
            or not _fixed_axis_matches_plan(
                membership.axis,
                axis_assignments,
                ctx.station_offsets or {},
            )
        ):
            raise ExitTurnInvariantError(
                _failure(
                    membership.plan,
                    "fixed turn axis does not match its structural anchor",
                )
            )
    run = assignment.run_direction
    turn = assignment.turn_direction
    if (
        run is None
        or turn is None
        or assignment.launch_coordinate is None
        or assignment.minimum_runway is None
    ):
        raise ExitTurnInvariantError(
            _failure(membership.plan, "planned turn has incomplete source geometry")
        )
    segment_rank = _opening_turn_segment(route, run, turn)
    if segment_rank is None:
        raise ExitTurnInvariantError(
            _failure(membership.plan, "planned member emitted without a source turn")
        )
    source_axis = membership.axis.axis
    segment_start, segment_end = route.points[segment_rank : segment_rank + 2]
    if (
        abs(
            get_point_coordinate(segment_start, source_axis)
            - membership.axis.coordinate
        )
        > COORD_TOLERANCE
        or abs(
            get_point_coordinate(segment_end, source_axis) - membership.axis.coordinate
        )
        > COORD_TOLERANCE
    ):
        turn_cohort = tuple(
            item
            for item in membership.plan.assignments
            if item.run_direction is run
            and item.turn_direction is turn
            and item.axis_id is not None
        )
        cohort_axis_ids = {item.axis_id for item in turn_cohort}
        cohort_axes = tuple(
            axis for axis in membership.plan.axes if axis.id in cohort_axis_ids
        )
        reference_axis = min(
            cohort_axes,
            key=lambda item: item.coordinate * run.sign,
        )
        offset = membership.axis.coordinate - reference_axis.coordinate
        shares_terminal_destination_entry = any(
            item.member_id != assignment.member_id
            and item.entry_group_id == assignment.entry_group_id
            and EmissionRole.TERMINAL in item.roles
            for item in turn_cohort
        )
        _reseat_concentric_flanking(
            route,
            segment_rank,
            membership.axis.coordinate,
            axis=0 if source_axis is DemandAxis.X else 1,
            offset_in=offset,
            offset_out=offset if shares_terminal_destination_entry else 0.0,
        )
    lead, start, end = route.points[segment_rank - 1 : segment_rank + 2]
    if (
        abs(get_point_coordinate(lead, source_axis) - assignment.launch_coordinate)
        > COORD_TOLERANCE
        or _segment_direction(lead, start) is not run
        or (
            get_point_coordinate(start, source_axis)
            - get_point_coordinate(lead, source_axis)
        )
        * run.sign
        < assignment.minimum_runway - COORD_TOLERANCE
        or _segment_direction(start, end) is not turn
    ):
        raise ExitTurnInvariantError(
            _failure(membership.plan, "source turn changed during dispatch")
        )
    route.exit_turn_axis_id = str(membership.axis.id)
    route.exit_turn_segment_rank = segment_rank


def exit_turn_failure(plan: ExitTurnPlan, detail: str) -> str:
    """Attribute a runtime failure to its route system and connectors."""
    connectors = ", ".join(str(item) for item in plan.connector_ids)
    return (
        f"exit-turn plan {plan.id} in system {plan.system_id} ({connectors}): {detail}"
    )


_failure = exit_turn_failure


def snapshot_exit_turn_segments(
    routes: list[RoutedPath],
    plans: tuple[ExitTurnPlan, ...] = (),
) -> _ExitTurnSnapshot:
    """Capture every planner-owned segment and hand-off after template emission."""
    values: dict[tuple[str, ...], _ExitTurnGeometryState] = {}
    for route in routes:
        if route.exit_turn_axis_id is not None:
            rank = route.exit_turn_segment_rank
            if rank is None:
                continue
            landing_point_settled_later = (
                route.exit_turn_family_id == RouteFamilyId.MERGE_BRANCH.value
            )
            radii = None
            if route.curve_radii is not None and 0 <= rank - 1 < len(route.curve_radii):
                radii = ((rank - 1, route.curve_radii[rank - 1]),)
            values[
                (
                    "axis",
                    route.exit_turn_plan_id or "",
                    route.exit_turn_member_id or "",
                )
            ] = _ExitTurnGeometryState(
                route.exit_turn_family_id,
                route.exit_turn_axis_id,
                rank,
                route.points[rank - 1],
                route.points[rank],
                None if landing_point_settled_later else route.points[rank + 1],
                radii,
                None,
                None,
                None,
                None,
            )
        if route.exit_lane_transition_plan_id is not None:
            values[
                (
                    "transition",
                    route.exit_lane_transition_plan_id,
                    route.edge.source,
                    route.edge.target,
                    route.line_id,
                )
            ] = _ExitTurnGeometryState(
                route.exit_turn_family_id,
                None,
                None,
                None,
                None,
                None,
                None,
                route.exit_lane_transition_plan_id,
                tuple(route.points),
                tuple(route.curve_radii) if route.curve_radii is not None else None,
                route.offset_regime,
            )
    return _ExitTurnSnapshot(
        MappingProxyType(values),
        MappingProxyType({str(plan.id): plan for plan in plans}),
    )


def assert_exit_turn_snapshot(
    routes: list[RoutedPath],
    snapshot: _ExitTurnSnapshot,
    pass_name: str,
) -> None:
    """Reject a legacy pass that changes geometry owned by the planner."""
    current = snapshot_exit_turn_segments(routes).geometry
    if current.keys() != snapshot.geometry.keys():
        changed_key = next(
            iter(current.keys() ^ snapshot.geometry.keys()),
            None,
        )
        detail = f"{pass_name} changed planned exit-turn geometry membership"
        plan = (
            snapshot.plans_by_id.get(changed_key[1])
            if changed_key is not None and len(changed_key) > 1
            else None
        )
        raise ExitTurnInvariantError(
            exit_turn_failure(plan, detail) if plan is not None else detail
        )
    for key, state in snapshot.geometry.items():
        if current[key] != state:
            detail = f"{pass_name} changed planned exit-turn geometry {key}"
            plan = snapshot.plans_by_id.get(key[1]) if len(key) > 1 else None
            raise ExitTurnInvariantError(
                exit_turn_failure(plan, detail) if plan is not None else detail
            )


def validate_exit_turn_plans(
    graph: MetroGraph,
    routes: list[RoutedPath],
    plan: RoutePlan | tuple[ExitTurnPlan, ...],
    station_offsets: dict[tuple[str, str], float],
) -> None:
    """Validate planned membership, compact lanes, and final owned geometry."""
    routes_by_member = defaultdict(list)
    routes_by_edge = defaultdict(list)
    for route in routes:
        routes_by_edge[(route.edge.source, route.edge.target, route.line_id)].append(
            route
        )
        if route.exit_turn_member_id is not None:
            routes_by_member[route.exit_turn_member_id].append(route)
    edge_by_key = {
        (edge.source, edge.target, edge.line_id): edge for edge in graph.edges
    }
    if isinstance(plan, RoutePlan):
        planned_system_ids = {
            system.id
            for system in plan.systems
            if system.disposition is RouteSystemDisposition.PLANNED
        }
        exit_turn_plans = tuple(
            item
            for item in plan.exit_turn_plans
            if item.system_id in planned_system_ids
        )
    else:
        exit_turn_plans = plan
    for exit_turn_plan in exit_turn_plans:
        if exit_turn_plan.disposition is not ExitTurnDisposition.PLANNED:
            continue
        for lane in exit_turn_plan.source_lanes:
            for station_id in lane.station_ids:
                if (
                    station_id not in graph.stations
                    or lane.line_id not in graph.station_lines(station_id)
                ):
                    raise ExitTurnInvariantError(
                        _failure(
                            exit_turn_plan,
                            "source lane owns an unknown station or line",
                        )
                    )
                if (station_id, lane.line_id) not in station_offsets or (
                    abs(
                        station_offsets[(station_id, lane.line_id)]
                        - lane.planned_offset
                    )
                    > COORD_TOLERANCE
                ):
                    raise ExitTurnInvariantError(
                        _failure(
                            exit_turn_plan,
                            "source lane compaction was not preserved",
                        )
                    )
        for transition in exit_turn_plan.lane_transitions:
            transition_routes = routes_by_edge[_edge_key(transition.edge)]
            if len(transition_routes) != 1:
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "lane transition does not bind exactly one route",
                    )
                )
            route = transition_routes[0]
            try:
                graph_edge = _graph_edge(edge_by_key, transition.edge)
            except KeyError as error:
                raise ExitTurnInvariantError(
                    _failure(exit_turn_plan, "lane transition edge is unknown")
                ) from error
            try:
                expected_route = route_lane_transition(
                    graph_edge,
                    transition.source_point,
                    transition.target_point,
                    source_offset=transition.source_offset,
                    target_offset=transition.target_offset,
                    run_direction=transition.run_direction,
                    source_runway=transition.source_runway,
                    target_runway=transition.target_runway,
                    diagonal_run=transition.diagonal_run,
                    place_at_source=(
                        transition.placement is ExitLaneTransitionPlacement.SOURCE
                    ),
                    is_inter_section=route.is_inter_section,
                )
            except ValueError as error:
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "lane transition cannot satisfy its geometry requirements",
                    )
                ) from error
            if (
                route.exit_lane_transition_plan_id != str(exit_turn_plan.id)
                or route.offset_regime is not OffsetRegime.BAKED
                or route.points != expected_route.points
                or route.curve_radii != expected_route.curve_radii
            ):
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "lane transition differs from its template decision",
                    )
                )
        axis_by_id = {str(item.id): item for item in exit_turn_plan.axes}
        assignment_by_axis = {
            axis.id: tuple(
                item for item in exit_turn_plan.assignments if item.axis_id == axis.id
            )
            for axis in exit_turn_plan.axes
        }
        for axis in exit_turn_plan.axes:
            assignments = assignment_by_axis[axis.id]
            if axis.fixed_anchor_id is not None:
                if not _fixed_axis_matches_plan(
                    axis,
                    assignments,
                    station_offsets,
                ):
                    raise ExitTurnInvariantError(
                        _failure(
                            exit_turn_plan,
                            "fixed turn axis moved away from its structural anchor",
                        )
                    )
            if any(
                assignment.run_direction is None
                or assignment.launch_coordinate is None
                or assignment.minimum_runway is None
                or (axis.coordinate - assignment.launch_coordinate)
                * assignment.run_direction.sign
                < assignment.minimum_runway - COORD_TOLERANCE
                for assignment in assignments
            ):
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "planned source turn does not satisfy its runway demand",
                    )
                )
        for assignment in exit_turn_plan.assignments:
            member_routes = routes_by_member[str(assignment.member_id)]
            if len(member_routes) != 1:
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "assignment does not bind exactly one route",
                    )
                )
            route = member_routes[0]
            if route.exit_turn_family_id != assignment.planned_family_id.value:
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "emitted route family differs from its assignment",
                    )
                )
            if assignment.axis_id is None:
                if route.exit_turn_axis_id is not None:
                    raise ExitTurnInvariantError(
                        _failure(
                            exit_turn_plan,
                            "straight continuation acquired a turn",
                        )
                    )
                if route.exit_lane_transition_plan_id is None:
                    points = apply_route_offsets(route, station_offsets)
                    if (
                        assignment.run_direction is None
                        or _segment_direction(points[0], points[-1])
                        is not assignment.run_direction
                        or abs(
                            get_point_coordinate(
                                points[0], lateral_axis(assignment.run_direction)
                            )
                            - get_point_coordinate(
                                points[-1], lateral_axis(assignment.run_direction)
                            )
                        )
                        > COORD_TOLERANCE
                    ):
                        raise ExitTurnInvariantError(
                            _failure(
                                exit_turn_plan,
                                "straight continuation changed source lane",
                            )
                        )
                continue
            if (
                route.exit_turn_axis_id != str(assignment.axis_id)
                or route.exit_turn_segment_rank is None
            ):
                raise ExitTurnInvariantError(
                    _failure(exit_turn_plan, "turn assignment metadata is incomplete")
                )
            rendered_points = apply_route_offsets(route, station_offsets)
            lead_in = rendered_points[route.exit_turn_segment_rank - 1]
            start, end = rendered_points[
                route.exit_turn_segment_rank : route.exit_turn_segment_rank + 2
            ]
            axis = axis_by_id[str(assignment.axis_id)]
            expected_axis = axis.coordinate
            actual = get_point_coordinate(start, axis.axis)
            if (
                abs(actual - expected_axis) > COORD_TOLERANCE
                or abs(get_point_coordinate(end, axis.axis) - expected_axis)
                > COORD_TOLERANCE
                or assignment.run_direction is None
                or assignment.launch_coordinate is None
                or assignment.minimum_runway is None
                or abs(
                    get_point_coordinate(lead_in, axis.axis)
                    - assignment.launch_coordinate
                )
                > COORD_TOLERANCE
                or _segment_direction(lead_in, start) is not assignment.run_direction
                or (
                    get_point_coordinate(start, axis.axis)
                    - get_point_coordinate(lead_in, axis.axis)
                )
                * assignment.run_direction.sign
                < assignment.minimum_runway - COORD_TOLERANCE
                or assignment.turn_direction is None
                or _segment_direction(start, end) is not assignment.turn_direction
            ):
                raise ExitTurnInvariantError(
                    _failure(
                        exit_turn_plan,
                        "emitted turn differs from its planned axis or direction",
                    )
                )
