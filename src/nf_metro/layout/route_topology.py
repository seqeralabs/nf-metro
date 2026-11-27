"""Semantic route identity for layout and routing consumers."""

from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from nf_metro.parser.model import MetroGraph
from nf_metro.parser.route_topology import (
    RouteTopologyQuery,
    build_route_topology_query,
)


def divergence_junction_sources(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
) -> dict[str, str]:
    """Map resolved divergence junctions to their upstream sources."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        return {view.junction_id: view.exit_port_id for view in query.divergences}

    result: dict[str, str] = {}
    for junction_id in graph.junctions:
        sources = {edge.source for edge in graph.edges_to(junction_id)}
        if len(sources) == 1 and graph.edges_from(junction_id):
            result[junction_id] = next(iter(sources))
    return result


def divergence_junction_exit_ports(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
) -> dict[str, str]:
    """Map divergence junctions fed by exit ports to those ports."""
    sources = divergence_junction_sources(graph, topology)
    return {
        junction_id: source_id
        for junction_id, source_id in sources.items()
        if (port := graph.ports.get(source_id)) is not None and not port.is_entry
    }


def fanout_junction_ids(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
) -> set[str]:
    """Return divergence junctions eligible for fan-out routing."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        return {view.junction_id for view in query.divergences}
    return {
        junction_id
        for junction_id in divergence_junction_sources(graph)
        if graph.is_fanout_junction(junction_id)
    }


def convergence_junction_ids(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
) -> tuple[str, ...]:
    """Return resolved convergence junction ids in semantic order."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        return tuple(view.junction_id for view in query.convergences)

    predecessors: dict[str, set[str]] = defaultdict(set)
    successors: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        predecessors[edge.target].add(edge.source)
        successors[edge.source].add(edge.target)
    result: list[str] = []
    for junction_id in graph.junctions:
        if len(predecessors[junction_id]) <= 1 or len(successors[junction_id]) != 1:
            continue
        successor = next(iter(successors[junction_id]))
        port = graph.ports.get(successor)
        if port is not None and port.is_entry:
            result.append(junction_id)
    return tuple(result)


def convergence_junction_entry_ports(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
) -> dict[str, str]:
    """Map resolved convergence junctions to their downstream entry ports."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        return {view.junction_id: view.entry_port_id for view in query.convergences}

    result: dict[str, str] = {}
    for junction_id in convergence_junction_ids(graph):
        entry_ports = {
            edge.target
            for edge in graph.edges_from(junction_id)
            if (port := graph.ports.get(edge.target)) is not None and port.is_entry
        }
        if len(entry_ports) == 1:
            result[junction_id] = next(iter(entry_ports))
    return result


def convergence_entry_port_id(
    graph: MetroGraph,
    junction_id: str,
    topology: RouteTopologyQuery | None = None,
) -> str | None:
    """Return the resolved entry port represented by a convergence junction."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        convergence = query.convergence_for_junction(junction_id)
        return convergence.entry_port_id if convergence is not None else None

    return convergence_junction_entry_ports(graph).get(junction_id)


class ForkJoinStations(NamedTuple):
    """Each station's distinct targets and sources, and the forks and joins
    among them: the stations with two or more of either."""

    targets: dict[str, set[str]]
    sources: dict[str, set[str]]
    forks: set[str]
    joins: set[str]


def fork_join_stations(graph: MetroGraph) -> ForkJoinStations:
    """:class:`ForkJoinStations` over every edge of *graph*, port edges included."""
    targets: defaultdict[str, set[str]] = defaultdict(set)
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        targets[edge.source].add(edge.target)
        sources[edge.target].add(edge.source)
    return ForkJoinStations(
        dict(targets),
        dict(sources),
        {sid for sid, ends in targets.items() if len(ends) > 1},
        {sid for sid, ends in sources.items() if len(ends) > 1},
    )


def merge_fanout_junction_ids(
    graph: MetroGraph,
    topology: RouteTopologyQuery | None = None,
    convergence_ids: set[str] | None = None,
) -> tuple[str, ...]:
    """Return divergence junctions feeding multiple same-line convergences."""
    query = topology if topology is not None else build_route_topology_query(graph)
    if query is not None:
        return query.merge_fanout_junction_ids()

    merges = (
        convergence_ids
        if convergence_ids is not None
        else set(convergence_junction_ids(graph))
    )
    result: list[str] = []
    for source in graph.stations:
        targets_by_line: dict[str, set[str]] = defaultdict(set)
        for edge in graph.edges_from(source):
            if edge.target in merges:
                targets_by_line[edge.line_id].add(edge.target)
        if any(len(targets) >= 2 for targets in targets_by_line.values()):
            result.append(source)
    return tuple(result)
