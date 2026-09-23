"""Owner-typed corridor records and the coordinate-free footprint relation graph.

These types and helpers describe corridor scalar ownership and the witness
evidence a corridor cohort compiles from one route snapshot, independent of
any solver. Nothing in the render or route-system planning pipeline calls
:func:`build_corridor_cohort_ledger` or :func:`compile_corridor_cohort_plan`
yet; both ship ahead of the wiring that will call them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from itertools import permutations
from math import isclose, isfinite
from types import MappingProxyType

from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS, graph_offset_step
from nf_metro.layout.geometry import cotravelling_lane_clearance
from nf_metro.layout.route_plan import (
    BindingKind,
    EmissionBinding,
    EmissionMember,
    RoutePlan,
    RouteSemanticScaffold,
    SettlementStage,
    SettlementStageTrace,
    register_settlement_stage,
)
from nf_metro.layout.route_reservations import (
    CanvasRegion,
    ColumnGapRegion,
    CorridorOrientation,
    CorridorRegion,
    RouteReservation,
    RowGapRegion,
    _reservation_content_id,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    convergence_owns_segment_boundary,
    planner_owns_segment,
    right_normal_axis_sign,
    segment_direction,
)
from nf_metro.layout.routing.corners import concentric_corner_radius_at
from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationFailureReason,
    CorridorAllocationProblem,
    CorridorAllocationResult,
    CorridorAllocationStatus,
    CorridorClearanceShortfall,
    CorridorCoordinateDomain,
    CorridorDirectedSeparation,
    CorridorEquality,
    CorridorFixedEquality,
    CorridorForbiddenInterval,
    CorridorLane,
    CorridorObstacle,
    CorridorSeparation,
    _overlaps,
    solve_corridor_cohorts,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.parser.route_topology import ConnectorId

CorridorCohortSegmentKey = tuple[str, tuple[str, str, str], int]
CorridorFootprintSegmentKey = tuple[str, tuple[str, str, str], int]
_PORT_LEAD_DIRECTION = {
    PortSide.LEFT: Direction.R,
    PortSide.RIGHT: Direction.L,
    PortSide.TOP: Direction.D,
    PortSide.BOTTOM: Direction.U,
}


class CorridorCohortClaimRole(str, Enum):
    MOVABLE = "movable"
    EQUALITY = "equality"
    FIXED = "fixed"


class CorridorScalarOwnerKind(str, Enum):
    MEMBER_CARRIER = "member-carrier"
    CONVERGENCE_TRUNK = "convergence-trunk"


class CorridorCrossingDisposition(str, Enum):
    FIXED_DOGLEG = "fixed-dogleg"
    LEGAL_CROSSING = "legal-crossing"


class CorridorCompatibilityReason(str, Enum):
    """Why a whole corridor component keeps its legacy geometry unmigrated."""

    INCOMPLETE_WITNESSES = "incomplete-witnesses"
    NO_MIGRABLE_GEOMETRY = "no-migrable-geometry"
    UNREPRESENTED_ENDPOINT_COHORT = "unrepresented-endpoint-cohort"


@dataclass(frozen=True, slots=True)
class CorridorScalarVariable:
    variable_id: str
    owner_kind: CorridorScalarOwnerKind
    owner_id: str
    member_id: str
    edge_key: tuple[str, str, str]
    connector_ids: tuple[ConnectorId, ...]
    segment_rank: int
    axis: int
    coordinate: float


@dataclass(frozen=True, slots=True)
class CorridorScalarControlledPoint:
    member_id: str
    edge_key: tuple[str, str, str]
    connector_ids: tuple[ConnectorId, ...]
    point_rank: int
    axis: int
    source_offset: float
    role_id: str


@dataclass(frozen=True, slots=True)
class CorridorScalarFixedPoint:
    member_id: str
    edge_key: tuple[str, str, str]
    connector_ids: tuple[ConnectorId, ...]
    point_rank: int
    axis: int
    coordinate: float
    role_id: str


@dataclass(frozen=True, slots=True)
class CorridorScalarDirectedRunway:
    owner_id: str
    member_id: str
    edge_key: tuple[str, str, str]
    controlled_role_id: str
    axis: int
    direction_sign: int
    minimum_distance: float
    anchor_role_id: str | None = None
    anchor_coordinate: float | None = None

    def __post_init__(self) -> None:
        if (self.anchor_role_id is None) == (self.anchor_coordinate is None):
            raise ValueError("runway anchor must be exactly controlled or fixed")


@dataclass(frozen=True, slots=True)
class CorridorScalarControlRecipe:
    owner_id: str
    source_coordinate: float
    controlled_points: tuple[CorridorScalarControlledPoint, ...]
    fixed_points: tuple[CorridorScalarFixedPoint, ...] = ()
    directed_runways: tuple[CorridorScalarDirectedRunway, ...] = ()


@dataclass(frozen=True, slots=True)
class CorridorScalarRequest:
    variable: CorridorScalarVariable
    preferred_coordinate: float
    domain: CorridorCoordinateDomain
    region: CorridorRegion | None = None
    control_recipe: CorridorScalarControlRecipe | None = None


@dataclass(frozen=True, slots=True)
class CorridorScalarGrant:
    variable_id: str
    owner_kind: CorridorScalarOwnerKind
    owner_id: str
    coordinate: float
    coordinate_delta: float = 0.0
    control_recipe: CorridorScalarControlRecipe | None = None


@dataclass(frozen=True, slots=True)
class CorridorFootprintWitness:
    footprint_id: str
    owner_id: str
    member_id: str
    edge_key: tuple[str, str, str]
    connector_ids: tuple[ConnectorId, ...]
    segment_rank: int
    axis: int
    coordinate: float
    longitudinal_start: float
    longitudinal_end: float
    direction: Direction
    line_id: str
    network_id: str | None
    regions: tuple[CorridorRegion, ...]
    semantic_rank: tuple[int, ...]
    crossing_disposition: CorridorCrossingDisposition
    coordinate_variable_id: str | None = None
    start_variable_id: str | None = None
    end_variable_id: str | None = None
    coordinate_variable_offset: float = 0.0
    start_variable_offset: float = 0.0
    end_variable_offset: float = 0.0


@dataclass(frozen=True, slots=True)
class CorridorCohortObstacleProvenance:
    obstacle_id: str
    member_id: str
    edge_key: tuple[str, str, str]
    segment_rank: int
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class CorridorCohortFailure:
    component_id: str
    result_rank: int
    reason: CorridorAllocationFailureReason
    blocking_member_ids: tuple[str, ...]
    blocking_obstacle_ids: tuple[str, ...]
    blocking_equality_owner_ids: tuple[str, ...]
    blocking_endpoint_owner_ids: tuple[str, ...]
    clearance_shortfall: CorridorClearanceShortfall | None = None
    blocking_obstacles: tuple[CorridorCohortObstacleProvenance, ...] = ()


class CorridorCohortCompilationError(RuntimeError):
    """The semantic ledger cannot be compiled against the current population."""

    def __init__(
        self,
        message: str,
        failures: tuple[CorridorCohortFailure, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failures = failures


@dataclass(frozen=True, slots=True)
class CorridorCohortLedgerClaim:
    claim_id: str
    reservation_id: str
    reservation_rank: int
    claim_rank: int
    region: CorridorRegion
    orientation: CorridorOrientation
    direction: Direction
    lane_rank: int | None
    member_id: str
    member_geometry_plan_id: str | None
    edge_key: tuple[str, str, str] | None
    family_id: RouteFamilyId | None
    connector_ids: tuple[ConnectorId, ...]
    segment_rank: int
    path_rank: int
    endpoint_cohort_id: str | None
    endpoint_network_rank: int | None
    destination_boundary_carrier: bool
    destination_boundary_axis_sign: int | None
    network_id: str | None
    reservation_complete: bool


@dataclass(frozen=True, slots=True)
class CorridorCohortLedger:
    claims: tuple[CorridorCohortLedgerClaim, ...]
    endpoint_members: tuple[tuple[str, frozenset[str]], ...]
    eligible_member_ids: frozenset[str]
    ambiguous_endpoint_cohort_ids: frozenset[str]
    offset_step: float
    curve_radius: float = CURVE_RADIUS
    finalized_owned_segments: frozenset[CorridorCohortSegmentKey] | None = None
    forced_crossings: frozenset[tuple[tuple[str, str, str], tuple[str, str, str]]] = (
        frozenset()
    )
    """``(member edge, perpendicular edge)`` pairs whose crossing the topology
    forces (:func:`_forced_loop_crossings`)."""


def _reaches_along(
    graph: MetroGraph, upstream: str, downstream: str, line_id: str
) -> bool:
    """Whether *line_id*'s authored edges lead from *upstream* to *downstream*."""
    seen = {downstream}
    pending = [downstream]
    while pending:
        for edge in graph.edges_to(pending.pop()):
            if edge.line_id != line_id or edge.source in seen:
                continue
            if edge.source == upstream:
                return True
            seen.add(edge.source)
            pending.append(edge.source)
    return False


def _forced_loop_crossings(
    graph: MetroGraph,
) -> frozenset[tuple[tuple[str, str, str], tuple[str, str, str]]]:
    """Edge pairs two same-line loops into one entry port are forced to cross.

    Lines A and B each reach entry port T from exactly the two sources X and
    Y, with X upstream of Y along both lines, so each line closes a loop
    X..Y -> T beside its own X -> T.  Two loops over the same two endpoints
    cross an even number of times, so A's X -> T crossing B's Y -> T is the
    topology's, not an avoidable seating.
    """
    pairs: set[tuple[tuple[str, str, str], tuple[str, str, str]]] = set()
    for port in graph.ports.values():
        if not port.is_entry:
            continue
        sources_by_line: defaultdict[str, set[str]] = defaultdict(set)
        for edge in graph.edges_to(port.id):
            sources_by_line[edge.line_id].add(edge.source)
        for line_a, sources_a in sources_by_line.items():
            for line_b, sources_b in sources_by_line.items():
                if line_a == line_b or len(sources_a) != 2 or sources_a != sources_b:
                    continue
                for upstream, downstream in permutations(sorted(sources_a)):
                    if _reaches_along(
                        graph, upstream, downstream, line_a
                    ) and _reaches_along(graph, upstream, downstream, line_b):
                        pairs.add(
                            (
                                (upstream, port.id, line_a),
                                (downstream, port.id, line_b),
                            )
                        )
    return frozenset(pairs)


def claims_share_fixed_lane_identity(
    left: CorridorCohortLedgerClaim, right: CorridorCohortLedgerClaim
) -> bool:
    """Report whether two claims identify the same semantic fixed lane.

    Keys only on witness identity a claim already carries -- its network,
    running direction, lane rank, and originating reservation -- never on a
    bound route's realised coordinates.
    """
    return (
        left.network_id is not None
        and left.network_id == right.network_id
        and left.direction is right.direction
        and left.lane_rank is not None
        and left.lane_rank == right.lane_rank
        and left.reservation_id == right.reservation_id
    )


@dataclass(frozen=True, slots=True)
class CorridorCohortTarget:
    member_id: str
    member_geometry_plan_id: str
    edge_key: tuple[str, str, str]
    family_id: RouteFamilyId
    connector_ids: tuple[ConnectorId, ...]
    route: RoutedPath
    mutable: bool
    endpoint_lane_axis: int | None = None
    endpoint_lane_coordinate: float | None = None
    network_id: str | None = None
    legal_crossing_segment_ranks: frozenset[int] = frozenset()


def _claim_is_destination_boundary_carrier(
    graph: MetroGraph,
    target_station_id: str,
    region: CorridorRegion,
) -> bool:
    """Whether *region* is the grid boundary feeding a target entry port."""
    port = graph.ports.get(target_station_id)
    if port is None or not port.is_entry:
        return False
    section = graph.sections.get(port.section_id)
    if section is None or section.grid_col < 0 or section.grid_row < 0:
        return False
    if isinstance(region, CanvasRegion):
        return False
    if port.side is PortSide.LEFT:
        return (
            isinstance(region, ColumnGapRegion)
            and region.right_column == section.grid_col
        )
    if port.side is PortSide.RIGHT:
        return (
            isinstance(region, ColumnGapRegion)
            and region.left_column == section.grid_col + section.grid_col_span - 1
        )
    if port.side is PortSide.TOP:
        return isinstance(region, RowGapRegion) and region.lower_row == section.grid_row
    return (
        isinstance(region, RowGapRegion)
        and region.upper_row == section.grid_row + section.grid_row_span - 1
    )


def _destination_claim_axis_sign(side: PortSide, direction: Direction) -> int:
    return right_normal_axis_sign(direction) // right_normal_axis_sign(
        _PORT_LEAD_DIRECTION[side]
    )


def _network_id(
    connector_ids: tuple[ConnectorId, ...], scaffold: RouteSemanticScaffold
) -> str | None:
    ids = {
        str(scaffold.query.connector(connector_id).network_id)
        for connector_id in connector_ids
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _endpoint_members(
    graph: MetroGraph,
    scaffold: RouteSemanticScaffold,
    prior_plan: RoutePlan,
) -> tuple[dict[str, str], dict[str, frozenset[str]], frozenset[str]]:
    by_target: defaultdict[str, list[EmissionMember]] = defaultdict(list)
    for member in prior_plan.members:
        by_target[member.target.station_id].append(member)
    memberships: defaultdict[str, list[str]] = defaultdict(list)
    expected: dict[str, frozenset[str]] = {}
    for group in scaffold.resolution.endpoint_groups:
        port = graph.ports.get(group.port_id)
        members = by_target.get(group.port_id, ())
        connectors = tuple(
            str(connector_id)
            for member in members
            for connector_id in member.connector_ids
        )
        if (
            port is None
            or not port.is_entry
            or len(group.connector_ids) < 2
            or Counter(connectors) != Counter(str(item) for item in group.connector_ids)
        ):
            continue
        cohort_id = f"endpoint-cohort|{group.id}"
        expected[cohort_id] = frozenset(str(member.id) for member in members)
        for member in members:
            memberships[str(member.id)].append(cohort_id)
    ambiguous = frozenset(
        cohort_id
        for cohort_ids in memberships.values()
        if len(cohort_ids) != 1
        for cohort_id in cohort_ids
    )
    return (
        {
            member_id: cohort_ids[0]
            for member_id, cohort_ids in memberships.items()
            if len(cohort_ids) == 1
        },
        expected,
        ambiguous,
    )


def _lane_rank(reservation: RouteReservation, claim_rank: int) -> int | None:
    matches = tuple(
        rank
        for rank, lane in enumerate(reservation.lanes)
        if claim_rank in lane.claim_indices
    )
    return matches[0] if len(matches) == 1 else None


def _resolved_claimants(
    reservation: RouteReservation,
    bindings_by_member: Mapping[str, list[EmissionBinding]],
) -> tuple[frozenset[str], dict[str, str]] | None:
    content_id = _reservation_content_id(
        reservation.system_id,
        reservation.kind,
        reservation.direction,
        reservation.region,
        reservation.measurement_scope,
        reservation.span,
        reservation.claimant_member_ids,
        reservation.claims,
    )
    if content_id != reservation.id or len(set(reservation.claimant_member_ids)) != len(
        reservation.claimant_member_ids
    ):
        return None
    resolved: set[str] = set()
    resolved_by_claimant: dict[str, str] = {}
    for member_id in reservation.claimant_member_ids:
        candidates = bindings_by_member.get(str(member_id), ())
        if len(candidates) != 1:
            return None
        binding = candidates[0]
        resolved_member_id = str(binding.covering_member_id or binding.member_id)
        resolved.add(resolved_member_id)
        resolved_by_claimant[str(member_id)] = resolved_member_id
    return frozenset(resolved), resolved_by_claimant


def build_corridor_cohort_ledger(
    graph: MetroGraph,
    scaffold: RouteSemanticScaffold,
    prior_plan: RoutePlan,
    *,
    station_offsets: Mapping[tuple[str, str], float],
    curve_radius: float = CURVE_RADIUS,
) -> CorridorCohortLedger:
    """Freeze coordinate-free cohort intent from one prior semantic ledger."""
    del station_offsets
    endpoint_by_member, expected, ambiguous = _endpoint_members(
        graph, scaffold, prior_plan
    )
    members = {str(member.id): member for member in prior_plan.members}
    edges = {
        str(member.id): (
            member.source.station_id,
            member.target.station_id,
            member.line_id,
        )
        for member in prior_plan.members
    }
    member_geometry_plan_ids: defaultdict[
        tuple[str, tuple[str, str, str]], list[str]
    ] = defaultdict(list)
    for plan in prior_plan.member_geometry_plans:
        member_geometry_plan_ids[
            (
                str(plan.member_id),
                (plan.edge.source, plan.edge.target, plan.edge.line_id),
            )
        ].append(str(plan.id))
    geometry_plan_id_by_member = {
        member_id: plan_ids[0]
        for (member_id, edge_key), plan_ids in member_geometry_plan_ids.items()
        if len(plan_ids) == 1 and edges.get(member_id) == edge_key
    }
    bindings_by_member: defaultdict[str, list[EmissionBinding]] = defaultdict(list)
    for binding in prior_plan.bindings:
        bindings_by_member[str(binding.member_id)].append(binding)
    resolved_by_reservation = {
        reservation.id: _resolved_claimants(reservation, bindings_by_member)
        for reservation in prior_plan.reservations
    }

    def emitted_path_rank(member_id: str) -> int | None:
        seen: set[str] = set()
        while member_id not in seen:
            seen.add(member_id)
            candidates = bindings_by_member.get(member_id, ())
            if len(candidates) != 1:
                return None
            binding = candidates[0]
            if binding.kind is BindingKind.EMITTED:
                return binding.path_rank
            if binding.covering_member_id is None:
                return None
            member_id = str(binding.covering_member_id)
        return None

    path_rank_by_member = {
        member_id: path_rank
        for member_id in endpoint_by_member
        if (path_rank := emitted_path_rank(member_id)) is not None
    }

    endpoint_ranks: dict[str, int] = {}
    ambiguous_path_rank_cohorts: set[str] = set()
    by_cohort: defaultdict[str, list[str]] = defaultdict(list)
    for member_id, cohort_id in endpoint_by_member.items():
        by_cohort[cohort_id].append(member_id)
    for cohort_id, member_ids in by_cohort.items():
        # A cohort is one entry port, and a port seats one lane per line, so
        # every member carrying a line lands in that line's one slot whichever
        # of the line's networks it belongs to.
        by_line: defaultdict[str, list[str]] = defaultdict(list)
        for member_id in member_ids:
            by_line[members[member_id].line_id].append(member_id)
        if any(
            any(member_id not in path_rank_by_member for member_id in line_members)
            for line_members in by_line.values()
        ):
            ambiguous_path_rank_cohorts.add(cohort_id)
            continue
        first_path_rank_by_line = {
            line_id: min(path_rank_by_member[member_id] for member_id in line_members)
            for line_id, line_members in by_line.items()
        }
        if len(set(first_path_rank_by_line.values())) != len(first_path_rank_by_line):
            ambiguous_path_rank_cohorts.add(cohort_id)
            continue
        ordered_lines = sorted(
            by_line,
            key=lambda line_id: (first_path_rank_by_line[line_id], line_id),
        )
        for rank, line_id in enumerate(ordered_lines):
            for member_id in by_line[line_id]:
                endpoint_ranks[member_id] = rank
    eligible_members = frozenset(
        member_id
        for resolved in resolved_by_reservation.values()
        if resolved is not None
        for member_id in resolved[0]
    )
    claim_semantics: list[tuple[str, str, object, int, int, str | None, bool]] = []
    for reservation in prior_plan.reservations:
        resolved = resolved_by_reservation[reservation.id]
        resolved_by_claimant = {} if resolved is None else resolved[1]
        for claim_rank, claim in enumerate(reservation.claims):
            member_id = resolved_by_claimant.get(
                str(claim.member_id), str(claim.member_id)
            )
            semantic_member = members.get(member_id)
            target_station_id = (
                None if semantic_member is None else semantic_member.target.station_id
            )
            claim_semantics.append(
                (
                    f"{reservation.id}|claim:{claim_rank}",
                    member_id,
                    claim.path_id,
                    claim.segment_rank,
                    claim.segment_end_rank,
                    target_station_id,
                    target_station_id is not None
                    and _claim_is_destination_boundary_carrier(
                        graph, target_station_id, reservation.region
                    ),
                )
            )
    carrier_claim_ids = {
        claim_id
        for (
            claim_id,
            _member_id,
            _path_id,
            _start,
            _end,
            _target,
            carrier,
        ) in claim_semantics
        if carrier
    }
    destination_claim_ids = set(carrier_claim_ids)
    claims_by_member_path_end: defaultdict[tuple[str, object, int], list[str]] = (
        defaultdict(list)
    )
    for (
        claim_id,
        member_id,
        path_id,
        _start,
        end,
        _target,
        _carrier,
    ) in claim_semantics:
        claims_by_member_path_end[(member_id, path_id, end)].append(claim_id)
    for (
        boundary_id,
        boundary_member_id,
        boundary_path_id,
        boundary_start,
        _boundary_end,
        _boundary_target,
        boundary_carrier,
    ) in claim_semantics:
        if not boundary_carrier:
            continue
        destination_claim_ids.update(
            candidate_id
            for candidate_id in claims_by_member_path_end.get(
                (boundary_member_id, boundary_path_id, boundary_start - 1), ()
            )
            if candidate_id != boundary_id
        )
    claims: list[CorridorCohortLedgerClaim] = []
    for reservation_rank, reservation in enumerate(prior_plan.reservations):
        resolved = resolved_by_reservation[reservation.id]
        resolved_member_ids = frozenset() if resolved is None else resolved[0]
        resolved_by_claimant = {} if resolved is None else resolved[1]
        reservation_member_ids = {
            resolved_by_claimant.get(str(claim.member_id), str(claim.member_id))
            for claim in reservation.claims
        }
        reservation_identity_complete = all(
            member_id in members
            and member_id in edges
            and members[member_id].family_id is not None
            and bool(members[member_id].connector_ids)
            for member_id in reservation_member_ids
        )
        reservation_complete = (
            resolved is not None
            and resolved_member_ids == reservation_member_ids
            and reservation_identity_complete
        )
        for claim_rank, claim in enumerate(reservation.claims):
            claim_id = f"{reservation.id}|claim:{claim_rank}"
            observed_member_id = str(claim.member_id)
            member_id = resolved_by_claimant.get(
                observed_member_id,
                observed_member_id,
            )
            current_member = members.get(member_id)
            destination_claim = claim_id in destination_claim_ids
            claims.append(
                CorridorCohortLedgerClaim(
                    claim_id=claim_id,
                    reservation_id=str(reservation.id),
                    reservation_rank=reservation_rank,
                    claim_rank=claim_rank,
                    region=reservation.region,
                    orientation=reservation.orientation,
                    direction=reservation.direction,
                    lane_rank=_lane_rank(reservation, claim_rank),
                    member_id=member_id,
                    member_geometry_plan_id=geometry_plan_id_by_member.get(member_id),
                    edge_key=edges.get(member_id),
                    family_id=None
                    if current_member is None
                    else current_member.family_id,
                    connector_ids=(
                        () if current_member is None else current_member.connector_ids
                    ),
                    segment_rank=claim.segment_rank,
                    path_rank=claim.path_rank,
                    endpoint_cohort_id=(
                        endpoint_by_member.get(member_id) if destination_claim else None
                    ),
                    endpoint_network_rank=(
                        endpoint_ranks.get(member_id) if destination_claim else None
                    ),
                    destination_boundary_carrier=claim_id in carrier_claim_ids,
                    destination_boundary_axis_sign=(
                        _destination_claim_axis_sign(
                            graph.ports[current_member.target.station_id].side,
                            reservation.direction,
                        )
                        if destination_claim and current_member is not None
                        else None
                    ),
                    network_id=(
                        None
                        if current_member is None
                        else _network_id(current_member.connector_ids, scaffold)
                    ),
                    reservation_complete=(
                        reservation_complete
                        and claim.segment_rank == claim.segment_end_rank
                    ),
                )
            )
    return CorridorCohortLedger(
        claims=tuple(claims),
        endpoint_members=tuple(sorted(expected.items())),
        eligible_member_ids=eligible_members,
        ambiguous_endpoint_cohort_ids=ambiguous
        | frozenset(ambiguous_path_rank_cohorts),
        offset_step=graph_offset_step(graph),
        curve_radius=curve_radius,
        finalized_owned_segments=None,
        forced_crossings=_forced_loop_crossings(graph),
    )


def _resolve_explicit_control(
    key: tuple[str, tuple[str, str, str], int, int],
    controlled_points: Mapping[
        tuple[str, tuple[str, str, str], int, int], tuple[str, float]
    ],
    variables_by_id: Mapping[str, CorridorScalarVariable],
) -> tuple[CorridorScalarVariable, float] | None:
    """The variable and offset explicitly controlling one route point, if any."""
    control = controlled_points.get(key)
    if control is None:
        return None
    variable_id, offset = control
    return variables_by_id[variable_id], offset


def build_corridor_footprint_witnesses(
    targets: Sequence[CorridorCohortTarget],
    variables: Sequence[CorridorScalarVariable] = (),
    regions_by_segment: Mapping[CorridorFootprintSegmentKey, tuple[CorridorRegion, ...]]
    | None = None,
    controlled_points: Mapping[
        tuple[str, tuple[str, str, str], int, int], tuple[str, float]
    ]
    | None = None,
) -> tuple[CorridorFootprintWitness, ...]:
    """Describe current orthogonal route footprints without choosing geometry."""
    target_keys = [(target.member_id, target.edge_key) for target in targets]
    if len(target_keys) != len(set(target_keys)):
        raise CorridorCohortCompilationError(
            "corridor footprint population has ambiguous target identities"
        )
    variables_by_segment: dict[CorridorFootprintSegmentKey, CorridorScalarVariable] = {}
    for variable in variables:
        key = variable.member_id, variable.edge_key, variable.segment_rank
        if key in variables_by_segment:
            raise CorridorCohortCompilationError(
                f"corridor footprint segment {key} has multiple scalar owners"
            )
        if variable.axis not in (0, 1) or not isfinite(variable.coordinate):
            raise CorridorCohortCompilationError(
                f"corridor scalar variable {variable.variable_id} has no finite axis"
            )
        variables_by_segment[key] = variable

    variables_by_id = {item.variable_id: item for item in variables}
    target_rank = {key: rank for rank, key in enumerate(sorted(target_keys))}
    regions_by_segment = regions_by_segment or {}
    controlled_points = controlled_points or {}
    witnesses: list[CorridorFootprintWitness] = []
    for target in targets:
        points = target.route.points
        for segment_rank, (start, end) in enumerate(zip(points, points[1:])):
            direction = segment_direction(start, end)
            if direction is None:
                continue
            axis = 1 if direction in (Direction.R, Direction.L) else 0
            key = target.member_id, target.edge_key, segment_rank
            coordinate_variable = variables_by_segment.get(key)
            coordinate_variable_offset = 0.0
            start_variable = variables_by_segment.get(
                (target.member_id, target.edge_key, segment_rank - 1)
            )
            start_variable_offset = 0.0
            end_variable = variables_by_segment.get(
                (target.member_id, target.edge_key, segment_rank + 1)
            )
            end_variable_offset = 0.0
            explicit_coordinate_controls = tuple(
                controlled_points.get(
                    (target.member_id, target.edge_key, point_rank, axis)
                )
                for point_rank in (segment_rank, segment_rank + 1)
            )
            if any(item is not None for item in explicit_coordinate_controls):
                if (
                    any(item is None for item in explicit_coordinate_controls)
                    or explicit_coordinate_controls[0]
                    != explicit_coordinate_controls[1]
                ):
                    raise CorridorCohortCompilationError(
                        f"corridor footprint segment {key} has partial affine "
                        "coordinate control"
                    )
                coordinate_control = explicit_coordinate_controls[0]
                if coordinate_control is None:
                    raise CorridorCohortCompilationError(
                        f"corridor footprint segment {key} has no affine "
                        "coordinate control"
                    )
                variable_id, coordinate_variable_offset = coordinate_control
                coordinate_variable = variables_by_id[variable_id]
            explicit_start = _resolve_explicit_control(
                (target.member_id, target.edge_key, segment_rank, 1 - axis),
                controlled_points,
                variables_by_id,
            )
            if explicit_start is not None:
                start_variable, start_variable_offset = explicit_start
            explicit_end = _resolve_explicit_control(
                (target.member_id, target.edge_key, segment_rank + 1, 1 - axis),
                controlled_points,
                variables_by_id,
            )
            if explicit_end is not None:
                end_variable, end_variable_offset = explicit_end
            for endpoint_variable in (start_variable, end_variable):
                if endpoint_variable is not None and endpoint_variable.axis != 1 - axis:
                    raise CorridorCohortCompilationError(
                        f"corridor footprint segment {key} has a non-perpendicular "
                        "endpoint controller"
                    )
            if coordinate_variable is not None and coordinate_variable.axis != axis:
                raise CorridorCohortCompilationError(
                    f"corridor footprint segment {key} disagrees with its scalar axis"
                )
            witnesses.append(
                CorridorFootprintWitness(
                    footprint_id=(
                        f"corridor-footprint|{target.member_id}|{target.edge_key}|"
                        f"segment:{segment_rank}"
                    ),
                    owner_id=target.member_geometry_plan_id,
                    member_id=target.member_id,
                    edge_key=target.edge_key,
                    connector_ids=target.connector_ids,
                    segment_rank=segment_rank,
                    axis=axis,
                    coordinate=start[axis],
                    longitudinal_start=min(start[1 - axis], end[1 - axis]),
                    longitudinal_end=max(start[1 - axis], end[1 - axis]),
                    direction=direction,
                    line_id=target.route.line_id,
                    network_id=target.network_id,
                    regions=tuple(regions_by_segment.get(key, ())),
                    semantic_rank=(
                        target_rank[(target.member_id, target.edge_key)],
                        segment_rank,
                    ),
                    crossing_disposition=(
                        CorridorCrossingDisposition.LEGAL_CROSSING
                        if segment_rank in target.legal_crossing_segment_ranks
                        else CorridorCrossingDisposition.FIXED_DOGLEG
                    ),
                    coordinate_variable_id=(
                        None
                        if coordinate_variable is None
                        else coordinate_variable.variable_id
                    ),
                    start_variable_id=(
                        None if start_variable is None else start_variable.variable_id
                    ),
                    end_variable_id=(
                        None if end_variable is None else end_variable.variable_id
                    ),
                    coordinate_variable_offset=coordinate_variable_offset,
                    start_variable_offset=start_variable_offset,
                    end_variable_offset=end_variable_offset,
                )
            )
    return tuple(
        sorted(witnesses, key=lambda item: (item.semantic_rank, item.footprint_id))
    )


@dataclass(frozen=True, slots=True)
class CorridorCohortAllocation:
    claim_id: str
    member_id: str
    member_geometry_plan_id: str
    edge_key: tuple[str, str, str]
    family_id: RouteFamilyId
    connector_ids: tuple[ConnectorId, ...]
    segment_rank: int
    axis: int
    longitudinal_start: float
    longitudinal_end: float
    coordinate: float


@dataclass(frozen=True, slots=True)
class CorridorCohortLanding:
    member_id: str
    member_geometry_plan_id: str
    edge_key: tuple[str, str, str]
    connector_ids: tuple[ConnectorId, ...]
    segment_rank: int
    axis: int
    coordinate: float


@dataclass(frozen=True, slots=True)
class CorridorCohortCompatibility:
    """Typed provenance for a whole unmigrated corridor group.

    Names every member, scalar variable and endpoint cohort a component leaves
    under its legacy geometry, so no compatibility disposition can stand without
    saying which group it covers and why.
    """

    reason: CorridorCompatibilityReason
    member_ids: tuple[str, ...]
    scalar_variable_ids: tuple[str, ...]
    endpoint_cohort_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorridorCohortComponentPlan:
    component_id: str
    endpoint_cohort_ids: tuple[str, ...]
    claim_roles: tuple[tuple[str, CorridorCohortClaimRole], ...]
    problems: tuple[CorridorAllocationProblem, ...]
    results: tuple[CorridorAllocationResult, ...]
    status: CorridorAllocationStatus
    allocations: tuple[CorridorCohortAllocation, ...] = ()
    protected_segments: tuple[tuple[str, int], ...] = ()
    compatibility: CorridorCohortCompatibility | None = None


@dataclass(slots=True)
class _RoutePatch:
    route: RoutedPath
    points: list[tuple[float, float]]
    radii: list[float] | None
    owned: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CorridorCohortPlan:
    components: tuple[CorridorCohortComponentPlan, ...]
    allocations: tuple[CorridorCohortAllocation, ...]
    protected_segments: tuple[tuple[str, int], ...]
    landings: tuple[CorridorCohortLanding, ...] = ()
    scalar_grants: tuple[CorridorScalarGrant, ...] = ()
    route_patches: tuple[_RoutePatch, ...] = ()
    settlement_trace: SettlementStageTrace | None = None

    @property
    def solve_call_count(self) -> int:
        """How many solver problems the compile ran, one per non-empty component."""
        return sum(len(component.problems) for component in self.components)


@dataclass(frozen=True, slots=True)
class _BoundClaim:
    ledger: CorridorCohortLedgerClaim
    target: CorridorCohortTarget
    longitudinal_start: float
    longitudinal_end: float
    coordinate: float
    landing_coordinate: float | None

    @property
    def claim_id(self) -> str:
        return self.ledger.claim_id

    @property
    def axis(self) -> int:
        return int(self.ledger.orientation is CorridorOrientation.HORIZONTAL)


@dataclass(frozen=True, slots=True)
class _FootprintTerm:
    variable_id: str | None
    coordinate: float | None
    witness_id: str

    def __post_init__(self) -> None:
        if (self.variable_id is None) == (self.coordinate is None):
            raise ValueError("footprint term must be exactly variable or fixed")


@dataclass(frozen=True, slots=True)
class _FootprintOrder:
    owner_id: str
    lower: _FootprintTerm
    upper: _FootprintTerm
    distance: float
    participant_variable_ids: tuple[str, ...]
    witness_ids: tuple[str, ...]
    regions: tuple[CorridorRegion, ...]


@dataclass(frozen=True, slots=True)
class _FootprintContact:
    owner_id: str
    participant_variable_ids: tuple[str, ...]
    witness_ids: tuple[str, ...]
    network_id: str
    connector_ids: tuple[ConnectorId, ...]
    regions: tuple[CorridorRegion, ...]


@dataclass(frozen=True, slots=True)
class _MemberFootprintModel:
    variables: tuple[CorridorScalarVariable, ...]
    witnesses: tuple[CorridorFootprintWitness, ...]
    claim_ids_by_variable: Mapping[str, tuple[str, ...]]
    orders: tuple[_FootprintOrder, ...]
    contacts: tuple[_FootprintContact, ...]
    forbidden_intervals: tuple[CorridorForbiddenInterval, ...] = ()


def _bind_claim(
    claim: CorridorCohortLedgerClaim,
    target: CorridorCohortTarget,
    landing_coordinate: float | None,
) -> _BoundClaim:
    """Match one ledger claim against its current route, or reject it.

    Validates identity and current segment orientation, then resolves the
    longitudinal span. ``landing_coordinate`` is the absolute port slot for an
    endpoint claim whose last mutable lead lands into the cohort frame; it is
    ``None`` for a claim that is not such a landing.
    """
    edge_key = (
        target.route.edge.source,
        target.route.edge.target,
        target.route.line_id,
    )
    if (
        claim.edge_key is None
        or claim.family_id is None
        or target.member_id != claim.member_id
        or (
            claim.member_geometry_plan_id is not None
            and target.member_geometry_plan_id != claim.member_geometry_plan_id
        )
        or target.edge_key != claim.edge_key
        or edge_key != claim.edge_key
        or target.family_id is not claim.family_id
        or target.connector_ids != claim.connector_ids
        or not claim.connector_ids
        or claim.segment_rank + 1 >= len(target.route.points)
    ):
        raise CorridorCohortCompilationError(
            f"corridor claim {claim.claim_id} does not match its current route "
            f"(expected plan {claim.member_geometry_plan_id}, observed plan "
            f"{target.member_geometry_plan_id})"
        )
    start, end = target.route.points[claim.segment_rank : claim.segment_rank + 2]
    axis = int(claim.orientation is CorridorOrientation.HORIZONTAL)
    longitudinal_axis = 1 - axis
    direction = segment_direction(start, end)
    if (
        abs(start[axis] - end[axis]) > COORD_TOLERANCE
        or direction is not claim.direction
    ):
        raise CorridorCohortCompilationError(
            f"corridor claim {claim.claim_id} changed segment orientation"
        )
    longitudinal_coordinates = start[longitudinal_axis], end[longitudinal_axis]
    landing_slot: float | None = None
    if (
        claim.endpoint_cohort_id is not None
        and target.mutable
        and claim.segment_rank == len(target.route.points) - 3
    ):
        _landing_rank, landing_axis, landing_slot = _landing_frame(
            (target.member_id, target.edge_key), target, landing_coordinate
        )
        if landing_axis != longitudinal_axis:
            raise CorridorCohortCompilationError(
                f"corridor claim {claim.claim_id} has no perpendicular endpoint lead"
            )
        prospective_end = list(end)
        prospective_end[landing_axis] = landing_slot
        if segment_direction(start, (prospective_end[0], prospective_end[1])) is not (
            claim.direction
        ):
            raise CorridorCohortCompilationError(
                f"corridor claim {claim.claim_id} reverses at its endpoint landing"
            )
        longitudinal_coordinates = (
            start[longitudinal_axis],
            landing_slot,
        )
    longitudinal_start, longitudinal_end = sorted(longitudinal_coordinates)
    if longitudinal_end - longitudinal_start <= COORD_TOLERANCE:
        raise CorridorCohortCompilationError(
            f"corridor claim {claim.claim_id} has no current longitudinal span"
        )
    return _BoundClaim(
        claim,
        target,
        longitudinal_start,
        longitudinal_end,
        start[axis],
        landing_slot,
    )


def _bind_ledger(
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
) -> tuple[_BoundClaim, ...]:
    landing_coordinates = _cohort_landing_coordinates(ledger, targets)
    by_key: defaultdict[
        tuple[str, tuple[str, str, str]], list[CorridorCohortTarget]
    ] = defaultdict(list)
    for target in targets:
        by_key[(target.member_id, target.edge_key)].append(target)
    if any(len(items) != 1 for items in by_key.values()):
        raise CorridorCohortCompilationError(
            "current corridor population has ambiguous route bindings"
        )
    bound: list[_BoundClaim] = []
    for claim in ledger.claims:
        if claim.edge_key is None or claim.family_id is None:
            continue
        matches = by_key.get((claim.member_id, claim.edge_key), ())
        if len(matches) != 1:
            if claim.reservation_complete:
                raise CorridorCohortCompilationError(
                    f"corridor claim {claim.claim_id} has no current route binding"
                )
            continue
        try:
            bound.append(
                _bind_claim(
                    claim,
                    matches[0],
                    landing_coordinates.get((claim.member_id, claim.edge_key)),
                )
            )
        except CorridorCohortCompilationError:
            if claim.reservation_complete:
                raise
    return tuple(bound)


def _directly_movable(
    claim: _BoundClaim,
    finalized_owned_segments: frozenset[CorridorCohortSegmentKey] | None,
) -> bool:
    """Whether normalisation is free to relocate *claim*'s current segment."""
    key = (
        claim.ledger.member_id,
        claim.target.edge_key,
        claim.ledger.segment_rank,
    )
    if finalized_owned_segments is not None:
        return key in finalized_owned_segments
    return (
        claim.target.mutable
        and claim.ledger.reservation_complete
        and not planner_owns_segment(claim.target.route, claim.ledger.segment_rank)
    )


def _validate_control_recipe(
    request: CorridorScalarRequest,
    targets: Sequence[CorridorCohortTarget],
) -> None:
    recipe = request.control_recipe
    if recipe is None:
        return
    if (
        recipe.owner_id != request.variable.owner_id
        or not isfinite(recipe.source_coordinate)
        or not isclose(
            recipe.source_coordinate,
            request.variable.coordinate,
            abs_tol=COORD_TOLERANCE,
        )
    ):
        raise CorridorCohortCompilationError(
            f"corridor scalar request {request.variable.variable_id} has an "
            "invalid control source"
        )
    targets_by_identity = {
        (target.member_id, target.edge_key): target for target in targets
    }
    role_ids: set[str] = set()
    point_keys: set[tuple[str, tuple[str, str, str], int, int]] = set()

    def check_point_identity(
        point: CorridorScalarControlledPoint | CorridorScalarFixedPoint,
        finite_value: float,
        expected_coordinate: float,
        kind: str,
    ) -> None:
        target = targets_by_identity.get((point.member_id, point.edge_key))
        point_key = (point.member_id, point.edge_key, point.point_rank, point.axis)
        if (
            target is None
            or target.connector_ids != point.connector_ids
            or point.axis not in (0, 1)
            or not 0 <= point.point_rank < len(target.route.points)
            or not isfinite(finite_value)
            or not isclose(
                target.route.points[point.point_rank][point.axis],
                expected_coordinate,
                abs_tol=COORD_TOLERANCE,
            )
            or point.role_id in role_ids
            or point_key in point_keys
        ):
            raise CorridorCohortCompilationError(
                f"corridor scalar request {request.variable.variable_id} has an "
                f"invalid {kind}"
            )
        role_ids.add(point.role_id)
        point_keys.add(point_key)

    for point in recipe.controlled_points:
        check_point_identity(
            point,
            point.source_offset,
            recipe.source_coordinate + point.source_offset,
            "controlled point",
        )
    for fixed in recipe.fixed_points:
        check_point_identity(
            fixed,
            fixed.coordinate,
            fixed.coordinate,
            "fixed point",
        )
    for runway in recipe.directed_runways:
        if (
            runway.controlled_role_id not in role_ids
            or (
                runway.anchor_role_id is not None
                and runway.anchor_role_id not in role_ids
            )
            or runway.axis not in (0, 1)
            or runway.direction_sign not in (-1, 1)
            or not isfinite(runway.minimum_distance)
            or runway.minimum_distance < 0
        ):
            raise CorridorCohortCompilationError(
                f"corridor scalar request {request.variable.variable_id} has an "
                "invalid directed runway"
            )


def _witnesses_by_lane(
    witnesses: tuple[CorridorFootprintWitness, ...],
) -> dict[tuple[str, tuple[str, str, str], int], list[CorridorFootprintWitness]]:
    by_lane: defaultdict[
        tuple[str, tuple[str, str, str], int], list[CorridorFootprintWitness]
    ] = defaultdict(list)
    for witness in witnesses:
        by_lane[(witness.member_id, witness.edge_key, witness.axis)].append(witness)
    return by_lane


def _member_footprint_model(
    claims: tuple[_BoundClaim, ...],
    targets: Sequence[CorridorCohortTarget],
    scalar_requests: Sequence[CorridorScalarRequest],
    finalized_owned_segments: frozenset[CorridorCohortSegmentKey] | None,
    offset_step: float,
    curve_radius: float,
    forced_crossings: frozenset[tuple[tuple[str, str, str], tuple[str, str, str]]],
) -> _MemberFootprintModel:
    """Build the witness/relation graph one member population publishes.

    Produces ownership (``orders``), shared-network (``contacts``), and
    perpendicular-crossing (``forbidden_intervals``) relations between
    footprints so a downstream solver can honour them without re-reading
    geometry.
    """
    by_segment: defaultdict[CorridorFootprintSegmentKey, list[_BoundClaim]] = (
        defaultdict(list)
    )
    regions_by_segment: defaultdict[
        CorridorFootprintSegmentKey, set[CorridorRegion]
    ] = defaultdict(set)
    for claim in claims:
        key = claim.ledger.member_id, claim.target.edge_key, claim.ledger.segment_rank
        regions_by_segment[key].add(claim.ledger.region)
        if _directly_movable(claim, finalized_owned_segments):
            by_segment[key].append(claim)

    variables: list[CorridorScalarVariable] = []
    claim_ids_by_variable: dict[str, tuple[str, ...]] = {}
    for key in sorted(by_segment):
        segment_claims = tuple(by_segment[key])
        reference = segment_claims[0]
        if any(
            item.axis != reference.axis
            or not isclose(
                item.coordinate,
                reference.coordinate,
                abs_tol=COORD_TOLERANCE,
            )
            or item.target.member_geometry_plan_id
            != reference.target.member_geometry_plan_id
            for item in segment_claims[1:]
        ):
            raise CorridorCohortCompilationError(
                f"corridor member carrier {key} has conflicting scalar claims"
            )
        variable_id = f"member-carrier|{key[0]}|{key[1]}|segment:{key[2]}"
        variables.append(
            CorridorScalarVariable(
                variable_id,
                CorridorScalarOwnerKind.MEMBER_CARRIER,
                reference.target.member_geometry_plan_id,
                reference.ledger.member_id,
                reference.target.edge_key,
                reference.target.connector_ids,
                reference.ledger.segment_rank,
                reference.axis,
                reference.coordinate,
            )
        )
        claim_ids_by_variable[variable_id] = tuple(
            sorted(item.claim_id for item in segment_claims)
        )

    request_variables = tuple(request.variable for request in scalar_requests)
    variable_ids = [item.variable_id for item in (*variables, *request_variables)]
    if len(variable_ids) != len(set(variable_ids)):
        raise CorridorCohortCompilationError(
            "corridor scalar population has ambiguous variable identities"
        )
    for request in scalar_requests:
        bounds = (
            request.domain.minimum_coordinate,
            request.domain.maximum_coordinate,
        )
        if (
            request.domain.member_id != request.variable.variable_id
            or not isfinite(request.preferred_coordinate)
            or any(bound is not None and not isfinite(bound) for bound in bounds)
        ):
            raise CorridorCohortCompilationError(
                f"corridor scalar request {request.variable.variable_id} has an "
                "invalid preference or domain"
            )
        _validate_control_recipe(request, targets)
        claim_ids_by_variable[request.variable.variable_id] = ()

    variables.extend(request_variables)
    controlled_points = {
        (point.member_id, point.edge_key, point.point_rank, point.axis): (
            request.variable.variable_id,
            point.source_offset,
        )
        for request in scalar_requests
        if request.control_recipe is not None
        for point in request.control_recipe.controlled_points
    }
    witnesses = build_corridor_footprint_witnesses(
        targets,
        variables,
        {
            key: tuple(sorted(regions, key=repr))
            for key, regions in regions_by_segment.items()
        },
        controlled_points,
    )
    variables_by_id = {item.variable_id: item for item in variables}
    claims_by_id = {item.claim_id: item for item in claims}
    endpoint_cohorts_by_variable = {
        variable_id: frozenset(
            claims_by_id[claim_id].ledger.endpoint_cohort_id
            for claim_id in claim_ids
            if claims_by_id[claim_id].ledger.endpoint_cohort_id is not None
        )
        for variable_id, claim_ids in claim_ids_by_variable.items()
    }
    carrier_by_variable = {
        item.coordinate_variable_id: item
        for item in witnesses
        if item.coordinate_variable_id is not None
    }
    orders: dict[str, _FootprintOrder] = {}
    contacts: dict[str, _FootprintContact] = {}
    for lead in witnesses:
        endpoint_variables = tuple(
            item
            for item in (lead.start_variable_id, lead.end_variable_id)
            if item is not None
        )
        if lead.coordinate_variable_id is not None or len(endpoint_variables) != 1:
            continue
        controller_id = endpoint_variables[0]
        controller = variables_by_id[controller_id]
        controller_carrier = carrier_by_variable.get(controller_id)
        if controller_carrier is None:
            raise CorridorCohortCompilationError(
                f"corridor controlled footprint {lead.footprint_id} has no carrier"
            )
        if controller.axis != 1 - lead.axis:
            raise CorridorCohortCompilationError(
                f"corridor controlled footprint {lead.footprint_id} has mixed axes"
            )
        if isclose(
            controller.coordinate,
            lead.longitudinal_start,
            abs_tol=COORD_TOLERANCE,
        ):
            fixed_endpoint = lead.longitudinal_end
        elif isclose(
            controller.coordinate,
            lead.longitudinal_end,
            abs_tol=COORD_TOLERANCE,
        ):
            fixed_endpoint = lead.longitudinal_start
        else:
            raise CorridorCohortCompilationError(
                f"corridor controlled footprint {lead.footprint_id} lost its owner"
            )
        variable_term = _FootprintTerm(
            controller_id, None, controller_carrier.footprint_id
        )
        fixed_term = _FootprintTerm(None, fixed_endpoint, lead.footprint_id)
        if fixed_endpoint > controller.coordinate:
            lower, upper = variable_term, fixed_term
        else:
            lower, upper = fixed_term, variable_term
        endpoint_owner_id = f"member-footprint-endpoint-order|{lead.footprint_id}"
        orders[endpoint_owner_id] = _FootprintOrder(
            endpoint_owner_id,
            lower,
            upper,
            COORD_TOLERANCE,
            (controller_id,),
            tuple(sorted((lead.footprint_id, controller_carrier.footprint_id))),
            tuple(sorted({*lead.regions, *controller_carrier.regions}, key=repr)),
        )
        if endpoint_cohorts_by_variable.get(controller_id):
            continue
        for candidate_id, carrier in carrier_by_variable.items():
            if candidate_id == controller_id:
                continue
            candidate = variables_by_id[candidate_id]
            if (
                candidate.axis != controller.axis
                or not endpoint_cohorts_by_variable.get(candidate_id)
                or candidate.edge_key[2] == lead.line_id
                or candidate.member_id == lead.member_id
                or candidate.owner_kind is not CorridorScalarOwnerKind.MEMBER_CARRIER
                or controller.owner_kind is not CorridorScalarOwnerKind.MEMBER_CARRIER
                or carrier.direction is not controller_carrier.direction
            ):
                continue
            if not (
                carrier.longitudinal_start + COORD_TOLERANCE
                < lead.coordinate
                < carrier.longitudinal_end - COORD_TOLERANCE
            ):
                continue
            lead_lo, lead_hi = sorted((controller.coordinate, fixed_endpoint))
            if not (
                lead_lo - offset_step <= candidate.coordinate <= lead_hi + offset_step
            ):
                continue
            if fixed_endpoint > controller.coordinate:
                lower_id, upper_id = candidate_id, controller_id
            else:
                lower_id, upper_id = controller_id, candidate_id
            owner_id = (
                f"member-footprint-order|{lead.footprint_id}|{carrier.footprint_id}"
            )
            orders[owner_id] = _FootprintOrder(
                owner_id,
                _FootprintTerm(
                    lower_id, None, carrier_by_variable[lower_id].footprint_id
                ),
                _FootprintTerm(
                    upper_id, None, carrier_by_variable[upper_id].footprint_id
                ),
                offset_step,
                tuple(sorted((lower_id, upper_id))),
                tuple(sorted((lead.footprint_id, carrier.footprint_id))),
                tuple(sorted({*lead.regions, *carrier.regions}, key=repr)),
            )
    fixed = tuple(
        witness
        for witness in witnesses
        if witness.coordinate_variable_id is None
        and witness.start_variable_id is None
        and witness.end_variable_id is None
    )
    contact_pairs: set[tuple[str, str]] = set()
    for variable_id, carrier in carrier_by_variable.items():
        variable = variables_by_id[variable_id]
        for witness in fixed:
            if (
                witness.edge_key != variable.edge_key
                or witness.connector_ids != variable.connector_ids
                or witness.segment_rank != variable.segment_rank
                or witness.axis != variable.axis
                or not isclose(
                    witness.coordinate,
                    variable.coordinate,
                    abs_tol=COORD_TOLERANCE,
                )
            ):
                continue
            owner_id = (
                f"member-footprint-contact|{carrier.footprint_id}|"
                f"{witness.footprint_id}"
            )
            contacts[owner_id] = _FootprintContact(
                owner_id,
                (variable_id,),
                tuple(sorted((carrier.footprint_id, witness.footprint_id))),
                carrier.network_id or witness.network_id or "",
                variable.connector_ids,
                tuple(sorted({*carrier.regions, *witness.regions}, key=repr)),
            )
            contact_pairs.add((variable_id, witness.footprint_id))
    fixed_by_lane = _witnesses_by_lane(fixed)
    intervals: dict[tuple[str, str], CorridorForbiddenInterval] = {}
    for variable_id, variable_witness in carrier_by_variable.items():
        variable = variables_by_id[variable_id]
        for perpendicular in fixed:
            if (
                (variable_id, perpendicular.footprint_id) in contact_pairs
                or (variable.edge_key, perpendicular.edge_key) in forced_crossings
                or perpendicular.axis == variable.axis
                or perpendicular.crossing_disposition
                is CorridorCrossingDisposition.LEGAL_CROSSING
                or not (
                    perpendicular.longitudinal_start + COORD_TOLERANCE
                    < variable.coordinate
                    < perpendicular.longitudinal_end - COORD_TOLERANCE
                )
                or not (
                    variable_witness.longitudinal_start + COORD_TOLERANCE
                    < perpendicular.coordinate
                    < variable_witness.longitudinal_end - COORD_TOLERANCE
                )
            ):
                continue
            parallel = next(
                (
                    witness
                    for witness in fixed_by_lane.get(
                        (
                            perpendicular.member_id,
                            perpendicular.edge_key,
                            variable.axis,
                        ),
                        (),
                    )
                    if abs(witness.segment_rank - perpendicular.segment_rank) == 1
                    and _footprints_overlap(witness, variable_witness)
                ),
                None,
            )
            if parallel is None:
                continue
            clearance = cotravelling_lane_clearance(
                same_line=parallel.line_id == variable_witness.line_id,
                counter_running=parallel.direction is not variable_witness.direction,
                curve_radius=curve_radius,
                offset_step=offset_step,
            )
            if clearance <= COORD_TOLERANCE or (
                abs(parallel.coordinate - variable.coordinate)
                > clearance + COORD_TOLERANCE
            ):
                continue
            interval = CorridorForbiddenInterval(
                variable_id,
                perpendicular.footprint_id,
                perpendicular.longitudinal_start - clearance,
                perpendicular.longitudinal_end + clearance,
                perpendicular.semantic_rank,
            )
            intervals[(interval.member_id, interval.obstacle_id)] = interval

    return _MemberFootprintModel(
        tuple(variables),
        witnesses,
        claim_ids_by_variable,
        tuple(orders[key] for key in sorted(orders)),
        tuple(contacts[key] for key in sorted(contacts)),
        tuple(intervals[key] for key in sorted(intervals)),
    )


@dataclass(frozen=True, slots=True)
class _AtomicComponentSpec:
    """One closed corridor component.

    ``physical_ranks`` indexes into the ``physical`` tuple a sibling
    :func:`_physical_components` call returned for the same claim
    population, not into that population's claims directly.
    """

    physical_ranks: tuple[int, ...]
    scalar_variable_ids: tuple[str, ...]


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, item: int) -> int:
        if self.parents[item] != item:
            self.parents[item] = self.find(self.parents[item])
        return self.parents[item]

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parents[max(left, right)] = min(left, right)


def _physical_components(
    claims: tuple[_BoundClaim, ...],
) -> tuple[tuple[int, ...], ...]:
    """Group claims that physically overlap within one shared lane family.

    Two claims can only occupy the same physical space if they run the same
    orientation through the same corridor region, so claims are bucketed by
    ``(region, orientation)`` first and overlap is tested only within a
    bucket, never across one.
    """
    union = _UnionFind(len(claims))
    buckets: defaultdict[tuple[CorridorRegion, CorridorOrientation], list[int]] = (
        defaultdict(list)
    )
    for rank, claim in enumerate(claims):
        buckets[(claim.ledger.region, claim.ledger.orientation)].append(rank)
    for ranks in buckets.values():
        for offset, left in enumerate(ranks):
            for right in ranks[offset + 1 :]:
                if _overlaps(
                    claims[left].longitudinal_start,
                    claims[left].longitudinal_end,
                    claims[right].longitudinal_start,
                    claims[right].longitudinal_end,
                ):
                    union.union(left, right)
    grouped: defaultdict[int, list[int]] = defaultdict(list)
    for rank in range(len(claims)):
        grouped[union.find(rank)].append(rank)
    return tuple(tuple(ranks) for _, ranks in sorted(grouped.items()))


def _validate_atomic_component_orientation(
    component: _AtomicComponentSpec,
    claims: tuple[_BoundClaim, ...],
    physical: tuple[tuple[int, ...], ...],
    variables_by_id: Mapping[str, CorridorScalarVariable],
) -> None:
    """Reject a component whose members do not share one physical lane axis.

    A typed relation can name participants drawn from two different
    ``(region, orientation)`` buckets; nothing before this check stops that
    union, and a component spanning two buckets would leave a downstream
    solver with no single axis to read for part of what it thinks is one
    component.
    """
    lanes = {
        (claims[claim_rank].ledger.region, claims[claim_rank].ledger.orientation)
        for physical_rank in component.physical_ranks
        for claim_rank in physical[physical_rank]
    }
    if len(lanes) > 1:
        raise CorridorCohortCompilationError(
            f"corridor component with physical ranks {component.physical_ranks} "
            "spans more than one (region, orientation) lane"
        )
    axes = {
        variables_by_id[variable_id].axis
        for variable_id in component.scalar_variable_ids
    }
    if component.physical_ranks:
        claim_rank = physical[component.physical_ranks[0]][0]
        axes.add(claims[claim_rank].axis)
    if len(axes) > 1:
        raise CorridorCohortCompilationError(
            f"corridor component with scalar variables {component.scalar_variable_ids} "
            "mixes axes across an orientation boundary"
        )


def _atomic_components(
    claims: tuple[_BoundClaim, ...],
    physical: tuple[tuple[int, ...], ...],
    footprint_model: _MemberFootprintModel,
) -> tuple[_AtomicComponentSpec, ...]:
    """Close *physical* overlap groups and scalar variables over typed relations.

    Every claim rank and every scalar variable id is a component node from
    the start. Physical overlap seeds the union; each ``_FootprintOrder`` and
    ``_FootprintContact`` then unions the nodes its participant variables
    resolve to -- a claim-carrying variable resolves to its own claim ranks,
    a scalar variable (one with no claim at all) resolves to its own node.
    That relation participation is the only thing that ever joins two scalar
    nodes, or a scalar node to a physical group: sharing an axis unions
    nothing by itself.
    """
    variables_by_id = {
        variable.variable_id: variable for variable in footprint_model.variables
    }
    scalar_variables = tuple(
        variable
        for variable in footprint_model.variables
        if not footprint_model.claim_ids_by_variable[variable.variable_id]
    )
    claim_rank_by_id = {claim.claim_id: rank for rank, claim in enumerate(claims)}
    scalar_node = {
        variable.variable_id: len(claims) + rank
        for rank, variable in enumerate(scalar_variables)
    }
    union = _UnionFind(len(claims) + len(scalar_variables))
    for group in physical:
        for claim_rank in group[1:]:
            union.union(group[0], claim_rank)

    def relation_nodes(variable_id: str) -> tuple[int, ...]:
        claim_ids = footprint_model.claim_ids_by_variable[variable_id]
        if claim_ids:
            return tuple(
                sorted(
                    {
                        claim_rank_by_id[claim_id]
                        for claim_id in claim_ids
                        if claim_id in claim_rank_by_id
                    }
                )
            )
        return (scalar_node[variable_id],)

    relations: tuple[_FootprintOrder | _FootprintContact, ...] = (
        *footprint_model.orders,
        *footprint_model.contacts,
    )
    for relation in relations:
        relation_ranks = tuple(
            rank
            for variable_id in relation.participant_variable_ids
            for rank in relation_nodes(variable_id)
        )
        for rank in relation_ranks[1:]:
            union.union(relation_ranks[0], rank)

    grouped_physical: defaultdict[int, list[int]] = defaultdict(list)
    for physical_rank, group in enumerate(physical):
        grouped_physical[union.find(group[0])].append(physical_rank)
    grouped_scalar: defaultdict[int, list[str]] = defaultdict(list)
    for variable in scalar_variables:
        grouped_scalar[union.find(scalar_node[variable.variable_id])].append(
            variable.variable_id
        )
    roots = sorted(set(grouped_physical) | set(grouped_scalar))
    components = tuple(
        _AtomicComponentSpec(
            tuple(grouped_physical[root]), tuple(sorted(grouped_scalar[root]))
        )
        for root in roots
    )
    for component in components:
        _validate_atomic_component_orientation(
            component, claims, physical, variables_by_id
        )
    return components


def _landing_frame(
    identity: tuple[str, tuple[str, str, str]],
    target: CorridorCohortTarget | None,
    coordinate: float | None = None,
) -> tuple[int, int, float]:
    if (
        target is None
        or not target.mutable
        or target.endpoint_lane_axis not in (0, 1)
        or (coordinate is None and target.endpoint_lane_coordinate is None)
        or len(target.route.points) < 2
    ):
        raise CorridorCohortCompilationError(
            f"corridor endpoint landing {identity} has no current port frame"
        )
    segment_rank = len(target.route.points) - 2
    start, end = target.route.points[segment_rank:]
    axis = target.endpoint_lane_axis
    if (
        abs(start[axis] - end[axis]) > COORD_TOLERANCE
        or abs(start[1 - axis] - end[1 - axis]) <= COORD_TOLERANCE
    ):
        raise CorridorCohortCompilationError(
            f"corridor endpoint landing {identity} changed lead shape"
        )
    landing_coordinate = (
        target.endpoint_lane_coordinate if coordinate is None else coordinate
    )
    if landing_coordinate is None or not isfinite(landing_coordinate):
        raise CorridorCohortCompilationError(
            f"corridor endpoint landing {identity} has no finite port slot"
        )
    return segment_rank, axis, landing_coordinate


def _cohort_landing_coordinates(
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
) -> dict[tuple[str, tuple[str, str, str]], float]:
    """Bind each endpoint member to the port slot its own network draws into.

    A network rank follows route emission order, which need not ascend with the
    slots the networks draw into, so a rank is never dealt a slot by position:
    it keeps the one slot its members share.
    """
    expected_by_cohort = dict(ledger.endpoint_members)
    ranks_by_member: defaultdict[str, set[tuple[str, int]]] = defaultdict(set)
    members_by_cohort: defaultdict[str, set[str]] = defaultdict(set)
    for claim in ledger.claims:
        if (
            claim.endpoint_cohort_id is not None
            and claim.endpoint_network_rank is not None
        ):
            ranks_by_member[claim.member_id].add(
                (claim.endpoint_cohort_id, claim.endpoint_network_rank)
            )
            members_by_cohort[claim.endpoint_cohort_id].add(claim.member_id)
    targets_by_member: defaultdict[str, list[CorridorCohortTarget]] = defaultdict(list)
    for target in targets:
        targets_by_member[target.member_id].append(target)

    coordinates: dict[tuple[str, tuple[str, str, str]], float] = {}
    for cohort_id, expected_members in expected_by_cohort.items():
        if cohort_id in ledger.ambiguous_endpoint_cohort_ids:
            continue
        eligible_members = expected_members.intersection(
            ledger.eligible_member_ids,
            members_by_cohort[cohort_id],
        )
        if not eligible_members:
            continue
        member_targets: dict[str, CorridorCohortTarget] = {}
        rank_by_member: dict[str, int] = {}
        complete = True
        for member_id in eligible_members:
            member_ranks = {
                rank
                for candidate_cohort, rank in ranks_by_member.get(member_id, ())
                if candidate_cohort == cohort_id
            }
            candidates = targets_by_member.get(member_id, ())
            if len(member_ranks) != 1 or len(candidates) != 1:
                complete = False
                break
            target = candidates[0]
            if (
                target.endpoint_lane_axis not in (0, 1)
                or target.endpoint_lane_coordinate is None
                or not isfinite(target.endpoint_lane_coordinate)
            ):
                complete = False
                break
            rank_by_member[member_id] = next(iter(member_ranks))
            member_targets[member_id] = target
        if not complete:
            raise CorridorCohortCompilationError(
                f"corridor endpoint cohort {cohort_id} has an incomplete eligible frame"
            )

        ranks = sorted(set(rank_by_member.values()))
        axes = {target.endpoint_lane_axis for target in member_targets.values()}
        if len(axes) != 1:
            raise CorridorCohortCompilationError(
                f"corridor endpoint cohort {cohort_id} has mixed port axes"
            )
        slots_by_rank: defaultdict[int, list[float]] = defaultdict(list)
        for member_id, target in member_targets.items():
            assert target.endpoint_lane_coordinate is not None
            slots_by_rank[rank_by_member[member_id]].append(
                target.endpoint_lane_coordinate
            )
        representative_slots: list[float] = []
        for rank in ranks:
            network_slots = slots_by_rank[rank]
            if any(
                not isclose(item, network_slots[0], abs_tol=COORD_TOLERANCE)
                for item in network_slots[1:]
            ):
                raise CorridorCohortCompilationError(
                    f"corridor endpoint cohort {cohort_id} has split network slots"
                )
            representative_slots.append(network_slots[0])
        ordered_slots = sorted(representative_slots)
        if any(
            isclose(left, right, abs_tol=COORD_TOLERANCE)
            for left, right in zip(ordered_slots, ordered_slots[1:])
        ):
            raise CorridorCohortCompilationError(
                f"corridor endpoint cohort {cohort_id} has tied port slots"
            )
        if len(ordered_slots) != len(ranks):
            raise CorridorCohortCompilationError(
                f"corridor endpoint cohort {cohort_id} has incomplete port slots"
            )
        slot_by_rank = dict(zip(ranks, representative_slots))
        for member_id, target in member_targets.items():
            coordinates[(member_id, target.edge_key)] = slot_by_rank[
                rank_by_member[member_id]
            ]
    return coordinates


def _overlap(left: _BoundClaim, right: _BoundClaim) -> bool:
    return _overlaps(
        left.longitudinal_start,
        left.longitudinal_end,
        right.longitudinal_start,
        right.longitudinal_end,
    )


def _same_lane(left: _BoundClaim, right: _BoundClaim) -> bool:
    return (
        left.ledger.lane_rank is not None
        and left.ledger.reservation_id == right.ledger.reservation_id
        and left.ledger.lane_rank == right.ledger.lane_rank
        and _overlap(left, right)
    )


def _same_semantic_fixed_lane(left: _BoundClaim, right: _BoundClaim) -> bool:
    if not _overlap(left, right):
        return False
    if claims_share_fixed_lane_identity(left.ledger, right.ledger):
        return True
    left_terminal = (
        left.longitudinal_end
        if left.ledger.direction in (Direction.R, Direction.D)
        else left.longitudinal_start
    )
    right_terminal = (
        right.longitudinal_end
        if right.ledger.direction in (Direction.R, Direction.D)
        else right.longitudinal_start
    )
    # Claims minted by different reservations name one fixed lane when they
    # terminate at the same coordinate: align the reservation id so the shared
    # identity check reduces to its network/direction/lane clause.
    return isclose(
        left_terminal, right_terminal, abs_tol=COORD_TOLERANCE
    ) and claims_share_fixed_lane_identity(
        left.ledger,
        replace(right.ledger, reservation_id=left.ledger.reservation_id),
    )


def _roles(
    claims: tuple[_BoundClaim, ...],
    finalized_owned_segments: frozenset[CorridorCohortSegmentKey] | None,
) -> dict[str, CorridorCohortClaimRole]:
    if finalized_owned_segments is not None:
        for item in claims:
            key = (
                item.ledger.member_id,
                item.target.edge_key,
                item.ledger.segment_rank,
            )
            if key not in finalized_owned_segments:
                continue
            route = item.target.route
            if (
                not item.target.mutable
                or convergence_owns_segment_boundary(route, item.ledger.segment_rank)
                or route.fan_route_emitter is not None
                or (
                    route.exit_turn_axis_id is not None
                    and route.exit_turn_segment_rank == item.ledger.segment_rank
                )
            ):
                raise CorridorCohortCompilationError(
                    f"finalized corridor claim {item.claim_id} conflicts with "
                    "another geometry owner"
                )
        return {
            item.claim_id: (
                CorridorCohortClaimRole.MOVABLE
                if (
                    item.ledger.member_id,
                    item.target.edge_key,
                    item.ledger.segment_rank,
                )
                in finalized_owned_segments
                else CorridorCohortClaimRole.FIXED
            )
            for item in claims
        }
    terminal_fixed = {
        item.claim_id
        for item in claims
        if not item.target.mutable
        or planner_owns_segment(item.target.route, item.ledger.segment_rank)
    }
    roles = {
        item.claim_id: (
            CorridorCohortClaimRole.MOVABLE
            if item.ledger.reservation_complete and item.claim_id not in terminal_fixed
            else CorridorCohortClaimRole.FIXED
        )
        for item in claims
    }
    changed = True
    while changed:
        changed = False
        for follower in claims:
            if roles[follower.claim_id] != CorridorCohortClaimRole.FIXED:
                continue
            if follower.claim_id in terminal_fixed:
                continue
            if follower.ledger.network_id is None:
                continue
            if any(
                roles[leader.claim_id] != CorridorCohortClaimRole.FIXED
                and leader.ledger.network_id == follower.ledger.network_id
                and _same_lane(leader, follower)
                for leader in claims
            ):
                roles[follower.claim_id] = CorridorCohortClaimRole.EQUALITY
                changed = True
    return roles


def _direction_owner(owner_id: str, direction: Direction) -> str:
    return f"{owner_id}|direction:{direction.value}"


def _end_turn_sides(claim: _BoundClaim) -> tuple[int, int]:
    """The raw side the route turns toward at the claim's low and high span ends."""
    points = claim.target.route.points
    rank = claim.ledger.segment_rank
    axis = claim.axis
    start, end = points[rank], points[rank + 1]

    def turn_side(corner: tuple[float, float], beyond: int) -> int:
        if not 0 <= beyond < len(points):
            return 0
        delta = points[beyond][axis] - corner[axis]
        if abs(delta) <= COORD_TOLERANCE:
            return 0
        return 1 if delta > 0 else -1

    at_start, at_end = turn_side(start, rank - 1), turn_side(end, rank + 2)
    if start[1 - axis] <= end[1 - axis]:
        return at_start, at_end
    return at_end, at_start


def _lane(
    claim: _BoundClaim,
    cohort_first_path_rank: int,
    local_boundary_rank: int | None,
    direction: Direction,
    offset_step: float,
) -> CorridorLane:
    base_cohort_id = claim.ledger.endpoint_cohort_id or f"equality|{claim.claim_id}"
    base_owner_id = (
        claim.ledger.endpoint_cohort_id
        or f"reservation-lane|{claim.ledger.reservation_id}|{claim.ledger.lane_rank}"
    )
    boundary_rank = (
        local_boundary_rank
        if local_boundary_rank is not None
        else claim.ledger.path_rank
    )
    boundary_axis_sign = claim.ledger.destination_boundary_axis_sign
    return CorridorLane(
        claim.claim_id,
        _direction_owner(base_cohort_id, direction),
        _direction_owner(base_owner_id, direction),
        boundary_rank * offset_step * boundary_axis_sign
        if claim.ledger.endpoint_cohort_id is not None
        and boundary_axis_sign is not None
        else claim.coordinate,
        claim.coordinate,
        claim.longitudinal_start,
        claim.longitudinal_end,
        (
            boundary_rank,
            cohort_first_path_rank,
            claim.ledger.path_rank,
            claim.ledger.reservation_rank,
            claim.ledger.claim_rank,
        ),
        *_end_turn_sides(claim),
    )


def _obstacle(claim: _BoundClaim) -> CorridorObstacle:
    return CorridorObstacle(
        claim.claim_id,
        claim.coordinate,
        claim.coordinate,
        claim.longitudinal_start,
        claim.longitudinal_end,
        (
            claim.ledger.path_rank,
            claim.ledger.reservation_rank,
            claim.ledger.claim_rank,
        ),
    )


def _scalar_boundary_domains(
    request: CorridorScalarRequest,
    intervals_by_member: Mapping[str, Sequence[CorridorForbiddenInterval]],
) -> tuple[tuple[CorridorCoordinateDomain, ...], dict[str, str]]:
    """Lower one scalar request's domain, naming the interval each boundary
    obstacle attributes a shortfall to.

    A forbidden interval that pokes past a bound contributes an attribution-only
    obstacle id on the domain nearest to it, mirroring the shortfall provenance a
    member relation domain carries.
    """
    domain = request.domain
    sources: dict[str, str] = {}
    minimum_obstacles: list[str] = []
    maximum_obstacles: list[str] = []
    for interval in intervals_by_member.get(domain.member_id, ()):
        minimum_deficit = (
            None
            if domain.minimum_coordinate is None
            or interval.minimum_coordinate >= domain.minimum_coordinate
            else domain.minimum_coordinate - interval.minimum_coordinate
        )
        maximum_deficit = (
            None
            if domain.maximum_coordinate is None
            or interval.maximum_coordinate <= domain.maximum_coordinate
            else interval.maximum_coordinate - domain.maximum_coordinate
        )
        if minimum_deficit is not None and (
            maximum_deficit is None or minimum_deficit < maximum_deficit
        ):
            obstacle_id = (
                f"scalar-boundary|{domain.member_id}|minimum|{interval.obstacle_id}"
            )
            minimum_obstacles.append(obstacle_id)
            sources[obstacle_id] = interval.obstacle_id
        elif maximum_deficit is not None and (
            minimum_deficit is None or maximum_deficit < minimum_deficit
        ):
            obstacle_id = (
                f"scalar-boundary|{domain.member_id}|maximum|{interval.obstacle_id}"
            )
            maximum_obstacles.append(obstacle_id)
            sources[obstacle_id] = interval.obstacle_id
    domains: list[CorridorCoordinateDomain] = []
    if domain.minimum_coordinate is not None:
        domains.append(
            CorridorCoordinateDomain(
                domain.member_id,
                minimum_coordinate=domain.minimum_coordinate,
                obstacle_ids=tuple(sorted(minimum_obstacles)),
            )
        )
    if domain.maximum_coordinate is not None:
        domains.append(
            CorridorCoordinateDomain(
                domain.member_id,
                maximum_coordinate=domain.maximum_coordinate,
                obstacle_ids=tuple(sorted(maximum_obstacles)),
            )
        )
    if domain.minimum_coordinate is None and domain.maximum_coordinate is None:
        domains.append(domain)
    return tuple(domains), sources


def _ranks_in_slot_order(slot_by_rank: Mapping[int, float | None]) -> list[int]:
    """Endpoint network ranks in the order of the port slots they draw into.

    A rank whose member has no port slot sorts after every slotted one.
    """
    return sorted(
        slot_by_rank,
        key=lambda rank: (
            slot_by_rank[rank] is None,
            slot_by_rank[rank] or 0.0,
            rank,
        ),
    )


def _problem(
    claims: tuple[_BoundClaim, ...],
    roles: dict[str, CorridorCohortClaimRole],
    complete: bool,
    offset_step: float,
    curve_radius: float,
    footprint_model: _MemberFootprintModel,
    witnesses_by_id: Mapping[str, CorridorFootprintWitness],
    *,
    scalar_requests: tuple[CorridorScalarRequest, ...] = (),
    intervals_by_member: Mapping[str, Sequence[CorridorForbiddenInterval]] = (
        MappingProxyType({})
    ),
) -> CorridorAllocationProblem:
    movable = tuple(
        item for item in claims if roles[item.claim_id] != CorridorCohortClaimRole.FIXED
    )
    fixed_claims = tuple(
        item for item in claims if roles[item.claim_id] is CorridorCohortClaimRole.FIXED
    )
    movable_ids = {item.claim_id for item in movable}
    scalar_by_id = {
        request.variable.variable_id: request for request in scalar_requests
    }
    scalar_witness_matches: defaultdict[str, list[CorridorFootprintWitness]] = (
        defaultdict(list)
    )
    if scalar_by_id:
        for witness in footprint_model.witnesses:
            if witness.coordinate_variable_id in scalar_by_id:
                scalar_witness_matches[witness.coordinate_variable_id].append(witness)
    scalar_witnesses: dict[str, CorridorFootprintWitness] = {}
    for variable_id in scalar_by_id:
        matches = scalar_witness_matches.get(variable_id, [])
        if len(matches) != 1:
            raise CorridorCohortCompilationError(
                f"corridor scalar request {variable_id} has no unique footprint"
            )
        scalar_witnesses[variable_id] = matches[0]

    def lane_ids(variable_id: str) -> tuple[str, ...]:
        """The solver lane ids one relation variable resolves to in this problem.

        A member carrier resolves to its movable claim ids; a scalar variable
        resolves to its own id when this problem seats it. A variable seated in
        another component resolves to nothing, so iterating the global relation
        set only constrains lanes this problem owns.
        """
        claim_ids = footprint_model.claim_ids_by_variable.get(variable_id, ())
        if claim_ids:
            return tuple(claim_id for claim_id in claim_ids if claim_id in movable_ids)
        if variable_id in scalar_by_id:
            return (variable_id,)
        return ()

    cohort_first_path_rank: dict[tuple[Direction, str], int] = {}
    for item in movable:
        cohort_id = item.ledger.endpoint_cohort_id or f"equality|{item.claim_id}"
        cohort_key = item.ledger.direction, cohort_id
        cohort_first_path_rank[cohort_key] = min(
            item.ledger.path_rank,
            cohort_first_path_rank.get(cohort_key, item.ledger.path_rank),
        )
    local_boundary_ranks: dict[tuple[str, int], int] = {}
    for cohort_id in {
        item.ledger.endpoint_cohort_id
        for item in movable
        if item.ledger.endpoint_cohort_id is not None
    }:
        ranks = _ranks_in_slot_order(
            {
                item.ledger.endpoint_network_rank: item.target.endpoint_lane_coordinate
                for item in movable
                if item.ledger.endpoint_cohort_id == cohort_id
                and item.ledger.endpoint_network_rank is not None
            }
        )
        local_boundary_ranks.update(
            {
                (cohort_id, boundary_rank): local_rank
                for local_rank, boundary_rank in enumerate(ranks)
            }
        )
    equalities = tuple(
        CorridorEquality(
            _direction_owner(
                f"reservation-lane|{left.ledger.reservation_id}|{left.ledger.lane_rank}",
                left.ledger.direction,
            ),
            left.claim_id,
            right.claim_id,
            0.0,
        )
        for rank, left in enumerate(movable)
        for right in movable[rank + 1 :]
        if _same_lane(left, right)
        and left.ledger.direction is right.ledger.direction
        and left.ledger.network_id is not None
        and left.ledger.network_id == right.ledger.network_id
    )
    fixed_equalities = list(
        CorridorFixedEquality(
            _direction_owner(
                f"network-lane|{movable_claim.ledger.network_id}|"
                f"{movable_claim.ledger.lane_rank}",
                movable_claim.ledger.direction,
            ),
            movable_claim.claim_id,
            fixed_claim.claim_id,
        )
        for movable_claim in movable
        for fixed_claim in fixed_claims
        if _same_semantic_fixed_lane(movable_claim, fixed_claim)
    )
    contact_obstacles: dict[str, CorridorObstacle] = {}
    for contact in footprint_model.contacts:
        fixed_witnesses = tuple(
            witnesses_by_id[witness_id]
            for witness_id in contact.witness_ids
            if witnesses_by_id[witness_id].coordinate_variable_id is None
            and witnesses_by_id[witness_id].start_variable_id is None
            and witnesses_by_id[witness_id].end_variable_id is None
        )
        if len(fixed_witnesses) != 1:
            continue
        witness = fixed_witnesses[0]
        contact_obstacles[contact.owner_id] = CorridorObstacle(
            contact.owner_id,
            witness.coordinate,
            witness.coordinate,
            witness.longitudinal_start,
            witness.longitudinal_end,
            witness.semantic_rank,
        )
        fixed_equalities.extend(
            CorridorFixedEquality(contact.owner_id, member_id, contact.owner_id)
            for variable_id in contact.participant_variable_ids
            for member_id in lane_ids(variable_id)
        )
    separations = tuple(
        CorridorSeparation(
            left.claim_id,
            right.claim_id,
            cotravelling_lane_clearance(
                same_line=left.target.route.line_id == right.target.route.line_id,
                counter_running=left.ledger.direction is not right.ledger.direction,
                curve_radius=curve_radius,
                offset_step=offset_step,
            ),
        )
        for rank, left in enumerate(claims)
        for right in claims[rank + 1 :]
        if _overlap(left, right)
        and (
            roles[left.claim_id] != CorridorCohortClaimRole.FIXED
            or roles[right.claim_id] != CorridorCohortClaimRole.FIXED
        )
    )
    directed_separations: list[CorridorDirectedSeparation] = [
        CorridorDirectedSeparation(
            f"{order.owner_id}|{lower_id}|{upper_id}",
            lower_id,
            upper_id,
            order.distance,
        )
        for order in footprint_model.orders
        if order.lower.variable_id is not None and order.upper.variable_id is not None
        for lower_id in lane_ids(order.lower.variable_id)
        for upper_id in lane_ids(order.upper.variable_id)
    ]
    relation_domains: list[CorridorCoordinateDomain] = []
    for order in footprint_model.orders:
        if order.lower.variable_id is None and order.upper.variable_id is not None:
            assert order.lower.coordinate is not None
            relation_domains.extend(
                CorridorCoordinateDomain(
                    member_id,
                    minimum_coordinate=order.lower.coordinate + order.distance,
                    obstacle_ids=(order.owner_id,),
                )
                for member_id in lane_ids(order.upper.variable_id)
            )
        elif order.lower.variable_id is not None and order.upper.variable_id is None:
            assert order.upper.coordinate is not None
            relation_domains.extend(
                CorridorCoordinateDomain(
                    member_id,
                    maximum_coordinate=order.upper.coordinate - order.distance,
                    obstacle_ids=(order.owner_id,),
                )
                for member_id in lane_ids(order.lower.variable_id)
            )
        elif order.lower.variable_id is None and order.upper.variable_id is None:
            assert order.lower.coordinate is not None
            assert order.upper.coordinate is not None
            if order.lower.coordinate + order.distance > order.upper.coordinate:
                raise CorridorCohortCompilationError(
                    f"fixed footprint order {order.owner_id} is infeasible"
                )
    forbidden_intervals = tuple(
        replace(interval, member_id=member_id)
        for interval in footprint_model.forbidden_intervals
        for member_id in lane_ids(interval.member_id)
    )
    scalar_domains: list[CorridorCoordinateDomain] = []
    for request in scalar_requests:
        request_domains, _sources = _scalar_boundary_domains(
            request, intervals_by_member
        )
        scalar_domains.extend(request_domains)
    ordered_requests = sorted(
        scalar_requests,
        key=lambda item: (
            scalar_witnesses[item.variable.variable_id].semantic_rank,
            item.variable.variable_id,
        ),
    )
    scalar_lanes = tuple(
        CorridorLane(
            request.variable.variable_id,
            request.variable.variable_id,
            request.variable.owner_id,
            request.variable.coordinate,
            request.preferred_coordinate,
            scalar_witnesses[request.variable.variable_id].longitudinal_start,
            scalar_witnesses[request.variable.variable_id].longitudinal_end,
            scalar_witnesses[request.variable.variable_id].semantic_rank,
        )
        for request in ordered_requests
    )
    scalar_separations = tuple(
        CorridorSeparation(
            left.variable.variable_id,
            right.variable.variable_id,
            cotravelling_lane_clearance(
                same_line=(
                    scalar_witnesses[left.variable.variable_id].line_id
                    == scalar_witnesses[right.variable.variable_id].line_id
                ),
                counter_running=(
                    scalar_witnesses[left.variable.variable_id].direction
                    is not scalar_witnesses[right.variable.variable_id].direction
                ),
                curve_radius=curve_radius,
                offset_step=offset_step,
            ),
        )
        for rank, left in enumerate(ordered_requests)
        for right in ordered_requests[rank + 1 :]
        if _footprints_overlap(
            scalar_witnesses[left.variable.variable_id],
            scalar_witnesses[right.variable.variable_id],
        )
    )
    member_lanes = tuple(
        _lane(
            item,
            cohort_first_path_rank[
                (
                    item.ledger.direction,
                    item.ledger.endpoint_cohort_id or f"equality|{item.claim_id}",
                )
            ],
            (
                local_boundary_ranks[
                    (
                        item.ledger.endpoint_cohort_id,
                        item.ledger.endpoint_network_rank,
                    )
                ]
                if item.ledger.endpoint_cohort_id is not None
                and item.ledger.endpoint_network_rank is not None
                else None
            ),
            item.ledger.direction,
            offset_step,
        )
        for item in movable
    )
    coordinate_axis = movable[0].axis if movable else scalar_requests[0].variable.axis
    return CorridorAllocationProblem(
        (*member_lanes, *scalar_lanes),
        (
            *(_obstacle(item) for item in fixed_claims),
            *contact_obstacles.values(),
        ),
        equalities,
        (*separations, *scalar_separations),
        (*relation_domains, *scalar_domains),
        fixed_equalities=tuple(fixed_equalities),
        directed_separations=tuple(directed_separations),
        forbidden_intervals=forbidden_intervals,
        clearance=offset_step,
        witnesses_complete=complete,
        axis_sign=_lane_order_axis_sign(
            coordinate_axis,
            {item.ledger.direction for item in movable}
            | {
                scalar_witnesses[request.variable.variable_id].direction
                for request in scalar_requests
            },
        ),
        coordinate_axis=coordinate_axis,
    )


def _lane_order_axis_sign(coordinate_axis: int, travel: set[Direction]) -> int:
    """Screen sign of the canonical lane order along *coordinate_axis*.

    A bundle fans its members along the right-hand normal of its travel, so a
    component whose lanes all travel one way orders along that normal.
    Counter-running lanes share no travel frame; they order in the frame of
    screen-positive travel along the corridor (R for a Y-coordinate lane, D
    for an X-coordinate one).
    """
    if len(travel) == 1:
        return right_normal_axis_sign(next(iter(travel)))
    return right_normal_axis_sign(Direction.R if coordinate_axis == 1 else Direction.D)


def _component_complete(
    claims: tuple[_BoundClaim, ...],
    endpoint_ids: tuple[str, ...],
    ledger: CorridorCohortLedger,
) -> bool:
    expected = dict(ledger.endpoint_members)
    eligible_by_endpoint: defaultdict[str, set[str]] = defaultdict(set)
    for claim in ledger.claims:
        if (
            claim.endpoint_cohort_id is not None
            and claim.endpoint_network_rank is not None
            and claim.member_id in ledger.eligible_member_ids
        ):
            eligible_by_endpoint[claim.endpoint_cohort_id].add(claim.member_id)
    observed: defaultdict[str, set[str]] = defaultdict(set)
    for item in claims:
        if item.ledger.endpoint_cohort_id is not None:
            observed[item.ledger.endpoint_cohort_id].add(item.ledger.member_id)
    return (
        not ledger.ambiguous_endpoint_cohort_ids.intersection(endpoint_ids)
        and all(
            observed[item] == expected[item].intersection(eligible_by_endpoint[item])
            for item in endpoint_ids
        )
        and all(item.ledger.reservation_complete for item in claims)
        and len({(item.ledger.member_id, item.ledger.segment_rank) for item in claims})
        == len(claims)
    )


def _footprints_overlap(
    left: CorridorFootprintWitness,
    right: CorridorFootprintWitness,
) -> bool:
    return _overlaps(
        left.longitudinal_start,
        left.longitudinal_end,
        right.longitudinal_start,
        right.longitudinal_end,
    )


def _clearance_resolver(
    problems: tuple[CorridorAllocationProblem, ...],
) -> Callable[[str, str], float]:
    """Map an unordered member pair to its required clearance.

    Unlisted pairs fall back to the shared problem clearance, the same rule
    ``solve_corridor_cohorts`` applies over one exact-arithmetic problem.
    """
    by_pair = {
        frozenset((separation.left_member_id, separation.right_member_id)): (
            separation.distance
        )
        for problem in problems
        for separation in problem.separations
    }
    default = problems[0].clearance
    return lambda left_id, right_id: by_pair.get(
        frozenset((left_id, right_id)), default
    )


def _planned_allocations_are_atomic_and_clear(
    claims: tuple[_BoundClaim, ...],
    physical_claims: tuple[tuple[_BoundClaim, ...], ...],
    roles: dict[str, CorridorCohortClaimRole],
    problems: tuple[CorridorAllocationProblem, ...],
    results: tuple[CorridorAllocationResult, ...],
    scalar_variable_ids: frozenset[str],
) -> bool:
    allocated = tuple(
        claim_id for result in results for claim_id, _coordinate in result.allocations
    )
    expected = {
        item.claim_id
        for item in claims
        if roles[item.claim_id] != CorridorCohortClaimRole.FIXED
    } | set(scalar_variable_ids)
    if len(allocated) != len(set(allocated)) or set(allocated) != expected:
        return False
    coordinates = {
        claim_id: coordinate
        for result in results
        for claim_id, coordinate in result.allocations
    }
    required_clearance = _clearance_resolver(problems)
    for group in physical_claims:
        movable = tuple(
            item
            for item in group
            if roles[item.claim_id] != CorridorCohortClaimRole.FIXED
        )
        for rank, left in enumerate(movable):
            for right in movable[rank + 1 :]:
                if left.ledger.direction is right.ledger.direction or not _overlap(
                    left, right
                ):
                    continue
                if abs(
                    coordinates[left.claim_id] - coordinates[right.claim_id]
                ) + COORD_TOLERANCE < required_clearance(left.claim_id, right.claim_id):
                    return False
    return True


def _allocation(claim: _BoundClaim, coordinate: float) -> CorridorCohortAllocation:
    assert claim.ledger.member_geometry_plan_id is not None
    return CorridorCohortAllocation(
        claim.claim_id,
        claim.ledger.member_id,
        claim.ledger.member_geometry_plan_id,
        claim.target.edge_key,
        claim.target.family_id,
        claim.target.connector_ids,
        claim.ledger.segment_rank,
        claim.axis,
        claim.longitudinal_start,
        claim.longitudinal_end,
        coordinate,
    )


def _landing_from_claim(claim: _BoundClaim) -> CorridorCohortLanding:
    identity = claim.ledger.member_id, claim.target.edge_key
    segment_rank, axis, coordinate = _landing_frame(
        identity,
        claim.target,
        claim.landing_coordinate,
    )
    target = claim.target
    return CorridorCohortLanding(
        target.member_id,
        target.member_geometry_plan_id,
        target.edge_key,
        target.connector_ids,
        segment_rank,
        axis,
        coordinate,
    )


def _planned_landings(
    claims: tuple[_BoundClaim, ...],
    allocations: tuple[CorridorCohortAllocation, ...],
) -> tuple[CorridorCohortLanding, ...]:
    allocated_identities = {(item.member_id, item.edge_key) for item in allocations}
    landing_claims = {
        (item.ledger.member_id, item.target.edge_key): item
        for item in claims
        if item.ledger.endpoint_cohort_id is not None
        and item.landing_coordinate is not None
    }
    landings: list[CorridorCohortLanding] = []
    for identity in sorted(allocated_identities.intersection(landing_claims)):
        claim = landing_claims[identity]
        landings.append(_landing_from_claim(claim))
    return tuple(landings)


def _component_plan(
    rank: int,
    spec: _AtomicComponentSpec,
    physical: tuple[tuple[int, ...], ...],
    claims: tuple[_BoundClaim, ...],
    ledger: CorridorCohortLedger,
    footprint_model: _MemberFootprintModel,
    witnesses_by_id: Mapping[str, CorridorFootprintWitness],
    scalar_requests_by_id: Mapping[str, CorridorScalarRequest],
    intervals_by_member: Mapping[str, Sequence[CorridorForbiddenInterval]],
    solve: Callable[[CorridorAllocationProblem], CorridorAllocationResult],
) -> CorridorCohortComponentPlan:
    """Lower one closed component into a single per-axis solver problem.

    Every physical overlap group and every relation-joined scalar variable the
    closure gave this component seats in one ``CorridorAllocationProblem``, so a
    member and a scalar joined by a typed relation always share one solve.
    """
    physical_ranks = spec.physical_ranks
    component_claims = tuple(
        claims[item] for group in physical_ranks for item in physical[group]
    )
    component_scalar_requests = tuple(
        scalar_requests_by_id[variable_id]
        for variable_id in spec.scalar_variable_ids
        if variable_id in scalar_requests_by_id
    )
    endpoint_ids = tuple(
        sorted(
            {
                item.ledger.endpoint_cohort_id
                for item in component_claims
                if item.ledger.endpoint_cohort_id
            }
        )
    )
    roles = _roles(component_claims, ledger.finalized_owned_segments)
    complete = _component_complete(component_claims, endpoint_ids, ledger)
    physical_claims = tuple(
        tuple(claims[item] for item in physical[group]) for group in physical_ranks
    )
    has_movable = any(
        roles[item.claim_id] != CorridorCohortClaimRole.FIXED
        for item in component_claims
    )
    member_ids = tuple(sorted({item.ledger.member_id for item in component_claims}))
    problems: tuple[CorridorAllocationProblem, ...] = ()
    results: tuple[CorridorAllocationResult, ...] = ()
    if has_movable or component_scalar_requests:
        problem = _problem(
            component_claims,
            roles,
            complete,
            ledger.offset_step,
            ledger.curve_radius,
            footprint_model,
            witnesses_by_id,
            scalar_requests=component_scalar_requests,
            intervals_by_member=intervals_by_member,
        )
        problems = (problem,)
        results = (solve(problem),)
    status = (
        CorridorAllocationStatus.PLANNED
        if results
        and all(item.status is CorridorAllocationStatus.PLANNED for item in results)
        else CorridorAllocationStatus.FAILURE
        if any(item.status is CorridorAllocationStatus.FAILURE for item in results)
        else CorridorAllocationStatus.COMPATIBILITY
    )
    if status is CorridorAllocationStatus.PLANNED and not (
        _planned_allocations_are_atomic_and_clear(
            component_claims,
            physical_claims,
            roles,
            problems,
            results,
            frozenset(spec.scalar_variable_ids),
        )
    ):
        status = CorridorAllocationStatus.FAILURE
    allocations: tuple[CorridorCohortAllocation, ...] = ()
    protected: tuple[tuple[str, int], ...] = ()
    compatibility: CorridorCohortCompatibility | None = None
    if status is CorridorAllocationStatus.PLANNED:
        by_claim = {item.claim_id: item for item in component_claims}
        allocations = tuple(
            _allocation(by_claim[claim_id], coordinate)
            for result in results
            for claim_id, coordinate in result.allocations
            if claim_id in by_claim
        )
        protected = tuple(
            sorted({(item.member_id, item.segment_rank) for item in allocations})
        )
    elif status is CorridorAllocationStatus.COMPATIBILITY:
        compatibility = CorridorCohortCompatibility(
            CorridorCompatibilityReason.INCOMPLETE_WITNESSES
            if problems
            else CorridorCompatibilityReason.NO_MIGRABLE_GEOMETRY,
            member_ids,
            spec.scalar_variable_ids,
            endpoint_ids,
        )
    return CorridorCohortComponentPlan(
        f"corridor-component|{rank}",
        endpoint_ids,
        tuple((item.claim_id, roles[item.claim_id]) for item in component_claims),
        problems,
        results,
        status,
        allocations,
        protected,
        compatibility,
    )


def _corner_input_candidates(
    route: RoutedPath, radius_rank: int
) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    return (
        (
            route.concentric_corner_offsets_by_segment.get(radius_rank, (None, None))[
                1
            ],
            route.concentric_corner_bases_by_segment.get(radius_rank, (None, None))[1],
        ),
        (
            route.concentric_corner_offsets_by_segment.get(
                radius_rank + 1, (None, None)
            )[0],
            route.concentric_corner_bases_by_segment.get(radius_rank + 1, (None, None))[
                0
            ],
        ),
    )


def _corner_input(
    candidates: tuple[
        tuple[float | None, float | None], tuple[float | None, float | None]
    ],
) -> tuple[float, float] | None:
    complete = tuple(
        (offset, base)
        for offset, base in candidates
        if offset is not None and base is not None
    )
    if any((offset is None) != (base is None) for offset, base in candidates) or (
        complete
        and any(
            not isclose(offset, complete[0][0], abs_tol=COORD_TOLERANCE)
            or not isclose(base, complete[0][1], abs_tol=COORD_TOLERANCE)
            for offset, base in complete[1:]
        )
    ):
        return None
    return complete[0] if complete else None


def _has_corner_input(
    candidates: tuple[
        tuple[float | None, float | None], tuple[float | None, float | None]
    ],
) -> bool:
    return any(offset is not None or base is not None for offset, base in candidates)


def _prepare_patches(
    allocations: tuple[CorridorCohortAllocation, ...],
    landings: tuple[CorridorCohortLanding, ...],
    targets: Sequence[CorridorCohortTarget],
) -> tuple[_RoutePatch, ...] | None:
    targets_by_key = {(target.member_id, target.edge_key): target for target in targets}
    changes: defaultdict[
        tuple[str, tuple[str, str, str]], list[CorridorCohortAllocation]
    ] = defaultdict(list)
    for allocation in allocations:
        changes[(allocation.member_id, allocation.edge_key)].append(allocation)
    landings_by_key: defaultdict[
        tuple[str, tuple[str, str, str]], list[CorridorCohortLanding]
    ] = defaultdict(list)
    for landing in landings:
        landings_by_key[(landing.member_id, landing.edge_key)].append(landing)
    patches: list[_RoutePatch] = []
    for key in dict.fromkeys((*changes, *landings_by_key)):
        member_changes = changes[key]
        member_landings = landings_by_key[key]
        target = targets_by_key.get(key)
        if target is None or not target.mutable:
            return None
        route = target.route
        for change in member_changes:
            if (
                target.member_geometry_plan_id != change.member_geometry_plan_id
                or target.family_id is not change.family_id
                or target.connector_ids != change.connector_ids
                or change.segment_rank + 1 >= len(route.points)
            ):
                return None
        for change in member_changes:
            start, end = route.points[change.segment_rank : change.segment_rank + 2]
            if abs(start[change.axis] - end[change.axis]) > COORD_TOLERANCE:
                return None
        for landing in member_landings:
            if (
                target.member_geometry_plan_id != landing.member_geometry_plan_id
                or target.connector_ids != landing.connector_ids
                or landing.segment_rank != len(route.points) - 2
                or landing.axis not in (0, 1)
                or not isfinite(landing.coordinate)
            ):
                return None
            start, end = route.points[landing.segment_rank :]
            if (
                abs(start[landing.axis] - end[landing.axis]) > COORD_TOLERANCE
                or abs(start[1 - landing.axis] - end[1 - landing.axis])
                <= COORD_TOLERANCE
            ):
                return None
        points = list(route.points)
        coordinates: dict[tuple[int, int], float] = {}
        affected_corners: set[int] = set()
        for change in member_changes:
            segment_key = change.segment_rank, change.axis
            if segment_key in coordinates and not isclose(
                coordinates[segment_key], change.coordinate, abs_tol=COORD_TOLERANCE
            ):
                return None
            coordinates[segment_key] = change.coordinate
        for landing in member_landings:
            segment_key = landing.segment_rank, landing.axis
            if segment_key in coordinates and not isclose(
                coordinates[segment_key],
                landing.coordinate,
                abs_tol=COORD_TOLERANCE,
            ):
                return None
            coordinates[segment_key] = landing.coordinate
        for (segment_rank, axis), coordinate in coordinates.items():
            start, end = points[segment_rank : segment_rank + 2]
            if abs(start[axis] - end[axis]) > COORD_TOLERANCE:
                return None
            for point_rank in (segment_rank, segment_rank + 1):
                point = list(points[point_rank])
                point[axis] = coordinate
                points[point_rank] = point[0], point[1]
            affected_corners.update((segment_rank - 1, segment_rank))
        radii = None if route.curve_radii is None else list(route.curve_radii)
        if radii is not None:
            for radius_rank in affected_corners:
                if not 0 <= radius_rank < len(radii):
                    continue
                candidates = _corner_input_candidates(route, radius_rank)
                if not _has_corner_input(candidates):
                    continue
                if radius_rank + 2 >= len(points):
                    return None
                corner_input = _corner_input(candidates)
                if corner_input is None:
                    return None
                radii[radius_rank] = concentric_corner_radius_at(
                    points[radius_rank],
                    points[radius_rank + 1],
                    points[radius_rank + 2],
                    *corner_input,
                )
        owned = tuple(
            sorted(
                {
                    *route.route_system_owned_segment_ranks,
                    *(item.segment_rank for item in member_changes),
                    *(item.segment_rank for item in member_landings),
                }
            )
        )
        patches.append(_RoutePatch(route, points, radii, owned))
    return tuple(patches)


def _commit_patches(patches: tuple[_RoutePatch, ...]) -> None:
    for patch in patches:
        patch.route.points[:] = patch.points
        patch.route.curve_radii = patch.radii
        patch.route.route_system_owned_segment_ranks = patch.owned


def _finalize_allocation_geometry(
    allocations: tuple[CorridorCohortAllocation, ...],
    patches: tuple[_RoutePatch, ...],
    targets: Sequence[CorridorCohortTarget],
) -> tuple[CorridorCohortAllocation, ...] | None:
    targets_by_key = {(target.member_id, target.edge_key): target for target in targets}
    points_by_route = {id(patch.route): patch.points for patch in patches}
    finalized: list[CorridorCohortAllocation] = []
    for allocation in allocations:
        target = targets_by_key.get((allocation.member_id, allocation.edge_key))
        if target is None:
            return None
        points = points_by_route.get(id(target.route))
        if points is None or allocation.segment_rank + 1 >= len(points):
            return None
        start, end = points[allocation.segment_rank : allocation.segment_rank + 2]
        if not (
            isclose(
                start[allocation.axis],
                allocation.coordinate,
                abs_tol=COORD_TOLERANCE,
            )
            and isclose(
                end[allocation.axis],
                allocation.coordinate,
                abs_tol=COORD_TOLERANCE,
            )
        ):
            return None
        longitudinal_start, longitudinal_end = sorted(
            (start[1 - allocation.axis], end[1 - allocation.axis])
        )
        finalized.append(
            replace(
                allocation,
                longitudinal_start=longitudinal_start,
                longitudinal_end=longitudinal_end,
            )
        )
    return tuple(finalized)


def compile_corridor_cohort_plan(
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
    *,
    scalar_requests: Sequence[CorridorScalarRequest] = (),
    settlement_trace: SettlementStageTrace | None = None,
) -> CorridorCohortPlan:
    """Solve every cohort from one route snapshot without publishing geometry.

    This is the one production integration call site for the corridor cohort
    solver: each closed component solves exactly once, through the ``solve``
    closure below. Solving is separated from publication: the returned plan
    carries its route patches, and :func:`publish_corridor_cohort_plan` applies
    them.
    """
    claims = _bind_ledger(ledger, targets)
    scalar_requests_by_id = {
        request.variable.variable_id: request for request in scalar_requests
    }

    def solve(problem: CorridorAllocationProblem) -> CorridorAllocationResult:
        return solve_corridor_cohorts(problem)

    footprint_model = _member_footprint_model(
        claims,
        targets,
        scalar_requests,
        ledger.finalized_owned_segments,
        ledger.offset_step,
        ledger.curve_radius,
        ledger.forced_crossings,
    )
    obstacle_provenance: dict[str, CorridorCohortObstacleProvenance] = {}
    obstacle_provenance.update(
        {
            witness.footprint_id: CorridorCohortObstacleProvenance(
                witness.footprint_id,
                witness.member_id,
                witness.edge_key,
                witness.segment_rank,
                witness.connector_ids,
            )
            for witness in footprint_model.witnesses
            if witness.coordinate_variable_id is None
            and witness.start_variable_id is None
            and witness.end_variable_id is None
            and witness.crossing_disposition is CorridorCrossingDisposition.FIXED_DOGLEG
        }
    )
    witnesses_by_id = {
        witness.footprint_id: witness for witness in footprint_model.witnesses
    }
    for order in footprint_model.orders:
        fixed_term = next(
            (term for term in (order.lower, order.upper) if term.variable_id is None),
            None,
        )
        if fixed_term is None:
            continue
        witness = witnesses_by_id[fixed_term.witness_id]
        obstacle_provenance[order.owner_id] = CorridorCohortObstacleProvenance(
            order.owner_id,
            witness.member_id,
            witness.edge_key,
            witness.segment_rank,
            witness.connector_ids,
        )
    intervals_by_member: defaultdict[str, list[CorridorForbiddenInterval]] = (
        defaultdict(list)
    )
    for interval in footprint_model.forbidden_intervals:
        intervals_by_member[interval.member_id].append(interval)
    for request in scalar_requests:
        _domains, boundary_obstacle_sources = _scalar_boundary_domains(
            request, intervals_by_member
        )
        obstacle_provenance.update(
            {
                obstacle_id: replace(
                    obstacle_provenance[source_id],
                    obstacle_id=obstacle_id,
                )
                for obstacle_id, source_id in boundary_obstacle_sources.items()
                if source_id in obstacle_provenance
            }
        )
    physical = _physical_components(claims)
    logical = _atomic_components(claims, physical, footprint_model)
    components = [
        _component_plan(
            rank,
            spec,
            physical,
            claims,
            ledger,
            footprint_model,
            witnesses_by_id,
            scalar_requests_by_id,
            intervals_by_member,
            solve,
        )
        for rank, spec in enumerate(logical)
    ]
    represented = {
        cohort_id
        for component in components
        for cohort_id in component.endpoint_cohort_ids
    }
    for cohort_id in sorted(set(dict(ledger.endpoint_members)) - represented):
        components.append(
            CorridorCohortComponentPlan(
                f"corridor-component|{len(components)}",
                (cohort_id,),
                (),
                (),
                (),
                CorridorAllocationStatus.COMPATIBILITY,
                compatibility=CorridorCohortCompatibility(
                    CorridorCompatibilityReason.UNREPRESENTED_ENDPOINT_COHORT,
                    (),
                    (),
                    (cohort_id,),
                ),
            )
        )

    def resolved_shortfall(
        result: CorridorAllocationResult,
    ) -> tuple[
        CorridorClearanceShortfall | None,
        tuple[CorridorCohortObstacleProvenance, ...],
    ]:
        shortfall = result.clearance_shortfall
        if shortfall is None:
            return None, ()
        if shortfall.required_shift_sign not in (-1, 1):
            raise CorridorCohortCompilationError(
                "corridor clearance shortfall has no directed boundary side"
            )
        obstacle_ids = shortfall.blocking_obstacle_ids
        missing = tuple(
            obstacle_id
            for obstacle_id in obstacle_ids
            if obstacle_id not in obstacle_provenance
        )
        if missing:
            raise CorridorCohortCompilationError(
                "corridor clearance shortfall names unwitnessed obstacles: "
                f"{','.join(missing)}"
            )
        return shortfall, tuple(
            obstacle_provenance[obstacle_id] for obstacle_id in obstacle_ids
        )

    failure_records: list[CorridorCohortFailure] = []
    for component in components:
        if component.status is not CorridorAllocationStatus.FAILURE:
            continue
        component_failures = []
        for result_rank, result in enumerate(component.results):
            if (
                result.status is not CorridorAllocationStatus.FAILURE
                or result.reason is None
            ):
                continue
            shortfall, blocking_obstacles = resolved_shortfall(result)
            component_failures.append(
                CorridorCohortFailure(
                    component.component_id,
                    result_rank,
                    result.reason,
                    result.blocking_member_ids,
                    result.blocking_obstacle_ids,
                    result.blocking_equality_owner_ids,
                    result.blocking_endpoint_owner_ids,
                    shortfall,
                    blocking_obstacles,
                )
            )
        failure_records.extend(component_failures)
        if not component_failures:
            failure_records.append(
                CorridorCohortFailure(
                    component.component_id,
                    -1,
                    CorridorAllocationFailureReason.INVALID,
                    tuple(claim_id for claim_id, _role in component.claim_roles),
                    (),
                    (),
                    (),
                )
            )
    failures = tuple(failure_records)
    if failures:
        summary = "; ".join(
            f"{failure.component_id}/result:{failure.result_rank} "
            f"{failure.reason.value} "
            f"members={','.join(failure.blocking_member_ids) or '-'} "
            f"obstacles={','.join(failure.blocking_obstacle_ids) or '-'} "
            f"equalities={','.join(failure.blocking_equality_owner_ids) or '-'}"
            for failure in failures
        )
        raise CorridorCohortCompilationError(
            f"corridor cohort allocation failed: {summary}", failures
        )
    allocations = tuple(
        allocation
        for component in components
        if component.status is CorridorAllocationStatus.PLANNED
        for allocation in component.allocations
    )

    def scalar_grant(variable_id: str, coordinate: float) -> CorridorScalarGrant:
        request = scalar_requests_by_id[variable_id]
        recipe = request.control_recipe
        source_coordinate = (
            request.variable.coordinate if recipe is None else recipe.source_coordinate
        )
        return CorridorScalarGrant(
            variable_id,
            request.variable.owner_kind,
            request.variable.owner_id,
            coordinate,
            coordinate - source_coordinate,
            recipe,
        )

    scalar_grants = tuple(
        scalar_grant(variable_id, coordinate)
        for component in components
        for result in component.results
        for variable_id, coordinate in result.allocations
        if variable_id in scalar_requests_by_id
    )
    granted_ids = {grant.variable_id for grant in scalar_grants}
    compatibility_scalar_ids = {
        variable_id
        for component in components
        if component.compatibility is not None
        for variable_id in component.compatibility.scalar_variable_ids
    }
    ungranted = (set(scalar_requests_by_id) - granted_ids) - compatibility_scalar_ids
    if len(granted_ids) != len(scalar_grants) or ungranted:
        raise CorridorCohortCompilationError(
            "corridor scalar publication does not realize its complete grant"
        )
    landings = _planned_landings(claims, allocations)
    if ledger.finalized_owned_segments is not None:
        allocation_keys = {
            (item.member_id, item.edge_key, item.segment_rank) for item in allocations
        }
        allocation_keys.update(
            (item.member_id, item.edge_key, item.segment_rank) for item in landings
        )
        if allocation_keys != ledger.finalized_owned_segments:
            missing = ledger.finalized_owned_segments - allocation_keys
            added = allocation_keys - ledger.finalized_owned_segments
            raise CorridorCohortCompilationError(
                "finalized corridor cohort realization changed ownership: "
                f"missing={sorted(missing)}, added={sorted(added)}"
            )
    protected = tuple(
        sorted(
            {
                *(
                    item
                    for component in components
                    for item in component.protected_segments
                ),
                *((item.member_id, item.segment_rank) for item in landings),
            }
        )
    )
    patches = _prepare_patches(allocations, landings, targets)
    if patches is None:
        raise CorridorCohortCompilationError(
            "corridor cohort publication does not match its source snapshot"
        )
    finalized_allocations = _finalize_allocation_geometry(
        allocations,
        patches,
        targets,
    )
    if finalized_allocations is None:
        raise CorridorCohortCompilationError(
            "corridor cohort publication does not realize its complete grant"
        )
    finalized_by_claim = {
        allocation.claim_id: allocation for allocation in finalized_allocations
    }
    components = [
        replace(
            component,
            allocations=tuple(
                finalized_by_claim[allocation.claim_id]
                for allocation in component.allocations
            ),
        )
        for component in components
    ]
    ordered_grants = tuple(sorted(scalar_grants, key=lambda item: item.variable_id))
    solve_call_count = sum(len(component.problems) for component in components)
    trace = settlement_trace
    if trace is not None:
        fingerprint = repr(
            (
                tuple(
                    (allocation.claim_id, allocation.coordinate)
                    for allocation in sorted(
                        finalized_allocations, key=lambda item: item.claim_id
                    )
                ),
                tuple(
                    (grant.variable_id, grant.coordinate) for grant in ordered_grants
                ),
                solve_call_count,
            )
        )
        trace = register_settlement_stage(
            trace, SettlementStage.FINAL_SOLVE, geometry_fingerprint=fingerprint
        )
    return CorridorCohortPlan(
        tuple(components),
        finalized_allocations,
        protected,
        landings,
        ordered_grants,
        patches,
        trace,
    )


def publish_corridor_cohort_plan(plan: CorridorCohortPlan) -> None:
    """Apply a compiled plan's route patches, mutating its target routes.

    Publication is the only step that mutates geometry, kept distinct from the
    solve so a caller can inspect or discard a plan before committing it.
    """
    _commit_patches(plan.route_patches)
