"""Contract: every edge's endpoints resolve to a station in the graph.

The routing engine's handlers read ``graph.stations.get(edge.source)`` and
``.get(edge.target)`` and historically hedged each read with an ``if not src or
not tgt`` skip arm. Those arms are unreachable for any valid parse: the resolver
inserts ports and junctions as real stations, so both endpoints always resolve.
This module pins that contract as a construction guarantee -- :meth:`MetroGraph.
edge_endpoints` returns non-optional stations, and both the layout boundary and
:func:`validate_graph` reject an edge whose endpoint does not resolve -- so the
per-handler hedges can be retired.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.parser import (
    ERROR,
    UnresolvedEndpointError,
    parse_metro_mermaid,
    validate_graph,
)
from nf_metro.parser.model import Edge, MetroGraph, MetroLine, Station

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

CORPUS_FIXTURES = [
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "rnaseq_auto.mmd",
    EXAMPLES / "topologies" / "asymmetric_tree.mmd",
    EXAMPLES / "topologies" / "around_section_below.mmd",
    EXAMPLES / "centered_tracks.mmd",
    EXAMPLES / "cross_track_interchange.mmd",
]


def _dangling_graph() -> tuple[MetroGraph, Edge]:
    """A graph whose second edge points at a station that was never added."""
    g = MetroGraph()
    g.add_line(MetroLine(id="l1", display_name="L1", color="#f00"))
    g.add_station(Station(id="a", label="A"))
    g.add_station(Station(id="b", label="B"))
    g.add_edge(Edge(source="a", target="b", line_id="l1"))
    dangling = Edge(source="b", target="ghost", line_id="l1")
    g.add_edge(dangling)
    return g, dangling


@pytest.mark.parametrize("path", CORPUS_FIXTURES, ids=lambda p: p.name)
def test_every_edge_resolves_to_stations(path: Path) -> None:
    """Every edge in a parsed corpus fixture resolves both endpoints."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
    for edge in graph.edges:
        src, tgt = graph.edge_endpoints(edge)
        assert isinstance(src, Station)
        assert isinstance(tgt, Station)
        assert src is graph.stations[edge.source]
        assert tgt is graph.stations[edge.target]


def test_edge_endpoints_raises_on_dangling_endpoint() -> None:
    """The accessor fails loudly rather than returning a None endpoint."""
    graph, dangling = _dangling_graph()
    with pytest.raises(UnresolvedEndpointError) as excinfo:
        graph.edge_endpoints(dangling)
    assert "ghost" in str(excinfo.value)


def test_validate_graph_flags_unresolved_endpoint() -> None:
    """The CLI soft-validation path reports the dangling endpoint as an error."""
    graph, _ = _dangling_graph()
    issues = validate_graph(graph)
    assert any(i.severity == ERROR and "ghost" in i.message for i in issues), issues
