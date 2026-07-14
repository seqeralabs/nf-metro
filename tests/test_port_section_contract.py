"""Contract: every port resolves to a section in the graph.

A port carries a required ``section_id`` naming the section it bounds, and that
section is registered before layout, so a laid-out port always resolves to its
section. This module pins that as a construction guarantee:
:meth:`MetroGraph.section_for_port` returns a non-optional section, and both the
layout boundary and :func:`validate_graph` reject a port whose section does not
resolve.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.parser import (
    ERROR,
    UnresolvedPortSectionError,
    parse_metro_mermaid,
    require_resolved_port_sections,
    validate_graph,
)
from nf_metro.parser.model import MetroGraph, Port, PortSide, Section

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

CORPUS_FIXTURES = [
    EXAMPLES / "rnaseq_sections.mmd",
    EXAMPLES / "rnaseq_auto.mmd",
    EXAMPLES / "topologies" / "asymmetric_tree.mmd",
    EXAMPLES / "topologies" / "around_section_below.mmd",
    EXAMPLES / "rail_mode.mmd",
    EXAMPLES / "topologies" / "sectionless_skip_breeze.mmd",
]


def _port_graph() -> tuple[MetroGraph, Port]:
    """A graph with one section-bound port and one dangling-section port."""
    g = MetroGraph()
    g.add_section(Section(id="s1", name="S1"))
    g.add_port(Port(id="p_ok", section_id="s1", side=PortSide.LEFT))
    dangling = Port(id="p_bad", section_id="ghost", side=PortSide.LEFT)
    g.add_port(dangling)
    return g, dangling


@pytest.mark.parametrize("path", CORPUS_FIXTURES, ids=lambda p: p.name)
def test_every_port_resolves_to_a_section(path: Path) -> None:
    """Every port in a laid-out corpus fixture resolves to its section."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
    for port in graph.ports.values():
        section = graph.section_for_port(port)
        assert section is graph.sections[port.section_id]


def test_section_for_port_raises_on_unresolved_section() -> None:
    """The accessor fails loudly rather than returning a None section."""
    graph, dangling = _port_graph()
    assert graph.section_for_port(graph.ports["p_ok"]) is graph.sections["s1"]
    with pytest.raises(UnresolvedPortSectionError) as excinfo:
        graph.section_for_port(dangling)
    assert "ghost" in str(excinfo.value)


def test_require_resolved_port_sections_raises_at_boundary() -> None:
    """The layout-boundary check rejects a port whose section is missing."""
    graph, _ = _port_graph()
    with pytest.raises(UnresolvedPortSectionError) as excinfo:
        require_resolved_port_sections(graph)
    assert "ghost" in str(excinfo.value)


def test_validate_graph_flags_unresolved_port_section() -> None:
    """The CLI soft-validation path reports the dangling section as an error."""
    graph, _ = _port_graph()
    issues = validate_graph(graph)
    assert any(i.severity == ERROR and "ghost" in i.message for i in issues), issues
