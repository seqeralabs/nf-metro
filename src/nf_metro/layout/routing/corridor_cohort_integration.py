"""Owner-typed corridor records and the coordinate-free footprint relation graph.

These types and helpers describe corridor scalar ownership and the witness
evidence a corridor cohort compiles from one route snapshot, independent of
any solver. Nothing in :mod:`nf_metro.layout` constructs this module's
records yet; it ships ahead of the caller that will.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isclose, isfinite

from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS
from nf_metro.layout.route_reservations import CorridorOrientation, CorridorRegion
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    planner_owns_segment,
    segment_direction,
)
from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationFailureReason,
    CorridorAllocationProblem,
    CorridorAllocationResult,
    CorridorAllocationStatus,
    CorridorClearanceShortfall,
    CorridorCoordinateDomain,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.parser.route_topology import ConnectorId

CorridorCohortSegmentKey = tuple[str, tuple[str, str, str], int]
CorridorFootprintSegmentKey = tuple[str, tuple[str, str, str], int]


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
class CorridorCohortComponentPlan:
    component_id: str
    endpoint_cohort_ids: tuple[str, ...]
    claim_roles: tuple[tuple[str, CorridorCohortClaimRole], ...]
    problems: tuple[CorridorAllocationProblem, ...]
    results: tuple[CorridorAllocationResult, ...]
    status: CorridorAllocationStatus
    allocations: tuple[CorridorCohortAllocation, ...] = ()
    protected_segments: tuple[tuple[str, int], ...] = ()


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


def _bind_claim(
    claim: CorridorCohortLedgerClaim,
    target: CorridorCohortTarget,
) -> _BoundClaim:
    """Match one ledger claim against its current route, or reject it.

    Validates identity (edge, family, connectors, segment bounds) and current
    segment orientation only; it does not resolve an endpoint's landing
    coordinate, so ``landing_coordinate`` is always ``None`` on the result.
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
    longitudinal_start, longitudinal_end = sorted(
        (start[longitudinal_axis], end[longitudinal_axis])
    )
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
        None,
    )


def _bind_ledger(
    ledger: CorridorCohortLedger,
    targets: Sequence[CorridorCohortTarget],
) -> tuple[_BoundClaim, ...]:
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
            bound.append(_bind_claim(claim, matches[0]))
        except CorridorCohortCompilationError:
            if claim.reservation_complete:
                raise
    return tuple(bound)


def _directly_movable(claim: _BoundClaim) -> bool:
    """Whether normalisation is free to relocate *claim*'s current segment."""
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
    for point in recipe.controlled_points:
        target = targets_by_identity.get((point.member_id, point.edge_key))
        point_key = (
            point.member_id,
            point.edge_key,
            point.point_rank,
            point.axis,
        )
        if (
            target is None
            or target.connector_ids != point.connector_ids
            or point.axis not in (0, 1)
            or not 0 <= point.point_rank < len(target.route.points)
            or not isfinite(point.source_offset)
            or not isclose(
                target.route.points[point.point_rank][point.axis],
                recipe.source_coordinate + point.source_offset,
                abs_tol=COORD_TOLERANCE,
            )
            or point.role_id in role_ids
            or point_key in point_keys
        ):
            raise CorridorCohortCompilationError(
                f"corridor scalar request {request.variable.variable_id} has an "
                "invalid controlled point"
            )
        role_ids.add(point.role_id)
        point_keys.add(point_key)
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


def _member_footprint_model(
    claims: tuple[_BoundClaim, ...],
    targets: Sequence[CorridorCohortTarget],
    scalar_requests: Sequence[CorridorScalarRequest],
    offset_step: float,
) -> _MemberFootprintModel:
    """Build the witness/relation graph one member population publishes.

    Produces ownership (``orders``) and shared-network (``contacts``)
    relations between footprints; it does not lower them into forbidden
    coordinate intervals.
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
        if _directly_movable(claim):
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

    return _MemberFootprintModel(
        tuple(variables),
        witnesses,
        claim_ids_by_variable,
        tuple(orders[key] for key in sorted(orders)),
        tuple(contacts[key] for key in sorted(contacts)),
    )
