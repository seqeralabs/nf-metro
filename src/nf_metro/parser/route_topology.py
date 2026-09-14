"""Immutable authored-route facts captured before resolver rewrites."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple, NewType, TypeVar

import networkx as nx

from nf_metro.graph_views import directed_graph
from nf_metro.parser.model import Edge, MetroGraph, PortSide

if TYPE_CHECKING:
    from nf_metro.parser.resolve import SectionEndpointResolution


ConnectorId = NewType("ConnectorId", str)
NetworkId = NewType("NetworkId", str)
BundleId = NewType("BundleId", str)
EndpointGroupId = NewType("EndpointGroupId", str)
DivergenceId = NewType("DivergenceId", str)
ConvergenceId = NewType("ConvergenceId", str)


class ResolvedEdge(NamedTuple):
    """One final graph edge in an authored connector's resolved path."""

    source: str
    target: str
    line_id: str


def semantic_route_id(kind: str, *parts: object) -> str:
    """Build an unambiguous content-derived identifier from typed parts."""
    encoded = "|".join(f"{len(str(part))}:{part}" for part in parts)
    return f"{kind}|{encoded}"


@dataclass(frozen=True, slots=True)
class AuthoredEdgeKey:
    """Stable identity for one authored per-line edge."""

    source: str
    target: str
    line_id: str
    duplicate_ordinal: int

    @property
    def id(self) -> ConnectorId:
        """Identity unaffected by authored edges with different content."""
        return ConnectorId(
            semantic_route_id(
                "connector",
                self.source,
                self.target,
                self.line_id,
                self.duplicate_ordinal,
            )
        )


@dataclass(frozen=True, slots=True)
class AuthoredEdgeFact:
    """Source facts and ordering for one authored per-line edge."""

    key: AuthoredEdgeKey
    rank: int
    source_line: int | None
    source_section: str | None
    target_section: str | None

    @property
    def id(self) -> ConnectorId:
        """Return the stable identity shared by topology and resolution records."""
        return self.key.id


@dataclass(frozen=True, slots=True)
class AuthoredRouteCapture:
    """Authored route facts and definition ranks before synthetic rewrites."""

    edges: tuple[AuthoredEdgeFact, ...]
    line_ids: tuple[str, ...]
    station_ids: tuple[str, ...]
    section_ids: tuple[str, ...]


@dataclass(slots=True)
class AuthoredEdgeLineage:
    """Parser-local mapping from current edges to their authored origins."""

    _origins_by_edge_id: dict[int, tuple[AuthoredEdgeKey, ...]]
    _rank_by_key: dict[AuthoredEdgeKey, int]

    @classmethod
    def from_capture(
        cls,
        edges: list[Edge],
        capture: AuthoredRouteCapture,
    ) -> AuthoredEdgeLineage:
        """Associate each captured edge with its current graph edge object."""
        if len(edges) != len(capture.edges):
            raise RouteTopologyLineageError(
                "authored capture and graph edge counts differ"
            )
        return cls(
            {
                id(edge): (fact.key,)
                for edge, fact in zip(edges, capture.edges, strict=True)
            },
            {fact.key: fact.rank for fact in capture.edges},
        )

    def origins(self, edge: Edge) -> tuple[AuthoredEdgeKey, ...]:
        """Return the authored origins carried by a current edge."""
        return self._origins_by_edge_id.get(id(edge), ())

    def replace(self, old_edge: Edge, new_edge: Edge) -> None:
        """Transfer one edge's origins to its replacement."""
        origins = self._origins_by_edge_id.pop(id(old_edge), ())
        self._origins_by_edge_id[id(new_edge)] = origins

    def discard(self, edge: Edge) -> None:
        """Remove an edge that has been replaced or deleted."""
        self._origins_by_edge_id.pop(id(edge), None)

    def bind(
        self, edge: Edge, origins: tuple[AuthoredEdgeKey, ...] | list[AuthoredEdgeKey]
    ) -> None:
        """Associate a synthetic edge with an authored-order origin union."""
        self._origins_by_edge_id[id(edge)] = self.ordered_union(origins)

    def ordered_union(
        self, origins: tuple[AuthoredEdgeKey, ...] | list[AuthoredEdgeKey]
    ) -> tuple[AuthoredEdgeKey, ...]:
        """Deduplicate origins and order them by their authored rank."""
        unique = set(origins)
        return tuple(sorted(unique, key=self._rank_by_key.__getitem__))


class RouteTopologyLineageError(RuntimeError):
    """The synthetic edge lineage cannot map authored connectors exactly."""


def capture_authored_routes(graph: MetroGraph) -> AuthoredRouteCapture:
    """Capture authored per-line edges and definition order without mutation."""
    occurrences: dict[tuple[str, str, str], int] = defaultdict(int)
    facts: list[AuthoredEdgeFact] = []
    for rank, edge in enumerate(graph.edges):
        content = (edge.source, edge.target, edge.line_id)
        duplicate_ordinal = occurrences[content]
        occurrences[content] += 1
        facts.append(
            AuthoredEdgeFact(
                key=AuthoredEdgeKey(*content, duplicate_ordinal),
                rank=rank,
                source_line=edge.source_line,
                source_section=graph.section_for_station(edge.source),
                target_section=graph.section_for_station(edge.target),
            )
        )
    return AuthoredRouteCapture(
        edges=tuple(facts),
        line_ids=tuple(graph.lines),
        station_ids=tuple(graph.stations),
        section_ids=tuple(graph.sections),
    )


@dataclass(frozen=True, slots=True)
class LineNetwork:
    """One connected component of one authored metro line."""

    id: NetworkId
    line_id: str
    station_ids: tuple[str, ...]
    edge_ids: tuple[ConnectorId, ...]
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class RouteConnector:
    """One authored per-line edge crossing a section boundary."""

    id: ConnectorId
    duplicate_ordinal: int
    source_line: int | None
    source: str
    target: str
    line_id: str
    source_section: str
    target_section: str
    exit_side: PortSide
    entry_side: PortSide
    network_id: NetworkId
    bundle_id: BundleId
    exit_group_id: EndpointGroupId
    entry_group_id: EndpointGroupId


@dataclass(frozen=True, slots=True)
class BundleRun:
    """Authored lines sharing one exact source-target endpoint pair."""

    id: BundleId
    source: str
    target: str
    connector_ids: tuple[ConnectorId, ...]
    line_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndpointGroup:
    """Connectors sharing one resolved section-boundary port requirement."""

    id: EndpointGroupId
    section_id: str
    side: PortSide
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class DivergenceGroup:
    """One exit group that feeds more than one entry group."""

    id: DivergenceId
    exit_group_id: EndpointGroupId
    entry_group_ids: tuple[EndpointGroupId, ...]
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class ConvergenceGroup:
    """Divergences that feed one entry group on the same line."""

    id: ConvergenceId
    entry_group_id: EndpointGroupId
    line_id: str
    divergence_ids: tuple[DivergenceId, ...]
    connector_ids: tuple[ConnectorId, ...]


@dataclass(frozen=True, slots=True)
class RouteTopology:
    """Canonical authored route topology, independent of mutable graph models."""

    authored_edges: tuple[AuthoredEdgeFact, ...]
    line_networks: tuple[LineNetwork, ...]
    connectors: tuple[RouteConnector, ...]
    bundles: tuple[BundleRun, ...]
    exit_groups: tuple[EndpointGroup, ...]
    entry_groups: tuple[EndpointGroup, ...]
    divergences: tuple[DivergenceGroup, ...]
    convergences: tuple[ConvergenceGroup, ...]


@dataclass(frozen=True, slots=True)
class ResolvedEndpointPort:
    """Synthetic port created for one topology endpoint group."""

    group_id: EndpointGroupId
    port_id: str


@dataclass(frozen=True, slots=True)
class ResolvedDivergence:
    """Synthetic junction created for one topology divergence."""

    group_id: DivergenceId
    junction_id: str


@dataclass(frozen=True, slots=True)
class ResolvedConvergence:
    """Synthetic junction created for one topology convergence."""

    group_id: ConvergenceId
    junction_id: str


@dataclass(frozen=True, slots=True)
class ResolvedAuthoredEdge:
    """Final ordered graph-edge paths for one authored edge."""

    authored_edge_id: ConnectorId
    edge_paths: tuple[tuple[ResolvedEdge, ...], ...]


@dataclass(frozen=True, slots=True)
class RouteResolutionTrace:
    """Immutable mapping from authored topology to resolved synthetic ids."""

    authored_edges: tuple[ResolvedAuthoredEdge, ...] = ()
    exit_ports: tuple[ResolvedEndpointPort, ...] = ()
    entry_ports: tuple[ResolvedEndpointPort, ...] = ()
    divergences: tuple[ResolvedDivergence, ...] = ()
    convergences: tuple[ResolvedConvergence, ...] = ()


def snapshot_resolved_authored_edges(
    capture: AuthoredRouteCapture,
    lineage: AuthoredEdgeLineage,
    edges: list[Edge],
) -> tuple[ResolvedAuthoredEdge, ...]:
    """Freeze one complete current path for every authored edge.

    Interchange and terminus rewrites preserve a single path for each authored
    edge. Boundary and bypass rewrites expand these paths later using explicit
    replacement records.
    """
    owned_by_key: dict[AuthoredEdgeKey, list[ResolvedEdge]] = {
        fact.key: [] for fact in capture.edges
    }
    for current_edge in edges:
        resolved = ResolvedEdge(
            current_edge.source, current_edge.target, current_edge.line_id
        )
        for origin in lineage.origins(current_edge):
            try:
                owned_by_key[origin].append(resolved)
            except KeyError as error:
                raise RouteTopologyLineageError(
                    f"current edge carries unknown authored origin {origin.id!r}"
                ) from error

    shared_paths: dict[
        tuple[tuple[ResolvedEdge, ...], ...],
        tuple[tuple[ResolvedEdge, ...], ...],
    ] = {}
    records: list[ResolvedAuthoredEdge] = []
    for fact in capture.edges:
        owned = owned_by_key[fact.key]
        if not owned:
            raise RouteTopologyLineageError(
                f"authored edge {fact.id!r} has no current resolved edge"
            )
        if any(edge.line_id != fact.key.line_id for edge in owned):
            raise RouteTopologyLineageError(
                f"authored edge {fact.id!r} resolves across multiple line ids"
            )

        outgoing: dict[str, list[tuple[int, ResolvedEdge]]] = defaultdict(list)
        incoming: dict[str, list[tuple[int, ResolvedEdge]]] = defaultdict(list)
        for index, resolved_edge in enumerate(owned):
            outgoing[resolved_edge.source].append((index, resolved_edge))
            incoming[resolved_edge.target].append((index, resolved_edge))
        if any(len(items) > 1 for items in (*outgoing.values(), *incoming.values())):
            raise RouteTopologyLineageError(
                f"authored edge {fact.id!r} has branching current lineage"
            )

        starts = [
            (index, resolved_edge)
            for index, resolved_edge in enumerate(owned)
            if resolved_edge.source not in incoming
        ]
        if len(starts) != 1:
            raise RouteTopologyLineageError(
                f"authored edge {fact.id!r} does not have one lineage start"
            )

        path: list[ResolvedEdge] = []
        used: set[int] = set()
        cursor = starts[0][1].source
        while cursor in outgoing:
            index, resolved_edge = outgoing[cursor][0]
            if index in used:
                raise RouteTopologyLineageError(
                    f"authored edge {fact.id!r} has cyclic current lineage"
                )
            used.add(index)
            path.append(resolved_edge)
            cursor = resolved_edge.target
        if len(used) != len(owned):
            raise RouteTopologyLineageError(
                f"authored edge {fact.id!r} has disconnected current lineage"
            )

        paths: tuple[tuple[ResolvedEdge, ...], ...] = (tuple(path),)
        paths = shared_paths.setdefault(paths, paths)
        records.append(ResolvedAuthoredEdge(fact.id, paths))
    return tuple(records)


class RouteTopologyQueryError(RuntimeError):
    """Route topology and resolver metadata do not form one complete index."""


@dataclass(frozen=True, slots=True)
class ResolvedDivergenceView:
    """One authored divergence with its resolved junction and boundary ports."""

    group: DivergenceGroup
    junction_id: str
    exit_port_id: str
    entry_port_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedConvergenceView:
    """One authored convergence with its resolved junction and boundary ports."""

    group: ConvergenceGroup
    junction_id: str
    entry_port_id: str
    source_junction_ids: tuple[str, ...]


_Record = TypeVar("_Record")
_RecordId = TypeVar("_RecordId")


def _exact_index(
    records: tuple[_Record, ...],
    expected_ids: tuple[_RecordId, ...],
    *,
    id_getter: Callable[[_Record], _RecordId],
    label: str,
) -> Mapping[_RecordId, _Record]:
    """Index records by id and reject incomplete or ambiguous resolver metadata."""
    result: dict[_RecordId, _Record] = {}
    for record in records:
        record_id = id_getter(record)
        if record_id in result:
            raise RouteTopologyQueryError(f"duplicate {label} id {record_id!r}")
        result[record_id] = record
    expected = set(expected_ids)
    observed = set(result)
    if observed != expected:
        missing = tuple(item for item in expected_ids if item not in observed)
        unknown = tuple(item for item in result if item not in expected)
        raise RouteTopologyQueryError(
            f"{label} ids do not match route topology: "
            f"missing={missing!r}, unknown={unknown!r}"
        )
    return MappingProxyType(result)


def _require_references(
    owner: str,
    field: str,
    references: tuple[_RecordId, ...],
    known: Mapping[_RecordId, object],
) -> None:
    """Reject references to records absent from the same route topology."""
    missing = tuple(reference for reference in references if reference not in known)
    if missing:
        raise RouteTopologyQueryError(
            f"{owner} has unknown {field} references: {missing!r}"
        )


@dataclass(frozen=True, slots=True)
class RouteTopologyQuery:
    """Ordered read-only queries over authored topology and resolver mappings."""

    authored_edges: tuple[AuthoredEdgeFact, ...]
    line_networks: tuple[LineNetwork, ...]
    connectors: tuple[RouteConnector, ...]
    bundles: tuple[BundleRun, ...]
    divergences: tuple[ResolvedDivergenceView, ...]
    convergences: tuple[ResolvedConvergenceView, ...]
    _authored_edges_by_id: Mapping[ConnectorId, AuthoredEdgeFact]
    _connectors_by_id: Mapping[ConnectorId, RouteConnector]
    _bundles_by_id: Mapping[BundleId, BundleRun]
    _resolved_authored_edges: Mapping[ConnectorId, ResolvedAuthoredEdge]
    _authored_edge_ids_by_resolved_edge: Mapping[ResolvedEdge, tuple[ConnectorId, ...]]
    _endpoint_groups_by_port: Mapping[str, EndpointGroup]
    _exit_ports_by_group: Mapping[EndpointGroupId, str]
    _entry_ports_by_group: Mapping[EndpointGroupId, str]
    _divergences_by_id: Mapping[DivergenceId, ResolvedDivergenceView]
    _divergences_by_junction: Mapping[str, ResolvedDivergenceView]
    _convergences_by_junction: Mapping[str, ResolvedConvergenceView]
    _merge_fanout_junction_ids: tuple[str, ...]
    _source_topology: RouteTopology = field(repr=False, compare=False)
    _source_resolution: RouteResolutionTrace = field(repr=False, compare=False)

    @classmethod
    def build(
        cls,
        topology: RouteTopology,
        resolution: RouteResolutionTrace,
    ) -> RouteTopologyQuery:
        """Build and validate the complete topology-to-resolution query surface."""
        authored_edges_by_id = _exact_index(
            topology.authored_edges,
            tuple(item.id for item in topology.authored_edges),
            id_getter=lambda item: item.id,
            label="authored edge",
        )
        networks_by_id = _exact_index(
            topology.line_networks,
            tuple(item.id for item in topology.line_networks),
            id_getter=lambda item: item.id,
            label="line network",
        )
        connectors_by_id = _exact_index(
            topology.connectors,
            tuple(item.id for item in topology.connectors),
            id_getter=lambda item: item.id,
            label="connector",
        )
        bundles_by_id = _exact_index(
            topology.bundles,
            tuple(item.id for item in topology.bundles),
            id_getter=lambda item: item.id,
            label="bundle",
        )
        exit_groups_by_id = _exact_index(
            topology.exit_groups,
            tuple(item.id for item in topology.exit_groups),
            id_getter=lambda item: item.id,
            label="exit group",
        )
        entry_groups_by_id = _exact_index(
            topology.entry_groups,
            tuple(item.id for item in topology.entry_groups),
            id_getter=lambda item: item.id,
            label="entry group",
        )
        divergence_groups_by_id = _exact_index(
            topology.divergences,
            tuple(item.id for item in topology.divergences),
            id_getter=lambda item: item.id,
            label="divergence group",
        )
        _exact_index(
            topology.convergences,
            tuple(item.id for item in topology.convergences),
            id_getter=lambda item: item.id,
            label="convergence group",
        )

        for network in topology.line_networks:
            _require_references(
                f"line network {network.id!r}",
                "authored edge",
                network.edge_ids,
                authored_edges_by_id,
            )
            _require_references(
                f"line network {network.id!r}",
                "connector",
                network.connector_ids,
                connectors_by_id,
            )
        for connector in topology.connectors:
            _require_references(
                f"connector {connector.id!r}",
                "authored edge",
                (connector.id,),
                authored_edges_by_id,
            )
            fact = authored_edges_by_id[connector.id]
            authored_values = (
                fact.key.source,
                fact.key.target,
                fact.key.line_id,
                fact.key.duplicate_ordinal,
                fact.source_line,
                fact.source_section,
                fact.target_section,
            )
            connector_values = (
                connector.source,
                connector.target,
                connector.line_id,
                connector.duplicate_ordinal,
                connector.source_line,
                connector.source_section,
                connector.target_section,
            )
            if connector_values != authored_values:
                raise RouteTopologyQueryError(
                    f"connector {connector.id!r} disagrees with its authored edge"
                )
            _require_references(
                f"connector {connector.id!r}",
                "network",
                (connector.network_id,),
                networks_by_id,
            )
            _require_references(
                f"connector {connector.id!r}",
                "bundle",
                (connector.bundle_id,),
                bundles_by_id,
            )
            _require_references(
                f"connector {connector.id!r}",
                "exit group",
                (connector.exit_group_id,),
                exit_groups_by_id,
            )
            _require_references(
                f"connector {connector.id!r}",
                "entry group",
                (connector.entry_group_id,),
                entry_groups_by_id,
            )
        for bundle in topology.bundles:
            _require_references(
                f"bundle {bundle.id!r}",
                "connector",
                bundle.connector_ids,
                connectors_by_id,
            )
        for endpoint_group in (*topology.exit_groups, *topology.entry_groups):
            _require_references(
                f"endpoint group {endpoint_group.id!r}",
                "connector",
                endpoint_group.connector_ids,
                connectors_by_id,
            )
        for divergence_group in topology.divergences:
            _require_references(
                f"divergence {divergence_group.id!r}",
                "exit group",
                (divergence_group.exit_group_id,),
                exit_groups_by_id,
            )
            _require_references(
                f"divergence {divergence_group.id!r}",
                "entry group",
                divergence_group.entry_group_ids,
                entry_groups_by_id,
            )
            _require_references(
                f"divergence {divergence_group.id!r}",
                "connector",
                divergence_group.connector_ids,
                connectors_by_id,
            )
        for convergence_group in topology.convergences:
            _require_references(
                f"convergence {convergence_group.id!r}",
                "entry group",
                (convergence_group.entry_group_id,),
                entry_groups_by_id,
            )
            _require_references(
                f"convergence {convergence_group.id!r}",
                "divergence",
                convergence_group.divergence_ids,
                divergence_groups_by_id,
            )
            _require_references(
                f"convergence {convergence_group.id!r}",
                "connector",
                convergence_group.connector_ids,
                connectors_by_id,
            )
        resolved_authored_edges = _exact_index(
            resolution.authored_edges,
            tuple(item.id for item in topology.authored_edges),
            id_getter=lambda item: item.authored_edge_id,
            label="resolved authored edge",
        )
        authored_edge_ids_by_resolved_edge: dict[ResolvedEdge, list[ConnectorId]] = (
            defaultdict(list)
        )
        for fact in topology.authored_edges:
            record = resolved_authored_edges[fact.id]
            if not record.edge_paths:
                raise RouteTopologyQueryError(
                    f"resolved authored edge {fact.id!r} has no paths"
                )
            for path_rank, path in enumerate(record.edge_paths):
                if not path:
                    raise RouteTopologyQueryError(
                        f"resolved authored edge {fact.id!r} path {path_rank} is empty"
                    )
                if any(edge.line_id != fact.key.line_id for edge in path):
                    raise RouteTopologyQueryError(
                        f"resolved authored edge {fact.id!r} path {path_rank} "
                        "changes line id"
                    )
                if any(
                    left.target != right.source for left, right in zip(path, path[1:])
                ):
                    raise RouteTopologyQueryError(
                        f"resolved authored edge {fact.id!r} path {path_rank} "
                        "is not contiguous"
                    )
                for edge in path:
                    owners = authored_edge_ids_by_resolved_edge[edge]
                    if fact.id not in owners:
                        owners.append(fact.id)
        exit_ports = _exact_index(
            resolution.exit_ports,
            tuple(item.id for item in topology.exit_groups),
            id_getter=lambda item: item.group_id,
            label="resolved exit group",
        )
        entry_ports = _exact_index(
            resolution.entry_ports,
            tuple(item.id for item in topology.entry_groups),
            id_getter=lambda item: item.group_id,
            label="resolved entry group",
        )
        resolved_divergences = _exact_index(
            resolution.divergences,
            tuple(item.id for item in topology.divergences),
            id_getter=lambda item: item.group_id,
            label="resolved divergence",
        )
        resolved_convergences = _exact_index(
            resolution.convergences,
            tuple(item.id for item in topology.convergences),
            id_getter=lambda item: item.group_id,
            label="resolved convergence",
        )

        exit_port_ids = {
            group_id: record.port_id for group_id, record in exit_ports.items()
        }
        entry_port_ids = {
            group_id: record.port_id for group_id, record in entry_ports.items()
        }
        endpoint_groups_by_port: dict[str, EndpointGroup] = {}
        for groups, port_ids in (
            (exit_groups_by_id, exit_port_ids),
            (entry_groups_by_id, entry_port_ids),
        ):
            for group_id, port_id in port_ids.items():
                if port_id in endpoint_groups_by_port:
                    raise RouteTopologyQueryError(
                        f"resolved port {port_id!r} belongs to multiple endpoint groups"
                    )
                endpoint_groups_by_port[port_id] = groups[group_id]

        divergence_views = tuple(
            ResolvedDivergenceView(
                group=group,
                junction_id=resolved_divergences[group.id].junction_id,
                exit_port_id=exit_port_ids[group.exit_group_id],
                entry_port_ids=tuple(
                    entry_port_ids[group_id] for group_id in group.entry_group_ids
                ),
            )
            for group in topology.divergences
        )
        divergences_by_id = {view.group.id: view for view in divergence_views}
        divergences_by_junction = {view.junction_id: view for view in divergence_views}
        if len(divergences_by_junction) != len(divergence_views):
            raise RouteTopologyQueryError(
                "one resolved junction represents multiple divergence groups"
            )

        convergence_views = tuple(
            ResolvedConvergenceView(
                group=group,
                junction_id=resolved_convergences[group.id].junction_id,
                entry_port_id=entry_port_ids[group.entry_group_id],
                source_junction_ids=tuple(
                    divergences_by_id[group_id].junction_id
                    for group_id in group.divergence_ids
                ),
            )
            for group in topology.convergences
        )
        convergences_by_junction = {
            view.junction_id: view for view in convergence_views
        }
        if len(convergences_by_junction) != len(convergence_views):
            raise RouteTopologyQueryError(
                "one resolved junction represents multiple convergence groups"
            )

        convergence_by_entry_line = {
            (view.group.entry_group_id, view.group.line_id): view
            for view in convergence_views
        }
        merge_fanout_junction_ids: list[str] = []
        for divergence in divergence_views:
            by_line: dict[str, list[str]] = defaultdict(list)
            for connector_id in divergence.group.connector_ids:
                connector = connectors_by_id[connector_id]
                convergence = convergence_by_entry_line.get(
                    (connector.entry_group_id, connector.line_id)
                )
                if (
                    convergence is not None
                    and convergence.junction_id not in by_line[connector.line_id]
                ):
                    by_line[connector.line_id].append(convergence.junction_id)
            if any(len(junction_ids) >= 2 for junction_ids in by_line.values()):
                merge_fanout_junction_ids.append(divergence.junction_id)

        return cls(
            authored_edges=topology.authored_edges,
            line_networks=topology.line_networks,
            connectors=topology.connectors,
            bundles=topology.bundles,
            divergences=divergence_views,
            convergences=convergence_views,
            _authored_edges_by_id=authored_edges_by_id,
            _connectors_by_id=connectors_by_id,
            _bundles_by_id=bundles_by_id,
            _resolved_authored_edges=resolved_authored_edges,
            _authored_edge_ids_by_resolved_edge=MappingProxyType(
                {
                    edge: tuple(authored_edge_ids)
                    for edge, authored_edge_ids in (
                        authored_edge_ids_by_resolved_edge.items()
                    )
                }
            ),
            _endpoint_groups_by_port=MappingProxyType(endpoint_groups_by_port),
            _exit_ports_by_group=MappingProxyType(exit_port_ids),
            _entry_ports_by_group=MappingProxyType(entry_port_ids),
            _divergences_by_id=MappingProxyType(divergences_by_id),
            _divergences_by_junction=MappingProxyType(divergences_by_junction),
            _convergences_by_junction=MappingProxyType(convergences_by_junction),
            _merge_fanout_junction_ids=tuple(merge_fanout_junction_ids),
            _source_topology=topology,
            _source_resolution=resolution,
        )

    def __deepcopy__(self, memo: dict[int, object]) -> RouteTopologyQuery:
        del memo
        return self

    def is_for(
        self,
        topology: RouteTopology,
        resolution: RouteResolutionTrace,
    ) -> bool:
        """Return whether this query indexes the exact immutable metadata pair."""
        return (
            self._source_topology is topology and self._source_resolution is resolution
        )

    def authored_edge(self, authored_edge_id: ConnectorId) -> AuthoredEdgeFact:
        """Return one authored edge fact by stable id."""
        return self._authored_edges_by_id[authored_edge_id]

    def connector(self, connector_id: ConnectorId) -> RouteConnector:
        """Return one authored connector by stable id."""
        return self._connectors_by_id[connector_id]

    def bundle(self, bundle_id: BundleId) -> BundleRun:
        """Return one authored endpoint bundle by stable id."""
        return self._bundles_by_id[bundle_id]

    def resolved_paths(
        self, authored_edge_id: ConnectorId
    ) -> tuple[tuple[ResolvedEdge, ...], ...]:
        """Return every final contiguous path for one authored edge."""
        return self._resolved_authored_edges[authored_edge_id].edge_paths

    def authored_edge_ids_for_edge(self, edge: ResolvedEdge) -> tuple[ConnectorId, ...]:
        """Return all authored edges owning a final edge, in authored order."""
        return self._authored_edge_ids_by_resolved_edge.get(edge, ())

    def connector_ids_for_edge(self, edge: ResolvedEdge) -> tuple[ConnectorId, ...]:
        """Return every authored connector owning a final edge, in authored order."""
        return tuple(
            authored_edge_id
            for authored_edge_id in self.authored_edge_ids_for_edge(edge)
            if authored_edge_id in self._connectors_by_id
        )

    def exit_port(self, group_id: EndpointGroupId) -> str:
        """Return the resolved exit port for an authored endpoint group."""
        return self._exit_ports_by_group[group_id]

    def entry_port(self, group_id: EndpointGroupId) -> str:
        """Return the resolved entry port for an authored endpoint group."""
        return self._entry_ports_by_group[group_id]

    def endpoint_group_for_port(self, port_id: str) -> EndpointGroup:
        """Return the authored endpoint group represented by a resolved port."""
        return self._endpoint_groups_by_port[port_id]

    def connector_ids_for_port(self, port_id: str) -> tuple[ConnectorId, ...]:
        """Return authored connectors represented by a resolved boundary port."""
        return self._endpoint_groups_by_port[port_id].connector_ids

    def divergence_by_id(self, group_id: DivergenceId) -> ResolvedDivergenceView:
        """Return one resolved divergence by stable group id."""
        return self._divergences_by_id[group_id]

    def divergence_for_junction(
        self, junction_id: str
    ) -> ResolvedDivergenceView | None:
        """Return the authored divergence represented by a resolved junction."""
        return self._divergences_by_junction.get(junction_id)

    def convergence_for_junction(
        self, junction_id: str
    ) -> ResolvedConvergenceView | None:
        """Return the authored convergence represented by a resolved junction."""
        return self._convergences_by_junction.get(junction_id)

    def connector_ids_for_junction(self, junction_id: str) -> tuple[ConnectorId, ...]:
        """Return authored connectors represented by a resolved route junction."""
        divergence = self._divergences_by_junction.get(junction_id)
        if divergence is not None:
            return divergence.group.connector_ids
        convergence = self._convergences_by_junction.get(junction_id)
        return convergence.group.connector_ids if convergence is not None else ()

    def merge_fanout_junction_ids(self) -> tuple[str, ...]:
        """Return divergence junctions feeding multiple same-line convergences."""
        return self._merge_fanout_junction_ids


def build_route_topology_query(graph: MetroGraph) -> RouteTopologyQuery | None:
    """Build the query for a parsed graph or return ``None`` without metadata."""
    topology = graph.route_topology
    resolution = graph.route_resolution
    if topology is None and resolution is None:
        graph._route_topology_query = None
        return None
    if topology is None or resolution is None:
        graph._route_topology_query = None
        raise RouteTopologyQueryError(
            "routing requires both route_topology and route_resolution metadata"
        )
    cached = graph._route_topology_query
    if cached is not None and cached.is_for(topology, resolution):
        return cached
    query = RouteTopologyQuery.build(topology, resolution)
    graph._route_topology_query = query
    return query


@dataclass(frozen=True, slots=True)
class _ResolvedAuthoredConnector:
    fact: AuthoredEdgeFact
    source_section: str
    target_section: str
    exit_side: PortSide
    entry_side: PortSide


def _definition_rank(values: tuple[str, ...]) -> dict[str, int]:
    return {value: rank for rank, value in enumerate(values)}


def _rank_key(rank: dict[str, int], value: str) -> tuple[int, str]:
    return rank.get(value, len(rank)), value


def _line_networks(
    capture: AuthoredRouteCapture,
    connector_ids: set[ConnectorId],
) -> tuple[tuple[LineNetwork, ...], dict[ConnectorId, NetworkId]]:
    edges_by_line: dict[str, list[AuthoredEdgeFact]] = defaultdict(list)
    for fact in capture.edges:
        edges_by_line[fact.key.line_id].append(fact)

    line_rank = _definition_rank(capture.line_ids)
    station_rank = _definition_rank(capture.station_ids)
    records: list[LineNetwork] = []
    network_by_edge: dict[ConnectorId, NetworkId] = {}

    for line_id in sorted(edges_by_line, key=lambda item: _rank_key(line_rank, item)):
        line_edges = edges_by_line[line_id]
        graph = directed_graph(
            {
                endpoint
                for fact in line_edges
                for endpoint in (fact.key.source, fact.key.target)
            },
            [(fact.key.source, fact.key.target) for fact in line_edges],
        )
        components = [
            tuple(sorted(component, key=lambda item: _rank_key(station_rank, item)))
            for component in nx.weakly_connected_components(graph)
        ]
        components.sort(
            key=lambda stations: min(
                _rank_key(station_rank, station) for station in stations
            )
        )
        component_by_station = {
            station: component_rank
            for component_rank, stations in enumerate(components)
            for station in stations
        }
        edges_by_component: list[list[AuthoredEdgeFact]] = [
            [] for _component in components
        ]
        for fact in line_edges:
            component_rank = component_by_station[fact.key.source]
            edges_by_component[component_rank].append(fact)

        for station_ids, component_edges in zip(
            components, edges_by_component, strict=True
        ):
            edge_ids = tuple(fact.key.id for fact in component_edges)
            network_id = NetworkId(semantic_route_id("network", line_id, *edge_ids))
            records.append(
                LineNetwork(
                    id=network_id,
                    line_id=line_id,
                    station_ids=station_ids,
                    edge_ids=edge_ids,
                    connector_ids=tuple(
                        edge_id for edge_id in edge_ids if edge_id in connector_ids
                    ),
                )
            )
            for edge_id in edge_ids:
                network_by_edge[edge_id] = network_id

    return tuple(records), network_by_edge


def _endpoint_group_id(kind: str, section_id: str, side: PortSide) -> EndpointGroupId:
    return EndpointGroupId(semantic_route_id(kind, section_id, side.value))


def _resolved_connectors(
    capture: AuthoredRouteCapture,
    lineage: AuthoredEdgeLineage,
    boundary_plan: SectionEndpointResolution | None,
) -> tuple[_ResolvedAuthoredConnector, ...]:
    if boundary_plan is None:
        return ()

    fact_by_key = {fact.key: fact for fact in capture.edges}
    assignments: dict[AuthoredEdgeKey, _ResolvedAuthoredConnector] = {}
    assignment_counts: dict[AuthoredEdgeKey, int] = defaultdict(int)
    for endpoint in boundary_plan.connectors:
        for key in lineage.origins(endpoint.edge):
            fact = fact_by_key[key]
            if fact.source_section == fact.target_section:
                continue
            assignment_counts[key] += 1
            assignments[key] = _ResolvedAuthoredConnector(
                fact=fact,
                source_section=endpoint.source_section,
                target_section=endpoint.target_section,
                exit_side=endpoint.exit_side,
                entry_side=endpoint.entry_side,
            )

    expected = [
        fact
        for fact in capture.edges
        if fact.source_section is not None
        and fact.target_section is not None
        and fact.source_section != fact.target_section
    ]
    invalid = [
        (fact.key.id, assignment_counts[fact.key])
        for fact in expected
        if assignment_counts[fact.key] != 1
    ]
    if invalid:
        detail = ", ".join(f"{connector_id}={count}" for connector_id, count in invalid)
        raise RouteTopologyLineageError(
            "authored cross-section connectors require one boundary assignment: "
            f"{detail}"
        )

    return tuple(assignments[fact.key] for fact in expected)


def build_route_topology(
    capture: AuthoredRouteCapture,
    lineage: AuthoredEdgeLineage,
    boundary_plan: SectionEndpointResolution | None = None,
) -> RouteTopology:
    """Project authored topology through the resolver's authoritative boundaries."""
    resolved = _resolved_connectors(capture, lineage, boundary_plan)
    connector_ids = {item.fact.key.id for item in resolved}
    networks, network_by_edge = _line_networks(capture, connector_ids)

    line_rank = _definition_rank(capture.line_ids)
    bundles_by_endpoint: dict[tuple[str, str], list[ConnectorId]] = defaultdict(list)
    lines_by_bundle: dict[tuple[str, str], set[str]] = defaultdict(set)
    exit_members: dict[tuple[str, PortSide], list[ConnectorId]] = defaultdict(list)
    entry_members: dict[tuple[str, PortSide], list[ConnectorId]] = defaultdict(list)
    for item in resolved:
        fact = item.fact
        endpoint = (fact.key.source, fact.key.target)
        bundles_by_endpoint[endpoint].append(fact.key.id)
        lines_by_bundle[endpoint].add(fact.key.line_id)
        exit_members[(item.source_section, item.exit_side)].append(fact.key.id)
        entry_members[(item.target_section, item.entry_side)].append(fact.key.id)

    bundle_id_by_endpoint = {
        endpoint: BundleId(semantic_route_id("bundle", *endpoint))
        for endpoint in bundles_by_endpoint
    }
    bundles = tuple(
        BundleRun(
            id=bundle_id_by_endpoint[(source, target)],
            source=source,
            target=target,
            connector_ids=tuple(bundles_by_endpoint[(source, target)]),
            line_ids=tuple(
                sorted(
                    lines_by_bundle[(source, target)],
                    key=lambda item: _rank_key(line_rank, item),
                )
            ),
        )
        for source, target in bundles_by_endpoint
    )

    exit_groups = tuple(
        EndpointGroup(
            id=_endpoint_group_id("exit", section_id, side),
            section_id=section_id,
            side=side,
            connector_ids=tuple(members),
        )
        for (section_id, side), members in exit_members.items()
    )
    entry_groups = tuple(
        EndpointGroup(
            id=_endpoint_group_id("entry", section_id, side),
            section_id=section_id,
            side=side,
            connector_ids=tuple(members),
        )
        for (section_id, side), members in entry_members.items()
    )

    connectors = tuple(
        RouteConnector(
            id=item.fact.key.id,
            duplicate_ordinal=item.fact.key.duplicate_ordinal,
            source_line=item.fact.source_line,
            source=item.fact.key.source,
            target=item.fact.key.target,
            line_id=item.fact.key.line_id,
            source_section=item.source_section,
            target_section=item.target_section,
            exit_side=item.exit_side,
            entry_side=item.entry_side,
            network_id=network_by_edge[item.fact.key.id],
            bundle_id=bundle_id_by_endpoint[
                (item.fact.key.source, item.fact.key.target)
            ],
            exit_group_id=_endpoint_group_id(
                "exit", item.source_section, item.exit_side
            ),
            entry_group_id=_endpoint_group_id(
                "entry", item.target_section, item.entry_side
            ),
        )
        for item in resolved
    )

    entry_targets_by_exit: dict[EndpointGroupId, list[EndpointGroupId]] = defaultdict(
        list
    )
    connector_ids_by_exit: dict[EndpointGroupId, list[ConnectorId]] = defaultdict(list)
    for connector in connectors:
        targets = entry_targets_by_exit[connector.exit_group_id]
        if connector.entry_group_id not in targets:
            targets.append(connector.entry_group_id)
        connector_ids_by_exit[connector.exit_group_id].append(connector.id)

    divergences = tuple(
        DivergenceGroup(
            id=DivergenceId(
                semantic_route_id("divergence", exit_group_id, *entry_group_ids)
            ),
            exit_group_id=exit_group_id,
            entry_group_ids=tuple(entry_group_ids),
            connector_ids=tuple(connector_ids_by_exit[exit_group_id]),
        )
        for exit_group_id, entry_group_ids in entry_targets_by_exit.items()
        if len(entry_group_ids) > 1
    )

    divergence_by_exit = {
        divergence.exit_group_id: divergence for divergence in divergences
    }
    divergence_ids_by_entry_line: dict[
        tuple[EndpointGroupId, str], list[DivergenceId]
    ] = defaultdict(list)
    connector_ids_by_entry_line: dict[
        tuple[EndpointGroupId, str], list[ConnectorId]
    ] = defaultdict(list)
    for connector in connectors:
        divergence = divergence_by_exit.get(connector.exit_group_id)
        if divergence is None:
            continue
        key = (connector.entry_group_id, connector.line_id)
        if divergence.id not in divergence_ids_by_entry_line[key]:
            divergence_ids_by_entry_line[key].append(divergence.id)
        connector_ids_by_entry_line[key].append(connector.id)

    convergences = tuple(
        ConvergenceGroup(
            id=ConvergenceId(
                semantic_route_id(
                    "convergence", entry_group_id, line_id, *divergence_ids
                )
            ),
            entry_group_id=entry_group_id,
            line_id=line_id,
            divergence_ids=tuple(divergence_ids),
            connector_ids=tuple(connector_ids_by_entry_line[(entry_group_id, line_id)]),
        )
        for (
            entry_group_id,
            line_id,
        ), divergence_ids in divergence_ids_by_entry_line.items()
        if len(divergence_ids) > 1
    )

    return RouteTopology(
        authored_edges=capture.edges,
        line_networks=networks,
        connectors=connectors,
        bundles=bundles,
        exit_groups=exit_groups,
        entry_groups=entry_groups,
        divergences=divergences,
        convergences=convergences,
    )
