"""Structural fan recognition and immutable relative geometry plans.

This module is intentionally independent of layout phase order and routing
dispatch.  It reads authored edge identity plus resolver lineage, recognises a
complete fan, and either gives that whole object one owner or records one
deterministic legacy disposition.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
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


def _appearance_centreline_branch_id(
    branches: Sequence[FanBranchPlan],
    appearance_policy: FanAppearancePolicy,
    structural_trunk_rank: int | None,
) -> FanBranchPlanId | None:
    """Choose the branch that a straight local fan keeps on its main track."""
    if appearance_policy is not FanAppearancePolicy.STRAIGHT or not any(
        branch.lane_station_ids for branch in branches
    ):
        return None
    trunk_branches = tuple(
        branch for branch in branches if branch.is_trunk_continuation
    )
    if len(trunk_branches) == 1:
        return trunk_branches[0].id
    if structural_trunk_rank is not None:
        structural_trunk = next(
            (branch for branch in branches if branch.rank == structural_trunk_rank),
            None,
        )
        if structural_trunk is not None:
            return structural_trunk.id
    return min(branches, key=lambda branch: (branch.opening_rank, branch.rank)).id


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
    return 1.0


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
    _by_system: Mapping[RouteSystemId, tuple[FanPlan, ...]]
    _by_member: Mapping[EmissionMemberId, FanPlan]
    _by_fork: Mapping[str, FanPlan]
    _by_authored_edge: Mapping[ConnectorId, FanPlan]
    _structural_by_resolved_edge: Mapping[ResolvedEdge, FanPlan]
    _structural_branch_by_resolved_edge: Mapping[ResolvedEdge, FanBranchPlan]
    _route_emission_by_resolved_edge: Mapping[
        ResolvedEdge, tuple[FanPlan, FanBranchPlan, FanRouteEmission]
    ]
    _by_station: Mapping[str, FanPlan]

    @classmethod
    def build(cls, plans: tuple[FanPlan, ...]) -> FanPlanQuery:
        by_id: dict[FanPlanId, FanPlan] = {}
        by_system: dict[RouteSystemId, list[FanPlan]] = defaultdict(list)
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
            if plan.system_id is not None:
                by_system[plan.system_id].append(plan)
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
            for edge in plan.resolved_member_edges:
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
            for station_id in plan.owned_station_ids:
                if station_id in by_station:
                    raise ValueError("two planned fans own one station")
                by_station[station_id] = plan
        return cls(
            plans=plans,
            _by_id=MappingProxyType(by_id),
            _by_system=MappingProxyType(
                {key: tuple(value) for key, value in by_system.items()}
            ),
            _by_member=MappingProxyType(by_member),
            _by_fork=MappingProxyType(by_fork),
            _by_authored_edge=MappingProxyType(by_authored_edge),
            _structural_by_resolved_edge=MappingProxyType(structural_by_resolved_edge),
            _structural_branch_by_resolved_edge=MappingProxyType(
                structural_branch_by_resolved_edge
            ),
            _route_emission_by_resolved_edge=MappingProxyType(
                route_emission_by_resolved_edge
            ),
            _by_station=MappingProxyType(by_station),
        )

    def plan(self, plan_id: FanPlanId) -> FanPlan:
        return self._by_id[plan_id]

    def plans_for_system(self, system_id: RouteSystemId) -> tuple[FanPlan, ...]:
        return self._by_system.get(system_id, ())

    def owner_for_member(self, member_id: EmissionMemberId) -> FanPlan | None:
        return self._by_member.get(member_id)

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

    def owner_for_station(self, station_id: str) -> FanPlan | None:
        return self._by_station.get(station_id)


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
) -> str | None:
    distances = tuple(_distances(adjacency, root) for root in branch_roots)
    common = set(distances[0]).intersection(*(set(item) for item in distances[1:]))
    candidates = [
        station_id
        for station_id in common
        if all(item[station_id] > 0 for item in distances)
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
) -> tuple[AuthoredEdgeFact, ...] | None:
    result: list[AuthoredEdgeFact] = []
    for source, target in zip(path, path[1:]):
        matching = tuple(
            fact
            for fact in bundles[(source, target)]
            if line_ids is None or fact.key.line_id in line_ids
        )
        if not matching:
            return None
        result.extend(matching)
    return tuple(result)


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


def _grid_position(graph: MetroGraph, section_id: str) -> tuple[int, int]:
    section = graph.sections[section_id]
    override = graph.grid_overrides.get(section_id)
    if override is not None:
        return override[0], override[1]
    return section.grid_col, section.grid_row


def _trunk_followers(
    graph: MetroGraph,
    fork_id: str,
    join_id: str | None,
    approach_paths: Iterable[tuple[ResolvedEdge, ...]],
    departure_paths: Iterable[tuple[ResolvedEdge, ...]],
) -> tuple[str, ...]:
    result: list[str] = []
    for path in approach_paths:
        nodes = _path_nodes(path)
        if fork_id not in nodes:
            continue
        for station_id in reversed(nodes[: nodes.index(fork_id)]):
            if station_id in graph.ports or station_id in graph.junction_ids:
                continue
            if station_id not in result:
                result.append(station_id)
            break
    if join_id is None:
        return tuple(result)
    for path in departure_paths:
        nodes = _path_nodes(path)
        if join_id not in nodes:
            continue
        for station_id in nodes[nodes.index(join_id) + 1 :]:
            if station_id in graph.ports or station_id in graph.junction_ids:
                continue
            if station_id not in result:
                result.append(station_id)
            break
    return tuple(result)


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
            assignments=tuple(
                FanOffsetAssignment(line_id, rank * offset_sign)
                for rank, line_id in enumerate(offset_line_order)
                if line_id in carried_lines
            ),
        )
        for station_id, carried_lines in carriers.items()
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
        if len(by_branch) < 2:
            continue
        add_station(
            station_id,
            (line_id for lines in by_branch.values() for line_id in lines),
        )

    return tuple(
        FanOffsetCarrier(
            station_id=station_id,
            assignments=tuple(
                FanOffsetAssignment(line_id, rank * offset_sign)
                for rank, line_id in enumerate(offset_line_order)
                if line_id in lines
            ),
        )
        for station_id, lines in carrier_lines.items()
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
) -> tuple[str, ...]:
    """Freeze boundary ports that continue one fan's local centreline."""
    layout_section = graph.sections.get(layout_section_id or "")
    if direction is None or layout_section is None:
        return ()
    fan_is_horizontal = not lanes_run_along_x(direction)
    layout_column, layout_row = _grid_position(graph, layout_section.id)
    result: list[str] = []
    for port_id in port_ids:
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


def _lane_station_ids(
    graph: MetroGraph,
    paths: Iterable[tuple[ResolvedEdge, ...]],
    *,
    section_id: str | None,
    fork_id: str,
    join_id: str | None,
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
                and graph.section_for_station(station_id) == section_id
                and station_id not in station_ids
            ):
                station_ids.append(station_id)
    return tuple(station_ids)


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
    node_paths: list[tuple[str, ...]] = []
    if authored_join is not None:
        reaches_join = _reverse_reachable(ctx.incoming, authored_join)
        for target in branch_targets:
            path = _unique_path_to_join(adjacency, target, authored_join, reaches_join)
            if path is None:
                reason = reason or "ambiguous-branch-to-join"
                path = _linear_path(adjacency, target)
            node_paths.append((source_id, *path))
    else:
        node_paths = [
            (source_id, *_linear_path(adjacency, target)) for target in branch_targets
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
    if any(facts is None for facts in selected_continuations):
        reason = reason or "unsupported-branch-line-transition"
    continuation_facts = tuple(
        facts if facts is not None else _facts_for_node_path(path, bundles) or ()
        for path, facts in zip(node_paths, selected_continuations, strict=True)
    )
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
                ),
                is_trunk_continuation=any(
                    graph.ports[port_id].section_id == layout_section_id
                    for port_id in _port_ids(graph, raw_continuation)[1]
                )
                or rank == structural_trunk_rank,
                terminal=terminal,
                lane_offset=offsets[rank],
                diagonal_runway=max(minimum_runway, abs(offsets[rank])),
            )
        )
        all_member_facts.extend((*facts, *outputs))
        all_raw_paths.extend((*raw_continuation, *raw_outputs))

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
            branch_plans,
            appearance_policy,
            structural_trunk_rank,
        )
    )
    lane_offsets = fan_lane_offsets(
        tuple(branch.id for branch in branch_plans),
        lane_pitch,
        appearance_centreline_branch_id,
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
        fan_appearance_lane_sign(graph, frame, layout_section_id, source_id)
        if frame is not None and reason is None
        else None
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
                        else item.opening_rank
                    ),
                )
                for line_id in sorted(
                    branch.line_ids, key=lambda item: line_priority.get(item, 0)
                )
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
    offset_carriers = _apply_solo_branch_offset_assignments(
        graph,
        branch_plans,
        fork_id,
        offset_carriers,
    )
    if any(
        set(graph.station_lines(carrier.station_id)) != set(carrier.line_ids)
        for carrier in offset_carriers
    ):
        reason = reason or "offset-carrier-has-unowned-line"
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
    if (
        reason is None
        and needs_centreline_anchor
        and candidate_centreline_anchor is None
    ):
        reason = "missing-centreline-anchor"
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


def _reject_overlaps(
    plans: tuple[FanPlan, ...], facts_by_id: Mapping[ConnectorId, AuthoredEdgeFact]
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
    plans = tuple(plan for plan in plans if plan.id not in subsumed)
    conflicts: set[FanPlanId] = set()
    for index, left in enumerate(plans):
        left_authored = set(left.authored_edge_ids)
        left_resolved = set(left.resolved_member_edges)
        left_stations = set(left.owned_station_ids)
        for right in plans[index + 1 :]:
            if (
                left_authored.intersection(right.authored_edge_ids)
                or left_resolved.intersection(right.resolved_member_edges)
                or left_stations.intersection(right.owned_station_ids)
            ):
                conflicts.update((left.id, right.id))
    return tuple(
        _legacy(plan, "overlapping-fan-ownership") if plan.id in conflicts else plan
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
    member_ids = member_ids_for_edges(plan.resolved_member_edges)
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
                member_id=scaffold.member_id_by_edge.get(expectation.edge),
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
    plans = _reject_overlaps(plans, {fact.id: fact for fact in facts})
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


def _validate_fan_runtime_frame(
    graph: MetroGraph,
    plan: FanPlan,
    bound_routes: Mapping[ResolvedEdge, RoutedPath],
    station_offsets: dict[tuple[str, str], float],
) -> None:
    """Validate final route continuity against one fan's frozen frame."""
    from nf_metro.layout.routing.common import apply_route_offsets

    context = f"planned fan {plan.id!s} in route system {plan.system_id!s}"
    endpoints: dict[tuple[str, str], list[tuple[ResolvedEdge, tuple[float, float]]]] = (
        defaultdict(list)
    )
    for edge, route in bound_routes.items():
        if not route.points:
            raise FanRouteInvariantError(
                f"{context} emitted an empty final route for {edge!r}"
            )
        points = tuple(apply_route_offsets(route, station_offsets))
        endpoints[(edge.source, edge.line_id)].append((edge, points[0]))
        endpoints[(edge.target, edge.line_id)].append((edge, points[-1]))

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
            port = graph.ports.get(station_id)
            for (endpoint_id, line_id), incident in endpoints.items():
                if endpoint_id != station_id:
                    continue
                offset = (
                    0.0
                    if port is not None and port.side in perpendicular_sides
                    else station_offsets.get((station_id, line_id), 0.0)
                )
                expected = (
                    plan.frame.secondary.get(station)
                    + plan.frame.secondary_sign * offset
                )
                if any(
                    abs(point[secondary_axis] - expected) > COORD_TOLERANCE_FINE
                    for _edge, point in incident
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
        if any(
            abs(point[axis] - reference[axis]) > COORD_TOLERANCE_FINE
            for _edge, point in incident[1:]
            for axis in axes
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
        for _edge, point in incident
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
) -> None:
    """Bind every planned fan member and exclusive emitter exactly once."""

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
                f"compatibility route system {plan.system_id!s} consumed planned "
                f"fan emission {plan.id!s} for {edge!r}"
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
