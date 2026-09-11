"""Authored route topology contracts across parser resolution rewrites."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.route_topology import (
    convergence_junction_entry_ports,
    divergence_junction_exit_ports,
    divergence_junction_sources,
    fanout_junction_ids,
)
from nf_metro.layout.routing.common import merge_junction_ids
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
    is_bypass_v,
    is_converge_junction,
)
from nf_metro.parser.resolve import _resolve_sections, resolve_section_endpoints
from nf_metro.parser.route_topology import (
    AuthoredEdgeLineage,
    ConnectorId,
    NetworkId,
    ResolvedEdge,
    RouteTopology,
    RouteTopologyQueryError,
    build_route_topology,
    build_route_topology_query,
    capture_authored_routes,
    snapshot_resolved_authored_edges,
)

ROOT = Path(__file__).parents[1]
CORPUS = sorted(
    path
    for root in (ROOT / "examples", ROOT / "tests" / "fixtures")
    for path in root.rglob("*.mmd")
    if "nextflow" not in path.parts
)


def _parse(text: str) -> MetroGraph:
    graph = parse_metro_mermaid(text)
    assert graph.route_topology is not None
    return graph


def _resolved_paths_by_id(graph: MetroGraph):
    resolution = graph.route_resolution
    assert resolution is not None
    return {
        item.authored_edge_id: item.edge_paths for item in resolution.authored_edges
    }


def test_direct_connector_records_its_resolved_synthetic_chain() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None

    connector = topology.connectors[0]
    connector_resolution = next(
        item
        for item in resolution.authored_edges
        if item.authored_edge_id == connector.id
    )
    exit_port = resolution.exit_ports[0]
    entry_port = resolution.entry_ports[0]

    assert connector_resolution.authored_edge_id == connector.id
    assert exit_port.group_id == connector.exit_group_id
    assert entry_port.group_id == connector.entry_group_id
    assert connector_resolution.edge_paths == (
        (
            ("a", exit_port.port_id, "red"),
            (exit_port.port_id, entry_port.port_id, "red"),
            (entry_port.port_id, "b", "red"),
        ),
    )


def _two_section_text(edges: str, *, extra: str = "") -> str:
    return f"""\
%%metro line: red | Red | #f00
%%metro line: blue | Blue | #00f
{extra}graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph target [Target]
        b[B]
    end
{edges}
"""


def test_connector_identity_preserves_multiline_order_and_exact_duplicates() -> None:
    graph = _parse(_two_section_text("    a -->|red,blue| b\n    a -->|red| b\n"))
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None

    assert [
        (connector.source, connector.target, connector.line_id)
        for connector in topology.connectors
    ] == [("a", "b", "red"), ("a", "b", "blue"), ("a", "b", "red")]
    assert [connector.duplicate_ordinal for connector in topology.connectors] == [
        0,
        0,
        1,
    ]
    assert len({connector.id for connector in topology.connectors}) == 3
    assert topology.bundles[0].connector_ids == tuple(
        connector.id for connector in topology.connectors
    )
    assert topology.bundles[0].line_ids == ("red", "blue")
    traces = _resolved_paths_by_id(graph)
    connector_paths = [traces[connector.id] for connector in topology.connectors]
    assert connector_paths[0] == connector_paths[2]
    assert connector_paths[0] is connector_paths[2]


def test_one_to_many_fanout_maps_to_one_topology_divergence() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph first [First]
        b[B]
    end
    subgraph second [Second]
        c[C]
    end
    a -->|red| b
    a -->|red| c
"""
    )
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None
    assert len(topology.divergences) == len(resolution.divergences) == 1
    assert not topology.convergences

    divergence = topology.divergences[0]
    fan = resolution.divergences[0]
    exit_port = next(
        item
        for item in resolution.exit_ports
        if item.group_id == divergence.exit_group_id
    )
    assert fan.group_id == divergence.id
    assert fan.junction_id in graph.junctions
    traces = _resolved_paths_by_id(graph)
    for connector in topology.connectors:
        assert (
            exit_port.port_id,
            fan.junction_id,
            connector.line_id,
        ) in traces[connector.id][0]


def test_fanout_and_merge_groups_map_to_their_junctions() -> None:
    graph = _parse((ROOT / "examples" / "guide" / "03b_fan_in_merge.mmd").read_text())
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None
    assert topology.divergences and topology.convergences

    fan_by_group = {item.group_id: item.junction_id for item in resolution.divergences}
    merge_by_group = {
        item.group_id: item.junction_id for item in resolution.convergences
    }
    connector_traces = _resolved_paths_by_id(graph)
    entry_ports = {item.group_id: item.port_id for item in resolution.entry_ports}

    for convergence in topology.convergences:
        merge_id = merge_by_group[convergence.id]
        entry_port_id = entry_ports[convergence.entry_group_id]
        assert merge_id in graph.junctions
        for connector_id in convergence.connector_ids:
            connector = next(
                item for item in topology.connectors if item.id == connector_id
            )
            fan_id = fan_by_group[
                next(
                    item.id
                    for item in topology.divergences
                    if item.exit_group_id == connector.exit_group_id
                )
            ]
            paths = connector_traces[connector_id]
            assert any(
                (fan_id, merge_id, convergence.line_id) in path for path in paths
            )
            assert any(
                (merge_id, entry_port_id, convergence.line_id) in path for path in paths
            )


def test_line_networks_are_stable_connected_components() -> None:
    base = """\
%%metro line: red | Red | #f00
graph LR
    subgraph one [One]
        a[A]
        b[B]
        a -->|red| b
    end
    subgraph two [Two]
        c[C]
        d[D]
        c -->|red| d
    end
"""
    expanded = (
        base.replace(
            "graph LR",
            "%%metro line: green | Green | #0f0\ngraph LR",
        )
        + """
    subgraph unrelated [Unrelated]
        u[U]
        v[V]
        u -->|green| v
    end
"""
    )

    original = _parse(base).route_topology
    observed = _parse(expanded).route_topology
    assert original is not None and observed is not None

    original_red = {
        network.station_ids: network.id
        for network in original.line_networks
        if network.line_id == "red"
    }
    observed_red = {
        network.station_ids: network.id
        for network in observed.line_networks
        if network.line_id == "red"
    }
    assert original_red == observed_red
    assert tuple(original_red) == (("a", "b"), ("c", "d"))


def test_topology_and_trace_preserve_every_authored_edge() -> None:
    graph = _parse((ROOT / "examples" / "guide" / "03_fan_out.mmd").read_text())
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None

    assert len(topology.authored_edges) == 15
    assert len(topology.connectors) == 6
    assert tuple(item.authored_edge_id for item in resolution.authored_edges) == tuple(
        item.id for item in topology.authored_edges
    )
    assert Counter(
        edge_id for network in topology.line_networks for edge_id in network.edge_ids
    ) == Counter(item.id for item in topology.authored_edges)

    internal = next(
        item
        for item in topology.authored_edges
        if (item.key.source, item.key.target, item.key.line_id)
        == ("fastqc", "trim", "wgs")
    )
    query = build_route_topology_query(graph)
    assert query is not None
    assert query.authored_edge(internal.id) is internal
    assert query.resolved_paths(internal.id) == (
        (ResolvedEdge("fastqc", "trim", "wgs"),),
    )
    assert query.authored_edge_ids_for_edge(ResolvedEdge("fastqc", "trim", "wgs")) == (
        internal.id,
    )
    assert not query.connector_ids_for_edge(ResolvedEdge("fastqc", "trim", "wgs"))


def test_sectionless_graph_preserves_its_authored_edge_path() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
graph LR
    a[A]
    b[B]
    a -->|red| b
"""
    )
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None
    assert not topology.connectors
    assert len(topology.authored_edges) == len(resolution.authored_edges) == 1
    assert resolution.authored_edges[0].edge_paths == (
        (ResolvedEdge("a", "b", "red"),),
    )


def test_programmatic_duplicate_edges_get_stable_unique_identities() -> None:
    graph = MetroGraph(
        stations={
            "a": Station(id="a", label="A", section_id="source"),
            "b": Station(id="b", label="B", section_id="target"),
        },
        edges=[
            Edge(source="a", target="b", line_id="red"),
            Edge(source="a", target="b", line_id="red"),
        ],
        sections={
            "source": Section(id="source", name="Source", station_ids=["a"]),
            "target": Section(id="target", name="Target", station_ids=["b"]),
        },
    )
    capture = capture_authored_routes(graph)
    lineage = AuthoredEdgeLineage.from_capture(graph.edges, capture)
    resolution = resolve_section_endpoints(graph, lineage)

    topology = build_route_topology(capture, lineage, resolution)
    resolved_authored_edges = snapshot_resolved_authored_edges(
        capture, lineage, graph.edges
    )

    assert [connector.duplicate_ordinal for connector in topology.connectors] == [
        0,
        1,
    ]
    assert len({connector.id for connector in topology.connectors}) == 2
    assert all(connector.source_line is None for connector in topology.connectors)

    corrupted_endpoint = dataclasses.replace(
        resolution.connectors[0],
        connector_ids=resolution.connectors[0].connector_ids
        + (ConnectorId("missing"),),
    )
    corrupted_resolution = dataclasses.replace(
        resolution,
        connectors=(corrupted_endpoint,),
    )
    with pytest.raises(ValueError, match="absent from RouteTopology"):
        _resolve_sections(
            graph, corrupted_resolution, topology, resolved_authored_edges
        )


def test_terminus_topology_uses_the_resolvers_reanchored_entry_side() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
%%metro file: output | Results
graph LR
    subgraph upstream [Upstream]
        a[A]
    end
    subgraph output_section [Output]
        %%metro direction: LR
        %%metro entry: right | red
        b[B]
        output[Output]
        b -->|red| output
    end
    a -->|red| output
"""
    )
    topology = graph.route_topology
    assert topology is not None

    connector = next(
        connector for connector in topology.connectors if connector.source == "a"
    )
    assert connector.target == "output"
    assert connector.entry_side is PortSide.LEFT
    assert {
        port.side
        for port in graph.ports.values()
        if port.section_id == "output_section" and port.is_entry
    } == {PortSide.LEFT}


def test_duplicate_terminus_connectors_survive_many_to_one_lineage() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
%%metro file: output | Results
graph LR
    subgraph upstream [Upstream]
        a[A]
    end
    subgraph output_section [Output]
        b[B]
        output[Output]
        b -->|red| output
    end
    a -->|red| output
    a -->|red| output
"""
    )
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None

    connectors = [
        connector
        for connector in topology.connectors
        if (connector.source, connector.target, connector.line_id)
        == ("a", "output", "red")
    ]
    assert [connector.duplicate_ordinal for connector in connectors] == [0, 1]
    assert len({connector.id for connector in connectors}) == 2
    assert connectors[0].entry_group_id == connectors[1].entry_group_id
    traces = _resolved_paths_by_id(graph)
    assert traces[connectors[0].id] == traces[connectors[1].id]
    assert traces[connectors[0].id][0][0].source == "a"
    assert traces[connectors[0].id][0][-1].target == "output"

    internal = next(
        item
        for item in topology.authored_edges
        if (item.key.source, item.key.target, item.key.line_id)
        == ("b", "output", "red")
    )
    internal_path = traces[internal.id][0]
    assert internal_path[0].source == "b"
    assert internal_path[-1].target == "output"
    assert any(is_converge_junction(edge.target) for edge in internal_path)


def test_repeated_bypasses_record_every_parallel_resolved_path() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
%%metro line: blue | Blue | #00f
graph LR
    subgraph s [S]
        p[P]
        a[A]
        b[B]
        q[Q]
        p -->|red| a
        a -->|red| b
        b -->|red| q
    end
    subgraph t [T]
        z[Z]
    end
    p -->|blue| z
"""
    )
    resolution = graph.route_resolution
    assert resolution is not None
    topology = graph.route_topology
    assert topology is not None
    connector_id = topology.connectors[0].id
    paths = _resolved_paths_by_id(graph)[connector_id]

    assert len(paths) == 3
    assert {path[0].target for path in paths} == {
        "__bypass_a_p_1",
        "__bypass_b_p_2",
        "__bypass_q_p_3",
    }
    for path in paths:
        assert path[0].source == "p"
        assert path[-1].target == "z"
        assert all(left.target == right.source for left, right in zip(path, path[1:]))


def test_route_topology_query_reverse_maps_duplicate_connectors_in_order() -> None:
    graph = _parse(_two_section_text("    a -->|red,blue| b\n    a -->|red| b\n"))
    query = build_route_topology_query(graph)
    assert query is not None

    red_connectors = tuple(
        connector.id for connector in query.connectors if connector.line_id == "red"
    )
    red_edge = query.resolved_paths(red_connectors[0])[0][1]

    assert isinstance(red_edge, ResolvedEdge)
    assert query.connector_ids_for_edge(red_edge) == red_connectors


def test_route_topology_query_reverse_maps_every_bypass_path_leg() -> None:
    graph = _parse(
        """\
%%metro line: red | Red | #f00
%%metro line: blue | Blue | #00f
graph LR
    subgraph s [S]
        p[P]
        a[A]
        b[B]
        q[Q]
        p -->|red| a
        a -->|red| b
        b -->|red| q
    end
    subgraph t [T]
        z[Z]
    end
    p -->|blue| z
"""
    )
    query = build_route_topology_query(graph)
    assert query is not None
    connector_id = query.connectors[0].id

    for path in query.resolved_paths(connector_id):
        for edge in path:
            assert query.connector_ids_for_edge(edge) == (connector_id,)


def test_route_topology_query_resolves_ordered_fans_merges_and_ports() -> None:
    graph = _parse((ROOT / "examples" / "guide" / "03b_fan_in_merge.mmd").read_text())
    query = build_route_topology_query(graph)
    assert query is not None

    topology = graph.route_topology
    assert topology is not None
    assert tuple(view.group for view in query.divergences) == topology.divergences
    assert tuple(view.group for view in query.convergences) == topology.convergences
    for view in query.divergences:
        assert query.divergence_for_junction(view.junction_id) is view
        assert query.connector_ids_for_junction(view.junction_id) == (
            view.group.connector_ids
        )
        assert query.connector_ids_for_port(view.exit_port_id) == (
            query.endpoint_group_for_port(view.exit_port_id).connector_ids
        )
        assert (
            query.endpoint_group_for_port(view.exit_port_id).id
            == view.group.exit_group_id
        )
        assert (
            tuple(
                query.endpoint_group_for_port(port_id).id
                for port_id in view.entry_port_ids
            )
            == view.group.entry_group_ids
        )
    for view in query.convergences:
        assert query.convergence_for_junction(view.junction_id) is view
        assert query.connector_ids_for_junction(view.junction_id) == (
            view.group.connector_ids
        )
        assert (
            query.endpoint_group_for_port(view.entry_port_id).id
            == view.group.entry_group_id
        )
        assert view.source_junction_ids == tuple(
            query.divergence_by_id(divergence_id).junction_id
            for divergence_id in view.group.divergence_ids
        )


def test_route_topology_query_metadata_contract_is_explicit() -> None:
    assert build_route_topology_query(MetroGraph()) is None

    topology_only = _parse(_two_section_text("    a -->|red| b\n"))
    topology_only.route_resolution = None
    with pytest.raises(
        RouteTopologyQueryError, match="both route_topology and route_resolution"
    ):
        build_route_topology_query(topology_only)

    resolution_only = _parse(_two_section_text("    a -->|red| b\n"))
    resolution_only.route_topology = None
    with pytest.raises(
        RouteTopologyQueryError, match="both route_topology and route_resolution"
    ):
        build_route_topology_query(resolution_only)


def test_route_topology_query_cache_tracks_metadata_identity() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    first = build_route_topology_query(graph)
    assert first is not None
    assert build_route_topology_query(graph) is first

    topology = graph.route_topology
    resolution = graph.route_resolution
    assert topology is not None and resolution is not None
    graph.route_topology = dataclasses.replace(topology)
    second = build_route_topology_query(graph)
    assert second is not None and second is not first
    assert build_route_topology_query(graph) is second

    graph.route_resolution = dataclasses.replace(resolution)
    third = build_route_topology_query(graph)
    assert third is not None and third is not second
    assert build_route_topology_query(graph) is third


def test_route_topology_query_cache_preserves_deepcopy_isolation() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    query = build_route_topology_query(graph)
    topology = graph.route_topology
    resolution = graph.route_resolution
    assert query is not None and topology is not None and resolution is not None

    copied = copy.deepcopy(graph)
    copied_query = build_route_topology_query(copied)
    assert copied.route_topology is not topology
    assert copied.route_resolution is not resolution
    assert copied_query is not query
    assert build_route_topology_query(copied) is copied_query

    shared = copy.deepcopy(
        graph,
        {id(topology): topology, id(resolution): resolution},
    )
    assert build_route_topology_query(shared) is query


def test_route_topology_query_rejects_an_incomplete_authored_edge_trace() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    resolution = graph.route_resolution
    assert resolution is not None
    graph.route_resolution = dataclasses.replace(resolution, authored_edges=())

    with pytest.raises(
        RouteTopologyQueryError, match="resolved authored edge ids do not match"
    ):
        build_route_topology_query(graph)


def test_metadata_free_topology_adapters_preserve_source_contracts() -> None:
    graph = MetroGraph(
        stations={
            "exit": Station(id="exit", label="", section_id="source", is_port=True),
            "station": Station(id="station", label="Station"),
            "port_fan": Station(id="port_fan", label="", is_hidden=True),
            "station_fan": Station(id="station_fan", label="", is_hidden=True),
            "first": Station(id="first", label="First"),
            "second": Station(id="second", label="Second"),
            "third": Station(id="third", label="Third"),
            "left": Station(id="left", label="Left"),
            "right": Station(id="right", label="Right"),
            "merge": Station(id="merge", label="", is_hidden=True),
            "entry": Station(id="entry", label="", section_id="target", is_port=True),
        },
        edges=[
            Edge(source="exit", target="port_fan", line_id="red"),
            Edge(source="port_fan", target="first", line_id="red"),
            Edge(source="port_fan", target="third", line_id="blue"),
            Edge(source="station", target="station_fan", line_id="red"),
            Edge(source="station_fan", target="second", line_id="red"),
            Edge(source="left", target="merge", line_id="red"),
            Edge(source="right", target="merge", line_id="red"),
            Edge(source="merge", target="entry", line_id="red"),
            Edge(source="merge", target="entry", line_id="blue"),
        ],
        ports={
            "exit": Port(
                id="exit",
                section_id="source",
                side=PortSide.RIGHT,
                is_entry=False,
            ),
            "entry": Port(
                id="entry",
                section_id="target",
                side=PortSide.LEFT,
                is_entry=True,
            ),
        },
        junctions=["port_fan", "station_fan", "merge"],
    )

    assert divergence_junction_sources(graph) == {
        "port_fan": "exit",
        "station_fan": "station",
    }
    assert divergence_junction_exit_ports(graph) == {"port_fan": "exit"}
    assert fanout_junction_ids(graph) == {"port_fan"}
    assert convergence_junction_entry_ports(graph) == {"merge": "entry"}


def test_route_topology_query_rejects_unknown_topology_references() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    topology = graph.route_topology
    assert topology is not None

    connector = dataclasses.replace(
        topology.connectors[0],
        network_id=NetworkId("missing-network"),
    )
    graph.route_topology = dataclasses.replace(topology, connectors=(connector,))

    with pytest.raises(RouteTopologyQueryError, match="unknown network references"):
        build_route_topology_query(graph)


def test_corpus_query_matches_resolved_semantic_junction_shapes() -> None:
    for path in CORPUS:
        graph = parse_metro_mermaid(path.read_text())
        query = build_route_topology_query(graph)
        assert query is not None

        predecessors: dict[str, set[str]] = defaultdict(set)
        successors: dict[str, set[str]] = defaultdict(set)
        for edge in graph.edges:
            predecessors[edge.target].add(edge.source)
            successors[edge.source].add(edge.target)

        legacy_divergences = {
            junction_id: next(iter(predecessors[junction_id]))
            for junction_id in graph.junctions
            if len(predecessors[junction_id]) == 1 and successors[junction_id]
        }
        legacy_convergences = {
            junction_id
            for junction_id in graph.junctions
            if len(predecessors[junction_id]) > 1
            and len(successors[junction_id]) == 1
            and (port := graph.ports.get(next(iter(successors[junction_id]))))
            is not None
            and port.is_entry
        }

        assert {
            divergence.junction_id: divergence.exit_port_id
            for divergence in query.divergences
        } == legacy_divergences, path
        assert {
            convergence.junction_id for convergence in query.convergences
        } == legacy_convergences, path
        assert {
            *legacy_divergences,
            *legacy_convergences,
        } == set(graph.junctions), path

        legacy_merge_fanouts: set[str] = set()
        for source_id in graph.stations:
            merge_targets_by_line: dict[str, set[str]] = defaultdict(set)
            for edge in graph.edges_from(source_id):
                if edge.target in legacy_convergences:
                    merge_targets_by_line[edge.line_id].add(edge.target)
            if any(len(targets) >= 2 for targets in merge_targets_by_line.values()):
                legacy_merge_fanouts.add(source_id)
        assert set(query.merge_fanout_junction_ids()) == legacy_merge_fanouts, path


def test_topology_records_are_deeply_immutable_and_detached() -> None:
    graph = _parse(_two_section_text("    a -->|red| b\n"))
    topology = graph.route_topology
    assert topology is not None

    with pytest.raises(dataclasses.FrozenInstanceError):
        topology.connectors = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        topology.connectors[0].source = "other"  # type: ignore[misc]

    mutable_models = (MetroGraph, Edge, Section, Station, list, dict, set)

    def visit(value: object) -> None:
        assert not isinstance(value, mutable_models)
        if dataclasses.is_dataclass(value):
            for field in dataclasses.fields(value):
                visit(getattr(value, field.name))
        elif isinstance(value, tuple):
            for item in value:
                visit(item)

    resolution = graph.route_resolution
    assert resolution is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolution.authored_edges = ()  # type: ignore[misc]

    visit(topology)
    visit(resolution)
    assert [field.name for field in dataclasses.fields(Edge)] == [
        "source",
        "target",
        "line_id",
        "source_line",
    ]


def _identity_maps(topology: RouteTopology) -> dict[str, dict[tuple, str]]:
    return {
        "connectors": {
            (
                item.source,
                item.target,
                item.line_id,
                item.duplicate_ordinal,
            ): item.id
            for item in topology.connectors
        },
        "bundles": {(item.source, item.target): item.id for item in topology.bundles},
        "networks": {
            (item.line_id, item.edge_ids): item.id for item in topology.line_networks
        },
        "divergences": {
            (item.exit_group_id, item.entry_group_ids): item.id
            for item in topology.divergences
        },
        "convergences": {
            (item.entry_group_id, item.line_id, item.divergence_ids): item.id
            for item in topology.convergences
        },
    }


def test_ids_do_not_shift_when_an_unrelated_component_is_added() -> None:
    base = (ROOT / "examples" / "guide" / "03b_fan_in_merge.mmd").read_text()
    expanded = (
        base.replace(
            "graph LR",
            "%%metro line: unrelated | Unrelated | #0f0\ngraph LR",
        )
        + """
    subgraph unrelated [Unrelated]
        unrelated_a[A]
        unrelated_b[B]
        unrelated_a -->|unrelated| unrelated_b
    end
"""
    )
    original = _parse(base).route_topology
    observed = _parse(expanded).route_topology
    assert original is not None and observed is not None

    for kind, original_ids in _identity_maps(original).items():
        observed_ids = _identity_maps(observed)[kind]
        assert observed_ids.items() >= original_ids.items()


def _actual_shapes(
    graph: MetroGraph,
) -> tuple[set[tuple], set[tuple], set[tuple], set[tuple]]:
    port_key = {
        port_id: (port.section_id, port.side.value)
        for port_id, port in graph.ports.items()
    }
    exit_members: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    entry_members: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    junction_exit: dict[str, tuple[str, str]] = {}
    junction_targets: dict[str, set[tuple[tuple[str, str], str]]] = defaultdict(set)

    def authored_source(station_id: str, line_id: str) -> str:
        while is_bypass_v(station_id):
            incoming = [
                edge for edge in graph.edges_to(station_id) if edge.line_id == line_id
            ]
            assert len(incoming) == 1
            station_id = incoming[0].source
        return station_id

    def authored_target(station_id: str, line_id: str) -> str:
        while is_converge_junction(station_id):
            outgoing = [
                edge for edge in graph.edges_from(station_id) if edge.line_id == line_id
            ]
            assert len(outgoing) == 1
            station_id = outgoing[0].target
        return station_id

    for edge in graph.edges:
        target_port = graph.ports.get(edge.target)
        source_port = graph.ports.get(edge.source)
        if target_port is not None and not target_port.is_entry:
            exit_members[port_key[edge.target]].add(
                (authored_source(edge.source, edge.line_id), edge.line_id)
            )
        if source_port is not None and source_port.is_entry:
            entry_members[port_key[edge.source]].add(
                (authored_target(edge.target, edge.line_id), edge.line_id)
            )
        if (
            source_port is not None
            and not source_port.is_entry
            and graph.is_fanout_junction(edge.target)
        ):
            junction_exit[edge.target] = port_key[edge.source]
        if (
            graph.is_fanout_junction(edge.source)
            and target_port is not None
            and target_port.is_entry
        ):
            junction_targets[edge.source].add((port_key[edge.target], edge.line_id))

    merge_ids = merge_junction_ids(graph)
    fan_ids = {
        junction for junction in graph.junctions if graph.is_fanout_junction(junction)
    }
    for fan_id in fan_ids:
        for edge in graph.edges_from(fan_id):
            if edge.target not in merge_ids:
                continue
            for outgoing in graph.edges_from(edge.target):
                target_port = graph.ports.get(outgoing.target)
                if target_port is not None and target_port.is_entry:
                    junction_targets[fan_id].add(
                        (port_key[outgoing.target], outgoing.line_id)
                    )
    fan_groups = {
        (junction_exit[junction], tuple(sorted(junction_targets[junction])))
        for junction in fan_ids
        if junction in junction_exit
    }

    merge_groups: set[tuple] = set()
    for merge_id in merge_ids:
        outgoing = [
            edge for edge in graph.edges_from(merge_id) if edge.target in graph.ports
        ]
        assert len(outgoing) == 1
        target = outgoing[0]
        source_exit_groups = {
            junction_exit[edge.source]
            for edge in graph.edges_to(merge_id)
            if edge.source in junction_exit
        }
        merge_groups.add(
            (
                port_key[target.target],
                target.line_id,
                tuple(sorted(source_exit_groups)),
            )
        )

    return (
        {(key, tuple(sorted(members))) for key, members in exit_members.items()},
        {(key, tuple(sorted(members))) for key, members in entry_members.items()},
        fan_groups,
        merge_groups,
    )


def _predicted_shapes(
    topology: RouteTopology,
) -> tuple[set[tuple], set[tuple], set[tuple], set[tuple]]:
    connectors = {connector.id: connector for connector in topology.connectors}
    exit_groups = {
        (
            (group.section_id, group.side.value),
            tuple(
                sorted(
                    {
                        (
                            connectors[connector_id].source,
                            connectors[connector_id].line_id,
                        )
                        for connector_id in group.connector_ids
                    }
                )
            ),
        )
        for group in topology.exit_groups
    }
    entry_groups = {
        (
            (group.section_id, group.side.value),
            tuple(
                sorted(
                    {
                        (
                            connectors[connector_id].target,
                            connectors[connector_id].line_id,
                        )
                        for connector_id in group.connector_ids
                    }
                )
            ),
        )
        for group in topology.entry_groups
    }
    entry_by_id = {group.id: group for group in topology.entry_groups}
    exit_by_id = {group.id: group for group in topology.exit_groups}
    divergence_by_id = {group.id: group for group in topology.divergences}
    fan_groups = {
        (
            (
                exit_by_id[group.exit_group_id].section_id,
                exit_by_id[group.exit_group_id].side.value,
            ),
            tuple(
                sorted(
                    {
                        (
                            (
                                entry_by_id[
                                    connectors[connector_id].entry_group_id
                                ].section_id,
                                entry_by_id[
                                    connectors[connector_id].entry_group_id
                                ].side.value,
                            ),
                            connectors[connector_id].line_id,
                        )
                        for connector_id in group.connector_ids
                    }
                )
            ),
        )
        for group in topology.divergences
    }
    merge_groups = {
        (
            (
                entry_by_id[group.entry_group_id].section_id,
                entry_by_id[group.entry_group_id].side.value,
            ),
            group.line_id,
            tuple(
                sorted(
                    (
                        exit_by_id[
                            divergence_by_id[divergence_id].exit_group_id
                        ].section_id,
                        exit_by_id[
                            divergence_by_id[divergence_id].exit_group_id
                        ].side.value,
                    )
                    for divergence_id in group.divergence_ids
                )
            ),
        )
        for group in topology.convergences
    }
    return exit_groups, entry_groups, fan_groups, merge_groups


def test_corpus_topology_shapes_match_byte_identical_resolved_graphs() -> None:
    digest = hashlib.sha256()
    saw_bypass_path = False
    for path in CORPUS:
        graph = parse_metro_mermaid(path.read_text())
        topology = graph.route_topology
        resolution = graph.route_resolution
        assert topology is not None and resolution is not None
        assert _predicted_shapes(topology) == _actual_shapes(graph), path
        assert tuple(
            item.authored_edge_id for item in resolution.authored_edges
        ) == tuple(item.id for item in topology.authored_edges), path
        assert tuple(item.group_id for item in resolution.exit_ports) == tuple(
            item.id for item in topology.exit_groups
        ), path
        assert tuple(item.group_id for item in resolution.entry_ports) == tuple(
            item.id for item in topology.entry_groups
        ), path
        assert {item.group_id for item in resolution.divergences} == {
            item.id for item in topology.divergences
        }, path
        assert {item.group_id for item in resolution.convergences} == {
            item.id for item in topology.convergences
        }, path

        final_edges = {(edge.source, edge.target, edge.line_id) for edge in graph.edges}
        authored_by_id = {item.id: item for item in topology.authored_edges}
        for resolved in resolution.authored_edges:
            authored = authored_by_id[resolved.authored_edge_id]
            assert resolved.edge_paths, (path, resolved)
            for edge_path in resolved.edge_paths:
                assert edge_path, (path, resolved)
                assert all(edge.line_id == authored.key.line_id for edge in edge_path)
                assert all(
                    left.target == right.source
                    for left, right in zip(edge_path, edge_path[1:])
                ), (path, resolved)
                assert set(edge_path) <= final_edges, (path, resolved)
                saw_bypass_path |= any(
                    is_bypass_v(edge.source) or is_bypass_v(edge.target)
                    for edge in edge_path
                )

        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(
            repr(
                (
                    tuple(graph.stations.items()),
                    tuple(graph.ports.items()),
                    tuple(graph.junctions),
                    tuple(graph.edges),
                )
            ).encode()
        )

    assert digest.hexdigest() == (
        "ba9d35379e6b6b82d02227ef44b73072904388797c30def188619560c8ce7434"
    )
    assert saw_bypass_path


def test_route_topology_is_hash_seed_deterministic() -> None:
    fixture = ROOT / "examples" / "topologies" / "fanout_bundle_plus_spurs.mmd"
    script = (
        "from pathlib import Path; "
        "from nf_metro.parser.mermaid import parse_metro_mermaid; "
        "from nf_metro.parser.route_topology import build_route_topology_query; "
        f"p=Path({str(fixture)!r}); "
        "g=parse_metro_mermaid(p.read_text()); "
        "q=build_route_topology_query(g); "
        "print(repr((g.route_topology,g.route_resolution,q)))"
    )
    outputs = []
    for seed in ("1", "7", "41"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1
