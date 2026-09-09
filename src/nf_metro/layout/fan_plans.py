"""Structural fan recognition and immutable relative geometry plans.

This module is intentionally independent of layout phase order and routing
dispatch.  It reads authored edge identity plus resolver lineage, recognises a
complete fan, and either gives that whole object one owner or records one
deterministic legacy disposition.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from collections.abc import Collection, Container, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeVar, cast, runtime_checkable

from nf_metro.graph_views import directed_graph, longest_path_layers
from nf_metro.layout.constants import COORD_TOLERANCE_FINE, graph_offset_step
from nf_metro.layout.fan_geometry import fan_lane_offsets, symmetric_lane_offsets
from nf_metro.layout.fan_ordering import fanout_divergence_peel_order
from nf_metro.layout.geometry import (
    AxisFrame,
    flow_port_sides,
    lanes_run_along_x,
    lanes_run_along_y,
    perpendicular_port_sides,
    point_to_polyline_distance,
    section_lane_sign,
)
from nf_metro.layout.labels import tb_left_label_marker_pitch
from nf_metro.layout.route_plan import (
    DemandId,
    EmissionMemberId,
    FanAppearancePolicy,
    FanBranchPlan,
    FanBranchPlanId,
    FanCentrelineAnchor,
    FanOffsetAssignment,
    FanOffsetCarrier,
    FanPlan,
    FanPlanDisposition,
    FanPlanId,
    FanRouteEmission,
    FanRouteEmitter,
    FanRouteExpectation,
    RouteSemanticScaffold,
    RouteSystemId,
    SharedReferenceId,
    build_route_semantic_scaffold,
    fan_has_vacant_trunk,
    fan_lane_seat_keys,
)
from nf_metro.parser.commitments import FlowDirection, is_flow_direction
from nf_metro.parser.model import LineSpread, MetroGraph, PortSide
from nf_metro.parser.route_topology import (
    AuthoredEdgeFact,
    BundleId,
    ConnectorId,
    ConvergenceId,
    ResolvedConvergenceView,
    ResolvedEdge,
    RouteConnector,
    RouteTopologyQuery,
    semantic_route_id,
)

if TYPE_CHECKING:
    from nf_metro.layout.routing.common import RoutedPath


class FanRouteInvariantError(RuntimeError):
    """A planned fan's emitted geometry drifted from its frozen frame."""


@runtime_checkable
class FanTopologyQuery(Protocol):
    """Route-topology surface required by the fan planner."""

    @property
    def authored_edges(self) -> tuple[AuthoredEdgeFact, ...]: ...

    @property
    def convergences(self) -> tuple[ResolvedConvergenceView, ...]: ...

    def resolved_paths(
        self, edge_id: ConnectorId
    ) -> tuple[tuple[ResolvedEdge, ...], ...]: ...

    def connector(self, edge_id: ConnectorId) -> RouteConnector: ...

    def convergence_for_junction(
        self, junction_id: str
    ) -> ResolvedConvergenceView | None: ...


def _landing_port_ids(plan: FanPlan) -> set[str]:
    """The section ports *plan*'s branches land on."""
    return {port_id for branch in plan.branches for port_id in branch.landing_port_ids}


def _laned_station_ids(plan: FanPlan) -> set[str]:
    """The stations *plan* seats on a branch lane or a line-order carrier."""
    return {
        *(
            station_id
            for branch in plan.branches
            for station_id in (*branch.lane_station_ids, *branch.landing_port_ids)
        ),
        *(carrier.station_id for carrier in plan.offset_carriers),
    }


def stated_station_ids(plan: FanPlan) -> set[str]:
    """The stations *plan* puts a coordinate on itself.

    These are the seats the fan derives: the stations on its centreline, the
    stations its branches lane, the carriers it orders lines at, and the ports
    its branches land on, which a branch seats on its own lane the way it seats
    any other station along it.  Two fans stating one of these state it
    independently, so only one of them may.
    """
    return {*plan.centreline_station_ids, *_laned_station_ids(plan)}


def claimed_station_ids(plan: FanPlan) -> set[str]:
    """The stations *plan* states a coordinate for.

    A fan's ``owned_station_ids`` is the closure of everything its members pass
    through, which includes junctions and interior path nodes it reads from
    whoever placed them.  What it *claims* is narrower: the stations it states
    (see :func:`stated_station_ids`) and the boundaries its frame is asserted
    against once the routes are emitted.  Only those are coordinates two fans
    could state differently, and a station another fan owns has been ceded, so
    this plan reads that seat rather than claiming it.
    """
    return (stated_station_ids(plan) | _fan_boundary_station_ids(plan)) - set(
        plan.ceded_station_ids
    )


def drawn_member_edges(plan: FanPlan) -> set[ResolvedEdge]:
    """The member edges *plan* draws a route on itself.

    A fan draws the legs of its branches.  The seams either side of them are
    edges it reaches to see where its geometry meets its neighbours', and a
    neighbouring fan may draw the very same edge as one of its legs.
    """
    return {
        edge
        for branch in plan.branches
        for path in branch.resolved_paths
        for edge in path
    }


def claimed_member_edges(plan: FanPlan) -> set[ResolvedEdge]:
    """The member edges *plan* answers for the route on.

    Every leg it draws, plus the seams it has not handed to the fan that draws
    them.  A ceded seam bounds the fan's frame like any other member, but its
    route belongs to its owner, so two fans holding one such edge are not
    rivals for it.
    """
    return set(plan.resolved_member_edges) - set(plan.ceded_member_edges)


def _entry_trunk_has_foreign_head(
    graph: MetroGraph,
    *,
    fork_id: str,
    layout_section_id: str | None,
    entry_port_ids: Sequence[str],
    layout_station_ids: Sequence[str],
) -> bool:
    """Whether the port feeding the fork also feeds a station the fan does not own.

    A fan anchors its centreline on the trunk arriving at its section, which
    states that the fork is where that trunk lands.  Where the same port feeds a
    station outside the fan, the trunk splits before the fork and which of the
    two heads keeps the trunk's row is the section allocator's decision, not the
    fan's: asserting it would seat the fork on the sibling.
    """
    owned = {fork_id, *layout_station_ids}
    return any(
        target not in owned
        and target not in graph.ports
        and target not in graph.junction_ids
        and graph.section_for_station(target) == layout_section_id
        for port_id in entry_port_ids
        if (port := graph.ports.get(port_id)) is not None
        and port.section_id == layout_section_id
        for target in (edge.target for edge in graph.edges_from(port_id))
    )


def _branch_riding_past_a_sibling(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
) -> FanBranchPlanId | None:
    """The branch that only rides past stations a sibling branch stops at.

    A hidden bypass helper names the station its line goes around, so a branch
    laning nothing but helpers stops nowhere in the fan.  Where the stations
    those helpers go around are a sibling's, the two branches are already
    ordered: the rider keeps the track and the sibling steps off it.
    """
    lane_owner = {
        station_id: branch.id
        for branch in branches
        for path in branch.resolved_paths
        for edge in path
        for station_id in (edge.source, edge.target)
    }
    riders = tuple(
        branch
        for branch in branches
        if branch.lane_station_ids
        and all(
            (station := graph.stations.get(station_id)) is not None
            and station.bypasses_station_id is not None
            and lane_owner.get(station.bypasses_station_id) not in (None, branch.id)
            for station_id in branch.lane_station_ids
        )
    )
    return riders[0].id if len(riders) == 1 else None


def _appearance_centreline_branch_id(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
    appearance_policy: FanAppearancePolicy,
    structural_trunk_rank: int | None,
) -> FanBranchPlanId | None:
    """Choose the branch that a straight local fan keeps on its main track.

    A straight frame holds exactly one branch at lane zero, so the centreline
    has to own its lane seat: branches that leave the fork through a shared
    station stand on one seat and could not be told apart on the main track.
    Each candidate is therefore passed over when it shares its seat, and the
    choice falls through to the next one.
    """
    if appearance_policy is not FanAppearancePolicy.STRAIGHT or not any(
        branch.lane_station_ids for branch in branches
    ):
        return None
    seat_keys = fan_lane_seat_keys(branches)
    seat_population = Counter(seat_keys)
    owns_seat = {
        branch.id: seat_population[seat_key] == 1
        for branch, seat_key in zip(branches, seat_keys, strict=True)
    }
    trunk_branches = tuple(
        branch for branch in branches if branch.is_trunk_continuation
    )
    if len(trunk_branches) == 1 and owns_seat[trunk_branches[0].id]:
        return trunk_branches[0].id
    rider_id = _branch_riding_past_a_sibling(graph, branches)
    if rider_id is not None and owns_seat[rider_id]:
        return rider_id
    if structural_trunk_rank is not None:
        structural_trunk = next(
            (branch for branch in branches if branch.rank == structural_trunk_rank),
            None,
        )
        if structural_trunk is not None and owns_seat[structural_trunk.id]:
            return structural_trunk.id
    seatable = tuple(branch for branch in branches if owns_seat[branch.id]) or branches
    return min(seatable, key=lambda branch: (branch.opening_rank, branch.rank)).id


def vertical_fan_label_lane_pitch(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
    frame: AxisFrame,
    section_layers: dict[str, dict[str, int]],
    appearance_lane_sign: float,
    line_lane_sign: float,
    floor: float = 0.0,
) -> float:
    """Return the uniform X pitch needed by same-layer vertical fan labels."""
    if frame.secondary.name != "x":
        return floor
    offset_step = graph_offset_step(graph)
    section_ids = {
        section_id
        for branch in branches
        for station_id in branch.lane_station_ids
        if (section_id := graph.section_for_station(station_id)) is not None
    }
    if len(section_ids) != 1:
        return floor
    section_id = next(iter(section_ids))
    layers = section_layers.get(section_id)
    if layers is None:
        section = graph.sections[section_id]
        node_ids = tuple(
            station_id
            for station_id in section.station_ids
            if station_id in graph.stations and station_id not in graph.ports
        )
        node_set = set(node_ids)
        layers = longest_path_layers(
            directed_graph(
                node_ids,
                (
                    (edge.source, edge.target)
                    for edge in graph.edges
                    if edge.source in node_set and edge.target in node_set
                ),
            ),
            node_ids,
        )
        section_layers[section_id] = layers
    screen_order = sorted(
        (branch for branch in branches if branch.lane_offset is not None),
        key=lambda branch: appearance_lane_sign * cast(float, branch.lane_offset),
    )
    pitch = floor
    for left_branch, right_branch in pairwise(screen_order):
        left_by_layer = {
            layers[station_id]: station_id
            for station_id in left_branch.lane_station_ids
            if station_id in layers
        }
        for right_id in right_branch.lane_station_ids:
            layer = layers.get(right_id)
            left_id = left_by_layer.get(layer) if layer is not None else None
            right = graph.stations.get(right_id)
            if left_id is None or right is None or not right.label:
                continue
            pitch = max(
                pitch,
                tb_left_label_marker_pitch(
                    right.label,
                    left_line_count=len(graph.station_lines(left_id)),
                    right_line_count=len(graph.station_lines(right_id)),
                    lane_sign=line_lane_sign,
                    offset_step=offset_step,
                ),
            )
    return pitch


def fan_lane_sign(
    graph: MetroGraph,
    frame: AxisFrame,
    layout_section_id: str | None,
    source_station_id: str,
    *,
    branches: Sequence[FanBranchPlan],
    tb_positive_fan: Collection[str],
) -> float:
    """The side a fan opens its branch lanes toward.

    Branches carrying disjoint lines ride the fork's bundle up to the point
    they peel apart, so the side the bundle stacks them on is the side they
    must open toward: stating the other side orders the same two lines twice
    and swaps them between the fork and their lanes, and they cross on the
    way.  Branches that all carry the same lines are concentric and state no
    such order, so there the fan is free to open away from its feeder.
    """
    section = graph.sections.get(layout_section_id or "")
    line_sets = [set(branch.line_ids) for branch in branches]
    partitions = any(
        left.isdisjoint(right)
        for index, left in enumerate(line_sets)
        for right in line_sets[index + 1 :]
    )
    if section is not None and partitions:
        return section_lane_sign(section, tb_positive_fan)
    return fan_appearance_lane_sign(graph, frame, layout_section_id, source_station_id)


def fan_appearance_lane_sign(
    graph: MetroGraph,
    frame: AxisFrame,
    layout_section_id: str | None,
    source_station_id: str,
) -> float:
    """Open a fan away from a clear feeder on its track axis.

    Section tracks use the same positive secondary-axis progression for LR,
    RL, TB, and BT.  A feeder arriving from the negative or positive end of
    that axis mirrors the progression so the hub occupies the nearest track.
    Flow reversal belongs to the primary axis and does not change this rule.
    """
    section = graph.sections.get(layout_section_id or "")
    if section is None:
        return 1.0

    near_side, far_side = perpendicular_port_sides(section.direction)

    pending = [source_station_id]
    seen: set[str] = set()
    entry_sides: set[PortSide] = set()
    feeder_section_ids: set[str] = set()
    while pending:
        station_id = pending.pop()
        if station_id in seen:
            continue
        seen.add(station_id)
        port = graph.ports.get(station_id)
        if port is not None and port.section_id == section.id and port.is_entry:
            entry_sides.add(port.side)
        station = graph.stations.get(station_id)
        station_section_id = station.section_id if station is not None else None
        if station_section_id is not None and station_section_id != section.id:
            feeder_section_ids.add(station_section_id)
            continue
        pending.extend(edge.source for edge in graph.edges_to(station_id))

    if near_side in entry_sides and far_side not in entry_sides:
        return 1.0
    if far_side in entry_sides and near_side not in entry_sides:
        return -1.0

    feeder_sections = tuple(
        graph.sections[section_id]
        for section_id in feeder_section_ids
        if section_id in graph.sections
    )
    if frame.secondary.name == "x":
        section_low = section.grid_col
        section_high = section.grid_col + section.grid_col_span - 1
        feeder_spans = tuple(
            (feeder.grid_col, feeder.grid_col + feeder.grid_col_span - 1)
            for feeder in feeder_sections
        )
    else:
        section_low = section.grid_row
        section_high = section.grid_row + section.grid_row_span - 1
        feeder_spans = tuple(
            (feeder.grid_row, feeder.grid_row + feeder.grid_row_span - 1)
            for feeder in feeder_sections
        )
    if feeder_spans and all(high < section_low for _low, high in feeder_spans):
        return 1.0
    if feeder_spans and all(low > section_high for low, _high in feeder_spans):
        return -1.0
    if _feeder_reconverges_its_own_section(graph, section.id, source_station_id):
        return -1.0
    return 1.0


def _feeder_reconverges_its_own_section(
    graph: MetroGraph, section_id: str, source_station_id: str
) -> bool:
    """True when a fan's feeder closes its own section's branches back together.

    Such a feeder hands over on the track those branches closed around, so the
    seam sits inside the row's band of tracks rather than at the origin the
    band counts from.  A fan opening there reaches either way, and reaching
    toward the origin is what keeps its section's content starting on the same
    track as its row-mates'.
    """
    source = graph.stations.get(source_station_id)
    if source is None or source.section_id in (None, section_id):
        return False
    section_mates = {
        edge.source
        for edge in graph.edges_to(source_station_id)
        if (station := graph.stations.get(edge.source)) is not None
        and station.section_id == source.section_id
    }
    return len(section_mates) >= 2


def _fan_branch_solo_station_ids(
    graph: MetroGraph, branch: FanBranchPlan
) -> tuple[str, ...]:
    """Branch stations whose only present line may return to its trunk."""
    if len(branch.line_ids) != 1:
        return ()
    return cast(
        tuple[str, ...],
        _ordered_unique(
            station_id
            for path in branch.resolved_paths
            for edge in path
            for station_id in (edge.source, edge.target)
            if station_id not in graph.junction_ids
            and graph.station_lines(station_id) == list(branch.line_ids)
        ),
    )


@dataclass(frozen=True, slots=True)
class FanPlanQuery:
    """Read-only ownership indexes over one complete fan-plan build."""

    plans: tuple[FanPlan, ...]
    _by_id: Mapping[FanPlanId, FanPlan]
    _by_fork: Mapping[str, FanPlan]
    _by_authored_edge: Mapping[ConnectorId, FanPlan]
    _structural_by_resolved_edge: Mapping[ResolvedEdge, FanPlan]
    _structural_branch_by_resolved_edge: Mapping[ResolvedEdge, FanBranchPlan]
    _route_emission_by_resolved_edge: Mapping[
        ResolvedEdge, tuple[FanPlan, FanBranchPlan, FanRouteEmission]
    ]

    @classmethod
    def build(cls, plans: tuple[FanPlan, ...]) -> FanPlanQuery:
        by_id: dict[FanPlanId, FanPlan] = {}
        by_member: dict[EmissionMemberId, FanPlan] = {}
        by_fork: dict[str, FanPlan] = {}
        by_authored_edge: dict[ConnectorId, FanPlan] = {}
        structural_by_resolved_edge: dict[ResolvedEdge, FanPlan] = {}
        structural_branch_by_resolved_edge: dict[ResolvedEdge, FanBranchPlan] = {}
        route_emission_by_resolved_edge: dict[
            ResolvedEdge, tuple[FanPlan, FanBranchPlan, FanRouteEmission]
        ] = {}
        shared_branch_edges: set[ResolvedEdge] = set()
        by_station: dict[str, FanPlan] = {}
        for plan in plans:
            if plan.id in by_id:
                raise ValueError(f"duplicate fan plan id {plan.id!r}")
            by_id[plan.id] = plan
            if plan.disposition is not FanPlanDisposition.PLANNED:
                continue
            for member_id in plan.member_ids:
                if member_id in by_member:
                    raise ValueError("two planned fans own one emission member")
                by_member[member_id] = plan
            if plan.fork_station_id in by_fork:
                raise ValueError("two planned fans own one fork")
            by_fork[plan.fork_station_id] = plan
            for edge_id in plan.authored_edge_ids:
                if edge_id in by_authored_edge:
                    raise ValueError("two planned fans own one authored edge")
                by_authored_edge[edge_id] = plan
            ceded_edges = set(plan.ceded_member_edges)
            for edge in plan.resolved_member_edges:
                if edge in ceded_edges:
                    continue
                if edge in structural_by_resolved_edge:
                    raise ValueError("two planned fans own one resolved edge")
                structural_by_resolved_edge[edge] = plan
            for branch in plan.branches:
                for path in branch.continuation_resolved_paths:
                    for edge in path:
                        if edge in shared_branch_edges:
                            continue
                        existing = structural_branch_by_resolved_edge.get(edge)
                        if existing is not None and existing is not branch:
                            del structural_branch_by_resolved_edge[edge]
                            shared_branch_edges.add(edge)
                        else:
                            structural_branch_by_resolved_edge[edge] = branch
            branches_by_id = {branch.id: branch for branch in plan.branches}
            for emission in plan.route_emissions:
                if emission.edge in route_emission_by_resolved_edge:
                    raise ValueError("two planned fan emitters own one resolved edge")
                route_emission_by_resolved_edge[emission.edge] = (
                    plan,
                    branches_by_id[emission.branch_id],
                    emission,
                )
            for station_id in sorted(claimed_station_ids(plan)):
                if station_id in by_station:
                    raise ValueError("two planned fans own one station")
                by_station[station_id] = plan
        return cls(
            plans=plans,
            _by_id=MappingProxyType(by_id),
            _by_fork=MappingProxyType(by_fork),
            _by_authored_edge=MappingProxyType(by_authored_edge),
            _structural_by_resolved_edge=MappingProxyType(structural_by_resolved_edge),
            _structural_branch_by_resolved_edge=MappingProxyType(
                structural_branch_by_resolved_edge
            ),
            _route_emission_by_resolved_edge=MappingProxyType(
                route_emission_by_resolved_edge
            ),
        )

    def plan(self, plan_id: FanPlanId) -> FanPlan:
        return self._by_id[plan_id]

    def __deepcopy__(self, memo: dict[int, object]) -> FanPlanQuery:
        del memo
        return self

    def planned_for_fork(self, station_id: str) -> FanPlan | None:
        return self._by_fork.get(station_id)

    def owner_for_authored_edge(self, edge_id: ConnectorId) -> FanPlan | None:
        return self._by_authored_edge.get(edge_id)

    def structural_owner_for_resolved_edge(self, edge: ResolvedEdge) -> FanPlan | None:
        return self._structural_by_resolved_edge.get(edge)

    def structural_branch_for_resolved_edge(
        self, edge: ResolvedEdge
    ) -> FanBranchPlan | None:
        return self._structural_branch_by_resolved_edge.get(edge)

    def route_emission_for_resolved_edge(
        self, edge: ResolvedEdge
    ) -> tuple[FanPlan, FanBranchPlan, FanRouteEmission] | None:
        return self._route_emission_by_resolved_edge.get(edge)


@dataclass(frozen=True, slots=True)
class FanPlanExecution:
    """Context-local result installed for later layout and routing consumers."""

    query: FanPlanQuery
    scaffold: RouteSemanticScaffold | None = None

    @property
    def plans(self) -> tuple[FanPlan, ...]:
        return self.query.plans

    def __deepcopy__(self, memo: dict[int, object]) -> FanPlanExecution:
        del memo
        return self


def _authored_edges(topology: FanTopologyQuery) -> tuple[AuthoredEdgeFact, ...]:
    return tuple(sorted(topology.authored_edges, key=lambda fact: fact.rank))


_T = TypeVar("_T")


def _ordered_unique(values: Iterable[_T]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(values))


def _node_rank(facts: Sequence[AuthoredEdgeFact]) -> dict[str, int]:
    result: dict[str, int] = {}
    for fact in facts:
        result.setdefault(fact.key.source, fact.rank)
        result.setdefault(fact.key.target, fact.rank)
    return result


def _adjacency(
    facts: Sequence[AuthoredEdgeFact],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[tuple[str, str], tuple[AuthoredEdgeFact, ...]],
]:
    targets: dict[str, list[str]] = defaultdict(list)
    sources: dict[str, list[str]] = defaultdict(list)
    bundles: dict[tuple[str, str], list[AuthoredEdgeFact]] = defaultdict(list)
    for fact in facts:
        key = (fact.key.source, fact.key.target)
        bundles[key].append(fact)
        if fact.key.target not in targets[fact.key.source]:
            targets[fact.key.source].append(fact.key.target)
        if fact.key.source not in sources[fact.key.target]:
            sources[fact.key.target].append(fact.key.source)
    return (
        {source: tuple(values) for source, values in targets.items()},
        {target: tuple(values) for target, values in sources.items()},
        {key: tuple(values) for key, values in bundles.items()},
    )


def _distances(adjacency: Mapping[str, tuple[str, ...]], root: str) -> dict[str, int]:
    result = {root: 0}
    pending = deque([root])
    while pending:
        source = pending.popleft()
        for target in adjacency.get(source, ()):
            if target not in result:
                result[target] = result[source] + 1
                pending.append(target)
    return result


def _nearest_common_join(
    adjacency: Mapping[str, tuple[str, ...]],
    branch_roots: tuple[str, ...],
    ranks: Mapping[str, int],
    *,
    allow_root: bool = True,
) -> str | None:
    """Nearest station every branch root reaches.

    Under ``allow_root`` a branch root qualifies as the join when the other
    roots reach it: a diamond whose short branch lands directly on the
    convergence node joins at that node, not at whatever follows it.
    """
    distances = tuple(_distances(adjacency, root) for root in branch_roots)
    common = set(distances[0]).intersection(*(set(item) for item in distances[1:]))
    reaches = any if allow_root else all
    candidates = [
        station_id
        for station_id in common
        if reaches(item[station_id] > 0 for item in distances)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda station_id: (
            max(item[station_id] for item in distances),
            sum(item[station_id] for item in distances),
            ranks.get(station_id, len(ranks)),
            station_id,
        ),
    )


def _reverse_reachable(
    incoming: Mapping[str, tuple[str, ...]], target: str
) -> set[str]:
    result = {target}
    pending = deque([target])
    while pending:
        station_id = pending.popleft()
        for source_id in incoming.get(station_id, ()):
            if source_id not in result:
                result.add(source_id)
                pending.append(source_id)
    return result


def _unique_path_to_join(
    adjacency: Mapping[str, tuple[str, ...]],
    root: str,
    join: str,
    reaches_join: set[str],
) -> tuple[str, ...] | None:
    path = [root]
    current = root
    visited = {root}
    while current != join:
        continuations = tuple(
            candidate
            for candidate in adjacency.get(current, ())
            if candidate in reaches_join
        )
        if len(continuations) != 1:
            return None
        current = continuations[0]
        if current in visited:
            return None
        visited.add(current)
        path.append(current)
    return tuple(path)


def _linear_path(
    adjacency: Mapping[str, tuple[str, ...]], root: str
) -> tuple[str, ...]:
    path = [root]
    visited = {root}
    current = root
    while len(adjacency.get(current, ())) == 1:
        target = adjacency[current][0]
        if target in visited:
            break
        visited.add(target)
        path.append(target)
        current = target
    return tuple(path)


def _paths_for(
    topology: FanTopologyQuery, facts: Iterable[AuthoredEdgeFact]
) -> tuple[tuple[ResolvedEdge, ...], ...]:
    result: list[tuple[ResolvedEdge, ...]] = []
    for fact in facts:
        result.extend(topology.resolved_paths(fact.id))
    return tuple(result)


def _path_nodes(path: tuple[ResolvedEdge, ...]) -> tuple[str, ...]:
    if not path:
        return ()
    return (path[0].source, *(edge.target for edge in path))


def _common_prefix_nodes(paths: Sequence[tuple[ResolvedEdge, ...]]) -> tuple[str, ...]:
    nodes = tuple(_path_nodes(path) for path in paths)
    if not nodes or any(not item for item in nodes):
        return ()
    prefix: list[str] = []
    for values in zip(*nodes, strict=False):
        if len(set(values)) != 1:
            break
        prefix.append(values[0])
    return tuple(prefix)


def _common_suffix_nodes(paths: Sequence[tuple[ResolvedEdge, ...]]) -> tuple[str, ...]:
    reversed_nodes = tuple(tuple(reversed(_path_nodes(path))) for path in paths)
    if not reversed_nodes or any(not item for item in reversed_nodes):
        return ()
    suffix: list[str] = []
    for values in zip(*reversed_nodes, strict=False):
        if len(set(values)) != 1:
            break
        suffix.append(values[0])
    return tuple(reversed(suffix))


def _trim_member_path(
    path: tuple[ResolvedEdge, ...], fork_id: str, join_id: str | None
) -> tuple[ResolvedEdge, ...]:
    nodes = _path_nodes(path)
    start = nodes.index(fork_id) if fork_id in nodes else 0
    end = (
        nodes.index(join_id) if join_id is not None and join_id in nodes else len(path)
    )
    if end < start:
        return ()
    return path[start:end]


def _facts_for_node_path(
    path: tuple[str, ...],
    bundles: Mapping[tuple[str, str], tuple[AuthoredEdgeFact, ...]],
    line_ids: frozenset[str] | None = None,
) -> tuple[AuthoredEdgeFact, ...]:
    """The authored edges a branch travels along *path*, leg by leg.

    A branch keeps the lines it left the fork on for as long as they continue.
    Where a leg carries none of them, the leg retags the branch: it takes the
    lines that leg actually carries and follows those from there, so each leg
    states its own line identity instead of the branch losing everything past
    the change.
    """
    carried = line_ids
    result: list[AuthoredEdgeFact] = []
    for source, target in zip(path, path[1:]):
        leg = bundles[(source, target)]
        matching = tuple(
            fact for fact in leg if carried is None or fact.key.line_id in carried
        )
        if not matching:
            matching = leg
            carried = frozenset(fact.key.line_id for fact in leg)
        result.extend(matching)
    return tuple(result)


def _line_extent(
    path: tuple[str, ...],
    bundles: Mapping[tuple[str, str], tuple[AuthoredEdgeFact, ...]],
    incoming: Mapping[str, tuple[str, ...]],
    line_ids: frozenset[str],
) -> tuple[str, ...]:
    """Trim *path* where the branch's lines end and another run carries on.

    A leg carrying none of the lines the branch arrived on retags the branch,
    which is how one run that changes line stays one branch (see
    :func:`_facts_for_node_path`).  Where the lines that leg carries also reach
    the station from another source, the leg is that source's run continuing,
    not this branch retagged, so the branch ends at the station and the run it
    would have swallowed stays with whoever brings those lines in.
    """
    carried = line_ids
    for index, (source, target) in enumerate(zip(path, path[1:])):
        leg = bundles[(source, target)]
        leg_lines = frozenset(fact.key.line_id for fact in leg)
        if leg_lines & carried:
            continue
        arrived_from = path[index - 1] if index else None
        if any(
            fact.key.line_id in leg_lines
            for other in incoming.get(source, ())
            if other != arrived_from
            for fact in bundles[(other, source)]
        ):
            return path[: max(index + 1, 2)]
        carried = leg_lines
    return path


def _leg_ordered_line_ids(
    facts: Sequence[AuthoredEdgeFact],
    line_priority: Mapping[str, int],
) -> tuple[str, ...]:
    """A branch's lines, priority-sorted within each leg, legs left in order.

    ``facts`` is one branch's flattened, per-leg-concatenated fact sequence
    (see :func:`_facts_for_node_path`): a leg boundary is a change in the
    authored ``(source, target)`` edge. Two lines sharing a leg are
    simultaneous and sort by declaration priority for a stable stack order;
    two lines from different legs are never simultaneous (a leg transition
    retags the branch, it does not add a sibling), so sorting across legs by
    priority would place a later leg's line between an earlier leg's
    co-present lines whenever declaration order disagrees with leg order,
    splitting a bundle that travels together.
    """
    result: list[str] = []
    current_key: tuple[str, str] | None = None
    leg: list[str] = []

    def flush() -> None:
        result.extend(sorted(leg, key=lambda item: line_priority.get(item, 0)))

    for fact in facts:
        key = (fact.key.source, fact.key.target)
        if key != current_key:
            if leg:
                flush()
            leg = []
            current_key = key
        if fact.key.line_id not in leg:
            leg.append(fact.key.line_id)
    if leg:
        flush()
    return cast(tuple[str, ...], _ordered_unique(result))


def _inherited_branch_order(
    source_id: str,
    branch_plans: Sequence[FanBranchPlan],
    leg_ordered_lines_by_rank: Mapping[int, tuple[str, ...]],
    bundles: Mapping[tuple[str, str], tuple[AuthoredEdgeFact, ...]],
    incoming: Mapping[str, tuple[str, ...]],
) -> dict[int, int] | None:
    """Rank branches by a retagged ancestor bundle's own authored order.

    A fork fed directly by the same lines it is about to split (an ordinary
    diamond, one hop below its own shared entry) has no other order to
    consult, so its authored (opening) order stands -- that is the common
    case, and it is deliberately left alone. A fork some further hop below a
    shared bundle -- reached only after a leg retagged every one of those
    lines to a different identity and back -- has no entry edge of its own
    naming them, yet every carrier between that distant bundle and here
    already reads its comma-list order (see :func:`_leg_ordered_line_ids`);
    matching it here is what keeps this fork's lanes stacked the same way
    round as theirs. Returns ``None`` when no such distant edge exists.

    Only a bundle this fork descends from can have fixed those slots, so a
    candidate qualifies only when its target reaches *source_id* through the
    authored graph -- which also excludes the fan's own entry edge, whose
    target is *source_id* itself. Among qualifying bundles the nearest wins
    -- fewest hops from its target down to *source_id*, then earliest
    authored edge -- so the answer is a property of the graph rather than of
    declaration or iteration order.
    """
    divergent = frozenset(
        line_id for lines in leg_ordered_lines_by_rank.values() for line_id in lines
    )
    if len(divergent) < 2:
        return None
    hops_to_source = _distances(incoming, source_id)
    candidates = [
        (hops_to_source[target], min(fact.rank for fact in facts), facts)
        for (_source, target), facts in bundles.items()
        if target != source_id
        and target in hops_to_source
        and divergent.issubset({fact.key.line_id for fact in facts})
    ]
    if not candidates:
        return None
    ancestor = min(candidates, key=lambda candidate: candidate[:2])[2]
    order = {
        line_id: rank
        for rank, line_id in enumerate(
            dict.fromkeys(fact.key.line_id for fact in ancestor)
        )
    }
    return {
        branch.rank: min(
            (
                order[line_id]
                for line_id in leg_ordered_lines_by_rank[branch.rank]
                if line_id in order
            ),
            default=branch.opening_rank,
        )
        for branch in branch_plans
    }


def _extra_output_facts(
    path: tuple[str, ...],
    adjacency: Mapping[str, tuple[str, ...]],
    bundles: Mapping[tuple[str, str], tuple[AuthoredEdgeFact, ...]],
) -> tuple[AuthoredEdgeFact, ...]:
    result: list[AuthoredEdgeFact] = []
    for index, source in enumerate(path[:-1]):
        continuation = path[index + 1]
        for target in adjacency.get(source, ()):
            if target != continuation:
                result.extend(bundles[(source, target)])
    return tuple(result)


def _direction_for_fork(
    graph: MetroGraph,
    fork_id: str,
    source_id: str,
    lead_facts: Sequence[AuthoredEdgeFact],
) -> FlowDirection | None:
    section_id = graph.section_for_station(fork_id)
    if section_id is None and fork_id in graph.ports:
        section_id = graph.ports[fork_id].section_id
    if section_id is None:
        section_id = next(
            (
                fact.source_section
                for fact in lead_facts
                if fact.source_section is not None
            ),
            None,
        )
    if section_id is None:
        section_id = graph.section_for_station(source_id)
    section = graph.sections.get(section_id or "")
    if section is None or not is_flow_direction(section.direction):
        return None
    return section.direction


def _port_ids(
    graph: MetroGraph, paths: Iterable[tuple[ResolvedEdge, ...]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    entry: list[str] = []
    exit_: list[str] = []
    for path in paths:
        for station_id in _path_nodes(path):
            port = graph.ports.get(station_id)
            if port is None:
                continue
            target = entry if port.is_entry else exit_
            if station_id not in target:
                target.append(station_id)
    return tuple(entry), tuple(exit_)


def _section_exit_ports_from(
    graph: MetroGraph, station_id: str, section_id: str | None
) -> frozenset[str]:
    """Exit ports of *section_id* the run leaving *station_id* hands straight on to."""
    return frozenset(
        edge.target
        for edge in graph.edges_from(station_id)
        if (port := graph.ports.get(edge.target)) is not None
        and not port.is_entry
        and port.section_id == section_id
    )


def _carries_the_trunk_out(
    graph: MetroGraph,
    exit_port_ids: Iterable[str],
    section_id: str | None,
    sibling_exit_port_ids: AbstractSet[str],
) -> bool:
    """Whether an arm carries the fork's trunk out of *section_id* on its own.

    An arm running out through the section's own exit port carries the trunk
    the fork stands on past the fan.  Where a sibling arm's tail hands on to
    that same port, the two arms meet again at the boundary: the run to the
    port is the section's own chain through both of them, and neither arm
    carries it alone.
    """
    return any(
        (port := graph.ports.get(port_id)) is not None
        and port.section_id == section_id
        and port_id not in sibling_exit_port_ids
        for port_id in exit_port_ids
    )


def _grid_position(graph: MetroGraph, section_id: str) -> tuple[int, int]:
    section = graph.sections[section_id]
    override = graph.grid_overrides.get(section_id)
    if override is not None:
        return override[0], override[1]
    return section.grid_col, section.grid_row


def heads_its_own_fork(graph: MetroGraph, station_id: str) -> bool:
    """Whether *station_id* stands at the head of a fan of its own.

    Two or more on-track successors inside its own section make it the apex of
    a fan ``assign_tracks`` has already centred it on, so its lane is settled
    before any fan plan is built.  Ports, junctions, cross-section successors
    and off-track outputs leave the apex free: none of them takes a lane out of
    the section's own frame.
    """
    section_id = graph.section_for_station(station_id)
    successors = {
        edge.target
        for edge in graph.edges_from(station_id)
        if edge.target not in graph.ports
        and edge.target not in graph.junction_ids
        and graph.section_for_station(edge.target) == section_id
        and (target := graph.stations.get(edge.target)) is not None
        and not target.off_track
    }
    return len(successors) >= 2


def _trunk_followers(
    graph: MetroGraph,
    fork_id: str,
    join_id: str | None,
    approach_paths: Iterable[tuple[ResolvedEdge, ...]],
    departure_paths: Iterable[tuple[ResolvedEdge, ...]],
) -> tuple[str, ...]:
    """The stations the fan's trunk runs through either side of its span.

    A follower is the one station the trunk continues from before the fork, or
    continues to after the join, and it stands on the fan's centreline.  Where a
    side reaches more than one station the trunk continues through none of them:
    those are the arms of a convergence or of a second fan, and putting them all
    on one centreline would draw them in a single row.  A station heading a fan
    of its own is likewise no follower: it already holds the centre of that fan,
    and this one's centreline is a branch lane away from it.
    """

    def _side(
        node_id: str, paths: Iterable[tuple[ResolvedEdge, ...]], *, upstream: bool
    ) -> tuple[str, ...]:
        found: list[str] = []
        for path in paths:
            nodes = _path_nodes(path)
            if node_id not in nodes:
                continue
            index = nodes.index(node_id)
            walk = reversed(nodes[:index]) if upstream else iter(nodes[index + 1 :])
            for station_id in walk:
                if station_id in graph.ports or station_id in graph.junction_ids:
                    continue
                if station_id not in found:
                    found.append(station_id)
                break
        if len(found) != 1 or heads_its_own_fork(graph, found[0]):
            return ()
        return tuple(found)

    return (
        *_side(fork_id, approach_paths, upstream=True),
        *(() if join_id is None else _side(join_id, departure_paths, upstream=False)),
    )


def _carriers_within_the_fan(
    graph: MetroGraph,
    carriers: Sequence[FanOffsetCarrier],
    owned_station_ids: Sequence[str],
) -> tuple[FanOffsetCarrier, ...]:
    """Drop every carrier slot where a carried run leaves the fan.

    Carrier slots state how the fan's lines sit against each other along one
    run.  Where that run carries the same line on into a station the fan
    neither carries nor owns, the order at the two ends is decided twice: the
    fan restates it here and whoever placed the neighbour keeps it there, so
    the run steps sideways at the seam between them.  One such run makes the
    whole chain's order contentious, since the fan's carriers are consistent
    only with each other.
    """
    reachable = {carrier.station_id for carrier in carriers}.union(owned_station_ids)
    leaves = any(
        (edge.target if edge.source == carrier.station_id else edge.source)
        not in reachable
        for carrier in carriers
        for edge in (
            *graph.edges_from(carrier.station_id),
            *graph.edges_to(carrier.station_id),
        )
        if edge.line_id in carrier.line_ids
    )
    return () if leaves else tuple(carriers)


def _retags_its_line(graph: MetroGraph, station_id: str) -> bool:
    """Whether one arrival hands the track over to a departure of another name.

    Both names belong to *station_id*, so the fan seats both: they are
    distinct lines and take the two adjacent lanes its frame gives them,
    which is what makes the marker span the arriving and departing runs
    instead of either run stepping across it.
    """
    incoming = tuple(graph.edges_to(station_id))
    outgoing = tuple(graph.edges_from(station_id))
    return (
        len(incoming) == 1
        and len(outgoing) == 1
        and incoming[0].line_id != outgoing[0].line_id
    )


def _seats_a_retag(graph: MetroGraph, station_id: str) -> bool:
    """Whether the fan gives a hand-over station a lane per name of its own.

    Collapsed offsets seat every name a station carries on the one lane its
    busiest side needs, and a hand-over station carries one line in and one
    out, so both its names ride the track through it and the marker stays a
    single-lane dot.  Only a map that spreads its lines gives the two names
    the adjacent lanes the marker spans.
    """
    return not graph.compact_offsets and _retags_its_line(graph, station_id)


def _contiguous_assignments(
    offset_line_order: Sequence[str],
    present: Container[str],
    offset_sign: int,
) -> tuple[FanOffsetAssignment, ...]:
    """Seat the fan lines a station carries on one consecutive run of lanes.

    A carrier occupies as many lanes as it has lines, never more: a line the
    fan carries only elsewhere -- the far side of a leg transition that
    retags it, above all -- runs down no lane here, so it opens none between
    two lines that do, and the bundle stays a solid stack.

    The run starts at the first frame lane this carrier reaches rather than
    at lane zero, which is what separates the two sides of a retag by
    exactly one lane. Both sides then draw straight, and the hand-over
    station spans the two adjacent lanes its arriving and departing names
    hold -- distinct lines, distinct offsets.
    """
    lanes = [
        rank for rank, line_id in enumerate(offset_line_order) if line_id in present
    ]
    return tuple(
        FanOffsetAssignment(offset_line_order[rank], (lanes[0] + index) * offset_sign)
        for index, rank in enumerate(lanes)
    )


def _entry_offset_carriers(
    graph: MetroGraph,
    entry_handoff_paths: tuple[tuple[ResolvedEdge, ...], ...],
    offset_line_order: tuple[str, ...],
    offset_sign: int,
) -> tuple[FanOffsetCarrier, ...]:
    """Return the exact flat, full-bundle chain feeding a fan handoff."""
    if not entry_handoff_paths or not offset_line_order:
        return ()
    fan_line_ids = frozenset(offset_line_order)
    carried_to_station: dict[str, set[str]] = defaultdict(set)
    for path in entry_handoff_paths:
        if path:
            carried_to_station[path[0].source].update(
                edge.line_id for edge in path if edge.line_id in fan_line_ids
            )
    path_station_ids = {
        station_id
        for path in entry_handoff_paths
        for edge in path
        for station_id in (edge.source, edge.target)
    }
    carriers: dict[str, set[str]] = {}
    queue = deque(carried_to_station)
    while queue:
        current_id = queue.popleft()
        current_lines = carried_to_station[current_id]
        section_id = graph.section_for_station(current_id)
        section = graph.sections.get(section_id or "")
        if section is None or lanes_run_along_x(section.direction):
            continue
        incoming_by_source: dict[str, set[str]] = defaultdict(set)
        for edge in graph.edges_to(current_id):
            if graph.section_for_station(edge.source) == section_id:
                incoming_by_source[edge.source].add(edge.line_id)
        predecessors = [
            (source_id, current_lines.intersection(carried_lines))
            for source_id, carried_lines in incoming_by_source.items()
            if current_lines.intersection(carried_lines)
        ]
        if len(predecessors) != 1:
            continue
        source_id, propagated = predecessors[0]
        if propagated != current_lines:
            continue
        if source_id not in path_station_ids:
            carriers.setdefault(source_id, set()).update(propagated)
        known = carried_to_station.setdefault(source_id, set())
        unseen = propagated.difference(known)
        if unseen:
            known.update(unseen)
            queue.append(source_id)
    return tuple(
        FanOffsetCarrier(
            station_id=station_id,
            assignments=_contiguous_assignments(
                offset_line_order, carried_lines, offset_sign
            ),
        )
        for station_id, carried_lines in carriers.items()
    )


def _section_line_index(graph: MetroGraph) -> dict[str, frozenset[str]]:
    """Every line the stations of each section carry, indexed in one walk."""
    return {
        section_id: frozenset(
            line_id
            for member_id in section.station_ids
            for line_id in graph.station_lines(member_id)
        )
        for section_id, section in graph.sections.items()
    }


def _rides_foreign_line_corridor(
    graph: MetroGraph,
    station_id: str,
    fan_lines: frozenset[str],
    section_lines: dict[str, frozenset[str]] | None = None,
) -> bool:
    """Whether *station_id* only relays a corridor that carries a non-fan line.

    *section_lines* is :func:`_section_line_index`, which a caller asking about
    a whole carrier set builds once and shares; a lone question builds its own.

    Scoped to ports, junctions and hand-over stations: a fork or join station
    is the point a fan actively dispatches its own lines from, so it states
    their order regardless of what else its section carries. A port, a
    junction or a station that merely renames the one track running through
    it, by contrast, only relays a corridor another station already lays out;
    where that corridor's section also carries a line outside the fan, the
    section's own line-priority ordering already reserves that line's slot,
    so the fan's own numbering at the boundary would recentre the bundle away
    from the position its section fixes upstream. A junction has no section
    of its own, so it is classified by every section feeding it a fan line:
    the corridor it relays is drawn as a continuation of all of them, and one
    such section carrying a foreign line is enough to have fixed the bundle's
    slots before the junction sees them.
    """
    if (
        station_id not in graph.ports
        and station_id not in graph.junction_ids
        and not _retags_its_line(graph, station_id)
    ):
        return False
    index = _section_line_index(graph) if section_lines is None else section_lines

    def carries_foreign_line(section_id: str | None) -> bool:
        return not index.get(section_id or "", frozenset()) <= fan_lines

    own_section_id = graph.section_for_station(station_id)
    if own_section_id is not None:
        return carries_foreign_line(own_section_id)
    return any(
        carries_foreign_line(graph.section_for_station(edge.source))
        for edge in graph.edges_to(station_id)
        if edge.line_id in fan_lines
    )


def _offset_carriers(
    graph: MetroGraph,
    *,
    branches: Sequence[FanBranchPlan],
    offset_line_order: tuple[str, ...],
    shared_paths: Sequence[tuple[ResolvedEdge, ...]],
    shared_station_ids: Iterable[str | None],
    upstream_carriers: Sequence[FanOffsetCarrier],
    offset_sign: int,
) -> tuple[FanOffsetCarrier, ...]:
    """Freeze stations whose fan-line permutation is structurally shared."""
    if not offset_line_order:
        return ()

    fan_lines = frozenset(offset_line_order)
    carrier_lines: dict[str, set[str]] = {}

    def add_station(station_id: str | None, lines: Iterable[str]) -> None:
        if station_id is None or station_id not in graph.stations:
            return
        present = fan_lines.intersection(lines, graph.station_lines(station_id))
        if len(present) >= 2:
            carrier_lines.setdefault(station_id, set()).update(present)

    shared_path_lines: dict[str, set[str]] = defaultdict(set)
    for path in shared_paths:
        for edge in path:
            shared_path_lines[edge.source].add(edge.line_id)
            shared_path_lines[edge.target].add(edge.line_id)
    for station_id, lines in shared_path_lines.items():
        add_station(station_id, lines)
    for shared_station_id in shared_station_ids:
        add_station(shared_station_id, fan_lines)
    for carrier in upstream_carriers:
        add_station(carrier.station_id, carrier.line_ids)

    branch_incidence: dict[str, dict[int, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for branch in branches:
        for path in branch.resolved_paths:
            for edge in path:
                branch_incidence[edge.source][branch.rank].add(edge.line_id)
                branch_incidence[edge.target][branch.rank].add(edge.line_id)
    for station_id, by_branch in branch_incidence.items():
        if len(by_branch) < 2 and not _seats_a_retag(graph, station_id):
            continue
        add_station(
            station_id,
            (line_id for lines in by_branch.values() for line_id in lines),
        )

    if not carrier_lines:
        return ()
    section_lines = _section_line_index(graph)
    return tuple(
        FanOffsetCarrier(
            station_id=station_id,
            assignments=_contiguous_assignments(offset_line_order, lines, offset_sign),
        )
        for station_id, lines in carrier_lines.items()
        if not _rides_foreign_line_corridor(graph, station_id, fan_lines, section_lines)
    )


def _bottom_exit_source_port_id(
    graph: MetroGraph,
    exit_port_ids: Sequence[str],
) -> str | None:
    candidates = tuple(
        port_id
        for port_id in exit_port_ids
        if (port := graph.ports.get(port_id)) is not None
        and not port.is_entry
        and port.side is PortSide.BOTTOM
        and (section := graph.sections.get(port.section_id)) is not None
        and lanes_run_along_x(section.direction)
        and AxisFrame.flow_sign(section.direction) > 0
    )
    return candidates[0] if len(candidates) == 1 else None


def _route_emissions(
    graph: MetroGraph,
    fork_id: str,
    branches: Sequence[FanBranchPlan],
    exit_port_ids: Sequence[str],
    offset_line_order: Sequence[str],
) -> tuple[FanRouteEmission, ...]:
    """Freeze edges handled by the stacked RIGHT-landing fan emitter."""
    if (
        fork_id not in graph.junction_ids
        or _bottom_exit_source_port_id(graph, exit_port_ids) is None
    ):
        return ()
    landing_section_ids: list[str] = []
    for branch in branches:
        if len(branch.landing_port_ids) != 1:
            return ()
        port = graph.ports.get(branch.landing_port_ids[0])
        section = graph.sections.get(port.section_id) if port is not None else None
        if (
            port is None
            or port.side is not PortSide.RIGHT
            or section is None
            or not lanes_run_along_y(section.direction)
        ):
            return ()
        if port.section_id not in landing_section_ids:
            landing_section_ids.append(port.section_id)
    if len(landing_section_ids) != len(branches):
        return ()

    result = tuple(
        FanRouteEmission(
            edge=edge,
            branch_id=branch.id,
            emitter=FanRouteEmitter.BOTTOM_EXIT_RIGHT_LANDINGS,
        )
        for branch in branches
        for path in branch.continuation_resolved_paths
        for edge in path
        if edge.source == fork_id and edge.target in branch.landing_port_ids
    )
    if {item.branch_id for item in result} != {branch.id for branch in branches}:
        return ()
    emitted_by_branch: dict[FanBranchPlanId, set[str]] = defaultdict(set)
    for item in result:
        emitted_by_branch[item.branch_id].add(item.edge.line_id)
    if any(
        emitted_by_branch.get(branch.id, set()) != set(branch.line_ids)
        for branch in branches
    ):
        return ()
    emitted_lines = tuple(item.edge.line_id for item in result)
    if len(emitted_lines) != len(set(emitted_lines)) or set(emitted_lines) != set(
        offset_line_order
    ):
        return ()
    return result


def _apply_screen_offset_assignments(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
    route_emissions: Sequence[FanRouteEmission],
    exit_port_ids: Sequence[str],
    owned_station_ids: Sequence[str],
    carriers: Sequence[FanOffsetCarrier],
    line_priority: Mapping[str, int],
) -> tuple[FanOffsetCarrier, ...]:
    """Freeze exact source-side slots for the stacked RIGHT-landing emitter."""
    exit_port_id = _bottom_exit_source_port_id(graph, exit_port_ids)
    if not route_emissions or exit_port_id is None:
        return tuple(carriers)
    # A BOTTOM-exit fold into a RIGHT entry stores the receiving horizontal
    # section's lanes reflected. Earlier landing branches take the leftmost
    # descent block; lines within each block follow that reflected seam order.
    ordered_lines = tuple(
        line_id
        for branch in sorted(branches, key=lambda item: item.landing_rank)
        for line_id in sorted(
            branch.line_ids,
            key=lambda item: line_priority.get(item, len(line_priority)),
            reverse=True,
        )
    )
    if len(set(ordered_lines)) != len(ordered_lines):
        return tuple(carriers)
    screen_slots = {
        line_id: len(ordered_lines) - rank - 1
        for rank, line_id in enumerate(ordered_lines)
    }

    assignments: dict[str, dict[str, int]] = {
        carrier.station_id: {
            assignment.line_id: assignment.slot for assignment in carrier.assignments
        }
        for carrier in carriers
    }
    source_section_id = graph.ports[exit_port_id].section_id
    for station_id in owned_station_ids:
        station = graph.stations.get(station_id)
        if station is None:
            continue
        if station.section_id != source_section_id and station_id not in {
            exit_port_id,
            route_emissions[0].edge.source,
        }:
            continue
        present_lines = set(graph.station_lines(station_id))
        station_assignments = assignments.setdefault(station_id, {})
        for line_id in ordered_lines:
            if line_id in present_lines:
                station_assignments[line_id] = screen_slots[line_id]

    return tuple(
        FanOffsetCarrier(
            station_id=station_id,
            assignments=tuple(
                FanOffsetAssignment(line_id, slot)
                for line_id, slot in line_assignments.items()
            ),
        )
        for station_id, line_assignments in assignments.items()
        if line_assignments
    )


def _apply_solo_branch_offset_assignments(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
    fork_id: str,
    carriers: Sequence[FanOffsetCarrier],
) -> tuple[FanOffsetCarrier, ...]:
    """Freeze trunk-slot assignments for single-line branch stations."""
    assignments: dict[str, dict[str, int]] = {
        carrier.station_id: {
            assignment.line_id: assignment.slot for assignment in carrier.assignments
        }
        for carrier in carriers
    }
    fork_assignments = assignments.get(fork_id, {})
    for branch in branches:
        if len(branch.line_ids) != 1:
            continue
        line_id = branch.line_ids[0]
        if fork_assignments.get(line_id) != 0:
            continue
        for station_id in _fan_branch_solo_station_ids(graph, branch):
            assignments.setdefault(station_id, {})[line_id] = 0

    return tuple(
        FanOffsetCarrier(
            station_id=station_id,
            assignments=tuple(
                FanOffsetAssignment(line_id, slot)
                for line_id, slot in line_assignments.items()
            ),
        )
        for station_id, line_assignments in assignments.items()
    )


def _layout_section_id(graph: MetroGraph, fork_id: str) -> str | None:
    port = graph.ports.get(fork_id)
    if port is not None:
        section = graph.sections.get(port.section_id)
        if section is None:
            return None
        if port.side not in flow_port_sides(section.direction):
            return None
        return port.section_id
    return graph.section_for_station(fork_id)


def _centreline_port_ids(
    graph: MetroGraph,
    direction: FlowDirection | None,
    layout_section_id: str | None,
    port_ids: Sequence[str],
    branches: Sequence[FanBranchPlan],
) -> tuple[str, ...]:
    """Freeze boundary ports that continue one fan's local centreline.

    A port only continues the centreline when the fan's un-offset
    (``lane_offset`` 0 or unset) branch runs through it: that is the branch
    riding the trunk straight across the boundary. A port carrying only
    laterally-offset branches carries lines the fan has already pulled off the
    centreline to keep them visually separated near the crossing; forcing it
    onto the raw centreline overrides whatever row its own section settles
    those branches on downstream, and where the trunk ends inside the
    fan it drags the whole peeled bundle back up to a row nothing runs along.

    Where one branch does hold the centreline, read over every boundary port a
    branch passes rather than only the ports it lands on: the port a fan's own
    section hands off through is on the peeled branches' run just as much as
    their landing is.  A fan with no branch on the centreline states nothing
    about which port the trunk continues through, so there it is only the
    landings that speak.
    """
    holds_the_centreline = any(branch.lane_offset in (None, 0.0) for branch in branches)
    branch_port_ids = {
        branch.id: frozenset(
            port_id
            for side in _port_ids(graph, branch.resolved_paths)
            for port_id in side
        )
        if holds_the_centreline
        else frozenset(branch.landing_port_ids)
        for branch in branches
    }
    offset_only_port_ids = {
        port_id
        for branch in branches
        if branch.lane_offset not in (None, 0.0)
        for port_id in branch_port_ids[branch.id]
    } - {
        port_id
        for branch in branches
        if branch.lane_offset in (None, 0.0)
        for port_id in branch_port_ids[branch.id]
    }
    layout_section = graph.sections.get(layout_section_id or "")
    if direction is None or layout_section is None:
        return ()
    fan_is_horizontal = not lanes_run_along_x(direction)
    layout_column, layout_row = _grid_position(graph, layout_section.id)
    result: list[str] = []
    for port_id in port_ids:
        if port_id in offset_only_port_ids:
            continue
        port = graph.ports.get(port_id)
        section = graph.sections.get(port.section_id) if port is not None else None
        if port is None or section is None:
            continue
        neighbour_section_ids = {
            neighbour.section_id
            for edge in (*graph.edges_to(port_id), *graph.edges_from(port_id))
            for neighbour_id in (
                edge.source if edge.target == port_id else edge.target,
            )
            if (neighbour := graph.stations.get(neighbour_id)) is not None
            and neighbour.section_id is not None
            and neighbour.section_id != section.id
        }
        has_perpendicular_neighbour = any(
            (not lanes_run_along_x(neighbour_section.direction)) != fan_is_horizontal
            for neighbour_id in neighbour_section_ids
            if (neighbour_section := graph.sections.get(neighbour_id)) is not None
        )
        if (
            (not lanes_run_along_x(section.direction)) != fan_is_horizontal
            or port.side not in flow_port_sides(section.direction)
            or has_perpendicular_neighbour
            or (
                _grid_position(graph, section.id)[1] != layout_row
                if fan_is_horizontal
                else _grid_position(graph, section.id)[0] != layout_column
            )
        ):
            continue
        result.append(port_id)
    return tuple(dict.fromkeys(result))


def _centreline_anchor_reason(
    anchor: FanCentrelineAnchor | None,
    branches: tuple[FanBranchPlan, ...],
    appearance_policy: FanAppearancePolicy,
) -> str | None:
    """Why the fan cannot state a centreline from *anchor*, if it cannot.

    The anchor reads the centreline off one station's settled coordinate.  Where
    that station rides a single branch, that branch's lane says where the
    centreline lies too, so an anchor offset that disagrees with the lane is one
    frame stated twice in two places.  Under the symmetric diamond the second
    reading is not the fan's to take: the diamond seats its branches in slots
    balanced about the trunk, so moving the centreline onto the riding branch's
    lane pulls those stations off the slots layout settled.
    """
    if anchor is None:
        return "missing-centreline-anchor"
    riding = _branches_riding(anchor.station_id, branches)
    if (
        len(riding) == 1
        and riding[0].lane_offset is not None
        and riding[0].lane_offset != anchor.lane_offset
    ):
        return (
            "symmetric-diamond-layout-owns-the-anchor"
            if appearance_policy is FanAppearancePolicy.SYMMETRIC
            else "centreline-anchor-off-its-branch-lane"
        )
    return None


def _branches_riding(
    station_id: str, branches: Sequence[FanBranchPlan]
) -> list[FanBranchPlan]:
    """The branches whose resolved runs pass through *station_id*."""
    return [
        branch
        for branch in branches
        if any(
            station_id in (edge.source, edge.target)
            for path in branch.resolved_paths
            for edge in path
        )
    ]


def _centreline_anchor(
    graph: MetroGraph,
    *,
    direction: FlowDirection | None,
    frame: AxisFrame | None,
    fork_id: str,
    layout_section_id: str | None,
    branches: Sequence[FanBranchPlan],
    entry_port_ids: Sequence[str],
    exit_port_ids: Sequence[str],
    local_frame_anchor: FanCentrelineAnchor | None,
) -> FanCentrelineAnchor | None:
    """Freeze the source of one fan's settled absolute centreline."""
    layout_section = graph.sections.get(layout_section_id or "")
    if frame is not None and direction is not None and layout_section is not None:
        horizontal = not lanes_run_along_x(direction)
        candidates: list[tuple[float, str]] = []
        for port_id in (*entry_port_ids, *exit_port_ids):
            port = graph.ports.get(port_id)
            section = graph.sections.get(port.section_id) if port is not None else None
            if (
                port is None
                or port.is_entry
                or section is None
                or section.id == layout_section.id
                or (not lanes_run_along_x(section.direction)) != horizontal
                or port.side not in flow_port_sides(section.direction)
            ):
                continue
            section_col, section_row = _grid_position(graph, section.id)
            layout_col, layout_row = _grid_position(graph, layout_section.id)
            if horizontal:
                same_strip = section_row == layout_row
                distance = (layout_col - section_col) * frame.primary_sign
            else:
                same_strip = section_col == layout_col
                distance = (layout_row - section_row) * frame.primary_sign
            if same_strip and distance > 0:
                candidates.append((distance, port_id))
        if candidates:
            return FanCentrelineAnchor(min(candidates)[1])

        local_trunks = tuple(
            branch
            for branch in branches
            if branch.is_trunk_continuation
            and branch.lane_station_ids
            and not branch.landing_port_ids
        )
        if len(local_trunks) == 1 and fork_id in graph.stations:
            return FanCentrelineAnchor(fork_id)

        flow_sides = flow_port_sides(direction)
        local_ports = [
            port_id
            for port_id in (*entry_port_ids, *exit_port_ids)
            if (port := graph.ports.get(port_id)) is not None
            and port.section_id == layout_section.id
            and port.side in flow_sides
        ]
        local_ports = list(dict.fromkeys(local_ports))
        local_ports.sort(key=lambda port_id: not graph.ports[port_id].is_entry)
        if local_ports:
            return FanCentrelineAnchor(local_ports[0])
        if fork_id in graph.stations:
            return FanCentrelineAnchor(fork_id)

    return local_frame_anchor


def _foreign_ridden_station_ids(
    graph: MetroGraph, fan_lines: frozenset[str]
) -> set[str]:
    """Stations a bypass helper of some line outside *fan_lines* goes around.

    Such a station stands in a column with that helper, and where the two sit
    relative to one another is settled by whoever laid the helper out.  A fan
    stating one of the two coordinates would ladder a column it only half owns.
    """
    return {
        bypassed_id
        for station in graph.stations.values()
        if (bypassed_id := station.bypasses_station_id) is not None
        and not fan_lines.issuperset(graph.station_lines(station.id))
    }


def _lane_station_ids(
    graph: MetroGraph,
    paths: Iterable[tuple[ResolvedEdge, ...]],
    *,
    section_id: str | None,
    fork_id: str,
    join_id: str | None,
    foreign_ridden_ids: Collection[str] = (),
) -> tuple[str, ...]:
    if section_id is None:
        return ()
    station_ids: list[str] = []
    for path in paths:
        nodes = _path_nodes(path)
        for index, station_id in enumerate(nodes):
            if station_id == join_id:
                break
            if index > 0:
                predecessor_id = nodes[index - 1]
                incoming_sources = {edge.source for edge in graph.edges_to(station_id)}
                if incoming_sources.difference({predecessor_id}):
                    break
            if (
                station_id != fork_id
                and station_id not in graph.ports
                and station_id not in graph.junction_ids
                and station_id not in foreign_ridden_ids
                and graph.section_for_station(station_id) == section_id
                and station_id not in station_ids
            ):
                station_ids.append(station_id)
    return tuple(station_ids)


def _lanes_with_one_seat_holder(
    branches: Sequence[FanBranchPlan],
) -> tuple[FanBranchPlan, ...]:
    """Give every lane seat a single holder.

    A branch's lane is the row it holds, and every station along it takes that
    row.  Where more than one branch runs through the same station -- a bypass
    helper both their lines go around, say -- the row is stated once per branch
    and whichever branch is materialised last wins silently.  Branches that
    leave the fork through the same station share one seat, so they agree on
    the row and the first of them states it for all; branches that only meet
    further along hold different seats and neither may state it, and the fan
    orders its lines there as a carrier instead.
    """
    holders: dict[str, FanBranchPlanId] = {}
    contested: set[str] = set()
    roots: dict[str, str] = {}
    for branch in branches:
        for station_id in branch.lane_station_ids:
            if station_id not in holders:
                holders[station_id] = branch.id
                roots[station_id] = branch.root_station_id
            elif roots[station_id] != branch.root_station_id:
                contested.add(station_id)
    return tuple(
        replace(
            branch,
            lane_station_ids=tuple(
                station_id
                for station_id in branch.lane_station_ids
                if station_id not in contested and holders[station_id] == branch.id
            ),
        )
        for branch in branches
    )


def _trunk_ends_at_the_fork(
    graph: MetroGraph,
    branches: Sequence[FanBranchPlan],
    local_terminal_ids: Sequence[FanBranchPlanId],
    structural_trunk_rank: int | None,
    direction: FlowDirection | None,
) -> bool:
    """Whether no leg of an open fan carries its trunk on past the fork.

    A straight fan seats one leg on the trunk's own row because that leg
    continues the trunk.  Where every leg is a single station that dead-ends
    inside the fan's section, and they all carry the same bundle of two or more
    lines, no leg continues anything: the trunk ends at the fork.  Seating one
    leg there anyway makes every other leg cross it, because the leg that stays
    holds the whole lane band the others have to leave through.

    Read on the row band a horizontal fan opens into.  A vertical section
    separates its lines along X, where the section allocator owns the columns
    the legs stand in rather than the fan.
    """
    return (
        direction is not None
        and lanes_run_along_y(direction)
        and len(local_terminal_ids) == len(branches) >= 2
        and structural_trunk_rank is None
        and not any(branch.is_trunk_continuation for branch in branches)
        and all(branch.root_station_id == branch.tail_station_id for branch in branches)
        and _branch_riding_past_a_sibling(graph, branches) is None
        and len({frozenset(branch.line_ids) for branch in branches}) == 1
        and len(branches[0].line_ids) >= 2
    )


def _trunk_reaches_its_terminus(
    branches: Sequence[FanBranchPlan],
    direction: FlowDirection | None,
) -> bool:
    """Whether one leg of an open fan carries the whole bundle to its last stop.

    A symmetric fan mirrors its legs about a seat no leg occupies, which is
    right while every leg peels part of the bundle away.  Where one leg instead
    carries the bundle entire to a station that ends it inside the fan's own
    section, that leg is the trunk reaching its terminus: it stands on the row
    the trunk arrived on, and a mirrored frame lifts it off that row and bends
    the trunk around a seat nothing sits in.  Such a fan has a centre seat, and
    the legs that peel lines off take the slots beside it.

    Read on the row band a horizontal fan opens into, for the same reason
    :func:`_trunk_ends_at_the_fork` is.
    """
    if direction is None or not lanes_run_along_y(direction) or len(branches) < 3:
        return False
    bundle = {line_id for branch in branches for line_id in branch.line_ids}
    return (
        sum(
            branch.is_trunk_continuation
            and branch.terminal
            and bool(branch.lane_station_ids)
            and not branch.landing_port_ids
            and set(branch.line_ids) == bundle
            for branch in branches
        )
        == 1
        and sum(branch.is_trunk_continuation for branch in branches) == 1
        and all(
            set(branch.line_ids) != bundle
            for branch in branches
            if not branch.is_trunk_continuation
        )
    )


def _open_fan_appearance_policy(
    graph: MetroGraph,
    authored_policy: FanAppearancePolicy,
    branches: Sequence[FanBranchPlan],
    local_terminal_ids: Sequence[FanBranchPlanId],
    structural_trunk_rank: int | None,
    direction: FlowDirection | None,
) -> FanAppearancePolicy:
    """Settle which frame an open fan's legs are arranged in.

    The authored policy states a preference over diamonds, which an open fan
    only follows where its own shape leaves the choice open: whether a leg
    holds the trunk's row decides it, and both answers are structural.
    """
    if _trunk_ends_at_the_fork(
        graph, branches, local_terminal_ids, structural_trunk_rank, direction
    ):
        return FanAppearancePolicy.SYMMETRIC
    if authored_policy is FanAppearancePolicy.SYMMETRIC and _trunk_reaches_its_terminus(
        branches, direction
    ):
        return FanAppearancePolicy.STRAIGHT
    return authored_policy


def _uncontested_local_terminal_branch_ids(
    graph: MetroGraph,
    node_paths: Sequence[tuple[str, ...]],
    branches: Sequence[FanBranchPlan],
    incoming: Mapping[str, tuple[str, ...]],
    layout_section_id: str | None,
) -> tuple[FanBranchPlanId, ...]:
    """Return local terminal branches that do not enter another merge frame."""
    if layout_section_id is None:
        return ()
    result: list[FanBranchPlanId] = []
    for path, branch in zip(node_paths, branches, strict=True):
        if (
            not branch.terminal
            or branch.landing_port_ids
            or not branch.lane_station_ids
        ):
            continue
        if any(
            graph.section_for_station(station_id) != layout_section_id
            for station_id in path[1:]
        ):
            continue
        if any(
            set(incoming.get(station_id, ())).difference({predecessor_id})
            for predecessor_id, station_id in zip(path, path[1:])
        ):
            continue
        result.append(branch.id)
    return tuple(result)


def _handoff_ids(
    topology: FanTopologyQuery, edge_ids: tuple[ConnectorId, ...]
) -> tuple[tuple[BundleId, ...], tuple[ConvergenceId, ...]]:
    bundles: list[BundleId] = []
    for edge_id in edge_ids:
        try:
            bundle_id = topology.connector(edge_id).bundle_id
        except KeyError:
            continue
        if bundle_id not in bundles:
            bundles.append(bundle_id)
    convergences: list[ConvergenceId] = []
    for view in topology.convergences:
        group = view.group
        if set(group.connector_ids).intersection(edge_ids):
            convergences.append(group.id)
    return tuple(bundles), tuple(convergences)


def _legacy(plan: FanPlan, reason: str) -> FanPlan:
    branches = tuple(
        replace(
            branch,
            lane_station_ids=(),
            lane_offset=None,
            diagonal_runway=None,
        )
        for branch in plan.branches
    )
    return replace(
        plan,
        branches=branches,
        frame=None,
        entry_runway=None,
        exit_runway=None,
        centreline_reference_id=None,
        demand_ids=(),
        offset_carriers=(),
        route_expectations=(),
        route_emissions=(),
        centreline_port_ids=(),
        centreline_station_ids=(),
        centreline_anchor=None,
        local_frame_anchor=None,
        appearance_centreline_branch_id=None,
        appearance_lane_pitch=None,
        appearance_lane_sign=None,
        disposition=FanPlanDisposition.LEGACY,
        legacy_reason=reason,
    )


def _fan_resource_ids(
    plan_id: FanPlanId,
    branches: Sequence[FanBranchPlan],
) -> tuple[SharedReferenceId, tuple[DemandId, ...]]:
    reference_id = SharedReferenceId(semantic_route_id("fan-centreline", plan_id))
    demand_ids = (
        DemandId(semantic_route_id("fan-entry-runway", plan_id)),
        DemandId(semantic_route_id("fan-exit-runway", plan_id)),
        *(
            DemandId(semantic_route_id("fan-branch-runway", plan_id, branch.id))
            for branch in branches
        ),
    )
    return reference_id, demand_ids


@dataclass(frozen=True, slots=True)
class _FanPlanningContext:
    graph: MetroGraph
    topology: FanTopologyQuery
    adjacency: Mapping[str, tuple[str, ...]]
    incoming: Mapping[str, tuple[str, ...]]
    bundles: Mapping[tuple[str, str], tuple[AuthoredEdgeFact, ...]]
    ranks: Mapping[str, int]
    x_spacing: float
    y_spacing: float
    minimum_runway: float
    section_layers: dict[str, dict[str, int]]
    tb_positive_fan: set[str]


@dataclass(frozen=True, slots=True)
class _RecognisedFan:
    source_id: str
    branch_targets: tuple[str, ...]
    lead_fact_groups: tuple[tuple[AuthoredEdgeFact, ...], ...]
    lead_paths: tuple[tuple[tuple[ResolvedEdge, ...], ...], ...]
    all_lead_paths: tuple[tuple[ResolvedEdge, ...], ...]
    prefix: tuple[str, ...]
    fork_id: str
    reason: str | None
    authored_join: str | None
    node_paths: tuple[tuple[str, ...], ...]
    structural_trunk_rank: int | None
    continuation_facts: tuple[tuple[AuthoredEdgeFact, ...], ...]
    extra_facts: tuple[tuple[AuthoredEdgeFact, ...], ...]
    final_paths: tuple[tuple[ResolvedEdge, ...], ...]
    suffix: tuple[str, ...]
    join_id: str | None


def _branch_node_paths(
    ctx: _FanPlanningContext,
    source_id: str,
    branch_targets: tuple[str, ...],
    join_id: str | None,
) -> tuple[list[tuple[str, ...]], bool]:
    """Each branch's authored node path to *join_id*, and whether any is plural."""
    adjacency = ctx.adjacency
    if join_id is None:
        return [
            (source_id, *_linear_path(adjacency, target)) for target in branch_targets
        ], False
    reaches_join = _reverse_reachable(ctx.incoming, join_id)
    node_paths: list[tuple[str, ...]] = []
    ambiguous = False
    for target in branch_targets:
        path = _unique_path_to_join(adjacency, target, join_id, reaches_join)
        if path is None:
            ambiguous = True
            path = _linear_path(adjacency, target)
        node_paths.append((source_id, *path))
    return node_paths, ambiguous


def _recognise_fan(
    ctx: _FanPlanningContext,
    source_id: str,
    branch_targets: tuple[str, ...],
) -> _RecognisedFan:
    """Recognise complete authored and resolved membership without geometry."""
    topology = ctx.topology
    adjacency = ctx.adjacency
    bundles = ctx.bundles
    lead_fact_groups = tuple(bundles[(source_id, target)] for target in branch_targets)
    lead_paths = tuple(_paths_for(topology, facts) for facts in lead_fact_groups)
    all_lead_paths = tuple(path for paths in lead_paths for path in paths)
    complete_leads = all(paths and all(path for path in paths) for paths in lead_paths)
    prefix = _common_prefix_nodes(all_lead_paths) if complete_leads else (source_id,)
    fork_id = prefix[-1] if prefix else source_id
    reason = (
        "missing-resolved-member-path"
        if not complete_leads
        else None
        if prefix
        else "ambiguous-resolved-fork"
    )

    authored_join = _nearest_common_join(adjacency, branch_targets, ctx.ranks)
    node_paths, ambiguous = _branch_node_paths(
        ctx, source_id, branch_targets, authored_join
    )
    if ambiguous and authored_join in branch_targets:
        # A branch root closes the fan only where every other branch reaches it
        # one way.  Where they reach it through a web of alternatives it is a
        # downstream sink the whole graph drains into, not this fan's join.
        authored_join = _nearest_common_join(
            adjacency, branch_targets, ctx.ranks, allow_root=False
        )
        node_paths, ambiguous = _branch_node_paths(
            ctx, source_id, branch_targets, authored_join
        )
    if ambiguous:
        reason = reason or "ambiguous-branch-to-join"
    node_paths = [
        _line_extent(
            path,
            bundles,
            ctx.incoming,
            frozenset(fact.key.line_id for fact in lead_facts),
        )
        for path, lead_facts in zip(node_paths, lead_fact_groups, strict=True)
    ]
    extended_branch_ranks = tuple(
        rank for rank, path in enumerate(node_paths) if len(path) > 2
    )
    structural_trunk_rank = (
        extended_branch_ranks[0]
        if authored_join is None and len(extended_branch_ranks) == 1
        else None
    )

    selected_continuations = tuple(
        _facts_for_node_path(
            path,
            bundles,
            frozenset(fact.key.line_id for fact in lead_facts),
        )
        for path, lead_facts in zip(node_paths, lead_fact_groups, strict=True)
    )
    continuation_facts = selected_continuations
    extra_facts = tuple(
        _extra_output_facts(path[1:], adjacency, bundles)
        if authored_join is not None
        else ()
        for path in node_paths
    )
    final_fact_groups = tuple(bundles[(path[-2], path[-1])] for path in node_paths)
    final_paths = tuple(
        path for facts in final_fact_groups for path in _paths_for(topology, facts)
    )
    suffix = _common_suffix_nodes(final_paths) if authored_join is not None else ()
    join_id = suffix[0] if suffix else None
    if authored_join is not None and join_id is None:
        reason = reason or "ambiguous-resolved-join"

    return _RecognisedFan(
        source_id=source_id,
        branch_targets=branch_targets,
        lead_fact_groups=lead_fact_groups,
        lead_paths=lead_paths,
        all_lead_paths=all_lead_paths,
        prefix=prefix,
        fork_id=fork_id,
        reason=reason,
        authored_join=authored_join,
        node_paths=tuple(node_paths),
        structural_trunk_rank=structural_trunk_rank,
        continuation_facts=continuation_facts,
        extra_facts=extra_facts,
        final_paths=final_paths,
        suffix=suffix,
        join_id=join_id,
    )


def _seat_centered_symmetric_branches_by_line_rail(
    graph: MetroGraph,
    branch_plans: list[FanBranchPlan],
    appearance_policy: FanAppearancePolicy,
    appearance_lane_sign: float | None,
    layout_section_id: str | None,
    minimum_runway: float,
) -> list[FanBranchPlan]:
    """Seat a centered symmetric fan's slots on each branch's line rail.

    A centered section's exclusive run rides its line's symmetric base rail --
    the line's index above, on, or below the trunk midline -- exactly as the
    flat-graph track assignment seats it.  ``symmetric_lane_offsets`` yields the
    slots ascending; branch-discovery order need not agree with line order, so
    map the ascending slots onto the branches sorted by the screen side their
    lines want (line rail scaled by the frame's lane sign).

    Only a centered section is reseated; a ``diamond_style: symmetric`` fan keeps
    its discovery-order seating.
    """
    if (
        appearance_policy is not FanAppearancePolicy.SYMMETRIC
        or appearance_lane_sign is None
        or graph.section_line_spread(layout_section_id) is not LineSpread.CENTERED
    ):
        return branch_plans

    # Same canonical rail order the flat-graph track allocator uses
    # (ordering.assign_tracks), so a sectioned fan seats its lines identically.
    line_index = {lid: rank for rank, lid in enumerate(graph.lines)}
    n_lines = len(line_index)

    def screen_key(branch: FanBranchPlan) -> tuple[float, int]:
        rails = [
            line_index[lid] - (n_lines - 1) / 2
            for lid in branch.line_ids
            if lid in line_index
        ]
        mean_rail = sum(rails) / len(rails) if rails else 0.0
        return (mean_rail * appearance_lane_sign, branch.rank)

    offsets = sorted(
        branch.lane_offset if branch.lane_offset is not None else 0.0
        for branch in branch_plans
    )
    order = sorted(range(len(branch_plans)), key=lambda i: screen_key(branch_plans[i]))
    seated = [0.0] * len(branch_plans)
    for slot, index in zip(offsets, order, strict=True):
        seated[index] = slot
    return [
        replace(
            branch,
            lane_offset=lane_offset,
            diagonal_runway=max(
                minimum_runway,
                branch.diagonal_runway or 0.0,
                abs(lane_offset),
            ),
        )
        for branch, lane_offset in zip(branch_plans, seated, strict=True)
    ]


def _build_candidate(
    ctx: _FanPlanningContext,
    source_id: str,
    branch_targets: tuple[str, ...],
) -> FanPlan:
    recognised = _recognise_fan(ctx, source_id, branch_targets)
    graph = ctx.graph
    topology = ctx.topology
    adjacency = ctx.adjacency
    incoming = ctx.incoming
    bundles = ctx.bundles
    minimum_runway = ctx.minimum_runway
    lead_fact_groups = recognised.lead_fact_groups
    lead_paths = recognised.lead_paths
    all_lead_paths = recognised.all_lead_paths
    prefix = recognised.prefix
    fork_id = recognised.fork_id
    reason = recognised.reason
    authored_join = recognised.authored_join
    node_paths = recognised.node_paths
    structural_trunk_rank = recognised.structural_trunk_rank
    continuation_facts = recognised.continuation_facts
    extra_facts = recognised.extra_facts
    final_paths = recognised.final_paths
    suffix = recognised.suffix
    join_id = recognised.join_id

    direction = _direction_for_fork(graph, fork_id, source_id, lead_fact_groups[0])
    if direction is None:
        reason = reason or "unsupported-fan-direction"
    lane_pitch = (
        AxisFrame.for_direction(direction, ctx.x_spacing, ctx.y_spacing).secondary.step
        if direction is not None
        else ctx.y_spacing
    )
    offsets = symmetric_lane_offsets(len(branch_targets), lane_pitch)
    layout_section_id = _layout_section_id(graph, fork_id)
    appearance_policy = (
        FanAppearancePolicy.SYMMETRIC
        if graph.section_line_spread(layout_section_id) is LineSpread.CENTERED
        else FanAppearancePolicy(graph.diamond_style)
    )
    if layout_section_id is not None and any(
        station_id != authored_join
        and graph.section_for_station(station_id) == layout_section_id
        and set(incoming.get(station_id, ())).difference({predecessor_id})
        for path in node_paths
        for predecessor_id, station_id in zip(path, path[1:])
    ):
        reason = reason or "local-layout-has-foreign-owner"
    foreign_ridden_ids = _foreign_ridden_station_ids(
        graph,
        frozenset(
            fact.key.line_id
            for facts in (*continuation_facts, *extra_facts)
            for fact in facts
        ),
    )
    tail_exit_port_ids = [
        _section_exit_ports_from(graph, node_path[-1], layout_section_id)
        for node_path in node_paths
    ]
    branch_plans: list[FanBranchPlan] = []
    all_member_facts: list[AuthoredEdgeFact] = []
    all_raw_paths: list[tuple[ResolvedEdge, ...]] = []
    for rank, (node_path, facts, outputs, branch_lead_paths) in enumerate(
        zip(node_paths, continuation_facts, extra_facts, lead_paths, strict=True)
    ):
        raw_continuation = _paths_for(topology, facts)
        raw_outputs = _paths_for(topology, outputs)
        if not raw_continuation or any(not path for path in raw_continuation):
            reason = reason or "missing-resolved-member-path"
        if outputs and (not raw_outputs or any(not path for path in raw_outputs)):
            reason = reason or "missing-resolved-extra-output-path"
        branch_prefix = _common_prefix_nodes(branch_lead_paths)
        root_id = (
            branch_prefix[len(prefix)]
            if prefix and len(branch_prefix) > len(prefix)
            else node_path[1]
        )
        if authored_join is not None:
            tail_id = join_id or node_path[-1]
        else:
            tail_paths = _paths_for(topology, bundles[(node_path[-2], node_path[-1])])
            tails = {path[-1].target for path in tail_paths if path}
            tail_id = next(iter(tails)) if len(tails) == 1 else node_path[-1]
            if len(tails) != 1:
                reason = reason or "ambiguous-resolved-branch-tail"
        trimmed = tuple(
            _trim_member_path(path, fork_id, join_id) for path in raw_continuation
        )
        if any(not path for path in trimmed):
            reason = reason or "empty-resolved-member-path"
        lines = cast(
            tuple[str, ...],
            _ordered_unique(fact.key.line_id for fact in (*facts, *outputs)),
        )
        branch_id = FanBranchPlanId(
            semantic_route_id("fan-branch", source_id, *(fact.id for fact in facts))
        )
        terminal = authored_join is None and not adjacency.get(node_path[-1], ())
        branch_plans.append(
            FanBranchPlan(
                id=branch_id,
                rank=rank,
                landing_rank=rank,
                opening_rank=rank,
                root_station_id=root_id,
                tail_station_id=tail_id,
                continuation_edge_ids=tuple(fact.id for fact in facts),
                continuation_resolved_paths=trimmed,
                connector_ids=(),
                member_ids=(),
                line_ids=lines,
                extra_output_edge_ids=tuple(fact.id for fact in outputs),
                extra_output_resolved_paths=raw_outputs,
                landing_port_ids=_port_ids(graph, trimmed)[0],
                lane_station_ids=_lane_station_ids(
                    graph,
                    (*trimmed, *raw_outputs),
                    section_id=layout_section_id,
                    fork_id=fork_id,
                    join_id=join_id,
                    foreign_ridden_ids=foreign_ridden_ids,
                ),
                is_trunk_continuation=_carries_the_trunk_out(
                    graph,
                    _port_ids(graph, raw_continuation)[1],
                    layout_section_id,
                    frozenset().union(
                        *(
                            ports
                            for other, ports in enumerate(tail_exit_port_ids)
                            if other != rank
                        )
                    ),
                )
                or rank == structural_trunk_rank,
                terminal=terminal,
                lane_offset=offsets[rank],
                diagonal_runway=max(minimum_runway, abs(offsets[rank])),
            )
        )
        all_member_facts.extend((*facts, *outputs))
        all_raw_paths.extend((*raw_continuation, *raw_outputs))

    branch_plans = list(_lanes_with_one_seat_holder(branch_plans))

    def landing_key(branch: FanBranchPlan) -> tuple[int, int, int]:
        positions = [
            (row, column)
            for port_id in branch.landing_port_ids
            if (port := graph.ports.get(port_id)) is not None
            and (section := graph.sections.get(port.section_id)) is not None
            for column, row in (_grid_position(graph, section.id),)
            if row >= 0 and column >= 0
        ]
        if not positions:
            return len(graph.sections), len(graph.sections), branch.rank
        row, column = min(positions)
        return row, column, branch.rank

    landing_order = {
        branch.id: rank
        for rank, branch in enumerate(sorted(branch_plans, key=landing_key))
    }
    branch_plans = [
        replace(
            branch,
            landing_rank=landing_order[branch.id],
            diagonal_runway=max(
                branch.diagonal_runway or minimum_runway,
                minimum_runway + landing_order[branch.id] * lane_pitch,
            ),
        )
        for branch in branch_plans
    ]
    if fork_id in graph.junction_ids:
        peel_order = fanout_divergence_peel_order(
            graph,
            fork_id,
            {line_id: rank for rank, line_id in enumerate(graph.lines)},
            topology,
        )
        branch_by_line = {
            branch.line_ids[0]: branch
            for branch in branch_plans
            if len(branch.line_ids) == 1
        }
        if (
            peel_order is not None
            and len(branch_by_line) == len(branch_plans) == len(peel_order)
            and set(peel_order) == set(branch_by_line)
        ):
            opening_order = {
                branch_by_line[line_id].id: rank
                for rank, line_id in enumerate(peel_order)
            }
            branch_plans = [
                replace(branch, opening_rank=opening_order[branch.id])
                for branch in branch_plans
            ]
    local_terminal_ids = _uncontested_local_terminal_branch_ids(
        graph,
        node_paths,
        branch_plans,
        incoming,
        layout_section_id,
    )
    if len(local_terminal_ids) == 1:
        local_terminal_id = local_terminal_ids[0]
        branch_plans = [
            replace(
                branch,
                is_trunk_continuation=branch.id == local_terminal_id,
            )
            for branch in branch_plans
        ]
    if authored_join is None:
        appearance_policy = _open_fan_appearance_policy(
            graph,
            appearance_policy,
            branch_plans,
            local_terminal_ids,
            structural_trunk_rank,
            direction,
        )
    has_vacant_trunk = fan_has_vacant_trunk(
        appearance_policy,
        authored_join,
        branch_plans,
    )
    if has_vacant_trunk:
        lane_pitch *= 2.0
    appearance_centreline_branch_id = (
        None
        if has_vacant_trunk
        else _appearance_centreline_branch_id(
            graph,
            branch_plans,
            appearance_policy,
            structural_trunk_rank,
        )
    )
    lane_offsets = fan_lane_offsets(
        tuple(branch.id for branch in branch_plans),
        lane_pitch,
        appearance_centreline_branch_id,
        fan_lane_seat_keys(branch_plans),
    )
    branch_plans = [
        replace(
            branch,
            lane_offset=lane_offset,
            diagonal_runway=max(
                minimum_runway,
                branch.diagonal_runway or 0.0,
                abs(lane_offset),
            ),
        )
        for branch, lane_offset in zip(branch_plans, lane_offsets, strict=True)
    ]

    frame = (
        AxisFrame.for_direction(direction, ctx.x_spacing, ctx.y_spacing)
        if direction is not None
        else None
    )
    appearance_lane_sign = (
        fan_lane_sign(
            graph,
            frame,
            layout_section_id,
            source_id,
            branches=branch_plans,
            tb_positive_fan=ctx.tb_positive_fan,
        )
        if frame is not None and reason is None
        else None
    )
    branch_plans = _seat_centered_symmetric_branches_by_line_rail(
        graph,
        branch_plans,
        appearance_policy,
        appearance_lane_sign,
        layout_section_id,
        minimum_runway,
    )
    if frame is not None and appearance_lane_sign is not None:
        layout_section = graph.sections.get(layout_section_id or "")
        line_lane_sign = (
            section_lane_sign(layout_section, ctx.tb_positive_fan)
            if layout_section is not None
            else frame.secondary_sign
        )
        required_pitch = vertical_fan_label_lane_pitch(
            graph,
            branch_plans,
            frame,
            ctx.section_layers,
            appearance_lane_sign,
            line_lane_sign,
            lane_pitch,
        )
        if required_pitch > lane_pitch:
            scale = required_pitch / lane_pitch
            lane_pitch = required_pitch
            branch_plans = [
                replace(
                    branch,
                    lane_offset=(
                        branch.lane_offset * scale
                        if branch.lane_offset is not None
                        else None
                    ),
                    diagonal_runway=max(
                        minimum_runway + branch.landing_rank * lane_pitch,
                        abs(branch.lane_offset * scale)
                        if branch.lane_offset is not None
                        else 0.0,
                    ),
                )
                for branch in branch_plans
            ]

    branch_line_sets = [set(branch.line_ids) for branch in branch_plans]
    all_shared_lines = set.intersection(*branch_line_sets)
    has_line_divergence = bool(set.union(*branch_line_sets) - all_shared_lines)
    has_layout_lanes = any(branch.lane_station_ids for branch in branch_plans)
    line_priority = {line_id: rank for rank, line_id in enumerate(graph.lines)}
    leg_ordered_lines_by_rank = {
        branch.rank: _leg_ordered_line_ids(
            (*continuation_facts[branch.rank], *extra_facts[branch.rank]),
            line_priority,
        )
        for branch in branch_plans
    }
    inherited_branch_order = _inherited_branch_order(
        source_id,
        branch_plans,
        leg_ordered_lines_by_rank,
        bundles,
        incoming,
    )
    offset_line_order = (
        cast(
            tuple[str, ...],
            _ordered_unique(
                line_id
                for branch in sorted(
                    branch_plans,
                    key=lambda item: (
                        item.lane_offset
                        if has_layout_lanes and item.lane_offset is not None
                        else (
                            item.opening_rank
                            if inherited_branch_order is None
                            else inherited_branch_order[item.rank]
                        )
                    ),
                )
                for line_id in leg_ordered_lines_by_rank[branch.rank]
            ),
        )
        if has_line_divergence
        else ()
    )

    member_facts = tuple(dict.fromkeys(all_member_facts))
    member_ids = tuple(fact.id for fact in member_facts)
    branch_member_paths = tuple(
        path for branch in branch_plans for path in branch.resolved_paths
    )
    entry_seam_paths = (
        cast(
            tuple[tuple[ResolvedEdge, ...], ...],
            _ordered_unique(tuple(path[: len(prefix) - 1]) for path in all_lead_paths),
        )
        if len(prefix) > 1
        else ()
    )
    exit_seam_paths = (
        cast(
            tuple[tuple[ResolvedEdge, ...], ...],
            _ordered_unique(tuple(path[-(len(suffix) - 1) :]) for path in final_paths),
        )
        if len(suffix) > 1
        else ()
    )
    seam_edges = cast(
        tuple[ResolvedEdge, ...],
        _ordered_unique(
            edge for path in (*entry_seam_paths, *exit_seam_paths) for edge in path
        ),
    )
    member_paths = (*entry_seam_paths, *branch_member_paths, *exit_seam_paths)
    member_edges = cast(
        tuple[ResolvedEdge, ...],
        _ordered_unique(edge for path in member_paths for edge in path),
    )
    member_id_set = set(member_ids)
    incoming_facts = tuple(
        fact
        for predecessor in incoming.get(source_id, ())
        for fact in bundles[(predecessor, source_id)]
        if fact.id not in member_id_set
    )
    exit_facts = (
        tuple(
            fact
            for target in adjacency.get(authored_join, ())
            for fact in bundles[(authored_join, target)]
            if fact.id not in member_id_set
        )
        if authored_join is not None
        else ()
    )
    entry_handoff_ids = tuple(fact.id for fact in incoming_facts)
    exit_handoff_ids = tuple(fact.id for fact in exit_facts)
    entry_handoff_paths = _paths_for(topology, incoming_facts)
    exit_handoff_paths = _paths_for(topology, exit_facts)
    offset_sign = 1
    entry_offset_carriers = _entry_offset_carriers(
        graph,
        entry_handoff_paths,
        offset_line_order,
        offset_sign,
    )
    handoff_paths = (*entry_handoff_paths, *exit_handoff_paths)
    entry_ports, exit_ports = _port_ids(graph, (*all_raw_paths, *handoff_paths))
    owned_stations = cast(
        tuple[str, ...],
        _ordered_unique(
            station_id
            for edge in member_edges
            for station_id in (edge.source, edge.target)
        ),
    )
    if fork_id not in owned_stations:
        owned_stations = (fork_id, *owned_stations)
    if join_id is not None and join_id not in owned_stations:
        owned_stations = (*owned_stations, join_id)
    plan_id = FanPlanId(semantic_route_id("fan-plan", source_id, *member_ids))
    bundle_handoffs, convergence_handoffs = _handoff_ids(
        topology, (*member_ids, *entry_handoff_ids, *exit_handoff_ids)
    )
    trunk_follower_ids = _trunk_followers(
        graph,
        fork_id,
        join_id,
        (*all_lead_paths, *entry_handoff_paths),
        exit_handoff_paths,
    )
    fork_section_id = graph.section_for_station(fork_id)
    frame_port_ids = tuple(
        port_id
        for port_id in (*entry_ports, *exit_ports)
        if (port := graph.ports.get(port_id)) is not None
        and port.section_id == fork_section_id
    )
    offset_carriers = _offset_carriers(
        graph,
        branches=branch_plans,
        offset_line_order=offset_line_order,
        shared_paths=(
            *entry_seam_paths,
            *exit_seam_paths,
            *entry_handoff_paths,
            *exit_handoff_paths,
        ),
        shared_station_ids=(
            fork_id,
            join_id,
            *trunk_follower_ids,
            *frame_port_ids,
        ),
        upstream_carriers=entry_offset_carriers,
        offset_sign=offset_sign,
    )
    owned_stations = cast(
        tuple[str, ...],
        _ordered_unique(
            (
                *owned_stations,
                *trunk_follower_ids,
                *(carrier.station_id for carrier in offset_carriers),
            )
        ),
    )
    centreline_station_ids = (
        cast(
            tuple[str, ...],
            _ordered_unique(
                station_id
                for station_id in (fork_id, join_id, *trunk_follower_ids)
                if station_id is not None
                and station_id not in graph.ports
                and station_id not in graph.junction_ids
                and graph.section_for_station(station_id) == layout_section_id
            ),
        )
        if layout_section_id is not None
        else ()
    )
    layout_station_ids = (
        *centreline_station_ids,
        *(
            station_id
            for branch in branch_plans
            for station_id in branch.lane_station_ids
        ),
    )
    if len(set(layout_station_ids)) != len(layout_station_ids):
        reason = reason or "overlapping-branch-lane-ownership"
    if _entry_trunk_has_foreign_head(
        graph,
        fork_id=fork_id,
        layout_section_id=layout_section_id,
        entry_port_ids=entry_ports,
        layout_station_ids=layout_station_ids,
    ):
        reason = reason or "section-entry-trunk-has-foreign-head"
    if (
        layout_station_ids
        and appearance_lane_sign is not None
        and appearance_lane_sign < 0
    ):
        offset_carriers = tuple(
            replace(
                carrier,
                assignments=tuple(
                    replace(assignment, slot=-assignment.slot)
                    for assignment in carrier.assignments
                ),
            )
            for carrier in offset_carriers
        )
    if any(graph.station_is_rail(station_id) for station_id in owned_stations):
        reason = reason or "rail-layout-owns-fan-geometry"
    if any(
        station is not None and station.off_track
        for station_id in owned_stations
        if (station := graph.stations.get(station_id)) is not None
    ):
        reason = reason or "off-track-layout-owns-fan-geometry"
    route_emissions = (
        _route_emissions(
            graph,
            fork_id,
            branch_plans,
            exit_ports,
            offset_line_order,
        )
        if reason is None
        else ()
    )
    offset_carriers = _apply_screen_offset_assignments(
        graph,
        branch_plans,
        route_emissions,
        exit_ports,
        owned_stations,
        offset_carriers,
        line_priority,
    )
    offset_carriers = _carriers_within_the_fan(
        graph,
        _apply_solo_branch_offset_assignments(
            graph,
            branch_plans,
            fork_id,
            offset_carriers,
        ),
        owned_stations,
    )
    # A straight-appearance diamond keeps its top branch on the main track, so
    # its lane frame is the section allocator's symmetric one, not the fan's.
    if (
        reason is None
        and authored_join is not None
        and appearance_policy is FanAppearancePolicy.STRAIGHT
    ):
        reason = "straight-diamond-layout-owns-geometry"
    # Same-line terminal and boundary arms have no semantic trunk identity.
    # The section allocator must choose their tracks before it sizes the box.
    if (
        reason is None
        and authored_join is None
        and appearance_policy is FanAppearancePolicy.STRAIGHT
        and len(local_terminal_ids) == 1
        and any(branch.landing_port_ids for branch in branch_plans)
        and len({frozenset(branch.line_ids) for branch in branch_plans}) == 1
    ):
        reason = "same-line-open-fan-layout-owns-geometry"
    local_anchor = next(
        (FanCentrelineAnchor(station_id) for station_id in centreline_station_ids),
        None,
    )
    if local_anchor is None:
        local_anchor = next(
            (
                FanCentrelineAnchor(
                    branch.lane_station_ids[0],
                    cast(float, branch.lane_offset),
                )
                for branch in sorted(
                    branch_plans,
                    key=lambda branch: (
                        abs(branch.lane_offset)
                        if branch.lane_offset is not None
                        else math.inf,
                        branch.rank,
                    ),
                )
                if branch.lane_station_ids and branch.lane_offset is not None
            ),
            None,
        )
    candidate_centreline_port_ids = (
        _centreline_port_ids(
            graph,
            direction,
            layout_section_id,
            (*entry_ports, *exit_ports),
            branch_plans,
        )
        if reason is None
        else ()
    )
    needs_centreline_anchor = bool(layout_station_ids or candidate_centreline_port_ids)
    candidate_centreline_anchor = (
        _centreline_anchor(
            graph,
            direction=direction,
            frame=frame,
            fork_id=fork_id,
            layout_section_id=layout_section_id,
            branches=branch_plans,
            entry_port_ids=entry_ports,
            exit_port_ids=exit_ports,
            local_frame_anchor=local_anchor,
        )
        if reason is None and needs_centreline_anchor
        else None
    )
    if reason is None and needs_centreline_anchor:
        reason = _centreline_anchor_reason(
            candidate_centreline_anchor, tuple(branch_plans), appearance_policy
        )
    planned = reason is None
    if not planned:
        route_emissions = ()
    centreline_port_ids = candidate_centreline_port_ids if planned else ()
    owned_stations = cast(
        tuple[str, ...],
        _ordered_unique((*owned_stations, *centreline_port_ids)),
    )
    plan = FanPlan(
        id=plan_id,
        system_id=None,
        authored_source_id=source_id,
        authored_join_station_id=authored_join,
        fork_station_id=fork_id,
        direction=direction,
        join_station_id=join_id,
        appearance_policy=appearance_policy,
        appearance_centreline_branch_id=(
            appearance_centreline_branch_id if planned else None
        ),
        appearance_lane_pitch=lane_pitch if planned else None,
        appearance_lane_sign=appearance_lane_sign if planned else None,
        branches=(
            tuple(branch_plans)
            if planned
            else tuple(
                replace(
                    branch,
                    lane_station_ids=(),
                    lane_offset=None,
                    diagonal_runway=None,
                )
                for branch in branch_plans
            )
        ),
        offset_line_order=offset_line_order,
        authored_edge_ids=member_ids,
        connector_ids=(),
        member_ids=(),
        resolved_member_paths=member_paths,
        resolved_member_edges=member_edges,
        entry_seam_paths=entry_seam_paths,
        exit_seam_paths=exit_seam_paths,
        resolved_seam_edges=seam_edges,
        entry_handoff_edge_ids=entry_handoff_ids,
        exit_handoff_edge_ids=exit_handoff_ids,
        entry_handoff_paths=entry_handoff_paths,
        exit_handoff_paths=exit_handoff_paths,
        offset_carriers=offset_carriers if planned else (),
        route_expectations=(
            tuple(
                FanRouteExpectation(
                    edge=edge,
                    member_id=None,
                    branch_ids=tuple(
                        branch.id
                        for branch in branch_plans
                        if any(edge in path for path in branch.resolved_paths)
                    ),
                )
                for edge in member_edges
            )
            if planned
            else ()
        ),
        route_emissions=route_emissions,
        centreline_port_ids=centreline_port_ids,
        entry_port_ids=entry_ports,
        exit_port_ids=exit_ports,
        trunk_follower_ids=trunk_follower_ids,
        entry_runway=minimum_runway if planned else None,
        exit_runway=minimum_runway if planned else None,
        centreline_reference_id=None,
        demand_ids=(),
        bundle_handoff_ids=bundle_handoffs,
        convergence_handoff_ids=convergence_handoffs,
        owned_station_ids=owned_stations,
        centreline_station_ids=centreline_station_ids if planned else (),
        centreline_anchor=candidate_centreline_anchor if planned else None,
        local_frame_anchor=local_anchor if planned else None,
        frame=frame if planned else None,
        disposition=(
            FanPlanDisposition.PLANNED if planned else FanPlanDisposition.LEGACY
        ),
        legacy_reason=reason,
    )
    return plan


def _cede_read_claims(
    plans: tuple[FanPlan, ...], ranks: Mapping[str, int]
) -> dict[FanPlanId, dict[str, FanPlanId]]:
    """Name, per plan, the station seats it reads and the fan it reads them from.

    A boundary station is checked against its own settled coordinate, so a fan
    bounded by one states nothing that a fan which lanes, centres, carries or
    lands on it has not already stated: that fan keeps the seat outright.  Where
    no fan states the station, every contender is reading a coordinate somebody
    else settled, so which of them holds the seat decides no geometry and needs
    only to be one choice rather than the right one: the earliest fork keeps it,
    by the authored rank of the fork and then the plan id so the choice is total
    and independent of iteration order.  A station every stater holds only on
    its centreline is the trunk two forks hang off, and a trunk carries one run
    however many fans fork from it, so those staters say the same thing about it
    and the same earliest-first order picks which of them says it.  Two fans
    that state one station any other way do state it differently and are left to
    contend.
    """
    precedence = {
        plan.id: (ranks.get(plan.fork_station_id, len(ranks)), str(plan.id))
        for plan in plans
    }
    stated = {plan.id: stated_station_ids(plan) for plan in plans}
    holders: defaultdict[str, list[FanPlan]] = defaultdict(list)
    for plan in plans:
        for station_id in claimed_station_ids(plan):
            holders[station_id].append(plan)
    read_from: defaultdict[FanPlanId, dict[str, FanPlanId]] = defaultdict(dict)
    for station_id, contesting in holders.items():
        if len(contesting) < 2:
            continue
        staters = [plan for plan in contesting if station_id in stated[plan.id]]
        if len(staters) > 1:
            continue
        owner = (
            staters[0]
            if staters
            else min(contesting, key=lambda plan: precedence[plan.id])
        )
        for plan in contesting:
            if plan.id != owner.id:
                read_from[plan.id][station_id] = owner.id
    return dict(read_from)


def _cede_read_edges(
    plans: tuple[FanPlan, ...],
) -> dict[FanPlanId, dict[ResolvedEdge, FanPlanId]]:
    """Name, per plan, the seam edges it reads and the fan that draws them.

    A seam is an edge a fan reaches to meet its neighbours, not one it draws, so
    a fan holding an edge as a seam states no route another fan carrying it on a
    branch has not already stated: that fan draws it and every reader hands the
    route off.  Two fans drawing one edge state it independently and are left to
    contend.
    """
    drawn = {plan.id: drawn_member_edges(plan) for plan in plans}
    holders: defaultdict[ResolvedEdge, list[FanPlan]] = defaultdict(list)
    for plan in plans:
        for edge in claimed_member_edges(plan):
            holders[edge].append(plan)
    read_from: defaultdict[FanPlanId, dict[ResolvedEdge, FanPlanId]] = defaultdict(dict)
    for edge, contesting in holders.items():
        if len(contesting) < 2:
            continue
        drawers = [plan for plan in contesting if edge in drawn[plan.id]]
        if len(drawers) != 1:
            continue
        owner = drawers[0]
        for plan in contesting:
            if plan.id != owner.id:
                read_from[plan.id][edge] = owner.id
    return dict(read_from)


def _seat_cessions(
    plan: FanPlan,
    read_from: Mapping[FanPlanId, Mapping[str, FanPlanId]],
    edge_read_from: Mapping[FanPlanId, Mapping[ResolvedEdge, FanPlanId]],
) -> FanPlan:
    """Record on *plan* the seats and seam routes it reads rather than states."""
    ceded = read_from.get(plan.id)
    ceded_edges = edge_read_from.get(plan.id)
    if not ceded and not ceded_edges:
        return plan
    return replace(
        plan,
        ceded_station_ids=tuple(sorted({*plan.ceded_station_ids, *(ceded or ())})),
        ceded_member_edges=tuple(
            edge
            for edge in plan.resolved_member_edges
            if edge in plan.ceded_member_edges or edge in (ceded_edges or ())
        ),
    )


def _kept_cessions(
    cessions: Mapping[FanPlanId, Mapping[_T, FanPlanId]],
    declined: AbstractSet[FanPlanId],
) -> dict[FanPlanId, dict[_T, FanPlanId]]:
    """Drop every cession made to a plan in *declined*."""
    return {
        plan_id: {
            item: owner_id
            for item, owner_id in ceded.items()
            if owner_id not in declined
        }
        for plan_id, ceded in cessions.items()
    }


def _contention_reason(
    left: FanPlan, right: FanPlan, shared_edges: bool, shared: set[str]
) -> str | None:
    """Which owner holds the part *left* and *right* both reach.

    Two readings of one fork that split the lines between them are the two line
    groups of one symmetric or straight fan, whose stations the diamond layout
    seats: giving either reading the fork moves those stations off the seats
    that layout settled, so it holds the whole fork.  A pair of chained fans
    sharing only centreline stations is two forks on the trunk the row aligner
    placed between them, and a fan claiming that trunk drags the aligned row
    with it, so the local layout holds it.  A pair landing branches on one port
    from two forks reaches a seat ``_align_entry_ports`` (``phases/ports.py``,
    line 183) gives from the section frame and the runs arriving, which the fan
    reads back rather than writes (``inter_section_handlers.py:2457``): letting
    either fan state that seat puts its section's own content outside the box
    the allocator sized for it, so the allocator holds the landing.  A pair
    sharing no route and no seat
    either states -- one fan's boundary against another's -- would have settled
    by cession, and contends only because the fan it ceded to is contending:
    ``None`` leaves it to take its reason from that fan.  Anything else is two
    fans reaching the same geometry with no owner named for the shared part.
    """
    if left.fork_station_id == right.fork_station_id:
        return "line-split-fork-layout-owns-geometry"
    both_stated = shared & stated_station_ids(left) & stated_station_ids(right)
    if both_stated:
        if all(
            station_id in plan.centreline_station_ids
            and station_id not in _laned_station_ids(plan)
            for station_id in both_stated
            for plan in (left, right)
        ):
            return "chained-trunk-layout-owns-geometry"
        if both_stated <= _landing_port_ids(left) & _landing_port_ids(right):
            return "shared-landing-port-allocator-owns-the-seat"
        return "overlapping-fan-ownership"
    return "overlapping-fan-ownership" if shared_edges else None


def _claim_conflicts(contenders: tuple[FanPlan, ...]) -> dict[FanPlanId, str]:
    """Name, per plan, why it reaches an edge or station another plan holds."""
    reasons: dict[FanPlanId, str] = {}
    cascades: defaultdict[FanPlanId, list[FanPlanId]] = defaultdict(list)
    for index, left in enumerate(contenders):
        left_authored = set(left.authored_edge_ids)
        left_resolved = claimed_member_edges(left)
        left_stations = claimed_station_ids(left)
        for right in contenders[index + 1 :]:
            shared = left_stations.intersection(claimed_station_ids(right))
            shared_edges = bool(
                left_authored.intersection(right.authored_edge_ids)
                or left_resolved.intersection(claimed_member_edges(right))
            )
            if not (shared_edges or shared):
                continue
            reason = _contention_reason(left, right, shared_edges, shared)
            for plan, other in ((left, right), (right, left)):
                if reason is None:
                    cascades[plan.id].append(other.id)
                elif reasons.get(plan.id) != "overlapping-fan-ownership":
                    reasons[plan.id] = reason
    for plan_id, partners in sorted(cascades.items(), key=lambda item: str(item[0])):
        if plan_id in reasons:
            continue
        reasons[plan_id] = next(
            (
                reasons[partner]
                for partner in sorted(partners, key=str)
                if partner in reasons
            ),
            "overlapping-fan-ownership",
        )
    return reasons


def _folded_fork_readings(
    plans: tuple[FanPlan, ...], ranks: Mapping[str, int]
) -> set[FanPlanId]:
    """The candidates at a fork another candidate already reads the fan from.

    A fork has one set of branches leaving it however many runs arrive at it,
    and ``FanPlanQuery.build`` admits one planned fan per fork accordingly.
    Candidates are built per authored source, so feeders that merge before the
    fork each raise one: where those readings land the same branches on the same
    lines they are one fan read twice, differing only in the lead each feeder
    takes into it.  The earliest by the authored rank of the fork and then the
    plan id is the fan; the rest are folded away and their leads route as
    ordinary runs.  Readings that split the lines between them each carry a
    branch set the others do not, so folding would drop a line's fan.
    """
    by_fork: defaultdict[str, list[FanPlan]] = defaultdict(list)
    for plan in plans:
        by_fork[plan.fork_station_id].append(plan)
    folded: set[FanPlanId] = set()
    for fork_id, readings in by_fork.items():
        if len(readings) < 2:
            continue
        shapes = {
            tuple(
                (branch.landing_port_ids, branch.line_ids) for branch in plan.branches
            )
            for plan in readings
        }
        if len(shapes) > 1:
            continue
        keeper = min(
            readings,
            key=lambda plan: (ranks.get(fork_id, len(ranks)), str(plan.id)),
        )
        folded.update(plan.id for plan in readings if plan.id != keeper.id)
    return folded


def _reject_overlaps(
    plans: tuple[FanPlan, ...],
    facts_by_id: Mapping[ConnectorId, AuthoredEdgeFact],
    ranks: Mapping[str, int],
) -> tuple[FanPlan, ...]:
    subsumed: set[FanPlanId] = set()
    for inner in plans:
        lead_ids = {
            edge_id
            for edge_id in inner.authored_edge_ids
            if facts_by_id[edge_id].key.source == inner.authored_source_id
        }
        if any(
            inner.authored_source_id in outer.owned_station_ids
            and lead_ids.issubset(outer.authored_edge_ids)
            for outer in plans
            if outer.id != inner.id
        ):
            subsumed.add(inner.id)
    subsumed |= _folded_fork_readings(
        tuple(plan for plan in plans if plan.id not in subsumed), ranks
    )
    plans = tuple(plan for plan in plans if plan.id not in subsumed)
    candidates = tuple(plan for plan in plans if plan.legacy_reason is None)
    read_from = _cede_read_claims(candidates, ranks)
    edge_read_from = _cede_read_edges(candidates)
    # A seat or a seam route can only be read from a fan that goes on to state
    # it, so a cession to a plan that itself ends up declined is void and its
    # reader takes the station or edge back.  Taking one back only ever adds
    # contention, so the declined set grows with each round and the loop settles.
    while True:
        contenders = tuple(
            _seat_cessions(plan, read_from, edge_read_from) for plan in candidates
        )
        conflicts = _claim_conflicts(contenders)
        kept_stations = _kept_cessions(read_from, conflicts.keys())
        kept_edges = _kept_cessions(edge_read_from, conflicts.keys())
        if kept_stations == read_from and kept_edges == edge_read_from:
            break
        read_from, edge_read_from = kept_stations, kept_edges
    seated = {plan.id: plan for plan in contenders}
    return tuple(
        _legacy(seated.get(plan.id, plan), reason)
        if (reason := conflicts.get(plan.id)) is not None
        else seated.get(plan.id, plan)
        for plan in plans
    )


def _bind_semantic_ownership(
    plan: FanPlan,
    scaffold: RouteSemanticScaffold,
) -> FanPlan:
    """Bind one recognised fan to canonical systems and emission members."""
    connector_ids = tuple(
        edge_id
        for edge_id in plan.authored_edge_ids
        if edge_id in scaffold.system_by_connector
    )
    system_ids = {
        scaffold.system_by_connector[connector_id] for connector_id in connector_ids
    }
    if len(system_ids) > 1:
        raise ValueError(f"fan {plan.id!s} spans canonical route systems")
    system_id = next(iter(system_ids), None)

    def member_ids_for_edges(
        edges: Iterable[ResolvedEdge],
    ) -> tuple[EmissionMemberId, ...]:
        return cast(
            tuple[EmissionMemberId, ...],
            _ordered_unique(
                member_id
                for edge in edges
                if (member_id := scaffold.member_id_by_edge.get(edge)) is not None
            ),
        )

    branches = tuple(
        replace(
            branch,
            connector_ids=tuple(
                edge_id
                for edge_id in branch.authored_edge_ids
                if edge_id in scaffold.system_by_connector
            ),
            member_ids=member_ids_for_edges(
                edge for path in branch.resolved_paths for edge in path
            ),
        )
        for branch in plan.branches
    )
    ceded_edges = set(plan.ceded_member_edges)
    member_ids = member_ids_for_edges(
        edge for edge in plan.resolved_member_edges if edge not in ceded_edges
    )
    reference_id: SharedReferenceId | None = None
    demand_ids: tuple[DemandId, ...] = ()
    if plan.owns_geometry and system_id is not None:
        reference_id, demand_ids = _fan_resource_ids(plan.id, branches)
    if connector_ids and not member_ids:
        return _legacy(
            replace(
                plan,
                system_id=system_id,
                connector_ids=connector_ids,
                branches=branches,
                centreline_reference_id=reference_id,
                demand_ids=demand_ids,
            ),
            "fan-route-system-has-no-emission-member",
        )
    expectations = (
        tuple(
            replace(
                expectation,
                member_id=(
                    None
                    if expectation.edge in ceded_edges
                    else scaffold.member_id_by_edge.get(expectation.edge)
                ),
            )
            for expectation in plan.route_expectations
        )
        if plan.owns_geometry
        else ()
    )
    return replace(
        plan,
        system_id=system_id,
        connector_ids=connector_ids,
        member_ids=member_ids,
        branches=branches,
        route_expectations=expectations,
        centreline_reference_id=reference_id,
        demand_ids=demand_ids,
    )


def build_fan_plan_execution(
    graph: MetroGraph,
    topology: FanTopologyQuery,
    *,
    x_spacing: float,
    y_spacing: float,
    minimum_runway: float,
) -> FanPlanExecution:
    """Recognise every authored fan and plan each complete object atomically."""
    for name, spacing in (("x", x_spacing), ("y", y_spacing)):
        if not math.isfinite(spacing) or spacing <= 0:
            raise ValueError(f"fan {name}-spacing must be finite and positive")
    if not math.isfinite(minimum_runway) or minimum_runway <= 0:
        raise ValueError("fan minimum runway must be finite and positive")
    facts = _authored_edges(topology)
    adjacency, incoming, bundles = _adjacency(facts)
    ranks = _node_rank(facts)
    from nf_metro.layout.routing.reversal import tb_positive_fan_sections

    context = _FanPlanningContext(
        graph=graph,
        topology=topology,
        adjacency=adjacency,
        incoming=incoming,
        bundles=bundles,
        ranks=ranks,
        x_spacing=x_spacing,
        y_spacing=y_spacing,
        minimum_runway=minimum_runway,
        section_layers={},
        tb_positive_fan=tb_positive_fan_sections(graph),
    )
    plans = tuple(
        _build_candidate(context, source_id, targets)
        for source_id, targets in adjacency.items()
        if len(targets) >= 2
    )
    plans = _reject_overlaps(plans, {fact.id: fact for fact in facts}, ranks)
    semantic_scaffold = None
    if graph.route_topology is not None:
        connector_groups: list[tuple[ConnectorId, ...]] = []
        for plan in plans:
            connector_ids: list[ConnectorId] = []
            for connector_id in plan.authored_edge_ids:
                try:
                    topology.connector(connector_id)
                except KeyError:
                    continue
                connector_ids.append(connector_id)
            if connector_ids:
                connector_groups.append(tuple(connector_ids))
        semantic_scaffold = build_route_semantic_scaffold(
            graph,
            cast(RouteTopologyQuery, topology),
            coupled_connector_groups=tuple(connector_groups),
        )
    if semantic_scaffold is not None:
        plans = tuple(
            _bind_semantic_ownership(plan, semantic_scaffold) for plan in plans
        )
    return FanPlanExecution(
        query=FanPlanQuery.build(plans),
        scaffold=semantic_scaffold,
    )


def install_fan_plan_execution(graph: MetroGraph, execution: FanPlanExecution) -> None:
    """Publish one complete build for later layout and routing consumers."""
    graph.fan_plan_execution = execution


def _fan_runtime_edges(plan: FanPlan) -> tuple[ResolvedEdge, ...]:
    """Return the planned members and neighbouring hand-off edges."""
    return _ordered_unique(
        (
            *(expectation.edge for expectation in plan.route_expectations),
            *(
                edge
                for path in (*plan.entry_handoff_paths, *plan.exit_handoff_paths)
                for edge in path
            ),
        )
    )


def _fan_boundary_station_ids(plan: FanPlan) -> frozenset[str]:
    """Return hubs, ports, landings, and neighbouring hand-off boundaries."""
    return frozenset(
        (
            *plan.entry_port_ids,
            *plan.exit_port_ids,
            *(
                port_id
                for branch in plan.branches
                for port_id in branch.landing_port_ids
            ),
            *(path[-1].target for path in plan.entry_handoff_paths if path),
            *(path[0].source for path in plan.exit_handoff_paths if path),
            *((plan.join_station_id,) if plan.join_station_id is not None else ()),
        )
    )


def _boundary_spreads_on_axis(
    graph: MetroGraph,
    station_id: str,
    *,
    perpendicular_sides: Sequence[PortSide],
    axis_name: str,
) -> bool:
    """Whether a boundary station separates its lines along *axis_name*.

    A station holds its lane offset on the axis its own section spreads lines
    across.  A boundary standing in a section that spreads them the other way
    -- a hand-off inside a vertical-flow section feeding a horizontal fan --
    spends the offset on the fan's flow axis, so the fan's lane coordinate
    there is the station's own.  A port the bundle enters or leaves
    perpendicular turns at the boundary and likewise carries no lane offset
    across it.
    """
    port = graph.ports.get(station_id)
    if port is not None:
        return port.side not in perpendicular_sides
    section = graph.sections.get(graph.section_for_station(station_id) or "")
    if section is None:
        return True
    return (
        lanes_run_along_x(section.direction)
        if axis_name == "x"
        else lanes_run_along_y(section.direction)
    )


def _validate_fan_runtime_frame(
    graph: MetroGraph,
    plan: FanPlan,
    bound_routes: Mapping[ResolvedEdge, RoutedPath],
    station_offsets: dict[tuple[str, str], float],
) -> None:
    """Validate final route continuity against one fan's frozen frame."""
    from nf_metro.layout.routing.common import apply_route_offsets

    context = f"planned fan {plan.id!s} in route system {plan.system_id!s}"
    endpoints: dict[
        tuple[str, str],
        list[tuple[ResolvedEdge, tuple[float, float], tuple[tuple[float, float], ...]]],
    ] = defaultdict(list)
    for edge, route in bound_routes.items():
        if not route.points:
            raise FanRouteInvariantError(
                f"{context} emitted an empty final route for {edge!r}"
            )
        points = tuple(apply_route_offsets(route, station_offsets))
        endpoints[(edge.source, edge.line_id)].append((edge, points[0], points))
        endpoints[(edge.target, edge.line_id)].append((edge, points[-1], points))

    uses_one_boundary_frame = not (
        plan.appearance_policy is FanAppearancePolicy.STRAIGHT
        and plan.authored_join_station_id is not None
    )
    if (
        plan.frame is not None
        and plan.direction is not None
        and uses_one_boundary_frame
    ):
        secondary_axis = 0 if plan.frame.secondary.name == "x" else 1
        perpendicular_sides = perpendicular_port_sides(plan.direction)
        for station_id in _fan_boundary_station_ids(plan):
            if station_id == plan.fork_station_id:
                continue
            station = graph.stations.get(station_id)
            if station is None:
                raise FanRouteInvariantError(
                    f"{context} has no realised boundary station {station_id!r}"
                )
            spreads_on_frame_axis = _boundary_spreads_on_axis(
                graph,
                station_id,
                perpendicular_sides=perpendicular_sides,
                axis_name=plan.frame.secondary.name,
            )
            for (endpoint_id, line_id), incident in endpoints.items():
                if endpoint_id != station_id:
                    continue
                offset = (
                    station_offsets.get((station_id, line_id), 0.0)
                    if spreads_on_frame_axis
                    else 0.0
                )
                expected = (
                    plan.frame.secondary.get(station)
                    + plan.frame.secondary_sign * offset
                )
                if any(
                    abs(point[secondary_axis] - expected) > COORD_TOLERANCE_FINE
                    for _edge, point, _points in incident
                ):
                    raise FanRouteInvariantError(
                        f"{context} drifted from its planned boundary frame at "
                        f"{station_id!r} on {line_id!r}"
                    )

    for (station_id, line_id), incident in endpoints.items():
        if len(incident) < 2:
            continue
        reference = incident[0][1]
        axes = (
            (0 if plan.frame.secondary.name == "x" else 1,)
            if station_id == plan.fork_station_id and plan.frame is not None
            else (0, 1)
        )
        # A branch leaving a junction can open with a lead-in seated back along
        # the run that feeds it, so its end stands short of the station while
        # drawing over a sibling's stroke rather than away from it.
        if any(
            any(
                abs(point[axis] - reference[axis]) > COORD_TOLERANCE_FINE
                for axis in axes
            )
            and all(
                point_to_polyline_distance(point, other) > COORD_TOLERANCE_FINE
                for _other_edge, _other_point, other in incident
                if other is not points
            )
            for _edge, point, points in incident[1:]
        ):
            raise FanRouteInvariantError(
                f"{context} has a final route frame discontinuity at "
                f"{station_id!r} on {line_id!r}"
            )

    if plan.layout_station_ids or plan.fork_station_id not in graph.junction_ids:
        return
    if plan.frame is None:
        return
    fork = graph.stations.get(plan.fork_station_id)
    if fork is None:
        raise FanRouteInvariantError(f"{context} has no realised fork station")
    secondary_axis = 0 if plan.frame.secondary.name == "x" else 1
    planned_base = plan.frame.secondary.get(fork)
    carrier = next(
        (
            item
            for item in plan.offset_carriers
            if item.station_id == plan.fork_station_id
        ),
        None,
    )
    fork_endpoints = tuple(
        (line_id, point)
        for (station_id, line_id), incident in endpoints.items()
        if station_id == plan.fork_station_id
        for _edge, point, _points in incident
    )
    if carrier is None:
        for line_id, point in fork_endpoints:
            external_offset = (
                station_offsets.get((plan.fork_station_id, line_id), 0.0)
                if secondary_axis == 1
                else 0.0
            )
            if (
                abs(point[secondary_axis] - planned_base - external_offset)
                > COORD_TOLERANCE_FINE
            ):
                raise FanRouteInvariantError(
                    f"{context} drifted from its planned fork centreline"
                )
        return
    slots = {assignment.line_id: assignment.slot for assignment in carrier.assignments}
    step = graph_offset_step(graph)
    bases = [
        point[secondary_axis] - plan.frame.secondary_sign * slots[line_id] * step
        for line_id, point in fork_endpoints
        if line_id in slots
    ]
    if any(abs(base - planned_base) > COORD_TOLERANCE_FINE for base in bases):
        raise FanRouteInvariantError(f"{context} drifted from its planned fork frame")


def validate_fan_route_emissions(
    graph: MetroGraph,
    routes: Sequence[RoutedPath],
    station_offsets: Mapping[tuple[str, str], float] | None = None,
    *,
    planned_system_ids: frozenset[RouteSystemId] | None = None,
    covered_edges: frozenset[ResolvedEdge] = frozenset(),
) -> None:
    """Bind every planned fan member and exclusive emitter exactly once.

    A leg in *covered_edges* is one another member draws end to end, so it
    carries no geometry of its own: a fan whose path runs over it reads it
    rather than emitting it, and binding it to its carrier would state the
    carrier's far endpoint at this leg's own.
    """

    def emitted_plan(plan: FanPlan) -> bool:
        return plan.owns_geometry and (
            planned_system_ids is None
            or plan.system_id is None
            or plan.system_id in planned_system_ids
        )

    routes_by_edge: dict[ResolvedEdge, list[RoutedPath]] = defaultdict(list)
    for route in routes:
        routes_by_edge[
            ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        ].append(route)
    bound_routes_by_plan: list[tuple[FanPlan, dict[ResolvedEdge, RoutedPath]]] = []
    for plan in graph.fan_plans:
        if not emitted_plan(plan):
            continue
        bound_routes: dict[ResolvedEdge, RoutedPath] = {}
        for edge in _fan_runtime_edges(plan):
            bound = routes_by_edge.get(edge, ())
            if not bound and edge in covered_edges:
                continue
            if len(bound) != 1:
                raise RuntimeError(
                    f"planned fan {plan.id!s} in route system {plan.system_id!s} "
                    f"expected one final route for {edge!r}; "
                    f"found {len(bound)}"
                )
            bound_routes[edge] = bound[0]
        bound_routes_by_plan.append((plan, bound_routes))

    expected = tuple(
        (plan, emission)
        for plan in graph.fan_plans
        if emitted_plan(plan)
        for emission in plan.route_emissions
    )
    query = graph.fan_plan_query
    consumed: dict[ResolvedEdge, int] = defaultdict(int)
    for route in routes:
        tagged = route.fan_plan_id is not None or route.fan_route_emitter is not None
        if not tagged:
            continue
        edge = ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        binding = (
            query.route_emission_for_resolved_edge(edge) if query is not None else None
        )
        if binding is None:
            raise RuntimeError(f"unclaimed fan route emission tagged {edge!r}")
        plan, _branch, emission = binding
        if not emitted_plan(plan):
            raise RuntimeError(
                f"non-owning fan plan {plan.id!s} in route system "
                f"{plan.system_id!s} emitted geometry for {edge!r}"
            )
        if (
            route.fan_plan_id != plan.id
            or route.fan_route_emitter != emission.emitter.value
        ):
            raise RuntimeError(
                f"planned fan {plan.id!s} route tag drifted for {edge!r}"
            )
        consumed[edge] += 1
    for plan, emission in expected:
        edge = emission.edge
        if consumed.get(edge, 0) != 1:
            raise RuntimeError(
                f"planned fan {plan.id!s} expected one consumed route for {edge!r}; "
                f"found {consumed.get(edge, 0)}"
            )

    if station_offsets is None:
        return
    from nf_metro.layout.routing.invariants import check_no_hanging_routes

    if not bound_routes_by_plan:
        return
    route_list = list(routes)
    offset_dict = dict(station_offsets)
    for plan, bound_routes in bound_routes_by_plan:
        _validate_fan_runtime_frame(graph, plan, bound_routes, offset_dict)
    planned_routes = tuple(
        {
            id(route): route
            for _plan, bound_routes in bound_routes_by_plan
            for route in bound_routes.values()
        }.values()
    )
    hanging = next(
        (
            item
            for item in check_no_hanging_routes(
                graph,
                route_list,
                offset_dict,
                routes_to_check=planned_routes,
            )
        ),
        None,
    )
    if hanging is not None:
        raise RuntimeError(f"planned fan member route drifted: {hanging.message()}")
