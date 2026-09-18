"""Pure, deterministic allocation of rigid corridor-lane cohorts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import isfinite

_ObstacleInterval = tuple[
    Fraction,
    Fraction,
    set[str],
    tuple[tuple[int, ...], ...],
]


class _NoCoordinateInsideBounds(Exception):
    def __init__(
        self,
        root: int,
        obstacle_ids: set[str] | None = None,
        deficit: Fraction | None = None,
    ) -> None:
        self.root = root
        self.obstacle_ids = obstacle_ids or set()
        self.deficit = deficit


class CorridorAllocationStatus(Enum):
    PLANNED = "planned"
    COMPATIBILITY = "compatibility"
    FAILURE = "failure"


class CorridorAllocationFailureReason(Enum):
    CONTRADICTION = "contradiction"
    INFEASIBLE = "infeasible"
    INVALID = "invalid"


@dataclass(frozen=True)
class CorridorLane:
    member_id: str
    cohort_id: str
    endpoint_owner_id: str
    boundary_coordinate: float
    planned_coordinate: float
    span_start: float
    span_end: float
    semantic_rank: tuple[int, ...]


@dataclass(frozen=True)
class CorridorObstacle:
    obstacle_id: str
    order_coordinate: float
    realised_coordinate: float
    span_start: float
    span_end: float
    semantic_rank: tuple[int, ...]


@dataclass(frozen=True)
class CorridorEquality:
    owner_id: str
    left_member_id: str
    right_member_id: str
    delta: float


@dataclass(frozen=True)
class CorridorFixedEquality:
    owner_id: str
    member_id: str
    obstacle_id: str
    delta: float = 0.0


@dataclass(frozen=True)
class CorridorSeparation:
    left_member_id: str
    right_member_id: str
    distance: float


@dataclass(frozen=True)
class CorridorDirectedSeparation:
    """Require one raw coordinate to remain above another by a fixed distance."""

    owner_id: str
    lower_member_id: str
    upper_member_id: str
    distance: float


@dataclass(frozen=True)
class CorridorCoordinateDomain:
    member_id: str
    minimum_coordinate: float | None = None
    maximum_coordinate: float | None = None
    obstacle_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorridorForbiddenInterval:
    """A named open interval unavailable to one member coordinate."""

    member_id: str
    obstacle_id: str
    minimum_coordinate: float
    maximum_coordinate: float
    semantic_rank: tuple[int, ...]


@dataclass(frozen=True)
class CorridorClearanceShortfall:
    claim_ids: tuple[str, ...]
    blocking_obstacle_ids: tuple[str, ...]
    deficit: float
    axis: int
    required_shift_sign: int


@dataclass(frozen=True)
class CorridorAllocationProblem:
    """A complete preclosed endpoint, equality, and clearance component."""

    lanes: tuple[CorridorLane, ...]
    obstacles: tuple[CorridorObstacle, ...] = ()
    equalities: tuple[CorridorEquality, ...] = ()
    separations: tuple[CorridorSeparation, ...] = ()
    domains: tuple[CorridorCoordinateDomain, ...] = ()
    fixed_equalities: tuple[CorridorFixedEquality, ...] = ()
    directed_separations: tuple[CorridorDirectedSeparation, ...] = ()
    forbidden_intervals: tuple[CorridorForbiddenInterval, ...] = ()
    clearance: float = 4.0
    witnesses_complete: bool = True
    axis_sign: int = 1
    coordinate_axis: int = 0


@dataclass(frozen=True)
class CorridorAllocationResult:
    status: CorridorAllocationStatus
    allocations: tuple[tuple[str, float], ...] = ()
    reason: CorridorAllocationFailureReason | None = None
    blocking_member_ids: tuple[str, ...] = ()
    blocking_obstacle_ids: tuple[str, ...] = ()
    blocking_equality_owner_ids: tuple[str, ...] = ()
    blocking_order_owner_ids: tuple[str, ...] = ()
    blocking_endpoint_owner_ids: tuple[str, ...] = ()
    clearance_shortfall: CorridorClearanceShortfall | None = None


def _q(value: float) -> Fraction:
    return Fraction(str(value))


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def _lower_median(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _failure(
    reason: CorridorAllocationFailureReason,
    *,
    members: tuple[str, ...] | list[str] | set[str] = (),
    obstacles: tuple[str, ...] | list[str] | set[str] = (),
    equality_owners: tuple[str, ...] | list[str] | set[str] = (),
    order_owners: tuple[str, ...] | list[str] | set[str] = (),
    endpoint_owners: tuple[str, ...] | list[str] | set[str] = (),
    clearance_shortfall: CorridorClearanceShortfall | None = None,
) -> CorridorAllocationResult:
    return CorridorAllocationResult(
        CorridorAllocationStatus.FAILURE,
        reason=reason,
        blocking_member_ids=tuple(sorted(set(members))),
        blocking_obstacle_ids=tuple(sorted(set(obstacles))),
        blocking_equality_owner_ids=tuple(sorted(set(equality_owners))),
        blocking_order_owner_ids=tuple(sorted(set(order_owners))),
        blocking_endpoint_owner_ids=tuple(sorted(set(endpoint_owners))),
        clearance_shortfall=clearance_shortfall,
    )


class _WeightedUnionFind:
    """Exact union-find with potentials relative to canonical member roots."""

    def __init__(self, keys: list[tuple[tuple[int, ...], Fraction, Fraction]]) -> None:
        self.parent = list(range(len(keys)))
        self.weight = [Fraction(0)] * len(keys)
        self.keys = keys

    def find(self, item: int) -> tuple[int, Fraction]:
        parent = self.parent[item]
        if parent == item:
            return item, Fraction(0)
        root, parent_weight = self.find(parent)
        self.weight[item] += parent_weight
        self.parent[item] = root
        return root, self.weight[item]

    def union(self, left: int, right: int, delta: Fraction) -> bool:
        """Require coordinate(right) - coordinate(left) == delta."""
        left_root, left_weight = self.find(left)
        right_root, right_weight = self.find(right)
        if left_root == right_root:
            return right_weight - left_weight == delta
        if self.keys[left_root] <= self.keys[right_root]:
            self.parent[right_root] = left_root
            self.weight[right_root] = delta + left_weight - right_weight
        else:
            self.parent[left_root] = right_root
            self.weight[left_root] = right_weight - left_weight - delta
        return True


def _invalid_problem(
    problem: CorridorAllocationProblem,
) -> CorridorAllocationResult | None:
    lanes = problem.lanes
    member_ids = [lane.member_id for lane in lanes]
    numeric = [problem.clearance]
    for lane in lanes:
        numeric.extend(
            (
                lane.boundary_coordinate,
                lane.planned_coordinate,
                lane.span_start,
                lane.span_end,
            )
        )
    for obstacle in problem.obstacles:
        numeric.extend(
            (
                obstacle.order_coordinate,
                obstacle.realised_coordinate,
                obstacle.span_start,
                obstacle.span_end,
            )
        )
    numeric.extend(equality.delta for equality in problem.equalities)
    numeric.extend(equality.delta for equality in problem.fixed_equalities)
    numeric.extend(separation.distance for separation in problem.separations)
    numeric.extend(separation.distance for separation in problem.directed_separations)
    numeric.extend(
        coordinate
        for domain in problem.domains
        for coordinate in (domain.minimum_coordinate, domain.maximum_coordinate)
        if coordinate is not None
    )
    numeric.extend(
        coordinate
        for interval in problem.forbidden_intervals
        for coordinate in (interval.minimum_coordinate, interval.maximum_coordinate)
    )
    invalid_members = {
        lane.member_id
        for lane in lanes
        if not lane.member_id
        or not lane.cohort_id
        or not lane.endpoint_owner_id
        or not lane.semantic_rank
    }
    duplicate_members = {
        member_id for member_id in member_ids if member_ids.count(member_id) > 1
    }
    invalid_obstacles = {
        obstacle.obstacle_id
        for obstacle in problem.obstacles
        if not obstacle.obstacle_id or not obstacle.semantic_rank
    }
    cohort_endpoint_owners: dict[str, set[str]] = {}
    for lane in lanes:
        cohort_endpoint_owners.setdefault(lane.cohort_id, set()).add(
            lane.endpoint_owner_id
        )
    split_cohorts = {
        cohort_id
        for cohort_id, endpoint_owners in cohort_endpoint_owners.items()
        if len(endpoint_owners) != 1
    }
    split_members = {
        lane.member_id for lane in lanes if lane.cohort_id in split_cohorts
    }
    split_endpoint_owners = {
        lane.endpoint_owner_id for lane in lanes if lane.cohort_id in split_cohorts
    }
    obstacle_ids = [obstacle.obstacle_id for obstacle in problem.obstacles]
    duplicate_obstacles = {
        obstacle_id
        for obstacle_id in obstacle_ids
        if obstacle_ids.count(obstacle_id) > 1
    }
    by_id = set(member_ids)
    all_ids = by_id | set(obstacle_ids)
    invalid_equalities = {
        equality.owner_id
        for equality in problem.equalities
        if not equality.owner_id
        or equality.left_member_id not in by_id
        or equality.right_member_id not in by_id
    }
    fixed_equality_pairs = [
        (equality.member_id, equality.obstacle_id)
        for equality in problem.fixed_equalities
    ]
    invalid_fixed_equalities = {
        equality.owner_id
        for equality in problem.fixed_equalities
        if not equality.owner_id
        or equality.member_id not in by_id
        or equality.obstacle_id not in set(obstacle_ids)
        or fixed_equality_pairs.count((equality.member_id, equality.obstacle_id)) > 1
    }
    separation_pairs = [
        frozenset((separation.left_member_id, separation.right_member_id))
        for separation in problem.separations
    ]
    invalid_separations = {
        member_id
        for separation, pair in zip(problem.separations, separation_pairs, strict=True)
        if (
            len(pair) != 2
            or not pair.issubset(all_ids)
            or not pair.intersection(by_id)
            or separation.distance < 0
            or separation_pairs.count(pair) > 1
        )
        for member_id in pair
    }
    invalid_directed_separations = {
        separation.owner_id
        for separation in problem.directed_separations
        if not separation.owner_id
        or separation.lower_member_id not in by_id
        or separation.upper_member_id not in by_id
        or separation.lower_member_id == separation.upper_member_id
        or separation.distance < 0
    }
    invalid_directed_members = {
        member_id
        for separation in problem.directed_separations
        if separation.owner_id in invalid_directed_separations
        for member_id in (separation.lower_member_id, separation.upper_member_id)
        if member_id in by_id
    }
    invalid_domains = {
        domain.member_id
        for domain in problem.domains
        if domain.member_id not in by_id
        or (
            domain.minimum_coordinate is not None
            and domain.maximum_coordinate is not None
            and domain.minimum_coordinate > domain.maximum_coordinate
        )
    }
    invalid_forbidden_intervals = {
        interval.obstacle_id
        for interval in problem.forbidden_intervals
        if not interval.obstacle_id
        or interval.member_id not in by_id
        or interval.obstacle_id in by_id
        or interval.minimum_coordinate >= interval.maximum_coordinate
        or not interval.semantic_rank
    }
    invalid_forbidden_members = {
        interval.member_id
        for interval in problem.forbidden_intervals
        if interval.member_id not in by_id
        or interval.obstacle_id in invalid_forbidden_intervals
    }
    invalid_shape = (
        problem.clearance < 0
        or problem.axis_sign not in (-1, 1)
        or problem.coordinate_axis not in (0, 1)
        or not all(isfinite(value) for value in numeric)
        or any(lane.span_start > lane.span_end for lane in lanes)
        or any(
            obstacle.span_start > obstacle.span_end for obstacle in problem.obstacles
        )
    )
    if (
        invalid_members
        or duplicate_members
        or invalid_obstacles
        or duplicate_obstacles
        or invalid_equalities
        or invalid_fixed_equalities
        or invalid_separations
        or invalid_directed_separations
        or invalid_domains
        or invalid_forbidden_intervals
        or by_id.intersection(obstacle_ids)
        or split_cohorts
        or invalid_shape
    ):
        return _failure(
            CorridorAllocationFailureReason.INVALID,
            members=(
                invalid_members
                | duplicate_members
                | split_members
                | invalid_separations.intersection(by_id)
                | invalid_directed_members
                | invalid_domains.intersection(by_id)
                | invalid_forbidden_members.intersection(by_id)
                | by_id.intersection(obstacle_ids)
            ),
            obstacles=(
                invalid_obstacles
                | duplicate_obstacles
                | invalid_separations.intersection(obstacle_ids)
                | invalid_forbidden_intervals
                | by_id.intersection(obstacle_ids)
            ),
            equality_owners=invalid_equalities | invalid_fixed_equalities,
            order_owners=invalid_directed_separations,
            endpoint_owners={
                lane.endpoint_owner_id
                for lane in lanes
                if lane.member_id in invalid_members | duplicate_members
            }
            | split_endpoint_owners,
        )
    return None


def _equality_peer_owners(
    equalities: tuple[CorridorEquality, ...], by_id: dict[str, int]
) -> dict[frozenset[int], set[str]]:
    adjacency: dict[str, dict[int, set[int]]] = {}
    for equality in equalities:
        left = by_id[equality.left_member_id]
        right = by_id[equality.right_member_id]
        owner_graph = adjacency.setdefault(equality.owner_id, {})
        owner_graph.setdefault(left, set()).add(right)
        owner_graph.setdefault(right, set()).add(left)
    peers: dict[frozenset[int], set[str]] = {}
    for owner, graph in adjacency.items():
        unseen = set(graph)
        while unseen:
            pending = [min(unseen)]
            component: set[int] = set()
            while pending:
                member = pending.pop()
                if member in component:
                    continue
                component.add(member)
                pending.extend(graph[member] - component)
            unseen -= component
            for left in component:
                for right in component:
                    if left < right:
                        peers.setdefault(frozenset((left, right)), set()).add(owner)
    return peers


def _contradiction_attribution(
    lanes: tuple[CorridorLane, ...],
    equalities: tuple[CorridorEquality, ...],
    union_find: _WeightedUnionFind,
    equality: CorridorEquality,
) -> CorridorAllocationResult:
    left = next(
        i for i, lane in enumerate(lanes) if lane.member_id == equality.left_member_id
    )
    root, _ = union_find.find(left)
    members = {
        lane.member_id
        for index, lane in enumerate(lanes)
        if union_find.find(index)[0] == root
    }
    owners = {
        item.owner_id
        for item in equalities
        if item.left_member_id in members and item.right_member_id in members
    }
    owners.add(equality.owner_id)
    return _failure(
        CorridorAllocationFailureReason.CONTRADICTION,
        members=members,
        equality_owners=owners,
        endpoint_owners={
            lane.endpoint_owner_id for lane in lanes if lane.member_id in members
        },
    )


def _ordered_roots_or_cycle(
    roots: set[int],
    edges: dict[tuple[int, int], Fraction],
    root_key: dict[int, tuple[tuple[int, ...], tuple[tuple[int, ...], ...], Fraction]],
) -> tuple[tuple[int, ...], frozenset[int]]:
    successors: dict[int, set[int]] = {root: set() for root in roots}
    indegree = {root: 0 for root in roots}
    for before, after in edges:
        if after not in successors[before]:
            successors[before].add(after)
            indegree[after] += 1
    ready = sorted(
        (root for root, count in indegree.items() if count == 0),
        key=root_key.__getitem__,
    )
    ordered: list[int] = []
    while ready:
        root = ready.pop(0)
        ordered.append(root)
        for after in sorted(successors[root], key=root_key.__getitem__):
            indegree[after] -= 1
            if indegree[after] == 0:
                ready.append(after)
                ready.sort(key=root_key.__getitem__)
    if len(ordered) == len(roots):
        return tuple(ordered), frozenset()

    unresolved = set(roots) - set(ordered)

    def reaches_self(start: int) -> bool:
        pending = list(successors[start])
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current == start:
                return True
            if current in visited or current not in unresolved:
                continue
            visited.add(current)
            pending.extend(successors[current])
        return False

    return (), frozenset(root for root in unresolved if reaches_self(root))


def solve_corridor_cohorts(  # noqa: C901, PLR0915
    problem: CorridorAllocationProblem,
) -> CorridorAllocationResult:
    """Return the unique canonical seating for a complete witness set."""
    if not problem.witnesses_complete:
        return CorridorAllocationResult(CorridorAllocationStatus.COMPATIBILITY)
    invalid = _invalid_problem(problem)
    if invalid is not None:
        return invalid
    if not problem.lanes:
        return CorridorAllocationResult(CorridorAllocationStatus.PLANNED)

    sign = problem.axis_sign
    lanes = problem.lanes
    by_id = {lane.member_id: index for index, lane in enumerate(lanes)}
    canonical_member_keys = [
        (
            lane.semantic_rank,
            sign * _q(lane.boundary_coordinate),
            sign * _q(lane.planned_coordinate),
        )
        for lane in lanes
    ]
    union_find = _WeightedUnionFind(canonical_member_keys)
    clearance_by_pair = {
        frozenset((separation.left_member_id, separation.right_member_id)): _q(
            separation.distance
        )
        for separation in problem.separations
    }

    def required_clearance(left_id: str, right_id: str) -> Fraction:
        return clearance_by_pair.get(
            frozenset((left_id, right_id)), _q(problem.clearance)
        )

    cohorts: dict[str, list[int]] = {}
    for index, lane in enumerate(lanes):
        cohorts.setdefault(lane.cohort_id, []).append(index)
    for members in cohorts.values():
        anchor = min(members, key=lambda index: canonical_member_keys[index])
        anchor_boundary = sign * _q(lanes[anchor].boundary_coordinate)
        for member in members:
            delta = sign * _q(lanes[member].boundary_coordinate) - anchor_boundary
            if not union_find.union(anchor, member, delta):
                return _failure(
                    CorridorAllocationFailureReason.CONTRADICTION,
                    members={lanes[index].member_id for index in members},
                    endpoint_owners={
                        lanes[index].endpoint_owner_id for index in members
                    },
                )

    processed_equalities: list[CorridorEquality] = []
    for equality in sorted(
        problem.equalities,
        key=lambda item: (
            item.owner_id,
            item.left_member_id,
            item.right_member_id,
            item.delta,
        ),
    ):
        left = by_id[equality.left_member_id]
        right = by_id[equality.right_member_id]
        if not union_find.union(left, right, sign * _q(equality.delta)):
            return _contradiction_attribution(
                lanes,
                tuple((*processed_equalities, equality)),
                union_find,
                equality,
            )
        processed_equalities.append(equality)

    roots: dict[int, list[int]] = {}
    raw_potentials: dict[int, Fraction] = {}
    for index in range(len(lanes)):
        root, potential = union_find.find(index)
        roots.setdefault(root, []).append(index)
        raw_potentials[index] = potential

    # Rebase every component to its canonical semantic member.  This removes
    # union history and input order from preferred bases and fixed-side choices.
    potentials: dict[int, Fraction] = {}
    component_reference: dict[int, int] = {}
    for root, members in roots.items():
        reference = min(members, key=lambda index: canonical_member_keys[index])
        component_reference[root] = reference
        reference_potential = raw_potentials[reference]
        for member in members:
            potentials[member] = raw_potentials[member] - reference_potential

    preferred = {
        root: _lower_median(
            [
                sign * _q(lanes[index].planned_coordinate) - potentials[index]
                for index in members
            ]
        )
        for root, members in roots.items()
    }
    root_key = {
        root: (
            lanes[component_reference[root]].semantic_rank,
            tuple(sorted(lanes[index].semantic_rank for index in members)),
            preferred[root],
        )
        for root, members in roots.items()
    }
    peer_owners = _equality_peer_owners(problem.equalities, by_id)

    domain_floor: dict[int, Fraction] = {}
    domain_floor_obstacles: dict[int, set[str]] = {}
    domain_ceiling: dict[int, Fraction] = {}
    domain_ceiling_obstacles: dict[int, set[str]] = {}
    domain_obstacles: dict[int, set[str]] = {}
    obstacle_shift_signs: defaultdict[str, set[int]] = defaultdict(set)

    def tighten_floor(root: int, value: Fraction, obstacle_ids: set[str]) -> None:
        current = domain_floor.get(root)
        if current is None or value > current:
            domain_floor[root] = value
            domain_floor_obstacles[root] = set(obstacle_ids)
        elif value == current:
            domain_floor_obstacles.setdefault(root, set()).update(obstacle_ids)

    def tighten_ceiling(root: int, value: Fraction, obstacle_ids: set[str]) -> None:
        current = domain_ceiling.get(root)
        if current is None or value < current:
            domain_ceiling[root] = value
            domain_ceiling_obstacles[root] = set(obstacle_ids)
        elif value == current:
            domain_ceiling_obstacles.setdefault(root, set()).update(obstacle_ids)

    for domain in problem.domains:
        index = by_id[domain.member_id]
        root, _ = union_find.find(index)
        obstacle_ids = set(domain.obstacle_ids)
        minimum = (
            None
            if domain.minimum_coordinate is None
            else sign * _q(domain.minimum_coordinate) - potentials[index]
        )
        maximum = (
            None
            if domain.maximum_coordinate is None
            else sign * _q(domain.maximum_coordinate) - potentials[index]
        )
        if sign < 0:
            minimum, maximum = maximum, minimum
        if minimum is not None:
            tighten_floor(root, minimum, obstacle_ids)
        if maximum is not None:
            tighten_ceiling(root, maximum, obstacle_ids)
        domain_obstacles.setdefault(root, set()).update(obstacle_ids)
        if obstacle_ids:
            if domain.minimum_coordinate is not None:
                for obstacle_id in obstacle_ids:
                    obstacle_shift_signs[obstacle_id].add(-1)
            if domain.maximum_coordinate is not None:
                for obstacle_id in obstacle_ids:
                    obstacle_shift_signs[obstacle_id].add(1)

    obstacles_by_id = {obstacle.obstacle_id: obstacle for obstacle in problem.obstacles}
    fixed_peer_members: defaultdict[str, set[int]] = defaultdict(set)
    fixed_root_values: defaultdict[int, list[tuple[Fraction, str, str]]] = defaultdict(
        list
    )
    fixed_root_owners: defaultdict[int, set[str]] = defaultdict(set)
    for fixed_equality in problem.fixed_equalities:
        index = by_id[fixed_equality.member_id]
        root, _ = union_find.find(index)
        obstacle = obstacles_by_id[fixed_equality.obstacle_id]
        value = (
            sign * (_q(obstacle.realised_coordinate) + _q(fixed_equality.delta))
            - potentials[index]
        )
        fixed_root_values[root].append(
            (value, fixed_equality.owner_id, fixed_equality.obstacle_id)
        )
        fixed_root_owners[root].add(fixed_equality.owner_id)
        fixed_peer_members[fixed_equality.obstacle_id].add(index)

    for root, values in fixed_root_values.items():
        fixed_coordinates = {value for value, _owner, _obstacle in values}
        if len(fixed_coordinates) != 1:
            return _failure(
                CorridorAllocationFailureReason.INFEASIBLE,
                members={lanes[index].member_id for index in roots[root]},
                obstacles={obstacle for _value, _owner, obstacle in values},
                equality_owners={owner for _value, owner, _obstacle in values},
                endpoint_owners={
                    lanes[index].endpoint_owner_id for index in roots[root]
                },
            )
        coordinate = next(iter(fixed_coordinates))
        tighten_floor(root, coordinate, set())
        tighten_ceiling(root, coordinate, set())

    def clearance_shortfall(
        root: int,
        deficit: Fraction | None,
        obstacle_ids: set[str],
    ) -> CorridorClearanceShortfall | None:
        boundary_obstacle_ids = {
            obstacle_id
            for obstacle_id in obstacle_ids
            if obstacle_id in obstacle_shift_signs
        }
        signs = {
            shift_sign
            for obstacle_id in boundary_obstacle_ids
            for shift_sign in obstacle_shift_signs[obstacle_id]
        }
        if (
            deficit is None
            or deficit <= 0
            or not boundary_obstacle_ids
            or len(signs) != 1
        ):
            return None
        return CorridorClearanceShortfall(
            tuple(sorted(lanes[index].member_id for index in roots[root])),
            tuple(sorted(boundary_obstacle_ids)),
            float(deficit),
            problem.coordinate_axis,
            next(iter(signs)),
        )

    conflicting_domain_roots = {
        root
        for root in roots
        if root in domain_floor
        and root in domain_ceiling
        and domain_floor[root] > domain_ceiling[root]
    }
    if conflicting_domain_roots:
        shortfall_root = min(conflicting_domain_roots)
        return _failure(
            CorridorAllocationFailureReason.INFEASIBLE,
            members={
                lanes[index].member_id
                for root in conflicting_domain_roots
                for index in roots[root]
            },
            obstacles={
                obstacle_id
                for root in conflicting_domain_roots
                for obstacle_id in domain_obstacles.get(root, ())
            },
            equality_owners={
                owner
                for root in conflicting_domain_roots
                for owner in fixed_root_owners.get(root, ())
            },
            clearance_shortfall=(
                clearance_shortfall(
                    shortfall_root,
                    domain_floor[shortfall_root] - domain_ceiling[shortfall_root],
                    domain_floor_obstacles.get(shortfall_root, set())
                    | domain_ceiling_obstacles.get(shortfall_root, set()),
                )
                if len(conflicting_domain_roots) == 1
                else None
            ),
        )

    exclusion_intervals_by_root: dict[int, tuple[_ObstacleInterval, ...]] = {}
    edges: dict[tuple[int, int], Fraction] = {}
    edge_order_owners: defaultdict[tuple[int, int], set[str]] = defaultdict(set)

    for directed_separation in sorted(
        problem.directed_separations,
        key=lambda item: (
            item.owner_id,
            item.lower_member_id,
            item.upper_member_id,
            item.distance,
        ),
    ):
        lower_index = by_id[directed_separation.lower_member_id]
        upper_index = by_id[directed_separation.upper_member_id]
        lower_root, _ = union_find.find(lower_index)
        upper_root, _ = union_find.find(upper_index)
        required = _q(directed_separation.distance)
        if lower_root == upper_root:
            raw_delta = sign * (potentials[upper_index] - potentials[lower_index])
            if raw_delta < required:
                return _failure(
                    CorridorAllocationFailureReason.INFEASIBLE,
                    members={lanes[index].member_id for index in roots[lower_root]},
                    equality_owners=fixed_root_owners.get(lower_root, set()),
                    order_owners=(directed_separation.owner_id,),
                    endpoint_owners={
                        lanes[index].endpoint_owner_id for index in roots[lower_root]
                    },
                )
            continue
        if sign > 0:
            before, after = lower_root, upper_root
            root_distance = required + potentials[lower_index] - potentials[upper_index]
        else:
            before, after = upper_root, lower_root
            root_distance = required + potentials[upper_index] - potentials[lower_index]
        edge = before, after
        edges[edge] = max(edges.get(edge, root_distance), root_distance)
        edge_order_owners[edge].add(directed_separation.owner_id)

    forbidden_by_root: defaultdict[int, list[_ObstacleInterval]] = defaultdict(list)
    for interval in problem.forbidden_intervals:
        index = by_id[interval.member_id]
        root, _ = union_find.find(index)
        if sign > 0:
            before_bound = _q(interval.minimum_coordinate) - potentials[index]
            after_bound = _q(interval.maximum_coordinate) - potentials[index]
        else:
            before_bound = -_q(interval.maximum_coordinate) - potentials[index]
            after_bound = -_q(interval.minimum_coordinate) - potentials[index]
        forbidden_by_root[root].append(
            (
                before_bound,
                after_bound,
                {interval.obstacle_id},
                (interval.semantic_rank,),
            )
        )

    for root, members in roots.items():
        exclusion_intervals = list(forbidden_by_root.get(root, ()))
        for obstacle in problem.obstacles:
            obstacle_coordinate = sign * _q(obstacle.realised_coordinate)
            for index in members:
                lane = lanes[index]
                if index in fixed_peer_members[obstacle.obstacle_id] or not _overlaps(
                    lane.span_start,
                    lane.span_end,
                    obstacle.span_start,
                    obstacle.span_end,
                ):
                    continue
                clearance = required_clearance(lane.member_id, obstacle.obstacle_id)
                exclusion_intervals.append(
                    (
                        obstacle_coordinate - clearance - potentials[index],
                        obstacle_coordinate + clearance - potentials[index],
                        {obstacle.obstacle_id},
                        (obstacle.semantic_rank,),
                    )
                )

        merged_intervals: list[_ObstacleInterval] = []
        for obstacle_interval in sorted(
            exclusion_intervals, key=lambda item: (item[0], item[1])
        ):
            if not merged_intervals or obstacle_interval[0] >= merged_intervals[-1][1]:
                merged_intervals.append(obstacle_interval)
                continue
            (
                before_bound,
                after_bound,
                obstacle_ids,
                obstacle_ranks,
            ) = merged_intervals[-1]
            merged_intervals[-1] = (
                before_bound,
                max(after_bound, obstacle_interval[1]),
                obstacle_ids | obstacle_interval[2],
                tuple(sorted((*obstacle_ranks, *obstacle_interval[3]))),
            )

        exclusion_intervals_by_root[root] = tuple(merged_intervals)

    for left_index, left_lane in enumerate(lanes):
        left_root, _ = union_find.find(left_index)
        for right_index in range(left_index + 1, len(lanes)):
            right_lane = lanes[right_index]
            if not _overlaps(
                left_lane.span_start,
                left_lane.span_end,
                right_lane.span_start,
                right_lane.span_end,
            ):
                continue
            pair = frozenset((left_index, right_index))
            if pair in peer_owners:
                continue
            right_root, _ = union_find.find(right_index)
            if left_root == right_root:
                if abs(
                    potentials[right_index] - potentials[left_index]
                ) < required_clearance(left_lane.member_id, right_lane.member_id):
                    return _failure(
                        CorridorAllocationFailureReason.INFEASIBLE,
                        members=(left_lane.member_id, right_lane.member_id),
                        equality_owners=(
                            fixed_root_owners.get(left_root, set())
                            | fixed_root_owners.get(right_root, set())
                        ),
                        endpoint_owners=(
                            left_lane.endpoint_owner_id,
                            right_lane.endpoint_owner_id,
                        ),
                    )
                continue
            if root_key[left_root] == root_key[right_root]:
                return _failure(
                    CorridorAllocationFailureReason.INVALID,
                    members=(left_lane.member_id, right_lane.member_id),
                    endpoint_owners=(
                        left_lane.endpoint_owner_id,
                        right_lane.endpoint_owner_id,
                    ),
                )
            directed_edge = (
                (left_root, right_root)
                if (left_root, right_root) in edge_order_owners
                else (right_root, left_root)
                if (right_root, left_root) in edge_order_owners
                else None
            )
            if directed_edge == (left_root, right_root) or (
                directed_edge is None and root_key[left_root] <= root_key[right_root]
            ):
                before, after = left_root, right_root
                lane_separation = (
                    required_clearance(left_lane.member_id, right_lane.member_id)
                    + potentials[left_index]
                    - potentials[right_index]
                )
            else:
                before, after = right_root, left_root
                lane_separation = (
                    required_clearance(left_lane.member_id, right_lane.member_id)
                    + potentials[right_index]
                    - potentials[left_index]
                )
            edge = (before, after)
            edges[edge] = max(edges.get(edge, lane_separation), lane_separation)

    order, cyclic_roots = _ordered_roots_or_cycle(set(roots), edges, root_key)
    if cyclic_roots:
        return _failure(
            CorridorAllocationFailureReason.INFEASIBLE,
            members={
                lanes[index].member_id for root in cyclic_roots for index in roots[root]
            },
            equality_owners={
                owner
                for root in cyclic_roots
                for owner in fixed_root_owners.get(root, set())
            },
            order_owners={
                owner
                for (before, after), owners in edge_order_owners.items()
                if before in cyclic_roots and after in cyclic_roots
                for owner in owners
            },
            endpoint_owners={
                lanes[index].endpoint_owner_id
                for root in cyclic_roots
                for index in roots[root]
            },
        )

    successors: dict[int, list[tuple[int, Fraction]]] = {root: [] for root in roots}
    predecessors: dict[int, list[tuple[int, Fraction]]] = {root: [] for root in roots}
    for (before, after), edge_distance in edges.items():
        successors[before].append((after, edge_distance))
        predecessors[after].append((before, edge_distance))

    def closest_allowed(
        root: int,
        floor: Fraction | None = None,
        ceiling: Fraction | None = None,
        floor_obstacle_ids: set[str] | None = None,
        ceiling_obstacle_ids: set[str] | None = None,
    ) -> Fraction:
        if floor is not None and ceiling is not None and floor > ceiling:
            raise _NoCoordinateInsideBounds(
                root,
                (floor_obstacle_ids or set()) | (ceiling_obstacle_ids or set()),
                floor - ceiling,
            )
        coordinate = preferred[root]
        if floor is not None:
            coordinate = max(coordinate, floor)
        if ceiling is not None:
            coordinate = min(coordinate, ceiling)
        component_semantic_key = (
            lanes[component_reference[root]].semantic_rank,
            tuple(sorted(lanes[index].member_id for index in roots[root])),
        )
        for (
            before_bound,
            after_bound,
            obstacle_ids,
            obstacle_ranks,
        ) in exclusion_intervals_by_root[root]:
            if not before_bound < coordinate < after_bound:
                continue
            before_feasible = floor is None or floor <= before_bound
            after_feasible = ceiling is None or after_bound <= ceiling
            if before_feasible and after_feasible:
                before_distance = abs(preferred[root] - before_bound)
                after_distance = abs(after_bound - preferred[root])
                choose_before = before_distance < after_distance or (
                    before_distance == after_distance
                    and component_semantic_key
                    < (min(obstacle_ranks), tuple(sorted(obstacle_ids)))
                )
                if choose_before:
                    coordinate = before_bound
                    break
            elif before_feasible:
                coordinate = before_bound
                break
            if after_feasible:
                coordinate = after_bound
                continue
            deficit_candidates = (
                (
                    None if floor is None else floor - before_bound,
                    floor_obstacle_ids or set(),
                ),
                (
                    None if ceiling is None else after_bound - ceiling,
                    ceiling_obstacle_ids or set(),
                ),
            )
            deficits = tuple(
                (deficit, bound_obstacle_ids)
                for deficit, bound_obstacle_ids in deficit_candidates
                if deficit is not None and deficit > 0
            )
            deficit = min((item for item, _ids in deficits), default=None)
            boundary_obstacles = {
                obstacle_id
                for item, bound_obstacle_ids in deficits
                if item == deficit
                for obstacle_id in bound_obstacle_ids
            }
            raise _NoCoordinateInsideBounds(
                root,
                obstacle_ids | boundary_obstacles,
                deficit,
            )
        return coordinate

    try:
        latest: dict[int, Fraction] = {}
        latest_ceiling_obstacles: dict[int, set[str]] = {}
        for root in roots:
            coordinate = closest_allowed(
                root,
                floor=domain_floor.get(root),
                ceiling=domain_ceiling.get(root),
                floor_obstacle_ids=domain_floor_obstacles.get(root),
                ceiling_obstacle_ids=domain_ceiling_obstacles.get(root),
            )
            latest[root] = coordinate
            latest_ceiling_obstacles[root] = (
                set(domain_ceiling_obstacles.get(root, set()))
                if coordinate == domain_ceiling.get(root)
                else set()
            )
        for root in reversed(order):
            ceiling_candidates = [
                (
                    latest[after] - separation,
                    latest_ceiling_obstacles[after],
                )
                for after, separation in successors[root]
            ]
            if root in domain_ceiling:
                ceiling_candidates.append(
                    (domain_ceiling[root], domain_ceiling_obstacles.get(root, set()))
                )
            ceiling_candidates.append((latest[root], latest_ceiling_obstacles[root]))
            ceiling = min(value for value, _obstacles in ceiling_candidates)
            ceiling_obstacles = {
                obstacle_id
                for value, obstacle_ids in ceiling_candidates
                if value == ceiling
                for obstacle_id in obstacle_ids
            }
            coordinate = closest_allowed(
                root,
                floor=domain_floor.get(root),
                ceiling=ceiling,
                floor_obstacle_ids=domain_floor_obstacles.get(root),
                ceiling_obstacle_ids=ceiling_obstacles,
            )
            latest[root] = coordinate
            latest_ceiling_obstacles[root] = (
                ceiling_obstacles if coordinate == ceiling else set()
            )
    except _NoCoordinateInsideBounds as error:
        blockers = error.obstacle_ids
        return _failure(
            CorridorAllocationFailureReason.INFEASIBLE,
            members={lanes[index].member_id for index in roots[error.root]},
            obstacles=blockers,
            equality_owners=fixed_root_owners.get(error.root, set()),
            endpoint_owners={
                lanes[index].endpoint_owner_id for index in roots[error.root]
            },
            clearance_shortfall=clearance_shortfall(
                error.root,
                error.deficit,
                blockers,
            ),
        )

    coordinates: dict[int, Fraction] = {}
    coordinate_floor_obstacles: dict[int, set[str]] = {}
    for root in order:
        floor_candidates = [
            (
                coordinates[before] + separation,
                coordinate_floor_obstacles[before],
            )
            for before, separation in predecessors[root]
        ]
        if root in domain_floor:
            floor_candidates.append(
                (domain_floor[root], domain_floor_obstacles.get(root, set()))
            )
        floor = max((value for value, _obstacles in floor_candidates), default=None)
        floor_obstacles = {
            obstacle_id
            for value, obstacle_ids in floor_candidates
            if value == floor
            for obstacle_id in obstacle_ids
        }
        if floor is not None and floor > latest[root]:
            blockers = floor_obstacles | latest_ceiling_obstacles[root]
            return _failure(
                CorridorAllocationFailureReason.INFEASIBLE,
                members={lanes[index].member_id for index in roots[root]},
                obstacles=blockers,
                equality_owners=fixed_root_owners.get(root, set()),
                endpoint_owners={
                    lanes[index].endpoint_owner_id for index in roots[root]
                },
                clearance_shortfall=clearance_shortfall(
                    root,
                    floor - latest[root],
                    blockers,
                ),
            )
        try:
            coordinate = closest_allowed(
                root,
                floor=floor,
                ceiling=latest[root],
                floor_obstacle_ids=floor_obstacles,
                ceiling_obstacle_ids=latest_ceiling_obstacles[root],
            )
        except _NoCoordinateInsideBounds as error:
            blockers = error.obstacle_ids
            return _failure(
                CorridorAllocationFailureReason.INFEASIBLE,
                members={lanes[index].member_id for index in roots[error.root]},
                obstacles=blockers,
                equality_owners=fixed_root_owners.get(error.root, set()),
                endpoint_owners={
                    lanes[index].endpoint_owner_id for index in roots[error.root]
                },
                clearance_shortfall=clearance_shortfall(
                    error.root,
                    error.deficit,
                    blockers,
                ),
            )
        coordinates[root] = coordinate
        coordinate_floor_obstacles[root] = (
            floor_obstacles if coordinate == floor else set()
        )

    allocations: list[tuple[str, float]] = []
    for index, lane in enumerate(lanes):
        root, _ = union_find.find(index)
        oriented_coordinate = coordinates[root] + potentials[index]
        output_coordinate = float(sign * oriented_coordinate)
        allocations.append(
            (
                lane.member_id,
                0.0 if output_coordinate == 0 else output_coordinate,
            )
        )
    return CorridorAllocationResult(
        CorridorAllocationStatus.PLANNED,
        tuple(sorted(allocations)),
    )


__all__ = [
    "CorridorAllocationFailureReason",
    "CorridorAllocationProblem",
    "CorridorAllocationResult",
    "CorridorAllocationStatus",
    "CorridorClearanceShortfall",
    "CorridorCoordinateDomain",
    "CorridorDirectedSeparation",
    "CorridorEquality",
    "CorridorFixedEquality",
    "CorridorForbiddenInterval",
    "CorridorLane",
    "CorridorObstacle",
    "CorridorSeparation",
    "solve_corridor_cohorts",
]
