"""Pre-emission convergence planning and template consumption."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TypeAlias

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    OFFSET_STEP,
)
from nf_metro.layout.geometry import (
    cotravelling_lane_clearance,
    point_to_polyline_distance,
    spans_share_corridor,
)
from nf_metro.layout.route_plan import (
    ConvergenceConflict,
    ConvergenceConflictKind,
    ConvergenceContinuation,
    ConvergenceDisposition,
    ConvergenceEndpointOwnership,
    ConvergenceEndpointRole,
    ConvergenceLanding,
    ConvergencePlan,
    ConvergencePlanId,
    ConvergenceTrunkAxis,
    ConvergenceTrunkReason,
    CoordinateRegime,
    DemandAxis,
    DemandKind,
    EmissionMemberId,
    ExitTurnPlan,
    ExitTurnPlanId,
    FanPlan,
    FanPlanId,
    GridSpan,
    KeepOutClass,
    RoutePlanDiagnostic,
    RouteSemanticScaffold,
    RouteSystemId,
    SharedReference,
    SharedReferenceKind,
    SymbolicDemand,
    TurnHandedness,
    _ordered_unique,
    _plan_provenance,
    convergence_resource_ids,
    grid_span_for_sections,
    reservation_decision_refs,
    turn_handedness,
)
from nf_metro.layout.routing.centrelines import gather_member_edges
from nf_metro.layout.routing.common import (
    Direction,
    GapLookupGeometry,
    HTrunkSeg,
    OffsetRegime,
    RoutedPath,
    apply_route_offsets,
    column_gap_edges,
    gap_lo_for_x,
    gap_lookup_geometry,
    iter_horizontal_trunks,
    merge_fanout_pivot_reference,
)
from nf_metro.layout.routing.context import (
    _EdgeKey,
    _resolve_section_colrow,
    _RoutingCtx,
)
from nf_metro.layout.routing.member_geometry import (
    MemberGeometryExecution,
    PreliminaryGapChannelClaim,
    empty_member_geometry_execution,
)
from nf_metro.layout.routing.orientation import direction_axis, lateral_axis
from nf_metro.layout.routing.reserved_bands import ReservedBand
from nf_metro.parser.model import Edge, MetroGraph, Station
from nf_metro.parser.route_topology import (
    ResolvedConvergenceView,
    ResolvedEdge,
    semantic_route_id,
)


class ConvergenceInvariantError(RuntimeError):
    """A planned convergence template violated its immutable contract."""


class FinalConvergenceFeasibilityError(ConvergenceInvariantError):
    """Final planned convergence geometry is not jointly feasible."""


class UnsupportedConvergenceError(ValueError):
    """Canonical templates cannot represent a complete convergence system.

    A rejection raised from a whole-system check carries the measurement behind
    it, so the compatibility record it produces can state where the limit was
    found instead of only what it was called.
    """

    def __init__(
        self, reason: str, conflict: ConvergenceConflict | None = None
    ) -> None:
        super().__init__(reason)
        self.conflict = conflict


class ConvergencePlanningError(RuntimeError):
    """Semantic convergence membership is internally inconsistent."""


_PlanMembership: TypeAlias = tuple[
    tuple[tuple[ResolvedEdge, ...], ...],
    tuple[ResolvedEdge, ...],
    tuple[EmissionMemberId, ...],
]


@dataclass(frozen=True, slots=True)
class ConvergenceRouteMembership:
    plan: ConvergencePlan
    member_id: EmissionMemberId
    landing: ConvergenceLanding | None
    continuation: ConvergenceContinuation | None
    ownership: ConvergenceEndpointOwnership
    covering_edge: ResolvedEdge | None


@dataclass(frozen=True, slots=True)
class PlannedConvergenceVerticalChannel:
    """One exact vertical run claimed by an earlier planned convergence."""

    system_id: RouteSystemId
    owner_edge: ResolvedEdge
    owner_source: str
    line_id: str
    canonical_edge_rank: int
    segment_rank: int
    x: float
    y_lo: float
    y_hi: float


@dataclass(frozen=True, slots=True)
class ConvergencePlanExecutionQuery:
    plans: tuple[ConvergencePlan, ...]
    _by_edge: Mapping[ResolvedEdge, ConvergenceRouteMembership]
    _edge_order: tuple[ResolvedEdge, ...]
    _vertical_channels: tuple[PlannedConvergenceVerticalChannel, ...]
    _edge_rank: Mapping[ResolvedEdge, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_edge_rank",
            MappingProxyType(
                {edge: rank for rank, edge in enumerate(self._edge_order)}
            ),
        )

    def membership_for_edge(
        self, edge: Edge | ResolvedEdge
    ) -> ConvergenceRouteMembership | None:
        key = (
            edge
            if isinstance(edge, ResolvedEdge)
            else ResolvedEdge(edge.source, edge.target, edge.line_id)
        )
        return self._by_edge.get(key)

    def covering_edge_for_edge(self, edge: Edge | ResolvedEdge) -> ResolvedEdge | None:
        membership = self.membership_for_edge(edge)
        return membership.covering_edge if membership is not None else None

    def prior_vertical_channels_for_edge(
        self, edge: Edge | ResolvedEdge
    ) -> tuple[PlannedConvergenceVerticalChannel, ...]:
        """Exact planned channels whose owners precede *edge* canonically."""
        resolved = (
            edge
            if isinstance(edge, ResolvedEdge)
            else ResolvedEdge(edge.source, edge.target, edge.line_id)
        )
        edge_rank = self._edge_rank.get(resolved)
        if edge_rank is None:
            return ()
        return tuple(
            claim
            for claim in self._vertical_channels
            if claim.canonical_edge_rank < edge_rank
        )

    def restrict_to_systems(
        self, system_ids: frozenset[RouteSystemId]
    ) -> ConvergencePlanExecutionQuery:
        """Return only convergence ownership consumed by planned systems."""
        plans = tuple(plan for plan in self.plans if plan.system_id in system_ids)
        by_edge = {
            edge: membership
            for edge, membership in self._by_edge.items()
            if membership.plan.system_id in system_ids
        }
        vertical_channels = tuple(
            claim for claim in self._vertical_channels if claim.system_id in system_ids
        )
        return ConvergencePlanExecutionQuery(
            plans,
            MappingProxyType(by_edge),
            self._edge_order,
            vertical_channels,
        )


@dataclass(frozen=True, slots=True)
class ConvergencePlanExecution:
    plans: tuple[ConvergencePlan, ...]
    references: tuple[SharedReference, ...]
    demands: tuple[SymbolicDemand, ...]
    diagnostics: tuple[RoutePlanDiagnostic, ...]
    query: ConvergencePlanExecutionQuery


def empty_convergence_plan_execution() -> ConvergencePlanExecution:
    query = ConvergencePlanExecutionQuery((), MappingProxyType({}), (), ())
    return ConvergencePlanExecution((), (), (), (), query)


def _entry_lane_order(
    graph: MetroGraph,
    scaffold: RouteSemanticScaffold,
    view: ResolvedConvergenceView,
) -> tuple[str, ...]:
    entry_group = scaffold.query.endpoint_group_for_port(
        scaffold.query.entry_port(view.group.entry_group_id)
    )
    system_id = scaffold.system_for(view.group.connector_ids)
    lines = {
        scaffold.query.connector(connector_id).line_id
        for connector_id in entry_group.connector_ids
        if scaffold.system_for((connector_id,)) == system_id
    }
    return tuple(line_id for line_id in graph.lines if line_id in lines)


def _direction(a: tuple[float, float], b: tuple[float, float]) -> Direction:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) > abs(dy):
        return Direction.R if dx > 0 else Direction.L
    return Direction.D if dy > 0 else Direction.U


def _owned_paths(
    scaffold: RouteSemanticScaffold,
    view: ResolvedConvergenceView,
) -> tuple[tuple[ResolvedEdge, ...], ...]:
    junction_id = view.junction_id
    paths: list[tuple[ResolvedEdge, ...]] = []
    for connector_id in view.group.connector_ids:
        for path in scaffold.query.resolved_paths(connector_id):
            owned = tuple(
                edge
                for edge in path
                if edge.target == junction_id or edge.source == junction_id
            )
            if owned:
                paths.append(owned)
    return tuple(paths)


def _trunk_route(
    edge: Edge,
    ctx: _RoutingCtx,
) -> RoutedPath:
    from nf_metro.layout.routing.inter_section_handlers import (
        _build_inter_facts,
        _route_merge_trunk_feeder,
    )

    src, tgt = ctx.graph.edge_endpoints(edge)
    route = _route_merge_trunk_feeder(_build_inter_facts(edge, src, tgt, ctx))
    if route is None:
        raise UnsupportedConvergenceError("primary trunk template declined its member")
    _consume_exit_turn(route, ctx)
    return route


def _trial_route(edge: Edge, ctx: _RoutingCtx) -> RoutedPath:
    from nf_metro.layout.routing.inter_section_handlers import (
        _build_inter_facts,
        _match_inter_section_rule,
        _route_l_shape,
    )

    source, target = ctx.graph.edge_endpoints(edge)
    facts = _build_inter_facts(edge, source, target, ctx)
    rule = _match_inter_section_rule(facts)
    route = (
        rule.route(facts)
        if rule is not None
        else _route_l_shape(edge, source, target, facts.i, facts.n, ctx)
    )
    if route is None:
        raise UnsupportedConvergenceError("convergence template declined its member")
    _consume_exit_turn(route, ctx)
    return route


def _consume_exit_turn(route: RoutedPath, ctx: _RoutingCtx) -> None:
    from nf_metro.layout.routing.exit_turns import consume_exit_turn_route
    from nf_metro.layout.routing.inter_section_handlers import (
        classify_inter_section_family,
    )

    source, target = ctx.graph.edge_endpoints(route.edge)
    family = classify_inter_section_family(route.edge, source, target, ctx)
    if family is None:
        raise UnsupportedConvergenceError(
            "planned convergence member has no routing family"
        )
    consume_exit_turn_route(route, family, ctx)


def _trunk_run(route: RoutedPath, expected_coordinate: float) -> HTrunkSeg:
    runs = tuple(segment for _rank, segment in iter_horizontal_trunks(route))
    if not runs:
        raise UnsupportedConvergenceError(
            "primary trunk template emitted no shared run"
        )
    return min(runs, key=lambda segment: abs(segment.y - expected_coordinate))


def _axis_from_run(run: HTrunkSeg, route: RoutedPath) -> ConvergenceTrunkAxis:
    return ConvergenceTrunkAxis(
        axis=DemandAxis.X,
        coordinate=run.y,
        extent_start=run.x_lo,
        extent_end=run.x_hi,
        direction=Direction.R if run.xb > run.xa else Direction.L,
        source_flank_coordinate=run.before_y,
        target_flank_coordinate=run.after_y,
        source_endpoint_coordinate=route.points[0][0],
        target_endpoint_coordinate=route.points[-1][0],
    )


def _run_from_axis(axis: ConvergenceTrunkAxis) -> HTrunkSeg:
    if axis.axis is not DemandAxis.X:
        raise ConvergenceInvariantError("planned bypass trunk is not horizontal")
    xa, xb = (
        (axis.extent_start, axis.extent_end)
        if axis.direction is Direction.R
        else (axis.extent_end, axis.extent_start)
    )
    return HTrunkSeg(
        y=axis.coordinate,
        xa=xa,
        xb=xb,
        before_y=axis.source_flank_coordinate,
        after_y=axis.target_flank_coordinate,
    )


def _exit_turn_geometry(route: RoutedPath) -> tuple[tuple[float, float], ...] | None:
    if route.exit_lane_transition_plan_id is not None:
        return tuple(route.points)
    if route.exit_turn_segment_rank is None:
        return None
    rank = route.exit_turn_segment_rank
    return tuple(route.points[max(0, rank - 1) : rank + 1])


def _closest_point_on_polyline(
    point: tuple[float, float], points: list[tuple[float, float]]
) -> tuple[float, float]:
    px, py = point
    candidates: list[tuple[float, tuple[float, float]]] = []
    for start, end in zip(points, points[1:]):
        ax, ay = start
        bx, by = end
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy
        proportion = (
            0.0
            if length_squared == 0.0
            else max(
                0.0,
                min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared),
            )
        )
        candidate = (ax + proportion * dx, ay + proportion * dy)
        candidates.append(
            (
                (px - candidate[0]) ** 2 + (py - candidate[1]) ** 2,
                candidate,
            )
        )
    if not candidates:
        raise UnsupportedConvergenceError("planned trunk has no drawable segment")
    return min(candidates, key=lambda item: item[0])[1]


def _connect_route_endpoint(route: RoutedPath, target: tuple[float, float]) -> None:
    endpoint = route.points[-1]
    if all(
        abs(actual - expected) <= COORD_TOLERANCE
        for actual, expected in zip(endpoint, target, strict=True)
    ):
        route.points[-1] = target
        return
    prior = route.points[-2]
    horizontal = abs(prior[1] - endpoint[1]) <= COORD_TOLERANCE
    vertical = abs(prior[0] - endpoint[0]) <= COORD_TOLERANCE
    if horizontal and abs(endpoint[1] - target[1]) <= COORD_TOLERANCE:
        route.points[-1] = target
        return
    if vertical and abs(endpoint[0] - target[0]) <= COORD_TOLERANCE:
        route.points[-1] = target
        return
    elbow = (target[0], endpoint[1]) if horizontal else (endpoint[0], target[1])
    if elbow != endpoint and elbow != target:
        route.points.append(elbow)
    route.points.append(target)


def _bake_route(route: RoutedPath, ctx: _RoutingCtx) -> None:
    if route.offset_regime is OffsetRegime.DEFERRED:
        route.points = apply_route_offsets(route, ctx.station_offsets or {})
        route.offset_regime = OffsetRegime.BAKED


def _landing_approach(
    route: RoutedPath, join_point: tuple[float, float]
) -> tuple[Direction, TurnHandedness | None, float] | None:
    for rank, (start, end) in enumerate(zip(route.points, route.points[1:])):
        runway = abs(start[0] - join_point[0]) + abs(start[1] - join_point[1])
        if (
            runway <= COORD_TOLERANCE
            or point_to_polyline_distance(join_point, (start, end)) > COORD_TOLERANCE
        ):
            continue
        approach = _direction(start, join_point)
        handedness = None
        if rank > 0:
            prior = route.points[rank - 1]
            if abs(prior[0] - start[0]) + abs(prior[1] - start[1]) > COORD_TOLERANCE:
                incoming = _direction(prior, start)
                if direction_axis(incoming) is not direction_axis(approach):
                    handedness = turn_handedness(incoming, approach)
        return approach, handedness, runway
    return None


def _bundled_sibling_owns_opening_column(ctx: _RoutingCtx, edge: Edge) -> bool:
    """Whether a bundle outside this convergence seats *edge*'s opening column.

    :func:`normalize._divergent_source_groups` fuses every same-line descent
    leaving one source onto a single column, and draws the reference from the
    members whose own edge carries a co-travelling bundle: a bundled descent
    holds the slot its bundle-mates nest around, and a lone descent of the same
    line snaps onto it rather than dragging it into a bundle-mate's lane.  A
    convergence owns its members' geometry, not that bundle's, so where such a
    sibling leaves the same source the landing states no opening turn and the
    fusion keeps the column.  Stating one would seat this line's descent a lane
    off the column its own colour already occupies there, drawing it twice and
    over a neighbouring line.
    """
    bundled = len(gather_member_edges(ctx.graph, edge)[1]) > 1
    return not bundled and any(
        sibling.line_id == edge.line_id
        and sibling.target != edge.target
        and len(gather_member_edges(ctx.graph, sibling)[1]) > 1
        for sibling in ctx.graph.edges_from(edge.source)
    )


def _landing_from_trial(
    *,
    plan_member_id: EmissionMemberId,
    edge: Edge,
    route: RoutedPath,
    source: Station,
    target: Station,
    run: HTrunkSeg | None,
    trunk_points: list[tuple[float, float]],
    is_trunk: bool,
    ctx: _RoutingCtx,
    lane_rank: int,
) -> ConvergenceLanding:
    if is_trunk:
        assert run is not None
        join_point = (run.xa, run.y)
    elif run is not None:
        from nf_metro.layout.routing.normalize import _land_feeder_on_run

        key = (edge.source, edge.target, edge.line_id)
        if key in ctx.merge.branch_edges:
            exit_turn_geometry = _exit_turn_geometry(route)
            _land_feeder_on_run(route, run, ctx)
            if exit_turn_geometry != _exit_turn_geometry(route):
                raise UnsupportedConvergenceError(
                    "convergence landing conflicts with an upstream exit turn"
                )
        _bake_route(route, ctx)
        join_point = _closest_point_on_polyline(route.points[-1], trunk_points)
        _connect_route_endpoint(route, join_point)
    else:
        _bake_route(route, ctx)
        join_point = _closest_point_on_polyline(route.points[-1], trunk_points)
        _connect_route_endpoint(route, join_point)
    approach = _landing_approach(route, join_point)
    if approach is None:
        raise UnsupportedConvergenceError("convergence landing has no approach")
    approach_direction, handedness, runway = approach
    source_column, source_row = _resolve_section_colrow(ctx.graph, source)
    target_column, target_row = _resolve_section_colrow(ctx.graph, target)
    from nf_metro.layout.routing.normalize import _opening_fanout_descent

    opening_turn = (
        None
        if _bundled_sibling_owns_opening_column(ctx, edge)
        else _opening_fanout_descent(route)
    )
    opening_turn_segment = (
        (route.points[opening_turn.idx], route.points[opening_turn.idx + 1])
        if opening_turn is not None
        else None
    )
    column_span = (
        abs(target_column - source_column)
        if source_column is not None and target_column is not None
        else 0
    )
    return ConvergenceLanding(
        member_id=plan_member_id,
        edge=ResolvedEdge(edge.source, edge.target, edge.line_id),
        source_junction_id=edge.source,
        approach_axis=direction_axis(approach_direction),
        approach_direction=approach_direction,
        source_column=source_column,
        source_row=source_row,
        lane_rank=lane_rank,
        order=0,
        join_point=join_point,
        corner_handedness=handedness,
        minimum_runway=runway,
        opening_turn_coordinate=(opening_turn.x if opening_turn is not None else None),
        opening_turn_segment=opening_turn_segment,
        bypass=is_trunk
        or (edge.source, edge.target, edge.line_id) in ctx.merge.branch_edges,
        long_haul=column_span > 1,
        multiple_row=(
            source_row is not None
            and target_row is not None
            and source_row != target_row
        ),
    )


def _direct_axis_points(
    merge: tuple[float, float], entry: tuple[float, float]
) -> ConvergenceTrunkAxis:
    direction = _direction(merge, entry)
    if direction in {Direction.R, Direction.L}:
        start, end = sorted((merge[0], entry[0]))
        coordinate = merge[1]
    else:
        start, end = sorted((merge[1], entry[1]))
        coordinate = merge[0]
    return ConvergenceTrunkAxis(
        direction_axis(direction),
        coordinate,
        start,
        end,
        direction,
        coordinate,
        coordinate,
        merge[0] if direction in {Direction.R, Direction.L} else merge[1],
        entry[0] if direction in {Direction.R, Direction.L} else entry[1],
    )


def _direct_axis(merge: Station, entry: Station) -> ConvergenceTrunkAxis:
    return _direct_axis_points((merge.x, merge.y), (entry.x, entry.y))


def _shared_terminal_axis(
    routes: tuple[RoutedPath, ...],
    target_point: tuple[float, float],
) -> tuple[ConvergenceTrunkAxis, int]:
    segments: list[tuple[DemandAxis, float, float, float, Direction, int]] = []
    for rank, route in enumerate(routes):
        if len(route.points) < 2:
            continue
        start, end = route.points[-2:]
        if any(
            abs(actual - expected) > COORD_TOLERANCE
            for actual, expected in zip(end, target_point, strict=True)
        ):
            continue
        direction = _direction(start, end)
        axis = direction_axis(direction)
        if axis is DemandAxis.X:
            if abs(start[1] - end[1]) > COORD_TOLERANCE:
                continue
            coordinate = end[1]
            extent_start, extent_end = sorted((start[0], end[0]))
        else:
            if abs(start[0] - end[0]) > COORD_TOLERANCE:
                continue
            coordinate = end[0]
            extent_start, extent_end = sorted((start[1], end[1]))
        if extent_end - extent_start > COORD_TOLERANCE:
            segments.append(
                (
                    axis,
                    coordinate,
                    extent_start,
                    extent_end,
                    direction,
                    rank,
                )
            )
    if not segments:
        raise UnsupportedConvergenceError(
            "direct convergence has no emitted terminal approach"
        )
    axis, coordinate, extent_start, extent_end, direction, rank = max(
        segments, key=lambda item: item[3] - item[2]
    )
    source_longitudinal, target_longitudinal = (
        (extent_start, extent_end)
        if direction in {Direction.R, Direction.D}
        else (extent_end, extent_start)
    )
    carrier = routes[rank]
    predecessor = carrier.points[-3] if len(carrier.points) >= 3 else None
    if axis is DemandAxis.X:
        source_flank_coordinate = (
            predecessor[1]
            if predecessor is not None
            and abs(predecessor[0] - source_longitudinal) <= COORD_TOLERANCE
            else coordinate
        )
        source_endpoint_coordinate = carrier.points[0][0]
        target_endpoint_coordinate = carrier.points[-1][0]
    else:
        source_flank_coordinate = (
            predecessor[0]
            if predecessor is not None
            and abs(predecessor[1] - source_longitudinal) <= COORD_TOLERANCE
            else coordinate
        )
        source_endpoint_coordinate = carrier.points[0][1]
        target_endpoint_coordinate = carrier.points[-1][1]
    return (
        ConvergenceTrunkAxis(
            axis,
            coordinate,
            extent_start,
            extent_end,
            direction,
            source_flank_coordinate,
            coordinate,
            source_endpoint_coordinate,
            target_endpoint_coordinate,
        ),
        rank,
    )


def _axis_target_point(axis: ConvergenceTrunkAxis) -> tuple[float, float]:
    longitudinal = (
        axis.extent_end
        if axis.direction in {Direction.R, Direction.D}
        else axis.extent_start
    )
    return (
        (longitudinal, axis.coordinate)
        if axis.axis is DemandAxis.X
        else (axis.coordinate, longitudinal)
    )


def _axis_source_point(axis: ConvergenceTrunkAxis) -> tuple[float, float]:
    longitudinal = (
        axis.extent_start
        if axis.direction in {Direction.R, Direction.D}
        else axis.extent_end
    )
    return (
        (longitudinal, axis.coordinate)
        if axis.axis is DemandAxis.X
        else (axis.coordinate, longitudinal)
    )


def _plan_membership(
    scaffold: RouteSemanticScaffold,
    view: ResolvedConvergenceView,
) -> _PlanMembership:
    paths = _owned_paths(scaffold, view)
    edges = _ordered_unique(edge for path in paths for edge in path)
    edge_order = set(scaffold.edge_order)
    if any(edge not in edge_order for edge in edges):
        raise ConvergencePlanningError(
            "convergence membership is absent from emission order"
        )
    try:
        member_ids = tuple(scaffold.member_id_by_edge[edge] for edge in edges)
    except KeyError as error:
        raise ConvergencePlanningError(
            f"resolved convergence member {error.args[0]!r} is not routable"
        ) from error
    return paths, edges, member_ids


def _build_planned_convergence(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    view: ResolvedConvergenceView,
    membership: _PlanMembership,
    exit_turn_plan_ids: tuple[ExitTurnPlanId, ...],
    fan_plan_ids: tuple[FanPlanId, ...],
) -> ConvergencePlan:
    group = view.group
    system_id = scaffold.system_for(group.connector_ids)
    paths, edges, member_ids = membership
    member_by_edge = dict(zip(edges, member_ids, strict=True))
    connector_ids_by_edge = {
        edge: _ordered_unique(
            reference.connector_id for reference in scaffold.refs_by_edge[edge]
        )
        for edge in edges
    }
    entry_port_id = scaffold.query.entry_port(group.entry_group_id)
    entry = graph.stations[entry_port_id]
    outgoing_edges = tuple(edge for edge in edges if edge.source == view.junction_id)
    incoming_edges = tuple(edge for edge in edges if edge.target == view.junction_id)
    if not incoming_edges or not outgoing_edges:
        raise ConvergencePlanningError(
            "convergence has incomplete feeder or continuation membership"
        )

    line_order = _entry_lane_order(graph, scaffold, view)
    if group.line_id not in line_order:
        raise ConvergencePlanningError(
            "convergence line is outside its target entry lane order"
        )
    lane_rank_by_line = {line_id: rank for rank, line_id in enumerate(line_order)}
    trunk_source_id = ctx.merge.trunk_source.get(view.junction_id)
    trial_routes: dict[ResolvedEdge, RoutedPath] = {}
    trunk_run = None
    if trunk_source_id is not None:
        trunk_edge_key = next(
            (
                edge
                for edge in incoming_edges
                if edge.source == trunk_source_id and edge.line_id == group.line_id
            ),
            None,
        )
        if trunk_edge_key is None:
            raise ConvergencePlanningError(
                "classified primary trunk is outside convergence membership"
            )
        edge = ctx.edge_by_key[
            (trunk_edge_key.source, trunk_edge_key.target, trunk_edge_key.line_id)
        ]
        trial_routes[trunk_edge_key] = _trunk_route(edge, ctx)
        trunk_run = _trunk_run(
            trial_routes[trunk_edge_key], ctx.merge.trunk_by[view.junction_id]
        )
        trunk_axis = _axis_from_run(trunk_run, trial_routes[trunk_edge_key])
        primary_member_id = member_by_edge[trunk_edge_key]
        primary_reason = ConvergenceTrunkReason.LONGEST_BYPASS
    else:
        trunk_edge_key = outgoing_edges[0]
        edge = ctx.edge_by_key[
            (trunk_edge_key.source, trunk_edge_key.target, trunk_edge_key.line_id)
        ]
        trial_routes[trunk_edge_key] = _trial_route(edge, ctx)
        direct_route = trial_routes[trunk_edge_key]
        trunk_axis = _direct_axis_points(
            direct_route.points[0], direct_route.points[-1]
        )
        primary_member_id = member_by_edge[trunk_edge_key]
        primary_reason = ConvergenceTrunkReason.OUTGOING_CONTINUATION

    for edge_key in incoming_edges:
        edge = ctx.edge_by_key[(edge_key.source, edge_key.target, edge_key.line_id)]
        route = trial_routes.get(edge_key)
        if route is None:
            if (
                trunk_run is not None
                and (
                    edge.source,
                    edge.target,
                    edge.line_id,
                )
                in ctx.merge.branch_edges
            ):
                from nf_metro.layout.routing.inter_section_handlers import (
                    _build_inter_facts,
                    _route_merge_branch_feeder,
                )

                src, tgt = graph.edge_endpoints(edge)
                route = _route_merge_branch_feeder(
                    _build_inter_facts(edge, src, tgt, ctx)
                )
                if route is not None:
                    _consume_exit_turn(route, ctx)
            else:
                route = _trial_route(edge, ctx)
        if route is None:
            raise UnsupportedConvergenceError("feeder template declined its member")
        trial_routes[edge_key] = route

    if trunk_run is not None:
        from nf_metro.layout.routing.normalize import (
            _merge_feeder_groups,
            _snap_merge_feeder_group,
        )

        exit_turn_geometry = {
            edge_key: _exit_turn_geometry(route)
            for edge_key, route in trial_routes.items()
        }
        trial_route_list = list(trial_routes.values())
        for feeder_group in _merge_feeder_groups(trial_route_list, ctx):
            _snap_merge_feeder_group(feeder_group, graph)
        if any(
            exit_turn_geometry[edge_key] != _exit_turn_geometry(route)
            for edge_key, route in trial_routes.items()
        ):
            raise UnsupportedConvergenceError(
                "convergence alignment conflicts with an upstream exit turn"
            )
    else:
        for edge_key in incoming_edges:
            _bake_route(trial_routes[edge_key], ctx)
        try:
            trunk_axis, carrier_rank = _shared_terminal_axis(
                tuple(trial_routes[edge_key] for edge_key in incoming_edges),
                trial_routes[outgoing_edges[0]].points[-1],
            )
        except UnsupportedConvergenceError:
            outgoing_edge_key = outgoing_edges[0]
            direct_route = trial_routes[outgoing_edge_key]
            if (
                outgoing_edge_key.source,
                outgoing_edge_key.target,
                outgoing_edge_key.line_id,
            ) in ctx.skip_edges:
                for edge_key in incoming_edges:
                    _connect_route_endpoint(
                        trial_routes[edge_key], direct_route.points[-1]
                    )
                trunk_axis, carrier_rank = _shared_terminal_axis(
                    tuple(trial_routes[edge_key] for edge_key in incoming_edges),
                    direct_route.points[-1],
                )
                trunk_edge_key = incoming_edges[carrier_rank]
                primary_member_id = member_by_edge[trunk_edge_key]
                primary_reason = ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH
            else:
                trunk_edge_key = outgoing_edge_key
                _bake_route(direct_route, ctx)
                trunk_axis = _direct_axis_points(
                    direct_route.points[0], direct_route.points[-1]
                )
                primary_member_id = member_by_edge[trunk_edge_key]
                primary_reason = ConvergenceTrunkReason.OUTGOING_CONTINUATION
        else:
            trunk_edge_key = incoming_edges[carrier_rank]
            primary_member_id = member_by_edge[trunk_edge_key]
            primary_reason = ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH

    landings: list[ConvergenceLanding] = []
    trunk_points = trial_routes[trunk_edge_key].points
    for edge_key in incoming_edges:
        edge = ctx.edge_by_key[(edge_key.source, edge_key.target, edge_key.line_id)]
        route = trial_routes[edge_key]
        is_trunk = edge_key == trunk_edge_key and trunk_run is not None
        landing = _landing_from_trial(
            plan_member_id=member_by_edge[edge_key],
            edge=edge,
            route=route,
            source=graph.stations[edge.source],
            target=entry,
            run=trunk_run,
            trunk_points=trunk_points,
            is_trunk=is_trunk,
            ctx=ctx,
            lane_rank=lane_rank_by_line[edge.line_id],
        )
        landings.append(landing)
    landings.sort(
        key=lambda item: (
            item.join_point[0]
            if trunk_axis.direction is Direction.R
            else -item.join_point[0],
            item.join_point[1]
            if trunk_axis.direction is Direction.D
            else -item.join_point[1],
            str(item.member_id),
        )
    )
    landings = [replace(item, order=rank) for rank, item in enumerate(landings)]

    continuations: list[ConvergenceContinuation] = []
    ownership: list[ConvergenceEndpointOwnership] = []
    landing_by_member = {item.member_id: item for item in landings}
    for edge_key, member_id in zip(edges, member_ids, strict=True):
        if edge_key.target == view.junction_id:
            landing = landing_by_member[member_id]
            role = (
                ConvergenceEndpointRole.TRUNK
                if member_id == primary_member_id
                and primary_reason
                in {
                    ConvergenceTrunkReason.LONGEST_BYPASS,
                    ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH,
                }
                else ConvergenceEndpointRole.FEEDER
            )
            endpoint = (
                trial_routes[trunk_edge_key].points[-1]
                if role is ConvergenceEndpointRole.TRUNK
                else landing.join_point
            )
            ownership.append(
                ConvergenceEndpointOwnership(
                    member_id=member_id,
                    edge=edge_key,
                    connector_ids=connector_ids_by_edge[edge_key],
                    role=role,
                    endpoint=endpoint,
                )
            )
            continue
        continuation_route = trial_routes.get(edge_key)
        if continuation_route is None:
            edge = ctx.edge_by_key[(edge_key.source, edge_key.target, edge_key.line_id)]
            continuation_route = _trial_route(edge, ctx)
            trial_routes[edge_key] = continuation_route
        start_point = (
            _axis_source_point(trunk_axis)
            if primary_reason
            in {
                ConvergenceTrunkReason.OUTGOING_CONTINUATION,
                ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH,
            }
            else _axis_target_point(trunk_axis)
        )
        end_point = continuation_route.points[-1]
        hop_start_point = continuation_route.points[0]
        feeder_at_start = any(
            all(
                abs(actual - expected) <= COORD_TOLERANCE
                for actual, expected in zip(
                    trial_routes[item].points[-1], hop_start_point, strict=True
                )
            )
            for item in incoming_edges
        )
        endpoint_carriers = tuple(
            item
            for item in incoming_edges
            if all(
                abs(actual - expected) <= COORD_TOLERANCE
                for actual, expected in zip(
                    trial_routes[item].points[-1], end_point, strict=True
                )
            )
        )
        carrier_edge = next(
            (
                item
                for item in endpoint_carriers
                if point_to_polyline_distance(start_point, trial_routes[item].points)
                <= COORD_TOLERANCE
            ),
            (
                endpoint_carriers[0]
                if endpoint_carriers
                and primary_reason is ConvergenceTrunkReason.OUTGOING_CONTINUATION
                and member_id == primary_member_id
                else None
            ),
        )
        covered_by = (
            member_by_edge[carrier_edge]
            if carrier_edge is not None
            and (
                not feeder_at_start
                or (edge_key.source, edge_key.target, edge_key.line_id)
                in ctx.skip_edges
            )
            else None
        )
        if (
            carrier_edge is not None
            and covered_by is not None
            and point_to_polyline_distance(
                start_point, trial_routes[carrier_edge].points
            )
            > COORD_TOLERANCE
            and not (
                primary_reason is ConvergenceTrunkReason.OUTGOING_CONTINUATION
                and member_id == primary_member_id
            )
        ):
            raise UnsupportedConvergenceError(
                "covered continuation is absent from its carrier"
            )
        continuations.append(
            ConvergenceContinuation(
                member_id,
                edge_key,
                entry_port_id,
                lane_rank_by_line[edge_key.line_id],
                start_point,
                end_point,
                covered_by,
            )
        )
        ownership.append(
            ConvergenceEndpointOwnership(
                member_id=member_id,
                edge=edge_key,
                connector_ids=connector_ids_by_edge[edge_key],
                role=(
                    ConvergenceEndpointRole.COVERED_CONTINUATION
                    if covered_by is not None
                    else ConvergenceEndpointRole.CONTINUATION
                ),
                endpoint=end_point,
                covered_by_member_id=covered_by,
            )
        )

    if primary_reason is ConvergenceTrunkReason.OUTGOING_CONTINUATION:
        (continuation,) = continuations
        if continuation.covered_by_member_id is not None:
            primary_member_id = continuation.covered_by_member_id
            carrier_ownership = next(
                item for item in ownership if item.member_id == primary_member_id
            )
            carrier_route = trial_routes[carrier_ownership.edge]
            trunk_axis, _rank = _shared_terminal_axis(
                (carrier_route,), continuation.end_point
            )
            primary_reason = ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH
            continuations = [
                replace(
                    continuation,
                    start_point=_axis_source_point(trunk_axis),
                )
            ]
            ownership = [
                replace(item, role=ConvergenceEndpointRole.TRUNK)
                if item.member_id == primary_member_id
                else item
                for item in ownership
            ]

    plan_id = ConvergencePlanId(
        semantic_route_id("convergence-plan", system_id, group.id)
    )
    reference_ids, demand_ids = convergence_resource_ids(plan_id)
    return ConvergencePlan(
        id=plan_id,
        system_id=system_id,
        convergence_ids=(group.id,),
        entry_group_ids=(group.entry_group_id,),
        merge_junction_ids=(view.junction_id,),
        target_entry_port_ids=(entry_port_id,),
        connector_ids=group.connector_ids,
        member_ids=member_ids,
        resolved_member_paths=paths,
        resolved_member_edges=edges,
        line_ids=(group.line_id,),
        upstream_exit_turn_plan_ids=exit_turn_plan_ids,
        upstream_fan_plan_ids=fan_plan_ids,
        primary_trunk_member_id=primary_member_id,
        primary_trunk_reason=primary_reason,
        trunk_axis=(
            None
            if trunk_axis is None
            else replace(
                trunk_axis,
                claimant_member_ids=_ordered_unique(
                    item.member_id for item in landings
                ),
            )
        ),
        landings=tuple(landings),
        outgoing_continuations=tuple(continuations),
        lane_order=line_order,
        endpoint_ownership=tuple(ownership),
        shared_reference_ids=reference_ids,
        demand_ids=demand_ids,
        foreign_reference_ids=(),
        disposition=ConvergenceDisposition.PLANNED,
        legacy_reason=None,
    )


def _legacy_plan(
    scaffold: RouteSemanticScaffold,
    view: ResolvedConvergenceView,
    membership: _PlanMembership,
    reason: str,
    conflict: ConvergenceConflict | None,
) -> ConvergencePlan:
    group = view.group
    paths, edges, member_ids = membership
    system_id = scaffold.system_for(group.connector_ids)
    return ConvergencePlan(
        id=ConvergencePlanId(
            semantic_route_id("convergence-plan", system_id, group.id)
        ),
        system_id=system_id,
        convergence_ids=(group.id,),
        entry_group_ids=(group.entry_group_id,),
        merge_junction_ids=(view.junction_id,),
        target_entry_port_ids=(scaffold.query.entry_port(group.entry_group_id),),
        connector_ids=group.connector_ids,
        member_ids=member_ids,
        resolved_member_paths=paths,
        resolved_member_edges=edges,
        line_ids=(group.line_id,),
        upstream_exit_turn_plan_ids=(),
        upstream_fan_plan_ids=(),
        primary_trunk_member_id=None,
        primary_trunk_reason=None,
        trunk_axis=None,
        landings=(),
        outgoing_continuations=(),
        lane_order=(),
        endpoint_ownership=(),
        shared_reference_ids=(),
        demand_ids=(),
        foreign_reference_ids=(),
        disposition=ConvergenceDisposition.LEGACY,
        legacy_reason=reason,
        conflict=conflict,
    )


def _plan_span(graph: MetroGraph, plan: ConvergencePlan) -> GridSpan:
    topology = graph.route_topology
    if topology is None:
        raise ValueError("convergence planning requires resolved route topology")
    connector_by_id = {item.id: item for item in topology.connectors}
    section_ids = _ordered_unique(
        section_id
        for connector_id in plan.connector_ids
        for section_id in (
            connector_by_id[connector_id].source_section,
            connector_by_id[connector_id].target_section,
        )
    )
    return grid_span_for_sections(graph, section_ids)


def _parallel_segments_conflict(
    first: tuple[tuple[float, float], tuple[float, float]],
    second: tuple[tuple[float, float], tuple[float, float]],
    clearance: float,
) -> bool:
    (first_start, first_end), (second_start, second_end) = first, second
    first_horizontal = abs(first_start[1] - first_end[1]) <= COORD_TOLERANCE
    second_horizontal = abs(second_start[1] - second_end[1]) <= COORD_TOLERANCE
    first_vertical = abs(first_start[0] - first_end[0]) <= COORD_TOLERANCE
    second_vertical = abs(second_start[0] - second_end[0]) <= COORD_TOLERANCE
    if not (
        first_horizontal and second_horizontal or first_vertical and second_vertical
    ):
        return False
    if first_horizontal:
        separation = abs(first_start[1] - second_start[1])
        first_extent = sorted((first_start[0], first_end[0]))
        second_extent = sorted((second_start[0], second_end[0]))
    else:
        separation = abs(first_start[0] - second_start[0])
        first_extent = sorted((first_start[1], first_end[1]))
        second_extent = sorted((second_start[1], second_end[1]))
    overlap = min(first_extent[1], second_extent[1]) - max(
        first_extent[0], second_extent[0]
    )
    return separation < clearance and overlap > COORD_TOLERANCE


def _landing_cross_segment(
    landing: ConvergenceLanding,
    graph: MetroGraph,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if landing.corner_handedness is None:
        return None
    source = graph.stations[landing.source_junction_id]
    if landing.approach_axis is DemandAxis.X:
        runway_sign = 1.0 if landing.approach_direction is Direction.R else -1.0
        turn_coordinate = (
            landing.opening_turn_coordinate
            if landing.opening_turn_coordinate is not None
            else landing.join_point[0] - runway_sign * landing.minimum_runway
        )
        segment = (
            (turn_coordinate, source.y),
            (turn_coordinate, landing.join_point[1]),
        )
    else:
        runway_sign = 1.0 if landing.approach_direction is Direction.D else -1.0
        turn_coordinate = landing.join_point[1] - (runway_sign * landing.minimum_runway)
        segment = (
            (source.x, turn_coordinate),
            (landing.join_point[0], turn_coordinate),
        )
    if all(
        abs(start - end) <= COORD_TOLERANCE for start, end in zip(*segment, strict=True)
    ):
        return None
    return segment


def _reconcile_landing_handedness(
    plans: tuple[ConvergencePlan, ...], graph: MetroGraph
) -> tuple[ConvergencePlan, ...]:
    """Derive each planned corner from its settled cross-run and approach."""
    reconciled: list[ConvergencePlan] = []
    for plan in plans:
        landings: list[ConvergenceLanding] = []
        for landing in plan.landings:
            handedness = landing.corner_handedness
            if handedness is not None:
                source = graph.stations[landing.source_junction_id]
                if landing.approach_axis is DemandAxis.X:
                    start = (landing.join_point[0], source.y)
                    end = landing.join_point
                else:
                    turn_y = (
                        landing.join_point[1]
                        - landing.minimum_runway * landing.approach_direction.sign
                    )
                    start = (source.x, turn_y)
                    end = (landing.join_point[0], turn_y)
                if (
                    abs(start[0] - end[0]) > COORD_TOLERANCE
                    or abs(start[1] - end[1]) > COORD_TOLERANCE
                ):
                    incoming = _direction(start, end)
                    if direction_axis(incoming) is not landing.approach_axis:
                        handedness = turn_handedness(
                            incoming, landing.approach_direction
                        )
            landings.append(replace(landing, corner_handedness=handedness))
        reconciled.append(replace(plan, landings=tuple(landings)))
    return tuple(reconciled)


def _reconcile_continuation_ownership(
    plans: tuple[ConvergencePlan, ...],
) -> tuple[ConvergencePlan, ...]:
    """Keep endpoint ownership aligned with settled continuation coverage."""
    reconciled: list[ConvergencePlan] = []
    for plan in plans:
        continuations = {
            continuation.member_id: continuation
            for continuation in plan.outgoing_continuations
        }
        ownership = tuple(
            replace(
                item,
                role=(
                    ConvergenceEndpointRole.COVERED_CONTINUATION
                    if continuation.covered_by_member_id is not None
                    else ConvergenceEndpointRole.CONTINUATION
                ),
                covered_by_member_id=continuation.covered_by_member_id,
            )
            if (continuation := continuations.get(item.member_id)) is not None
            else item
            for item in plan.endpoint_ownership
        )
        reconciled.append(replace(plan, endpoint_ownership=ownership))
    return tuple(reconciled)


def _reseated_runway(
    landing: ConvergenceLanding, join_point: tuple[float, float]
) -> float:
    """The runway *landing* keeps once its join moves to *join_point*.

    The approach run starts where the feeder last turned toward the trunk, and
    moving trunk geometry does not move that turn, so the run is re-measured
    from it along the approach axis alone: a move perpendicular to that axis
    carries the whole run sideways without lengthening it.
    """
    index = landing.approach_axis.point_index
    approach_start = (
        landing.join_point[index]
        - landing.minimum_runway * landing.approach_direction.sign
    )
    return abs(join_point[index] - approach_start)


def _move_trunk_flank(
    plan: ConvergencePlan,
    flank_rank: int,
    coordinate: float,
) -> ConvergencePlan:
    axis = plan.trunk_axis
    assert axis is not None
    source_flank = flank_rank == 1
    moves_start = source_flank == (axis.direction in {Direction.R, Direction.D})
    old_coordinate = axis.extent_start if moves_start else axis.extent_end
    new_axis = (
        replace(axis, extent_start=coordinate)
        if moves_start
        else replace(axis, extent_end=coordinate)
    )
    old_segment = _trunk_segments(axis)[flank_rank]

    landings: list[ConvergenceLanding] = []
    moved_join_by_member: dict[EmissionMemberId, tuple[float, float]] = {}
    for landing in plan.landings:
        opening_follows_flank = (
            axis.axis is DemandAxis.X
            and landing.approach_axis is DemandAxis.X
            and landing.opening_turn_coordinate is not None
            and abs(landing.opening_turn_coordinate - old_coordinate) <= COORD_TOLERANCE
        )
        if (
            point_to_polyline_distance(landing.join_point, old_segment)
            > COORD_TOLERANCE
        ):
            if opening_follows_flank:
                assert landing.opening_turn_segment is not None
                landings.append(
                    replace(
                        landing,
                        minimum_runway=abs(landing.join_point[0] - coordinate),
                        opening_turn_coordinate=coordinate,
                        opening_turn_segment=(
                            (coordinate, landing.opening_turn_segment[0][1]),
                            (coordinate, landing.opening_turn_segment[1][1]),
                        ),
                    )
                )
                continue
            landings.append(landing)
            continue
        join_point = (
            (coordinate, landing.join_point[1])
            if axis.axis is DemandAxis.X
            else (landing.join_point[0], coordinate)
        )
        runway = _reseated_runway(landing, join_point)
        opening = landing.opening_turn_coordinate
        opening_segment = landing.opening_turn_segment
        if opening_follows_flank:
            opening = coordinate
            assert opening_segment is not None
            opening_segment = (
                (coordinate, opening_segment[0][1]),
                (coordinate, opening_segment[1][1]),
            )
        elif axis.axis is DemandAxis.X and landing.approach_axis is DemandAxis.Y:
            if opening is not None and abs(opening - old_coordinate) <= COORD_TOLERANCE:
                opening = coordinate
                assert opening_segment is not None
                opening_segment = (
                    (coordinate, opening_segment[0][1]),
                    (coordinate, opening_segment[1][1]),
                )
        moved = replace(
            landing,
            join_point=join_point,
            minimum_runway=runway,
            opening_turn_coordinate=opening,
            opening_turn_segment=opening_segment,
        )
        landings.append(moved)
        moved_join_by_member[landing.member_id] = join_point

    old_axis_point = (
        _axis_source_point(axis) if source_flank else _axis_target_point(axis)
    )
    new_axis_point = (
        _axis_source_point(new_axis) if source_flank else _axis_target_point(new_axis)
    )
    continuations = tuple(
        replace(item, start_point=new_axis_point)
        if all(
            abs(actual - expected) <= COORD_TOLERANCE
            for actual, expected in zip(item.start_point, old_axis_point, strict=True)
        )
        else item
        for item in plan.outgoing_continuations
    )
    ownership = tuple(
        replace(item, endpoint=moved_join_by_member[item.member_id])
        if item.member_id in moved_join_by_member
        and item.role is ConvergenceEndpointRole.FEEDER
        else item
        for item in plan.endpoint_ownership
    )
    return replace(
        plan,
        trunk_axis=new_axis,
        landings=tuple(landings),
        outgoing_continuations=continuations,
        endpoint_ownership=ownership,
    )


def _reseat_landing_opening(
    landing: ConvergenceLanding,
    coordinate: float,
    curve_radius: float,
) -> ConvergenceLanding | None:
    """Re-seat a vertical landing opening when its runway remains feasible."""
    if landing.opening_turn_segment is None:
        return None
    runway = landing.minimum_runway
    if landing.approach_axis is DemandAxis.X:
        runway = abs(landing.join_point[0] - coordinate)
        if runway < curve_radius - COORD_TOLERANCE:
            return None
    return replace(
        landing,
        minimum_runway=runway,
        opening_turn_coordinate=coordinate,
        opening_turn_segment=(
            (coordinate, landing.opening_turn_segment[0][1]),
            (coordinate, landing.opening_turn_segment[1][1]),
        ),
    )


def _move_landing_opening(
    plan: ConvergencePlan,
    member_ids: frozenset[EmissionMemberId],
    coordinate: float,
    curve_radius: float,
) -> ConvergencePlan | None:
    """Re-seat one independently owned vertical landing opening."""
    landings = list(plan.landings)
    moved = False
    for rank, landing in enumerate(landings):
        if landing.member_id not in member_ids:
            continue
        reseated = _reseat_landing_opening(landing, coordinate, curve_radius)
        if reseated is None:
            return None
        landings[rank] = reseated
        moved = True
    return replace(plan, landings=tuple(landings)) if moved else None


def _move_trunk_axis(plan: ConvergencePlan, coordinate: float) -> ConvergencePlan:
    """Re-seat *plan*'s shared trunk on *coordinate* across its channel.

    The lateral coordinate is the one position every member standing on the
    central run shares, so it carries the joins made on that run, the opening
    turns that reach down to them, the continuation that leaves from it, and the
    endpoints those members own.  The flanks keep their own coordinates: they are
    the turns into the channel rather than part of it, and ``_trunk_segments``
    re-derives their length from the moved run.
    """
    axis = plan.trunk_axis
    assert axis is not None
    lateral = 1 if axis.axis is DemandAxis.X else 0
    old_coordinate = axis.coordinate
    central = _trunk_segments(axis)[0]

    def lifted(point: tuple[float, float]) -> tuple[float, float]:
        return (point[0], coordinate) if lateral == 1 else (coordinate, point[1])

    def on_central(point: tuple[float, float]) -> bool:
        return point_to_polyline_distance(point, central) <= COORD_TOLERANCE

    landings: list[ConvergenceLanding] = []
    moved_join_by_member: dict[EmissionMemberId, tuple[float, float]] = {}
    for landing in plan.landings:
        if not on_central(landing.join_point):
            landings.append(landing)
            continue
        join_point = lifted(landing.join_point)
        opening_segment = landing.opening_turn_segment
        if opening_segment is not None:
            opening_segment = (
                lifted(opening_segment[0])
                if abs(opening_segment[0][lateral] - old_coordinate) <= COORD_TOLERANCE
                else opening_segment[0],
                lifted(opening_segment[1])
                if abs(opening_segment[1][lateral] - old_coordinate) <= COORD_TOLERANCE
                else opening_segment[1],
            )
        landings.append(
            replace(
                landing,
                join_point=join_point,
                minimum_runway=_reseated_runway(landing, join_point),
                opening_turn_segment=opening_segment,
            )
        )
        moved_join_by_member[landing.member_id] = join_point

    continuations = tuple(
        replace(item, start_point=lifted(item.start_point))
        if on_central(item.start_point)
        else item
        for item in plan.outgoing_continuations
    )
    ownership = tuple(
        replace(item, endpoint=moved_join_by_member[item.member_id])
        if item.member_id in moved_join_by_member
        and item.role is ConvergenceEndpointRole.FEEDER
        else item
        for item in plan.endpoint_ownership
    )
    return replace(
        plan,
        trunk_axis=replace(axis, coordinate=coordinate),
        landings=tuple(landings),
        outgoing_continuations=continuations,
        endpoint_ownership=ownership,
    )


def _nearest_lane(
    coordinate: float,
    obstacles: tuple[float, ...],
    clearance: float,
    toward: float,
    feasible: Callable[[float], bool] | None = None,
) -> float | None:
    """The nearest lane one *clearance* clear of every obstacle that is *feasible*.

    Candidates sit one clearance either side of each obstacle, so the answer is
    the least the mover has to give up.  Ties break toward the *toward* sign,
    which is the side the mover's own geometry already commits it to.

    Omit *feasible* where every clear lane is one the mover can reach.
    """
    candidates = sorted(
        {obstacle + side * clearance for obstacle in obstacles for side in (-1.0, 1.0)},
        key=lambda candidate: (abs(candidate - coordinate), -toward * candidate),
    )
    return next(
        (
            candidate
            for candidate in candidates
            if all(abs(candidate - obstacle) >= clearance for obstacle in obstacles)
            and (feasible is None or feasible(candidate))
        ),
        None,
    )


# ``_trunk_segments`` lists a trunk's source-side runs from the central run
# outward, which is the reverse of the way a member travels them: it arrives at
# its endpoint, turns onto the flank, and only then joins the central run.
_TRUNK_RUN_LISTED_AGAINST_TRAVEL = (False, True, True, False, False)


def _trunk_run_travel_direction(axis: ConvergenceTrunkAxis, rank: int) -> Direction:
    """The direction a member travels *axis*'s run at *rank*."""
    segment = _trunk_segments(axis)[rank]
    if _TRUNK_RUN_LISTED_AGAINST_TRAVEL[rank]:
        segment = (segment[1], segment[0])
    return _direction(*segment)


def _settle_shared_trunk_channels(
    plans: tuple[ConvergencePlan, ...], curve_radius: float
) -> tuple[ConvergencePlan, ...]:
    """Lane the runs of one system's trunks that would share a single channel.

    Each plan derives its trunk geometry from its own trial route, and a trial
    route is produced with no knowledge of its siblings, so two plans of one
    system whose trunks take the same channel derive the same coordinate and each
    believes it has the channel to itself.  Which lane of a shared channel a run
    occupies is a decision for the system, not for either plan: the plans are
    laned here in their system's order, each taking the nearest lane that clears
    every run already seated by ``cotravelling_lane_clearance``.

    A trunk shares two kinds of channel and both are laned by that one rule: the
    channel its central run travels along, and the channel its flanks turn out
    into.  Only a line's own return leg is laned.  Two runs of one line going the
    same way along one coordinate are a deliberately fused stroke, and separating
    them would draw one route twice.
    """
    if len(plans) < 2:
        return plans
    clearance = cotravelling_lane_clearance(
        same_line=True, counter_running=True, curve_radius=curve_radius
    )
    settled = _lane_trunk_runs(list(plans), clearance)
    return tuple(_lane_trunk_flanks(settled, clearance, curve_radius))


def _settle_shared_opening_pivots(
    plans: tuple[ConvergencePlan, ...], graph: MetroGraph
) -> tuple[ConvergencePlan, ...]:
    groups: defaultdict[tuple[str, tuple[str, ...], Direction], list[int]] = (
        defaultdict(list)
    )
    for rank, plan in enumerate(plans):
        axis = plan.trunk_axis
        if axis is None:
            continue
        source = next(
            (
                landing.source_junction_id
                for landing in plan.landings
                if landing.member_id == plan.primary_trunk_member_id
            ),
            None,
        )
        if source is None:
            continue
        groups[(source, plan.line_ids, _trunk_run_travel_direction(axis, 1))].append(
            rank
        )

    settled = list(plans)
    for (source_id, _lines, _direction), ranks in groups.items():
        if len(ranks) < 2:
            continue
        columns: list[float] = []
        for rank in ranks:
            axis = settled[rank].trunk_axis
            assert axis is not None
            columns.append(_trunk_segments(axis)[1][0][0])
        reference = merge_fanout_pivot_reference(
            columns, graph.stations[source_id].x, COORD_TOLERANCE
        )
        if reference is None:
            continue
        for rank in ranks:
            settled[rank] = _move_trunk_flank(settled[rank], 1, reference)
    return tuple(settled)


def _settle_shared_source_openings(
    plans: tuple[ConvergencePlan, ...], curve_radius: float
) -> tuple[ConvergencePlan, ...]:
    """Fuse same-line opening descents that leave one source junction."""
    primary_flanks: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    for plan in plans:
        axis = plan.trunk_axis
        if axis is None or axis.axis is not DemandAxis.X:
            continue
        primary = next(
            (
                ownership
                for ownership in plan.endpoint_ownership
                if ownership.member_id == plan.primary_trunk_member_id
            ),
            None,
        )
        if primary is None:
            continue
        primary_flanks[(primary.edge.source, primary.edge.line_id)].append(
            _trunk_segments(axis)[1][0][0]
        )

    settled: list[ConvergencePlan] = []
    for plan in plans:
        landings = list(plan.landings)
        for rank, landing in enumerate(landings):
            if (
                landing.approach_axis is not DemandAxis.X
                or landing.opening_turn_coordinate is None
                or landing.opening_turn_segment is None
            ):
                continue
            references = set(
                primary_flanks.get(
                    (landing.source_junction_id, landing.edge.line_id), ()
                )
            )
            references.discard(landing.opening_turn_coordinate)
            if len(references) != 1:
                continue
            coordinate = next(iter(references))
            reseated = _reseat_landing_opening(landing, coordinate, curve_radius)
            if reseated is not None:
                landings[rank] = reseated
        settled.append(replace(plan, landings=tuple(landings)))
    return tuple(settled)


def _settle_opposing_landing_channels(
    plans: tuple[ConvergencePlan, ...],
    graph: MetroGraph,
    exit_turn_plans: tuple[ExitTurnPlan, ...],
    curve_radius: float,
) -> tuple[ConvergencePlan, ...]:
    """Lane counter-running opening descents before either route is emitted."""
    fixed_members = {
        assignment.member_id
        for exit_plan in exit_turn_plans
        for assignment in exit_plan.assignments
        if assignment.axis_id is not None
    }
    clearance = cotravelling_lane_clearance(
        same_line=True, counter_running=True, curve_radius=curve_radius
    )
    ordered = sorted(
        (
            (plan_rank, landing_rank, landing)
            for plan_rank, plan in enumerate(plans)
            for landing_rank, landing in enumerate(plan.landings)
            if landing.opening_turn_coordinate is not None
            and landing.approach_axis is DemandAxis.X
            and landing.corner_handedness is not None
        ),
        key=lambda item: (item[2].member_id not in fixed_members, item[0], item[1]),
    )
    settled = list(plans)
    resident: list[ConvergenceLanding] = []
    for plan_rank, landing_rank, original in ordered:
        landing = settled[plan_rank].landings[landing_rank]
        if landing.member_id not in fixed_members:
            for obstacle in resident:
                landing_segment = _landing_cross_segment(landing, graph)
                obstacle_segment = _landing_cross_segment(obstacle, graph)
                if (
                    obstacle.edge.line_id != landing.edge.line_id
                    or landing_segment is None
                    or obstacle_segment is None
                    or _direction(*obstacle_segment) is _direction(*landing_segment)
                ):
                    continue
                if not _parallel_segments_conflict(
                    landing_segment, obstacle_segment, clearance
                ):
                    continue
                assert obstacle.opening_turn_coordinate is not None
                candidate = (
                    obstacle.opening_turn_coordinate
                    + landing.approach_direction.sign * clearance
                )
                reseated = _reseat_landing_opening(landing, candidate, curve_radius)
                if reseated is None:
                    continue
                plan = settled[plan_rank]
                if landing.member_id == plan.primary_trunk_member_id:
                    original_continuations = plan.outgoing_continuations
                    plan = _move_trunk_flank(plan, 1, candidate)
                    plan = replace(
                        plan,
                        outgoing_continuations=tuple(
                            replace(
                                moved,
                                covered_by_member_id=plan.primary_trunk_member_id,
                            )
                            if moved.start_point != original.start_point
                            else moved
                            for original, moved in zip(
                                original_continuations,
                                plan.outgoing_continuations,
                                strict=True,
                            )
                        ),
                    )
                    landing = plan.landings[landing_rank]
                landing = replace(
                    landing,
                    minimum_runway=reseated.minimum_runway,
                    opening_turn_coordinate=reseated.opening_turn_coordinate,
                    opening_turn_segment=reseated.opening_turn_segment,
                )
                landings = list(plan.landings)
                landings[landing_rank] = landing
                settled[plan_rank] = replace(plan, landings=tuple(landings))
        resident.append(landing)
    return tuple(settled)


def _lane_trunk_runs(
    settled: list[ConvergencePlan], clearance: float
) -> list[ConvergencePlan]:
    """Give each central run its own lane across the channel it travels."""
    seated: list[tuple[tuple[str, ...], ConvergenceTrunkAxis]] = []
    for plan_rank, plan in enumerate(settled):
        axis = plan.trunk_axis
        if axis is None:
            continue
        obstacles = tuple(
            other
            for other_lines, other in seated
            if other_lines == plan.line_ids
            and other.axis is axis.axis
            and other.direction is not axis.direction
            and _parallel_segments_conflict(
                _trunk_segments(other)[0], _trunk_segments(axis)[0], clearance
            )
        )
        if obstacles:
            # The flanks are the turns this trunk already commits to, so the side
            # they lead is the side a lane can widen into.
            toward = (
                axis.source_flank_coordinate + axis.target_flank_coordinate
            ) / 2.0 - axis.coordinate
            coordinate = _nearest_lane(
                axis.coordinate,
                tuple(item.coordinate for item in obstacles),
                clearance,
                1.0 if toward >= 0.0 else -1.0,
            )
            if coordinate is not None:
                settled[plan_rank] = _move_trunk_axis(settled[plan_rank], coordinate)
                moved = settled[plan_rank].trunk_axis
                assert moved is not None
                axis = moved
        seated.append((plan.line_ids, axis))
    return settled


def _lane_trunk_flanks(
    settled: list[ConvergencePlan], clearance: float, curve_radius: float
) -> list[ConvergencePlan]:
    """Give each flank its own lane across the channel it turns out into."""
    seated: list[tuple[tuple[str, ...], _Segment, Direction]] = []
    for plan_rank, plan in enumerate(settled):
        resident = tuple(seated)
        if plan.trunk_axis is None:
            continue
        for flank_rank in (1, 3):
            # A flank move re-seats the whole plan, so each is read off the plan
            # as it stands after the last one.
            axis = settled[plan_rank].trunk_axis
            assert axis is not None
            flank = _trunk_segments(axis)[flank_rank]
            direction = _trunk_run_travel_direction(axis, flank_rank)
            obstacles = tuple(
                other
                for other_lines, other, other_direction in resident
                if other_lines == plan.line_ids
                and other_direction is not direction
                and _parallel_segments_conflict(other, flank, clearance)
            )
            endpoint = (
                axis.source_endpoint_coordinate
                if flank_rank == 1
                else axis.target_endpoint_coordinate
            )
            if not obstacles or endpoint is None:
                continue
            lateral = axis.axis.point_index
            column = flank[0][lateral]
            toward = 1.0 if endpoint > column else -1.0
            coordinate = _nearest_lane(
                column,
                tuple(item[0][lateral] for item in obstacles),
                clearance,
                toward,
                lambda candidate: (endpoint - candidate) * toward >= curve_radius,
            )
            if coordinate is not None:
                settled[plan_rank] = _move_trunk_flank(
                    settled[plan_rank], flank_rank, coordinate
                )
        axis = settled[plan_rank].trunk_axis
        assert axis is not None
        seated.extend(
            (
                plan.line_ids,
                _trunk_segments(axis)[flank_rank],
                _trunk_run_travel_direction(axis, flank_rank),
            )
            for flank_rank in (1, 3)
        )
    return settled


def _forked_flank(
    landing: ConvergenceLanding, trunk_plan: ConvergencePlan, flank_rank: int
) -> bool:
    """Whether a landing leg and a trunk flank are two arms off one fork.

    A trunk's source flank stands on the column its primary member descends, so a
    landing leaving that same junction is a sibling arm of the same fan-out: the
    two draw one stroke down to the depth where they part.
    """
    if flank_rank != 1:
        return False
    fork = next(
        (
            item.source_junction_id
            for item in trunk_plan.landings
            if item.member_id == trunk_plan.primary_trunk_member_id
            and item.opening_turn_coordinate is not None
        ),
        None,
    )
    if fork is None:
        fork = next(
            (
                ownership.edge.source
                for ownership in trunk_plan.endpoint_ownership
                if ownership.member_id == trunk_plan.primary_trunk_member_id
            ),
            None,
        )
    return fork is not None and landing.source_junction_id == fork


def _shared_source_bundle_stroke(
    landing_plan: ConvergencePlan,
    landing: ConvergenceLanding,
    trunk_plan: ConvergencePlan,
    flank_rank: int,
    landing_segment: _Segment,
    flank: _Segment,
) -> bool:
    """Whether two convergences deliberately share one complete source stroke."""
    landing_sources = frozenset(
        item.source_junction_id for item in landing_plan.landings
    )
    trunk_sources = frozenset(item.source_junction_id for item in trunk_plan.landings)
    return (
        flank_rank == 1
        and len(landing_sources) > 1
        and landing_sources == trunk_sources
        and landing.source_junction_id in trunk_sources
        and _segments_coincide(landing_segment, flank)
    )


def _shared_chained_source_stroke(
    landing_plan: ConvergencePlan,
    landing: ConvergenceLanding,
    trunk_plan: ConvergencePlan,
    flank_rank: int,
    landing_segment: _Segment,
    flank: _Segment,
) -> bool:
    """Whether chained convergences describe one source carrier twice.

    A source junction can feed more than one downstream convergence in the
    same route system.  Once same-line settlement seats their exact collinear
    legs together, those legs are one physical carrier even when the two
    convergence groups have different complete source sets.
    """
    return (
        flank_rank == 1
        and landing_plan.system_id == trunk_plan.system_id
        and landing.source_junction_id
        in {item.source_junction_id for item in trunk_plan.landings}
        and _segments_coincide(landing_segment, flank)
    )


def _flank_settled_column(
    *,
    forked: bool,
    flank_coordinate: float,
    landing_coordinate: float,
    endpoint: float,
    clearance: float,
    curve_radius: float,
) -> float | None:
    """The column a trunk flank settles on against one crowding landing leg.

    A flank descending from the fork the landing leaves is one stroke with it, so
    it fuses onto the landing's column: separating two arms of one line off one
    fork would draw the fork twice.  Any other crowding pair is two bundles, so
    the flank steps one clearance clear of the landing on the side its endpoint
    lies.  Either way the flank keeps its endpoint on that side with a full radius
    of runway, which is exactly the corner it turns on to reach it.
    """
    toward_endpoint = 1.0 if endpoint > flank_coordinate else -1.0
    if forked:
        if abs(flank_coordinate - landing_coordinate) <= COORD_TOLERANCE:
            return None
        coordinate = landing_coordinate
    else:
        if abs(endpoint - flank_coordinate) <= clearance:
            return None
        coordinate = landing_coordinate + toward_endpoint * clearance
    reach = (endpoint - coordinate) * toward_endpoint
    return None if reach < curve_radius else coordinate


def _settle_landing_trunk_flanks(
    plans: tuple[ConvergencePlan, ...], graph: MetroGraph, curve_radius: float
) -> tuple[ConvergencePlan, ...]:
    settled = list(plans)
    plan_rank_by_id = {plan.id: rank for rank, plan in enumerate(plans)}
    clearance = cotravelling_lane_clearance(
        same_line=True, counter_running=True, curve_radius=curve_radius
    )
    for landing_plan in plans:
        for landing in landing_plan.landings:
            landing_segment = _landing_cross_segment(landing, graph)
            if landing_segment is None:
                continue
            landing_direction = _direction(*landing_segment)
            landing_horizontal = (
                abs(landing_segment[0][1] - landing_segment[1][1]) <= COORD_TOLERANCE
            )
            landing_coordinate = (
                landing_segment[0][1] if landing_horizontal else landing_segment[0][0]
            )
            for plan_rank, trunk_plan in enumerate(tuple(settled)):
                axis = trunk_plan.trunk_axis
                if trunk_plan.id == landing_plan.id or axis is None:
                    continue
                for flank_rank in (1, 3):
                    flank = _trunk_segments(axis)[flank_rank]
                    if (
                        landing.edge.line_id not in trunk_plan.line_ids
                        or landing_direction
                        is _trunk_run_travel_direction(axis, flank_rank)
                        or not _parallel_segments_conflict(
                            landing_segment, flank, curve_radius
                        )
                        or _shared_source_bundle_stroke(
                            landing_plan,
                            landing,
                            trunk_plan,
                            flank_rank,
                            landing_segment,
                            flank,
                        )
                    ):
                        continue
                    flank_coordinate = (
                        flank[0][1] if landing_horizontal else flank[0][0]
                    )
                    endpoint = (
                        axis.source_endpoint_coordinate
                        if flank_rank == 1
                        else axis.target_endpoint_coordinate
                    )
                    if endpoint is None:
                        continue
                    coordinate = _flank_settled_column(
                        forked=_forked_flank(landing, trunk_plan, flank_rank),
                        flank_coordinate=flank_coordinate,
                        landing_coordinate=landing_coordinate,
                        endpoint=endpoint,
                        clearance=clearance,
                        curve_radius=curve_radius,
                    )
                    if coordinate is None:
                        forked = _forked_flank(landing, trunk_plan, flank_rank)
                        if (
                            not forked
                            or landing.member_id == landing_plan.primary_trunk_member_id
                            or landing.opening_turn_coordinate is None
                        ):
                            continue
                        reseated = _reseat_landing_opening(
                            landing, flank_coordinate, curve_radius
                        )
                        if reseated is None:
                            continue
                        landing_rank = landing_plan.landings.index(landing)
                        landing_plan_rank = plan_rank_by_id[landing_plan.id]
                        current_plan = settled[landing_plan_rank]
                        current_landings = list(current_plan.landings)
                        current_landings[landing_rank] = reseated
                        settled[landing_plan_rank] = replace(
                            current_plan, landings=tuple(current_landings)
                        )
                        continue
                    moved = _move_trunk_flank(trunk_plan, flank_rank, coordinate)
                    settled[plan_rank] = moved
                    trunk_plan = moved
                    axis = moved.trunk_axis
                    assert axis is not None
    return tuple(settled)


@dataclass(frozen=True)
class _PlanGapChannel:
    """One vertical leg a convergence plan pins inside an inter-column gap.

    ``flank_rank`` names the trunk flank whose column carries the leg, so a
    settling move knows which :func:`_move_trunk_flank` call re-seats the whole
    stack standing on it; ``None`` marks a leg on a column no flank owns, which
    only ever acts as an obstacle. ``line_ids`` and ``claimant_member_ids``
    describe this physical leg, not the wider convergence plan.
    """

    flank_rank: int | None
    coordinate: float
    y_lo: float
    y_hi: float
    down: bool
    gap: tuple[int, int | None]
    line_ids: frozenset[str]
    claimant_member_ids: frozenset[EmissionMemberId]
    source_junction_ids: frozenset[str]
    connector_ids: frozenset[str]
    system_id: RouteSystemId
    continuation_endpoint_ids: frozenset[str]
    member_geometry_owned: bool

    def __post_init__(self) -> None:
        if not (
            self.claimant_member_ids and self.source_junction_ids and self.connector_ids
        ):
            raise ValueError("planned gap channel requires complete carrier provenance")


def _plan_gap_channels(
    plan: ConvergencePlan,
    graph: MetroGraph,
    lookup: GapLookupGeometry,
) -> tuple[_PlanGapChannel, ...]:
    """Every vertical leg *plan* seats inside an inter-column gap.

    A plan pins two families of vertical geometry: the flanks its trunk turns
    onto at each end, and the opening turn each landing descends (or climbs)
    before it meets the trunk.  Both are frozen before the post-routing gap
    passes run, so they are the plan's whole footprint in a gap.
    """
    axis = plan.trunk_axis
    if axis is None or axis.axis is not DemandAxis.X:
        return ()
    trunk = _trunk_segments(axis)
    ownership_by_member = {
        ownership.member_id: ownership.edge for ownership in plan.endpoint_ownership
    }
    connector_ids_by_member = {
        ownership.member_id: frozenset(ownership.connector_ids)
        for ownership in plan.endpoint_ownership
    }
    connector_ids_by_line = {
        line_id: frozenset(
            connector_id
            for ownership in plan.endpoint_ownership
            if ownership.edge.line_id == line_id
            for connector_id in ownership.connector_ids
        )
        for line_id in plan.line_ids
    }
    continuation_endpoints = frozenset(
        (
            *(landing.source_junction_id for landing in plan.landings),
            *(continuation.edge.target for continuation in plan.outgoing_continuations),
        )
    )
    primary_edge = (
        ownership_by_member.get(plan.primary_trunk_member_id)
        if plan.primary_trunk_member_id is not None
        else None
    )
    spans: list[
        tuple[
            int | None,
            float,
            float,
            float,
            frozenset[str],
            frozenset[EmissionMemberId],
            frozenset[str],
            frozenset[str],
        ]
    ] = []
    flank_columns: dict[int, tuple[float, frozenset[str], _Segment]] = {}
    for flank_rank in (1, 3):
        (start_x, start_y), (end_x, end_y) = trunk[flank_rank]
        if (
            abs(end_x - start_x) > COORD_TOLERANCE
            or abs(end_y - start_y) <= COORD_TOLERANCE
        ):
            continue
        flank_sources = (
            frozenset({primary_edge.source})
            if primary_edge is not None
            else frozenset()
        )
        flank_columns[flank_rank] = (start_x, flank_sources, trunk[flank_rank])
        if primary_edge is not None and plan.primary_trunk_member_id is not None:
            spans.append(
                (
                    flank_rank,
                    start_x,
                    start_y,
                    end_y,
                    frozenset({primary_edge.line_id}),
                    frozenset({plan.primary_trunk_member_id}),
                    frozenset({primary_edge.source}),
                    connector_ids_by_line[primary_edge.line_id],
                )
            )
    for landing in plan.landings:
        segment = landing.opening_turn_segment
        if segment is None:
            continue
        (start_x, start_y), (_end_x, end_y) = segment
        carrier = next(
            (
                rank
                for rank, (column, sources, segment) in flank_columns.items()
                if abs(column - start_x) <= COORD_TOLERANCE
                and (
                    point_to_polyline_distance(landing.join_point, segment)
                    <= COORD_TOLERANCE
                    or not sources
                    or landing.source_junction_id in sources
                )
            ),
            None,
        )
        spans.append(
            (
                carrier,
                start_x,
                start_y,
                end_y,
                frozenset({landing.edge.line_id}),
                frozenset({landing.member_id}),
                frozenset({landing.source_junction_id}),
                connector_ids_by_member[landing.member_id],
            )
        )
    channels: list[_PlanGapChannel] = []
    for (
        carrier_rank,
        x,
        start_y,
        end_y,
        line_ids,
        claimant_member_ids,
        source_junction_ids,
        connector_ids,
    ) in spans:
        y_lo, y_hi = sorted((start_y, end_y))
        gap = gap_lo_for_x(graph, x, y_lo, y_hi, lookup=lookup)
        if gap is None:
            continue
        channels.append(
            _PlanGapChannel(
                carrier_rank,
                x,
                y_lo,
                y_hi,
                end_y > start_y,
                gap,
                line_ids,
                claimant_member_ids,
                source_junction_ids,
                connector_ids,
                plan.system_id,
                continuation_endpoints,
                False,
            )
        )
    return tuple(channels)


def _gap_channels_crowd(first: _PlanGapChannel, second: _PlanGapChannel) -> bool:
    """Whether two distinct-line legs are too close in one corridor.

    ``same_line=False`` is sound because the caller filters an obstacle sharing
    any of the leg's lines before asking (:func:`_settle_opposing_gap_flanks`),
    so every pair reaching here carries distinct lines.
    """
    if first.line_ids & second.line_ids:
        return False
    clearance = (
        cotravelling_lane_clearance(
            same_line=False, counter_running=True, curve_radius=CURVE_RADIUS
        )
        if first.down is not second.down
        else OFFSET_STEP
    )
    return (
        first.gap == second.gap
        and spans_share_corridor(first.y_lo, first.y_hi, second.y_lo, second.y_hi)
        and abs(first.coordinate - second.coordinate) < clearance - COORD_TOLERANCE_FINE
    )


def _member_represented_convergence_lane(
    convergence_channel: _PlanGapChannel,
    member_channel: _PlanGapChannel,
    member_channels: tuple[_PlanGapChannel, ...],
) -> bool:
    """Whether frozen member geometry already owns the convergence stroke."""
    return any(
        not _gap_channels_crowd(candidate, member_channel)
        and min(
            convergence_channel.y_hi,
            member_channel.y_hi,
            candidate.y_hi,
        )
        - max(
            convergence_channel.y_lo,
            member_channel.y_lo,
            candidate.y_lo,
        )
        > COORD_TOLERANCE
        for candidate in member_channels
        if candidate.gap == convergence_channel.gap
        and candidate.line_ids & convergence_channel.line_ids
        and abs(candidate.coordinate - convergence_channel.coordinate)
        <= COORD_TOLERANCE
        and spans_share_corridor(
            candidate.y_lo,
            candidate.y_hi,
            convergence_channel.y_lo,
            convergence_channel.y_hi,
        )
    )


def _flank_lane_coordinate(
    plan: ConvergencePlan,
    flank_rank: int,
    column: float,
    obstacles: tuple[_PlanGapChannel, ...],
    graph: MetroGraph,
    lookup: GapLookupGeometry,
    curve_radius: float,
    *,
    carries_coupled_primary: bool = False,
    jointly_feasible: Callable[[float], bool] | None = None,
) -> float | None:
    """Nearest column for *flank_rank* that clears every counter-running obstacle.

    Candidates sit a bundle clearance either side of each obstacle; the chosen
    one keeps the whole stack standing on the flank inside its gap's usable band,
    leaves the flank a full corner runway to its endpoint, and re-seats every leg
    that column carried so nothing is orphaned at the old column.
    """
    axis = plan.trunk_axis
    assert axis is not None
    # Seating the primary trunk member's opening turn carries its whole tail
    # along, so a flank sharing that column would drag the member off the
    # endpoint the plan pins it to. That flank is out of this pass's reach.
    if not carries_coupled_primary and any(
        landing.member_id == plan.primary_trunk_member_id
        and landing.opening_turn_coordinate is not None
        and abs(landing.opening_turn_coordinate - column) <= COORD_TOLERANCE
        for landing in plan.landings
    ):
        return None
    endpoint = (
        axis.source_endpoint_coordinate
        if flank_rank == 1
        else axis.target_endpoint_coordinate
    )
    if endpoint is None or abs(endpoint - column) <= curve_radius:
        return None
    toward_endpoint = 1.0 if endpoint > column else -1.0
    # The trunk keeps every join it already carries, so the flank may not slide
    # back over one: a landing needs its corner's runway along the trunk too.
    trunk_side = [
        point[0]
        for rank, segment in enumerate(_trunk_segments(axis))
        if rank != flank_rank
        for point in segment
    ]
    trunk_side += [
        landing.join_point[0]
        for landing in plan.landings
        if abs(landing.join_point[1] - axis.coordinate) <= COORD_TOLERANCE
    ]

    def feasible(candidate: float) -> bool:
        if (endpoint - candidate) * toward_endpoint <= curve_radius:
            return False
        if any(
            (candidate - other) * toward_endpoint <= curve_radius
            for other in trunk_side
            if (column - other) * toward_endpoint > curve_radius
        ):
            return False
        moved = _move_trunk_flank(plan, flank_rank, candidate)
        seated = _plan_gap_channels(moved, graph, lookup)
        if any(
            abs(channel.coordinate - column) <= COORD_TOLERANCE for channel in seated
        ):
            return False
        for channel in seated:
            if channel.flank_rank != flank_rank:
                continue
            gap_left, gap_right = column_gap_edges(
                graph, channel.gap[0], channel.gap[0] + 1, row=channel.gap[1]
            )
            usable_left = gap_left + EDGE_TO_BUNDLE_CLEARANCE
            usable_right = gap_right - EDGE_TO_BUNDLE_CLEARANCE
            if (
                gap_right <= gap_left
                or channel.coordinate < usable_left - COORD_TOLERANCE
                or channel.coordinate > usable_right + COORD_TOLERANCE
            ):
                return False
            if any(_gap_channels_crowd(channel, obstacle) for obstacle in obstacles):
                return False
        return jointly_feasible is None or jointly_feasible(candidate)

    flank_down = next(
        channel.down
        for channel in _plan_gap_channels(plan, graph, lookup)
        if channel.flank_rank == flank_rank
        and abs(channel.coordinate - column) <= COORD_TOLERANCE
    )
    candidates = sorted(
        {
            obstacle.coordinate
            + sign
            * (
                BUNDLE_TO_BUNDLE_CLEARANCE
                if obstacle.down is not flank_down
                else OFFSET_STEP
            )
            for obstacle in obstacles
            for sign in (-1.0, 1.0)
        },
        key=lambda candidate: (abs(candidate - column), candidate),
    )
    return next((candidate for candidate in candidates if feasible(candidate)), None)


def _exit_owned_flanks(
    plans: tuple[ConvergencePlan, ...],
    exit_turn_plans: tuple[ExitTurnPlan, ...],
) -> frozenset[tuple[ConvergencePlanId, int]]:
    axes_by_member: dict[EmissionMemberId, float] = {}
    for exit_plan in exit_turn_plans:
        axes = {axis.id: axis for axis in exit_plan.axes}
        axes_by_member.update(
            {
                assignment.member_id: axes[assignment.axis_id].coordinate
                for assignment in exit_plan.assignments
                if assignment.axis_id is not None
            }
        )
    owned: set[tuple[ConvergencePlanId, int]] = set()
    for plan in plans:
        axis = plan.trunk_axis
        if axis is None:
            continue
        segments = _trunk_segments(axis)
        for landing in plan.landings:
            coordinate = axes_by_member.get(landing.member_id)
            if coordinate is None:
                continue
            for flank_rank in (1, 3):
                flank = segments[flank_rank]
                lateral = axis.axis.point_index
                if abs(flank[0][lateral] - coordinate) <= COORD_TOLERANCE:
                    owned.add((plan.id, flank_rank))
    return frozenset(owned)


def _fixed_exit_axis_channels(
    exit_turn_plans: tuple[ExitTurnPlan, ...],
) -> frozenset[tuple[EmissionMemberId, float]]:
    """Member coordinates fixed to structural exit-turn anchors."""
    return frozenset(
        (assignment.member_id, axis.coordinate)
        for exit_plan in exit_turn_plans
        for axis in exit_plan.axes
        for assignment in exit_plan.assignments
        if axis.fixed_anchor_id is not None and assignment.axis_id == axis.id
    )


def _settle_reserved_gap_flanks(
    plans: tuple[ConvergencePlan, ...],
    ctx: _RoutingCtx,
) -> tuple[ConvergencePlan, ...]:
    """Seat planned trunk flanks in column bands allocated by the prior ledger.

    The first render pass publishes segment claims and envelope settlement turns
    those claims into concrete bands.  A convergence re-planned for the settled
    render must consume that allocation while its trunk geometry is constructed:
    the generic corridor normalizer deliberately cannot move a segment the plan
    owns after emission.
    """

    settled: list[ConvergencePlan] = []
    for original in plans:
        plan = original
        axis = plan.trunk_axis
        primary = next(
            (
                landing
                for landing in plan.landings
                if landing.member_id == plan.primary_trunk_member_id
            ),
            None,
        )
        if axis is None or axis.axis is not DemandAxis.X or primary is None:
            settled.append(plan)
            continue
        bands = ctx.reserved_bands.claimed_column_bands(
            primary.edge.source, primary.edge.target, primary.edge.line_id
        )
        if not bands:
            settled.append(plan)
            continue
        for flank_rank in (1, 3):
            flank = _trunk_segments(axis)[flank_rank]
            if abs(flank[0][1] - flank[1][1]) <= COORD_TOLERANCE:
                continue
            coordinate = flank[0][0]

            def distance(band: ReservedBand) -> tuple[float, float]:
                if coordinate < band.lo:
                    return band.lo - coordinate, band.lo
                if coordinate > band.hi:
                    return coordinate - band.hi, band.lo
                return 0.0, band.lo

            band = min(bands, key=distance)
            if distance(band)[0] > EDGE_TO_BUNDLE_CLEARANCE + COORD_TOLERANCE:
                continue
            held = band.hold(coordinate)
            if abs(held - coordinate) <= COORD_TOLERANCE:
                continue
            plan = _move_trunk_flank(plan, flank_rank, held)
        settled.append(plan)
    return tuple(settled)


def _settle_opposing_gap_flanks(
    plans: tuple[ConvergencePlan, ...],
    graph: MetroGraph,
    curve_radius: float,
    exit_owned_flanks: frozenset[tuple[ConvergencePlanId, int]] = frozenset(),
    fixed_channels: tuple[_PlanGapChannel, ...] = (),
    fixed_exit_channels: frozenset[tuple[EmissionMemberId, float]] = frozenset(),
) -> tuple[ConvergencePlan, ...]:
    """Lane counter-running flank columns that share one inter-column gap.

    Each convergence plan reads its flank column from its own trial route, so two
    systems whose trunks turn into the same gap toward different entry ports both
    land near the middle of it and run opposite ways along one column.  The
    post-routing gap passes cannot lane them apart: a planned flank owns its
    geometry, so those passes treat it as immovable.  The flank column is a plan
    decision, so lane it here -- plans settle in order, and a later plan's flank
    steps to the nearest column giving every earlier leg the bundle clearance.
    """
    if len(plans) < 2 and not fixed_channels:
        return plans
    rank_by_id = {plan.id: rank for rank, plan in enumerate(plans)}
    ordered = tuple(
        sorted(
            plans,
            key=lambda plan: (
                not any((plan.id, rank) in exit_owned_flanks for rank in (1, 3)),
                rank_by_id[plan.id],
            ),
        )
    )
    settled = list(ordered)
    lookup = gap_lookup_geometry(graph)
    for current_rank, _plan in enumerate(ordered):
        resident = [
            *fixed_channels,
            *(
                channel
                for prior_plan in settled[:current_rank]
                for channel in _plan_gap_channels(prior_plan, graph, lookup)
            ),
        ]
        plan = settled[current_rank]
        if plan.trunk_axis is None or plan.trunk_axis.axis is not DemandAxis.X:
            continue
        channels = _plan_gap_channels(plan, graph, lookup)
        for flank_rank in (1, 3):
            if (plan.id, flank_rank) in exit_owned_flanks:
                continue
            seated = [
                channel for channel in channels if channel.flank_rank == flank_rank
            ]
            obstacles = tuple(
                obstacle
                for obstacle in resident
                if any(not channel.line_ids & obstacle.line_ids for channel in seated)
            )
            if not seated or not any(
                _gap_channels_crowd(channel, obstacle)
                for channel in seated
                for obstacle in obstacles
            ):
                continue
            coupled_flanks = tuple(
                (candidate_rank, candidate_flank_rank)
                for candidate_rank, candidate_plan in enumerate(settled)
                for candidate_flank_rank in (1, 3)
                if (candidate_plan.id, candidate_flank_rank) not in exit_owned_flanks
                if any(
                    candidate_channel.flank_rank == candidate_flank_rank
                    and candidate_channel.gap == channel.gap
                    and candidate_channel.line_ids & channel.line_ids
                    and abs(candidate_channel.coordinate - channel.coordinate)
                    <= COORD_TOLERANCE
                    and spans_share_corridor(
                        candidate_channel.y_lo,
                        candidate_channel.y_hi,
                        channel.y_lo,
                        channel.y_hi,
                    )
                    for candidate_channel in _plan_gap_channels(
                        candidate_plan, graph, lookup
                    )
                    for channel in seated
                )
            )

            def moved_coupled_flanks(
                candidate_coordinate: float,
            ) -> dict[int, ConvergencePlan]:
                moved_by_rank: dict[int, ConvergencePlan] = {}
                for candidate_rank, candidate_flank_rank in coupled_flanks:
                    candidate_plan = moved_by_rank.get(
                        candidate_rank, settled[candidate_rank]
                    )
                    moved_by_rank[candidate_rank] = _move_trunk_flank(
                        candidate_plan,
                        candidate_flank_rank,
                        candidate_coordinate,
                    )
                return moved_by_rank

            def landing_feasible(candidate_coordinate: float) -> bool:
                moved_by_rank = moved_coupled_flanks(candidate_coordinate)
                candidate = tuple(
                    moved_by_rank.get(rank, item) for rank, item in enumerate(settled)
                )
                return (
                    _landing_trunk_flank_conflict(candidate, graph, curve_radius)
                    is None
                )

            coordinate = _flank_lane_coordinate(
                settled[current_rank],
                flank_rank,
                seated[0].coordinate,
                obstacles,
                graph,
                lookup,
                curve_radius,
                carries_coupled_primary=len(coupled_flanks) > 1,
                jointly_feasible=landing_feasible,
            )
            if coordinate is None and len(coupled_flanks) == 1:
                direct_candidates = sorted(
                    {
                        obstacle.coordinate
                        + sign
                        * (
                            BUNDLE_TO_BUNDLE_CLEARANCE
                            if seated[0].down is not obstacle.down
                            else OFFSET_STEP
                        )
                        for obstacle in obstacles
                        for sign in (-1.0, 1.0)
                    },
                    key=lambda candidate: (
                        abs(candidate - seated[0].coordinate),
                        candidate,
                    ),
                )
                for candidate_coordinate in direct_candidates:
                    try:
                        moved_by_rank = moved_coupled_flanks(candidate_coordinate)
                    except ValueError:
                        continue
                    coupled_plan = moved_by_rank[current_rank]
                    moved_channels = tuple(
                        channel
                        for channel in _plan_gap_channels(coupled_plan, graph, lookup)
                        if channel.flank_rank == flank_rank
                    )
                    if not moved_channels or any(
                        _gap_channels_crowd(channel, obstacle)
                        for channel in moved_channels
                        for obstacle in resident
                    ):
                        continue
                    if any(
                        not (
                            (
                                bounds := column_gap_edges(
                                    graph,
                                    channel.gap[0],
                                    channel.gap[0] + 1,
                                    row=channel.gap[1],
                                )
                            )[0]
                            + EDGE_TO_BUNDLE_CLEARANCE
                            - COORD_TOLERANCE
                            <= channel.coordinate
                            <= bounds[1] - EDGE_TO_BUNDLE_CLEARANCE + COORD_TOLERANCE
                        )
                        for channel in moved_channels
                    ):
                        continue
                    if not landing_feasible(candidate_coordinate):
                        continue
                    coordinate = candidate_coordinate
                    break
            if coordinate is None:
                continue
            moved_by_rank = moved_coupled_flanks(coordinate)
            candidate = tuple(
                moved_by_rank.get(rank, item) for rank, item in enumerate(settled)
            )
            if (
                _landing_trunk_flank_conflict(candidate, graph, curve_radius)
                is not None
            ):
                continue
            for candidate_rank, moved in moved_by_rank.items():
                settled[candidate_rank] = moved
            resident = [
                *fixed_channels,
                *(
                    channel
                    for prior_plan in settled[:current_rank]
                    for channel in _plan_gap_channels(prior_plan, graph, lookup)
                ),
            ]
            channels = _plan_gap_channels(settled[current_rank], graph, lookup)

        for channel in tuple(item for item in channels if item.flank_rank is None):
            if any(
                member_id in channel.claimant_member_ids
                and abs(channel.coordinate - coordinate) <= COORD_TOLERANCE
                for member_id, coordinate in fixed_exit_channels
            ):
                continue
            obstacles = tuple(
                obstacle
                for obstacle in resident
                if not channel.line_ids & obstacle.line_ids
                and _gap_channels_crowd(channel, obstacle)
            )
            if not obstacles:
                continue
            gap_left, gap_right = column_gap_edges(
                graph, channel.gap[0], channel.gap[0] + 1, row=channel.gap[1]
            )
            candidates = sorted(
                {
                    obstacle.coordinate
                    + sign
                    * (
                        BUNDLE_TO_BUNDLE_CLEARANCE
                        if channel.down is not obstacle.down
                        else OFFSET_STEP
                    )
                    for obstacle in obstacles
                    for sign in (-1.0, 1.0)
                },
                key=lambda candidate: (
                    abs(candidate - channel.coordinate),
                    candidate,
                ),
            )
            for coordinate in candidates:
                if not (
                    gap_left + EDGE_TO_BUNDLE_CLEARANCE - COORD_TOLERANCE
                    <= coordinate
                    <= gap_right - EDGE_TO_BUNDLE_CLEARANCE + COORD_TOLERANCE
                ):
                    continue
                moved_plan = _move_landing_opening(
                    settled[current_rank],
                    channel.claimant_member_ids,
                    coordinate,
                    curve_radius,
                )
                if moved_plan is None:
                    continue
                moved_channel = next(
                    (
                        item
                        for item in _plan_gap_channels(moved_plan, graph, lookup)
                        if item.claimant_member_ids == channel.claimant_member_ids
                        and item.gap == channel.gap
                    ),
                    None,
                )
                if moved_channel is None or any(
                    _gap_channels_crowd(moved_channel, obstacle)
                    for obstacle in resident
                ):
                    continue
                candidate = tuple(
                    moved_plan if rank == current_rank else item
                    for rank, item in enumerate(settled)
                )
                if (
                    _landing_trunk_flank_conflict(candidate, graph, curve_radius)
                    is not None
                ):
                    continue
                settled[current_rank] = moved_plan
                channels = _plan_gap_channels(moved_plan, graph, lookup)
                break
    by_id = {plan.id: plan for plan in settled}
    return tuple(by_id[plan.id] for plan in plans)


def _settle_same_line_gap_flanks(
    plans: tuple[ConvergencePlan, ...],
    graph: MetroGraph,
    fixed_channels: tuple[_PlanGapChannel, ...],
    curve_radius: float,
) -> tuple[ConvergencePlan, ...]:
    """Fuse overlapping same-line flanks onto one planned channel.

    Stored traversal direction is irrelevant to semantic coincidence: two
    segments of one line can traverse their shared stroke in opposite graph
    directions and can retain one axis when that axis is a shared physical
    stroke. A target flank crossing an upstream landing is a separate run, so a
    fusion that creates such a collision is rejected.
    """
    lookup = gap_lookup_geometry(graph)
    settled = list(plans)
    resident = list(fixed_channels)
    for plan_rank, plan in enumerate(settled):
        channels = _plan_gap_channels(plan, graph, lookup)
        for flank_rank in (1, 3):
            seated = tuple(
                channel for channel in channels if channel.flank_rank == flank_rank
            )
            if not seated:
                continue
            obstacles = (
                *resident,
                *(channel for channel in channels if channel.flank_rank != flank_rank),
            )
            coordinates = {
                obstacle.coordinate
                for obstacle in obstacles
                for channel in seated
                if obstacle.line_ids & channel.line_ids
                if _channels_share_source_carrier(channel, obstacle)
                if channel.gap == obstacle.gap
                and spans_share_corridor(
                    channel.y_lo, channel.y_hi, obstacle.y_lo, obstacle.y_hi
                )
            }
            if len(coordinates) != 1:
                continue
            coordinate = next(iter(coordinates))
            if all(
                abs(channel.coordinate - coordinate) <= COORD_TOLERANCE
                for channel in seated
            ):
                continue
            moved = _move_trunk_flank(settled[plan_rank], flank_rank, coordinate)
            candidate = tuple(
                moved if rank == plan_rank else item
                for rank, item in enumerate(settled)
            )
            if (
                _landing_trunk_flank_conflict(candidate, graph, curve_radius)
                is not None
            ):
                continue
            settled[plan_rank] = moved
            channels = _plan_gap_channels(settled[plan_rank], graph, lookup)
        resident.extend(channels)
    return tuple(settled)


def _channels_share_source_carrier(
    first: _PlanGapChannel,
    second: _PlanGapChannel,
) -> bool:
    """Whether same-line channels are arms of one semantic source stroke."""
    if first.system_id != second.system_id:
        return False
    if first.claimant_member_ids & second.claimant_member_ids:
        return True
    if (
        first.source_junction_ids & second.source_junction_ids
        or first.connector_ids == second.connector_ids
    ):
        return True
    if first.member_geometry_owned == second.member_geometry_owned:
        return False
    member, convergence = (
        (first, second) if first.member_geometry_owned else (second, first)
    )
    return bool(
        (member.source_junction_ids | member.continuation_endpoint_ids)
        & convergence.continuation_endpoint_ids
    )


def _planned_member_gap_channels(
    plans: tuple[ConvergencePlan, ...],
    member_geometry: MemberGeometryExecution,
) -> tuple[_PlanGapChannel, ...]:
    owned_edges = frozenset(
        edge for plan in plans for edge in plan.resolved_member_edges
    )
    return tuple(
        _PlanGapChannel(
            None,
            channel.start[0],
            min(channel.start[1], channel.end[1]),
            max(channel.start[1], channel.end[1]),
            channel.direction is Direction.D,
            (channel.gap_lo_col, channel.row),
            frozenset({plan.edge.line_id}),
            frozenset({plan.member_id}),
            frozenset({plan.edge.source}),
            frozenset(plan.connector_ids),
            plan.system_id,
            frozenset({plan.edge.target}),
            True,
        )
        for plan in member_geometry.plans
        if plan.edge not in owned_edges
        for channel in plan.gap_channels
    )


_Segment: TypeAlias = tuple[tuple[float, float], tuple[float, float]]


def _conflict(
    kind: ConvergenceConflictKind,
    first: _Segment,
    second: _Segment,
    line_ids: Iterable[str],
) -> ConvergenceConflict:
    """Record where two runs the planner could not reconcile actually sit."""
    axis = lateral_axis(_direction(*first))
    index = axis.point_index
    return ConvergenceConflict(
        kind,
        axis,
        (first, second),
        abs(first[0][index] - second[0][index]),
        _ordered_unique(line_ids),
    )


def _landing_trunk_flank_conflict(
    plans: tuple[ConvergencePlan, ...], graph: MetroGraph, curve_radius: float
) -> ConvergenceConflict | None:
    """A landing leg and a trunk flank of one system crowding a single column.

    Two sibling arms off one fork on one column are a single stroke, which is what
    the planner settles a forked flank onto. Complete convergences over the same
    source set can likewise share an exact source stroke. Chained convergence
    groups in one route system may also describe the same source carrier.
    Other coincident legs are two runs in one place, which is a collision.
    """
    return next(
        (
            _conflict(
                ConvergenceConflictKind.NO_APPROACH_SETTLEMENT_ROOM,
                landing_segment,
                flank,
                (landing.edge.line_id, *trunk_plan.line_ids),
            )
            for landing_plan in plans
            for landing in landing_plan.landings
            if (landing_segment := _landing_cross_segment(landing, graph)) is not None
            for trunk_plan in plans
            if trunk_plan.trunk_axis is not None
            for rank, flank in enumerate(_trunk_segments(trunk_plan.trunk_axis))
            if rank in {1, 3}
            and landing_plan.id != trunk_plan.id
            and landing.edge.line_id in trunk_plan.line_ids
            and _direction(*landing_segment)
            is not _trunk_run_travel_direction(trunk_plan.trunk_axis, rank)
            and _parallel_segments_conflict(landing_segment, flank, curve_radius)
            and not (
                (
                    _forked_flank(landing, trunk_plan, rank)
                    and _segments_coincide(landing_segment, flank)
                )
                or _shared_source_bundle_stroke(
                    landing_plan,
                    landing,
                    trunk_plan,
                    rank,
                    landing_segment,
                    flank,
                )
                or _shared_chained_source_stroke(
                    landing_plan,
                    landing,
                    trunk_plan,
                    rank,
                    landing_segment,
                    flank,
                )
            )
        ),
        None,
    )


def _segments_coincide(first: _Segment, second: _Segment) -> bool:
    """Whether two parallel runs stand on one coordinate, hence draw one stroke."""
    index = lateral_axis(_direction(*first)).point_index
    return abs(first[0][index] - second[0][index]) <= COORD_TOLERANCE


def _system_conflict(
    plans: tuple[ConvergencePlan, ...],
    ctx: _RoutingCtx,
) -> ConvergenceConflict | None:
    return _landing_trunk_flank_conflict(plans, ctx.graph, ctx.curve_radius)


def _validate_final_convergence_feasibility(
    plans: tuple[ConvergencePlan, ...],
    graph: MetroGraph,
    ctx: _RoutingCtx,
    fixed_channels: tuple[_PlanGapChannel, ...],
) -> None:
    """Reject unresolved geometry after every movable decision is frozen."""
    plans_by_system: dict[RouteSystemId, list[ConvergencePlan]] = defaultdict(list)
    for plan in plans:
        plans_by_system[plan.system_id].append(plan)
    for system_id, system_plans in plans_by_system.items():
        conflict = _system_conflict(tuple(system_plans), ctx)
        if conflict is not None:
            raise FinalConvergenceFeasibilityError(
                f"final convergence system {system_id} has unresolved "
                f"{conflict.kind.name.lower().replace('_', '-')} geometry"
            )

    lookup = gap_lookup_geometry(graph)
    plan_channels = tuple(
        (plan, channel)
        for plan in plans
        for channel in _plan_gap_channels(plan, graph, lookup)
    )
    for plan, channel in plan_channels:
        for member_channel in fixed_channels:
            if channel.gap != member_channel.gap or not spans_share_corridor(
                channel.y_lo,
                channel.y_hi,
                member_channel.y_lo,
                member_channel.y_hi,
            ):
                continue
            shared_lines = channel.line_ids & member_channel.line_ids
            if shared_lines:
                separation = abs(channel.coordinate - member_channel.coordinate)
                shared_carrier = _channels_share_source_carrier(channel, member_channel)
                if shared_carrier and separation > COORD_TOLERANCE:
                    joined = ", ".join(sorted(shared_lines))
                    raise FinalConvergenceFeasibilityError(
                        f"final convergence plan {plan.id} has ambiguous "
                        f"same-line member channel for {joined} in gap "
                        f"{channel.gap}"
                    )
                if (
                    not shared_carrier
                    and channel.down is not member_channel.down
                    and separation
                    < cotravelling_lane_clearance(
                        same_line=True,
                        counter_running=True,
                        curve_radius=ctx.curve_radius,
                    )
                    - COORD_TOLERANCE
                ):
                    joined = ", ".join(sorted(shared_lines))
                    raise FinalConvergenceFeasibilityError(
                        f"final convergence plan {plan.id} crowds an opposing "
                        f"same-line member channel for {joined} in gap "
                        f"{channel.gap}"
                    )
                continue
            if _gap_channels_crowd(
                channel, member_channel
            ) and not _member_represented_convergence_lane(
                channel,
                member_channel,
                fixed_channels,
            ):
                raise FinalConvergenceFeasibilityError(
                    f"final convergence plan {plan.id} crowds a planned "
                    f"member channel in gap {channel.gap}"
                )


def _resources(
    graph: MetroGraph,
    plans: tuple[ConvergencePlan, ...],
) -> tuple[tuple[SharedReference, ...], tuple[SymbolicDemand, ...]]:
    assert graph.route_topology is not None
    provenance = _plan_provenance(graph, graph.route_topology.connectors)
    references: list[SharedReference] = []
    demands: list[SymbolicDemand] = []
    for plan in plans:
        if not plan.owns_geometry:
            continue
        assert plan.trunk_axis is not None
        span = _plan_span(graph, plan)
        decision_refs = reservation_decision_refs(provenance, plan.connector_ids, span)
        for reference_id, kind in zip(
            plan.shared_reference_ids,
            (SharedReferenceKind.TRUNK, SharedReferenceKind.LANDING_SEQUENCE),
            strict=True,
        ):
            references.append(
                SharedReference(
                    reference_id,
                    plan.system_id,
                    kind,
                    plan.member_ids,
                    CoordinateRegime.LAYOUT_CANVAS,
                    decision_refs,
                )
            )
        demands.extend(
            (
                SymbolicDemand(
                    plan.demand_ids[0],
                    plan.system_id,
                    plan.member_ids,
                    DemandKind.LANES,
                    DemandAxis.Y
                    if plan.trunk_axis.axis is DemandAxis.X
                    else DemandAxis.X,
                    span,
                    len(plan.lane_order),
                    None,
                    None,
                    (plan.shared_reference_ids[0],),
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    decision_refs,
                ),
                SymbolicDemand(
                    plan.demand_ids[1],
                    plan.system_id,
                    tuple(item.member_id for item in plan.landings),
                    DemandKind.RUNWAY,
                    plan.trunk_axis.axis,
                    span,
                    len(plan.landings),
                    max(item.minimum_runway for item in plan.landings),
                    CoordinateRegime.LAYOUT_CANVAS,
                    plan.shared_reference_ids,
                    (KeepOutClass.SECTION, KeepOutClass.MARKER),
                    decision_refs,
                ),
            )
        )
    return tuple(references), tuple(demands)


def _fixed_x_channel_claims(
    plans: tuple[ConvergencePlan, ...],
    edge_order: tuple[ResolvedEdge, ...],
) -> tuple[PlannedConvergenceVerticalChannel, ...]:
    edge_ranks = {edge: rank for rank, edge in enumerate(edge_order)}
    claims: list[PlannedConvergenceVerticalChannel] = []
    for plan in plans:
        if not plan.owns_geometry:
            continue
        assert plan.trunk_axis is not None
        primary = next(
            item
            for item in plan.endpoint_ownership
            if item.member_id == plan.primary_trunk_member_id
        )
        segments = [
            (primary.edge, segment) for segment in _trunk_segments(plan.trunk_axis)
        ]
        segments.extend(
            (landing.edge, landing.opening_turn_segment)
            for landing in plan.landings
            if landing.opening_turn_segment is not None
        )
        segments.extend(
            (continuation.edge, (continuation.start_point, continuation.end_point))
            for continuation in plan.outgoing_continuations
        )
        for segment_rank, (owner_edge, segment) in enumerate(segments):
            start, end = segment
            if (
                owner_edge not in edge_ranks
                or abs(start[0] - end[0]) > COORD_TOLERANCE
                or abs(start[1] - end[1]) <= COORD_TOLERANCE
            ):
                continue
            claims.append(
                PlannedConvergenceVerticalChannel(
                    plan.system_id,
                    owner_edge,
                    owner_edge.source,
                    owner_edge.line_id,
                    edge_ranks[owner_edge],
                    segment_rank,
                    start[0],
                    min(start[1], end[1]),
                    max(start[1], end[1]),
                )
            )
    return tuple(
        sorted(
            claims,
            key=lambda claim: (
                claim.canonical_edge_rank,
                claim.segment_rank,
                claim.owner_edge.source,
                claim.owner_edge.target,
                claim.owner_edge.line_id,
            ),
        )
    )


def _query(
    plans: tuple[ConvergencePlan, ...],
    edge_order: tuple[ResolvedEdge, ...],
) -> ConvergencePlanExecutionQuery:
    by_edge: dict[ResolvedEdge, ConvergenceRouteMembership] = {}
    for plan in plans:
        if not plan.owns_geometry:
            continue
        landings = {item.member_id: item for item in plan.landings}
        continuations = {item.member_id: item for item in plan.outgoing_continuations}
        ownership_by_member = {item.member_id: item for item in plan.endpoint_ownership}
        for ownership in plan.endpoint_ownership:
            covering_ownership = (
                ownership_by_member[ownership.covered_by_member_id]
                if ownership.covered_by_member_id is not None
                else None
            )
            membership = ConvergenceRouteMembership(
                plan,
                ownership.member_id,
                landings.get(ownership.member_id),
                continuations.get(ownership.member_id),
                ownership,
                covering_ownership.edge if covering_ownership is not None else None,
            )
            if ownership.edge in by_edge:
                raise ValueError("planned convergence edge has more than one owner")
            by_edge[ownership.edge] = membership
    return ConvergencePlanExecutionQuery(
        plans,
        MappingProxyType(by_edge),
        edge_order,
        _fixed_x_channel_claims(plans, edge_order),
    )


def build_convergence_plan_execution(
    graph: MetroGraph,
    ctx: _RoutingCtx,
    scaffold: RouteSemanticScaffold,
    *,
    exit_turn_plans: tuple[ExitTurnPlan, ...],
    fan_plans: tuple[FanPlan, ...],
    member_geometry: MemberGeometryExecution,
    include_resources: bool = True,
) -> ConvergencePlanExecution:
    """Plan every semantic convergence atomically by route system."""
    member_geometry = member_geometry or empty_member_geometry_execution()
    views_by_system: dict[RouteSystemId, list[ResolvedConvergenceView]] = defaultdict(
        list
    )
    for view in scaffold.query.convergences:
        views_by_system[scaffold.system_for(view.group.connector_ids)].append(view)
    exit_turn_plans_by_system: dict[RouteSystemId, list[ExitTurnPlan]] = defaultdict(
        list
    )
    for exit_turn_plan in exit_turn_plans:
        exit_turn_plans_by_system[exit_turn_plan.system_id].append(exit_turn_plan)
    fan_plans_by_system: dict[RouteSystemId, list[FanPlan]] = defaultdict(list)
    for fan_plan in fan_plans:
        if fan_plan.system_id is not None:
            fan_plans_by_system[fan_plan.system_id].append(fan_plan)
    plans: list[ConvergencePlan] = []
    diagnostics: list[RoutePlanDiagnostic] = []
    for system_id in scaffold.ordered_system_ids:
        views = views_by_system.get(system_id, [])
        if not views:
            continue
        connector_ids = set(
            connector_id for view in views for connector_id in view.group.connector_ids
        )
        memberships = tuple(_plan_membership(scaffold, view) for view in views)
        member_ids = {
            member_id
            for _paths, _edges, members in memberships
            for member_id in members
        }
        upstream_exit_plans = tuple(
            item
            for item in exit_turn_plans_by_system.get(system_id, ())
            if set(item.connector_ids) & connector_ids
            or set(item.member_ids) & member_ids
        )
        upstream_exit_ids = tuple(item.id for item in upstream_exit_plans)
        upstream_fan_ids = tuple(
            item.id
            for item in fan_plans_by_system.get(system_id, ())
            if (
                set(item.connector_ids) & connector_ids
                or set(item.member_ids) & member_ids
            )
        )
        try:
            system_plans = tuple(
                _build_planned_convergence(
                    graph,
                    ctx,
                    scaffold,
                    view,
                    membership,
                    upstream_exit_ids,
                    upstream_fan_ids,
                )
                for view, membership in zip(views, memberships, strict=True)
            )
            system_plans = _settle_shared_trunk_channels(system_plans, ctx.curve_radius)
            system_plans = _settle_shared_opening_pivots(system_plans, graph)
            system_plans = _settle_shared_source_openings(
                system_plans, ctx.curve_radius
            )
            system_plans = _settle_opposing_landing_channels(
                system_plans, graph, upstream_exit_plans, ctx.curve_radius
            )
            system_plans = _settle_landing_trunk_flanks(
                system_plans, graph, ctx.curve_radius
            )
            exit_owned_flanks = _exit_owned_flanks(system_plans, upstream_exit_plans)
            system_plans = _settle_reserved_gap_flanks(system_plans, ctx)
            system_plans = _settle_opposing_gap_flanks(
                system_plans,
                graph,
                ctx.curve_radius,
                exit_owned_flanks,
                _planned_member_gap_channels(
                    system_plans,
                    member_geometry,
                ),
                (
                    _fixed_exit_axis_channels(upstream_exit_plans)
                    if getattr(ctx, "prior_exit_turn_dispositions", None) is not None
                    else frozenset()
                ),
            )
            system_plans = _reconcile_continuation_ownership(system_plans)
            system_plans = _reconcile_landing_handedness(system_plans, graph)
            conflict = _system_conflict(
                system_plans,
                ctx,
            )
            if conflict is not None:
                raise UnsupportedConvergenceError(conflict.kind.reason, conflict)
        except UnsupportedConvergenceError as error:
            reason = str(error) or type(error).__name__
            system_plans = tuple(
                _legacy_plan(scaffold, view, membership, reason, error.conflict)
                for view, membership in zip(views, memberships, strict=True)
            )
            for item in system_plans:
                diagnostics.append(
                    RoutePlanDiagnostic(
                        None,
                        "convergence-plan-legacy",
                        f"convergence system {item.system_id} uses legacy routing: "
                        f"{reason}",
                        blocking=False,
                    )
                )
        plans.extend(system_plans)
    frozen_plans = tuple(plans)
    references, demands = (
        _resources(graph, frozen_plans) if include_resources else ((), ())
    )
    return ConvergencePlanExecution(
        frozen_plans,
        references,
        demands,
        tuple(diagnostics),
        _query(frozen_plans, scaffold.edge_order),
    )


def _settle_convergence_geometry(
    plans: tuple[ConvergencePlan, ...],
    graph: MetroGraph,
    ctx: _RoutingCtx,
    exit_turn_plans: tuple[ExitTurnPlan, ...],
    fixed_channels: tuple[_PlanGapChannel, ...] = (),
) -> tuple[ConvergencePlan, ...]:
    """Apply the shared convergence channel-settlement sequence."""
    settled = _settle_shared_trunk_channels(plans, ctx.curve_radius)
    settled = _settle_shared_opening_pivots(settled, graph)
    settled = _settle_shared_source_openings(settled, ctx.curve_radius)
    settled = _settle_opposing_landing_channels(
        settled, graph, exit_turn_plans, ctx.curve_radius
    )
    settled = _settle_landing_trunk_flanks(settled, graph, ctx.curve_radius)
    settled = _settle_opposing_gap_flanks(
        settled,
        graph,
        ctx.curve_radius,
        _exit_owned_flanks(settled, exit_turn_plans),
        fixed_channels,
        (
            _fixed_exit_axis_channels(exit_turn_plans)
            if getattr(ctx, "prior_exit_turn_dispositions", None) is not None
            else frozenset()
        ),
    )
    settled = _settle_same_line_gap_flanks(
        settled, graph, fixed_channels, ctx.curve_radius
    )
    settled = _reconcile_continuation_ownership(settled)
    return _reconcile_landing_handedness(settled, graph)


def settle_global_convergence_execution(
    execution: ConvergencePlanExecution,
    graph: MetroGraph,
    ctx: _RoutingCtx,
    *,
    exit_turn_plans: tuple[ExitTurnPlan, ...],
    member_geometry: MemberGeometryExecution,
    planned_system_ids: frozenset[RouteSystemId],
    include_resources: bool,
) -> ConvergencePlanExecution:
    """Settle post-member eligible owners before final atomic disposition."""
    eligible = tuple(
        plan
        for plan in execution.plans
        if plan.system_id in planned_system_ids and plan.owns_geometry
    )
    planned_exit_turns = tuple(
        plan for plan in exit_turn_plans if plan.system_id in planned_system_ids
    )
    fixed_channels = _planned_member_gap_channels(eligible, member_geometry)
    settled = _settle_convergence_geometry(
        eligible, graph, ctx, planned_exit_turns, fixed_channels
    )
    _validate_final_convergence_feasibility(settled, graph, ctx, fixed_channels)
    settled_by_id = {plan.id: plan for plan in settled}
    plans = tuple(settled_by_id.get(plan.id, plan) for plan in execution.plans)
    references, demands = _resources(graph, plans) if include_resources else ((), ())
    return ConvergencePlanExecution(
        plans,
        references,
        demands,
        execution.diagnostics,
        _query(plans, execution.query._edge_order),
    )


def settle_preliminary_convergence_execution(
    execution: ConvergencePlanExecution,
    graph: MetroGraph,
    ctx: _RoutingCtx,
    *,
    exit_turn_plans: tuple[ExitTurnPlan, ...],
    planned_system_ids: frozenset[RouteSystemId],
) -> ConvergencePlanExecution:
    """Settle provisional convergence decisions before member allocation."""
    eligible = tuple(
        plan
        for plan in execution.plans
        if plan.system_id in planned_system_ids and plan.owns_geometry
    )
    planned_exit_turns = tuple(
        plan for plan in exit_turn_plans if plan.system_id in planned_system_ids
    )
    settled = _settle_convergence_geometry(eligible, graph, ctx, planned_exit_turns)
    by_id = {plan.id: plan for plan in settled}
    plans = tuple(by_id.get(plan.id, plan) for plan in execution.plans)
    return ConvergencePlanExecution(
        plans,
        (),
        (),
        execution.diagnostics,
        _query(plans, execution.query._edge_order),
    )


def preliminary_member_gap_claims(
    execution: ConvergencePlanExecution,
    graph: MetroGraph,
    planned_system_ids: frozenset[RouteSystemId],
    exit_turn_plans: tuple[ExitTurnPlan, ...] = (),
) -> tuple[PreliminaryGapChannelClaim, ...]:
    """Expose exact convergence legs to the mutable member allocator."""
    lookup = gap_lookup_geometry(graph)
    fixed_channels = _fixed_exit_axis_channels(exit_turn_plans)
    return tuple(
        PreliminaryGapChannelClaim(
            plan.system_id,
            channel.coordinate,
            channel.y_lo,
            channel.y_hi,
            channel.down,
            channel.gap,
            channel.line_ids,
            channel.source_junction_ids,
            channel.connector_ids,
            any(
                member_id in channel.claimant_member_ids
                and abs(channel.coordinate - coordinate) <= COORD_TOLERANCE
                for member_id, coordinate in fixed_channels
            ),
        )
        for plan in execution.plans
        if plan.system_id in planned_system_ids and plan.owns_geometry
        for channel in _plan_gap_channels(plan, graph, lookup)
    )


def restrict_convergence_execution(
    execution: ConvergencePlanExecution,
    graph: MetroGraph,
    planned_system_ids: frozenset[RouteSystemId],
    *,
    compatibility_system_ids: frozenset[RouteSystemId] = frozenset(),
    include_resources: bool,
) -> ConvergencePlanExecution:
    """Publish final planned ownership and non-owning compatibility records."""
    compatibility_reason = "whole route system uses compatibility emission"
    demoted = tuple(
        plan
        for plan in execution.plans
        if plan.system_id in compatibility_system_ids and plan.owns_geometry
    )
    plans = tuple(
        plan
        if plan.system_id in planned_system_ids or not plan.owns_geometry
        else replace(
            plan,
            upstream_exit_turn_plan_ids=(),
            upstream_fan_plan_ids=(),
            primary_trunk_member_id=None,
            primary_trunk_reason=None,
            trunk_axis=None,
            landings=(),
            outgoing_continuations=(),
            lane_order=(),
            endpoint_ownership=(),
            shared_reference_ids=(),
            demand_ids=(),
            foreign_reference_ids=(),
            disposition=ConvergenceDisposition.LEGACY,
            legacy_reason=compatibility_reason,
            conflict=None,
        )
        for plan in execution.plans
        if plan.system_id in planned_system_ids
        or plan.system_id in compatibility_system_ids
    )
    references, demands = _resources(graph, plans) if include_resources else ((), ())
    return ConvergencePlanExecution(
        plans,
        references,
        demands,
        execution.diagnostics
        + tuple(
            RoutePlanDiagnostic(
                None,
                "convergence-plan-legacy",
                f"convergence system {plan.system_id} uses legacy routing: "
                f"{compatibility_reason}",
                blocking=False,
            )
            for plan in demoted
        ),
        _query(plans, execution.query._edge_order),
    )


def convergence_failure(
    membership: ConvergenceRouteMembership,
    emitted_endpoint: tuple[float, float],
) -> str:
    plan = membership.plan
    connectors = ", ".join(str(item) for item in membership.ownership.connector_ids)
    expected = membership.ownership.endpoint
    return (
        f"convergence system {plan.system_id} connectors {connectors} member "
        f"{membership.member_id} planned join {expected} emitted endpoint "
        f"{emitted_endpoint}"
    )


def _point_on_trunk_geometry(
    point: tuple[float, float], axis: ConvergenceTrunkAxis
) -> bool:
    return any(
        point_to_polyline_distance(point, segment) <= COORD_TOLERANCE
        for segment in _trunk_segments(axis)
    )


def _route_covers_segment(
    route: RoutedPath,
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
) -> bool:
    if segment_start == segment_end:
        return (
            point_to_polyline_distance(segment_start, route.points) <= COORD_TOLERANCE
        )
    horizontal = abs(segment_start[1] - segment_end[1]) <= COORD_TOLERANCE
    vertical = abs(segment_start[0] - segment_end[0]) <= COORD_TOLERANCE
    if not horizontal and not vertical:
        return False
    coordinate = segment_start[1] if horizontal else segment_start[0]
    extent_start, extent_end = sorted(
        (segment_start[0], segment_end[0])
        if horizontal
        else (segment_start[1], segment_end[1])
    )
    intervals: list[tuple[float, float]] = []
    for start, end in zip(route.points, route.points[1:]):
        if horizontal:
            if (
                abs(start[1] - coordinate) > COORD_TOLERANCE
                or abs(end[1] - coordinate) > COORD_TOLERANCE
            ):
                continue
            interval = (min(start[0], end[0]), max(start[0], end[0]))
        else:
            if (
                abs(start[0] - coordinate) > COORD_TOLERANCE
                or abs(end[0] - coordinate) > COORD_TOLERANCE
            ):
                continue
            interval = (min(start[1], end[1]), max(start[1], end[1]))
        if interval[1] - interval[0] > COORD_TOLERANCE:
            intervals.append(interval)
    covered_until = extent_start
    for interval_start, interval_end in sorted(intervals):
        if interval_end < covered_until - COORD_TOLERANCE:
            continue
        if interval_start > covered_until + COORD_TOLERANCE:
            return False
        covered_until = max(covered_until, interval_end)
        if covered_until >= extent_end - COORD_TOLERANCE:
            return True
    return False


def _trunk_segments(
    axis: ConvergenceTrunkAxis,
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    source_longitudinal, target_longitudinal = (
        (axis.extent_start, axis.extent_end)
        if axis.direction in {Direction.R, Direction.D}
        else (axis.extent_end, axis.extent_start)
    )
    source_endpoint = (
        axis.source_endpoint_coordinate
        if axis.source_endpoint_coordinate is not None
        else source_longitudinal
    )
    target_endpoint = (
        axis.target_endpoint_coordinate
        if axis.target_endpoint_coordinate is not None
        else target_longitudinal
    )
    if axis.axis is DemandAxis.X:
        return (
            (
                (axis.extent_start, axis.coordinate),
                (axis.extent_end, axis.coordinate),
            ),
            (
                (source_longitudinal, axis.coordinate),
                (source_longitudinal, axis.source_flank_coordinate),
            ),
            (
                (source_longitudinal, axis.source_flank_coordinate),
                (source_endpoint, axis.source_flank_coordinate),
            ),
            (
                (target_longitudinal, axis.coordinate),
                (target_longitudinal, axis.target_flank_coordinate),
            ),
            (
                (target_longitudinal, axis.target_flank_coordinate),
                (target_endpoint, axis.target_flank_coordinate),
            ),
        )
    return (
        (
            (axis.coordinate, axis.extent_start),
            (axis.coordinate, axis.extent_end),
        ),
        (
            (axis.coordinate, source_longitudinal),
            (axis.source_flank_coordinate, source_longitudinal),
        ),
        (
            (axis.source_flank_coordinate, source_longitudinal),
            (axis.source_flank_coordinate, source_endpoint),
        ),
        (
            (axis.coordinate, target_longitudinal),
            (axis.target_flank_coordinate, target_longitudinal),
        ),
        (
            (axis.target_flank_coordinate, target_longitudinal),
            (axis.target_flank_coordinate, target_endpoint),
        ),
    )


def _route_covers_trunk(route: RoutedPath, axis: ConvergenceTrunkAxis) -> bool:
    return all(
        _route_covers_segment(route, start, end) for start, end in _trunk_segments(axis)
    )


def _segments_overlap(
    first: tuple[tuple[float, float], tuple[float, float]],
    second: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    (a, b), (c, d) = first, second
    first_horizontal = abs(a[1] - b[1]) <= COORD_TOLERANCE
    second_horizontal = abs(c[1] - d[1]) <= COORD_TOLERANCE
    if first_horizontal != second_horizontal:
        return False
    if first_horizontal:
        return (
            abs(a[1] - c[1]) <= COORD_TOLERANCE
            and min(a[0], b[0]) <= max(c[0], d[0]) + COORD_TOLERANCE
            and min(c[0], d[0]) <= max(a[0], b[0]) + COORD_TOLERANCE
        )
    return (
        abs(a[0] - c[0]) <= COORD_TOLERANCE
        and min(a[1], b[1]) <= max(c[1], d[1]) + COORD_TOLERANCE
        and min(c[1], d[1]) <= max(a[1], b[1]) + COORD_TOLERANCE
    )


def _trunk_segment_ranks(
    route: RoutedPath, axis: ConvergenceTrunkAxis
) -> tuple[int, ...]:
    """The route segments *axis* states the coordinate of.

    A flank the axis collapses onto its own coordinate states a point -- the
    corner the trunk turns at -- rather than a run.  Matched as a run it claims
    every leg passing through that point, and through
    :func:`common.convergence_owns_segment_boundary` the leg before that one as
    well, freezing coordinates the axis never states.  The corner itself stays
    owned by that same boundary rule around the trunk's own run, so nothing the
    axis does state is given up.
    """
    planned = [
        item
        for item in _trunk_segments(axis)
        if abs(item[0][0] - item[1][0]) > COORD_TOLERANCE
        or abs(item[0][1] - item[1][1]) > COORD_TOLERANCE
    ]
    return tuple(
        rank
        for rank, segment in enumerate(zip(route.points, route.points[1:]))
        if any(_segments_overlap(segment, item) for item in planned)
    )


def _emitted_trunk_run_rank(route: RoutedPath, axis: ConvergenceTrunkAxis) -> int:
    """The rank of the emitted run that carries *axis*'s central trunk.

    The run is the one travelling the trunk's own axis whose span overlaps its
    extent, taken nearest to the planned lane: a route can travel that axis
    several times, and the lane a plan names is not always the one its template
    chose, so neither the coordinate nor the span identifies the run alone.
    """
    horizontal = axis.axis is DemandAxis.X
    candidates: list[tuple[float, float, int]] = []
    for rank, (start, end) in enumerate(zip(route.points, route.points[1:])):
        across, along = (1, 0) if horizontal else (0, 1)
        if abs(start[across] - end[across]) > COORD_TOLERANCE:
            continue
        span = sorted((start[along], end[along]))
        overlap = min(span[1], axis.extent_end) - max(span[0], axis.extent_start)
        if overlap <= COORD_TOLERANCE:
            continue
        candidates.append((abs(start[across] - axis.coordinate), -overlap, rank))
    if not candidates:
        raise ConvergenceInvariantError(
            f"planned trunk run {_trunk_segments(axis)[0]} is absent from member "
            f"{route.edge!r}"
        )
    return min(candidates)[2]


def _seat_planned_run(
    route: RoutedPath,
    rank: int,
    planned: tuple[tuple[float, float], tuple[float, float]],
    graph: MetroGraph,
    offset_in: float,
    offset_out: float,
) -> None:
    """Move ``route.points[rank] -> [rank + 1]`` onto the coordinate *planned* names."""
    horizontal = abs(planned[0][1] - planned[1][1]) <= COORD_TOLERANCE
    coordinate = planned[0][1] if horizontal else planned[0][0]
    start, end = route.points[rank : rank + 2]
    if horizontal:
        from nf_metro.layout.routing.normalize import _set_htrunk_y

        _set_htrunk_y(
            route,
            rank,
            coordinate,
            offset_in=offset_in,
            offset_out=offset_out,
        )
        return
    from nf_metro.layout.routing.normalize import (
        _reconcile_moved_gap_slot,
        _set_vchannel_x,
        _VChannel,
    )

    channel = _VChannel(
        route=route,
        idx=rank,
        x=start[0],
        y_lo=min(start[1], end[1]),
        y_hi=max(start[1], end[1]),
        down=end[1] > start[1],
    )
    _reconcile_moved_gap_slot(channel, coordinate, graph)
    _set_vchannel_x(channel, coordinate, offset_in, offset_out=offset_out)


def _seat_route_on_trunk_flanks(
    route: RoutedPath,
    axis: ConvergenceTrunkAxis,
    graph: MetroGraph,
    lane_offset: float,
) -> None:
    """Seat a trunk member's emitted runs on the trunk geometry its plan owns.

    The trunk is one chain -- lead, flank, central run, flank, lead -- so the
    flanks are the runs either side of the central one.  Naming them by position
    is what lets the lane and the flank columns move together: each is measured
    from the others, so identifying any of them by the coordinates it had before
    the plan seated the rest would find a different run or none at all.

    The central run is seated first and carries no lane displacement, because it
    is the lane.  Each flank is seated whether or not its column moved, since its
    offsets nest its corners inside the bundle the trunk travels in, which the
    template that drew it did not know.
    """
    segments = _trunk_segments(axis)
    source_offset = (
        lane_offset if axis.direction in {Direction.R, Direction.D} else -lane_offset
    )
    rank = _emitted_trunk_run_rank(route, axis)
    across = 1 if axis.axis is DemandAxis.X else 0
    if abs(route.points[rank][across] - axis.coordinate) > COORD_TOLERANCE:
        _seat_planned_run(route, rank, segments[0], graph, 0.0, 0.0)
    for flank_rank, flank, offsets in (
        (rank - 1, segments[1], (source_offset, 0.0)),
        (rank + 1, segments[3], (0.0, -source_offset)),
    ):
        if all(
            abs(actual - expected) <= COORD_TOLERANCE
            for actual, expected in zip(*flank, strict=True)
        ):
            continue
        if not 0 <= flank_rank < len(route.points) - 1:
            raise ConvergenceInvariantError(
                f"planned trunk flank {flank} is absent from member {route.edge!r}"
            )
        _seat_planned_run(route, flank_rank, flank, graph, *offsets)


def _assert_landing_geometry(
    route: RoutedPath,
    plan: ConvergencePlan,
    landing: ConvergenceLanding,
) -> None:
    actual = _landing_approach(route, landing.join_point)
    if actual is None:
        raise ConvergenceInvariantError(
            f"convergence system {plan.system_id} feeder {landing.member_id} "
            "has no emitted approach to its planned join"
        )
    direction, handedness, runway = actual
    if (
        direction is not landing.approach_direction
        or handedness is not landing.corner_handedness
        or runway < landing.minimum_runway - COORD_TOLERANCE
    ):
        raise ConvergenceInvariantError(
            f"convergence system {plan.system_id} feeder {landing.member_id} "
            f"planned {landing.approach_direction.value} approach, "
            f"{landing.corner_handedness} handedness, and "
            f"{landing.minimum_runway:g}px runway but emitted "
            f"{direction.value}, {handedness}, and {runway:g}px"
        )
    if landing.opening_turn_coordinate is not None:
        from nf_metro.layout.routing.normalize import _opening_fanout_descent

        opening = _opening_fanout_descent(route)
        emitted_segment = (
            (route.points[opening.idx], route.points[opening.idx + 1])
            if opening is not None
            else None
        )
        if (
            opening is None
            or abs(opening.x - landing.opening_turn_coordinate) > COORD_TOLERANCE
            or emitted_segment != landing.opening_turn_segment
        ):
            raise ConvergenceInvariantError(
                f"convergence system {plan.system_id} feeder {landing.member_id} "
                f"planned opening {landing.opening_turn_segment} but emitted "
                f"{emitted_segment}"
            )


def consume_convergence_route(route: RoutedPath, ctx: _RoutingCtx) -> None:
    query = ctx.convergences
    if query is None:
        return
    membership = query.membership_for_edge(route.edge)
    if membership is None:
        return
    plan = membership.plan
    if not plan.owns_geometry:
        return
    route.convergence_plan_id = str(plan.id)
    route.convergence_member_id = str(membership.member_id)
    landing = membership.landing
    if landing is None:
        continuation = membership.continuation
        if continuation is not None and continuation.covered_by_member_id is not None:
            return
        if continuation is not None and (
            point_to_polyline_distance(continuation.start_point, route.points)
            > COORD_TOLERANCE
            or any(
                abs(actual - expected) > COORD_TOLERANCE
                for actual, expected in zip(
                    route.points[-1], continuation.end_point, strict=True
                )
            )
        ):
            raise ConvergenceInvariantError(
                f"convergence system {plan.system_id} continuation member "
                f"{membership.member_id} differs from its planned endpoints"
            )
        if plan.primary_trunk_member_id == membership.member_id:
            assert plan.trunk_axis is not None
            route.convergence_owned_segment_ranks = _trunk_segment_ranks(
                route, plan.trunk_axis
            )
        return
    opening_rank: int | None = None
    if landing.opening_turn_coordinate is not None:
        from nf_metro.layout.routing.normalize import (
            _opening_fanout_descent,
            _seat_merge_feeder_opening,
        )

        _seat_merge_feeder_opening(
            route,
            landing.opening_turn_coordinate,
            ctx.graph,
            planned=True,
            # The trunk member's every run and both endpoints are stated by the
            # plan, and the seat below puts each on the coordinate it names.
            carry_tail=plan.primary_trunk_member_id != membership.member_id,
        )
        opening = _opening_fanout_descent(route)
        if opening is None:
            raise ConvergenceInvariantError(
                f"convergence system {plan.system_id} feeder {landing.member_id} "
                "has no emitted opening turn"
            )
        opening_rank = opening.idx
    if plan.primary_trunk_member_id == membership.member_id:
        if plan.primary_trunk_reason is ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH:
            _bake_route(route, ctx)
            _connect_route_endpoint(route, landing.join_point)
        assert plan.trunk_axis is not None
        lane_rank = plan.lane_order.index(route.line_id)
        lane_offset = (len(plan.lane_order) - lane_rank - 1) * ctx.offset_step
        _seat_route_on_trunk_flanks(
            route,
            plan.trunk_axis,
            ctx.graph,
            lane_offset,
        )
        route.convergence_owned_segment_ranks = _ordered_unique(
            _trunk_segment_ranks(route, plan.trunk_axis)
            + (() if opening_rank is None else (opening_rank,))
        )
        if ctx.validate_final_route_frames:
            _assert_landing_geometry(route, plan, landing)
        return
    elif plan.primary_trunk_reason is ConvergenceTrunkReason.LONGEST_BYPASS:
        assert plan.trunk_axis is not None
        run = _run_from_axis(plan.trunk_axis)
        from nf_metro.layout.routing.normalize import _land_feeder_on_run

        key: _EdgeKey = (route.edge.source, route.edge.target, route.line_id)
        if key in ctx.merge.branch_edges:
            _land_feeder_on_run(route, run, ctx)
    _bake_route(route, ctx)
    _connect_route_endpoint(route, landing.join_point)
    route.convergence_owned_segment_ranks = _ordered_unique(
        (len(route.points) - 2,) + (() if opening_rank is None else (opening_rank,))
    )
    endpoint = route.points[-1]
    if any(
        abs(actual - expected) > COORD_TOLERANCE
        for actual, expected in zip(endpoint, landing.join_point, strict=True)
    ):
        raise ConvergenceInvariantError(convergence_failure(membership, endpoint))
    if ctx.validate_final_route_frames:
        _assert_landing_geometry(route, plan, landing)


def validate_convergence_plans(
    routes: list[RoutedPath],
    execution: ConvergencePlanExecution,
) -> None:
    """Require every planned emitted feeder to retain its exact endpoint."""
    by_edge = {
        ResolvedEdge(route.edge.source, route.edge.target, route.line_id): route
        for route in routes
    }
    for plan in execution.plans:
        if not plan.owns_geometry:
            continue
        assert plan.trunk_axis is not None
        primary_ownership = next(
            item
            for item in plan.endpoint_ownership
            if item.member_id == plan.primary_trunk_member_id
        )
        trunk_route = by_edge.get(primary_ownership.edge)
        if trunk_route is None or not _route_covers_trunk(trunk_route, plan.trunk_axis):
            raise ConvergenceInvariantError(
                f"convergence system {plan.system_id} primary trunk member "
                f"{plan.primary_trunk_member_id} does not emit planned "
                f"{plan.trunk_axis.axis.value}-axis {plan.trunk_axis.coordinate} "
                f"over [{plan.trunk_axis.extent_start}, "
                f"{plan.trunk_axis.extent_end}]"
            )
        for landing in plan.landings:
            route = by_edge.get(landing.edge)
            if route is None:
                raise ConvergenceInvariantError(
                    f"convergence system {plan.system_id} lost member "
                    f"{landing.member_id}"
                )
            endpoint = route.points[-1]
            if (
                not _point_on_trunk_geometry(landing.join_point, plan.trunk_axis)
                or point_to_polyline_distance(landing.join_point, trunk_route.points)
                > COORD_TOLERANCE
            ):
                raise ConvergenceInvariantError(
                    f"convergence system {plan.system_id} feeder "
                    f"{landing.member_id} joins outside its planned trunk axis"
                )
            if landing.member_id == plan.primary_trunk_member_id:
                _assert_landing_geometry(route, plan, landing)
                continue
            if any(
                abs(actual - expected) > COORD_TOLERANCE
                for actual, expected in zip(endpoint, landing.join_point, strict=True)
            ):
                membership = execution.query.membership_for_edge(landing.edge)
                assert membership is not None
                raise ConvergenceInvariantError(
                    convergence_failure(membership, endpoint)
                )
            _assert_landing_geometry(route, plan, landing)
        ownership_by_member = {
            ownership.member_id: ownership for ownership in plan.endpoint_ownership
        }
        covered_continuation_members = {
            continuation.member_id
            for continuation in plan.outgoing_continuations
            if continuation.covered_by_member_id is not None
        }
        for continuation in plan.outgoing_continuations:
            membership = execution.query.membership_for_edge(continuation.edge)
            assert membership is not None
            if continuation.covered_by_member_id is not None:
                carrier = ownership_by_member[continuation.covered_by_member_id]
                route = by_edge.get(carrier.edge)
                if (
                    route is None
                    or point_to_polyline_distance(
                        continuation.start_point, route.points
                    )
                    > COORD_TOLERANCE
                    or point_to_polyline_distance(continuation.end_point, route.points)
                    > COORD_TOLERANCE
                ):
                    raise ConvergenceInvariantError(
                        f"convergence system {plan.system_id} covered continuation "
                        f"{continuation.member_id} is absent from its carrier"
                    )
                continue
            route = by_edge.get(continuation.edge)
            if (
                route is None
                or point_to_polyline_distance(continuation.start_point, route.points)
                > COORD_TOLERANCE
                or any(
                    abs(actual - expected) > COORD_TOLERANCE
                    for actual, expected in zip(
                        route.points[-1], continuation.end_point, strict=True
                    )
                )
            ):
                raise ConvergenceInvariantError(
                    f"convergence system {plan.system_id} continuation member "
                    f"{continuation.member_id} differs from its planned endpoints"
                )
        for ownership in plan.endpoint_ownership:
            if (
                ownership.role
                not in {
                    ConvergenceEndpointRole.TRUNK,
                    ConvergenceEndpointRole.CONTINUATION,
                }
                or ownership.member_id in covered_continuation_members
            ):
                continue
            route = by_edge.get(ownership.edge)
            membership = execution.query.membership_for_edge(ownership.edge)
            if route is None or membership is None:
                raise ConvergenceInvariantError(
                    f"convergence system {plan.system_id} lost endpoint owner "
                    f"{ownership.member_id}"
                )
            endpoint = route.points[-1]
            if any(
                abs(actual - expected) > COORD_TOLERANCE
                for actual, expected in zip(endpoint, ownership.endpoint, strict=True)
            ):
                raise ConvergenceInvariantError(
                    convergence_failure(membership, endpoint)
                )
