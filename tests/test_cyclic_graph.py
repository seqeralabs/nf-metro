"""Cyclic / self-loop graphs must fail fast with a node-naming error.

The layout engine assumes a DAG (``assign_layers`` runs a topological sort).
A cycle or self-loop in the parsed graph must be rejected on both the
``validate`` plane (a structured :class:`ValidationIssue`) and the ``render``
plane (a ``ValueError`` out of ``compute_layout``), naming at least one node
so an author can locate the offending edge.

The section meta-graph is checked as well as the station digraph: sections can
form a cycle (``sec_a`` feeds ``sec_b`` which feeds ``sec_a``) while every
station edge still points forward, so a station-only check would miss it.
"""

import networkx as nx
import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import compute_layout
from nf_metro.parser import (
    ERROR,
    CyclicGraphError,
    parse_metro_mermaid,
    validate_graph,
)

_TWO_NODE_CYCLE = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    a[A] -->|l1| b[B]
    b -->|l1| a
"""

_SELF_LOOP = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    a[A] -->|l1| a
"""

# A cycle at the section level whose station edges all point forward
# (a1 -> b1, b2 -> a2): the station digraph is acyclic, only the section
# meta-graph (seca -> secb -> seca) closes the loop.
_SECTION_CYCLE = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    subgraph seca [Section A]
        a1[A1]
        a2[A2]
    end
    subgraph secb [Section B]
        b1[B1]
        b2[B2]
    end
    a1 -->|l1| b1
    b2 -->|l1| a2
"""

# A station cycle contained inside a single section (a1 -> a2 -> a1). The
# station digraph is cyclic, so the whole-graph check rejects it before any
# section-internal layout estimate is attempted.
_WITHIN_SECTION_CYCLE = """\
%%metro line: l1 | Line 1 | #ff0000 | solid
graph LR
    subgraph seca [Section A]
        a1[A1] -->|l1| a2[A2]
        a2 -->|l1| a1
    end
    a2 -->|l1| b1[B1]
"""

_CYCLIC = [
    pytest.param(_TWO_NODE_CYCLE, id="two-node-cycle"),
    pytest.param(_SELF_LOOP, id="self-loop"),
    pytest.param(_WITHIN_SECTION_CYCLE, id="within-section-cycle"),
]


@pytest.mark.parametrize("mmd", _CYCLIC)
def test_validate_graph_flags_cycle(mmd: str):
    graph = parse_metro_mermaid(mmd)

    cycle_errors = [
        issue
        for issue in validate_graph(graph)
        if issue.severity == ERROR and "cycle" in issue.message.lower()
    ]

    assert len(cycle_errors) == 1
    assert "a" in cycle_errors[0].message


@pytest.mark.parametrize("mmd", _CYCLIC)
def test_compute_layout_rejects_cycle(mmd: str):
    graph = parse_metro_mermaid(mmd)

    with pytest.raises(CyclicGraphError, match="cycle") as excinfo:
        compute_layout(graph)

    assert "a" in str(excinfo.value)


@pytest.mark.parametrize("mmd", _CYCLIC)
@pytest.mark.parametrize("command", ["render", "validate"])
def test_cli_rejects_cycle(mmd: str, command: str, tmp_path):
    src = tmp_path / "cyclic.mmd"
    src.write_text(mmd)
    args = [command, str(src)]
    if command == "render":
        args += ["-o", str(tmp_path / "out.svg")]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "cycle" in result.output.lower()
    assert "a" in result.output


def test_validate_graph_flags_section_cycle():
    graph = parse_metro_mermaid(_SECTION_CYCLE)

    cycle_errors = [
        issue
        for issue in validate_graph(graph)
        if issue.severity == ERROR and "cycle" in issue.message.lower()
    ]

    assert len(cycle_errors) == 1
    message = cycle_errors[0].message
    assert "seca" in message and "secb" in message


def test_compute_layout_rejects_section_cycle():
    graph = parse_metro_mermaid(_SECTION_CYCLE)

    with pytest.raises(CyclicGraphError, match="cycle") as excinfo:
        compute_layout(graph)

    message = str(excinfo.value)
    assert "seca" in message and "secb" in message


def test_section_cycle_raises_cyclic_not_networkx():
    """A section cycle surfaces as the authoring diagnostic, never a raw
    ``networkx`` topological-sort failure leaking out of the engine."""
    graph = parse_metro_mermaid(_SECTION_CYCLE)

    with pytest.raises(CyclicGraphError) as excinfo:
        compute_layout(graph)

    assert not isinstance(excinfo.value, nx.NetworkXException)


@pytest.mark.parametrize("command", ["render", "validate"])
def test_cli_rejects_section_cycle(command: str, tmp_path):
    src = tmp_path / "section_cyclic.mmd"
    src.write_text(_SECTION_CYCLE)
    args = [command, str(src)]
    if command == "render":
        args += ["-o", str(tmp_path / "out.svg")]

    result = CliRunner().invoke(cli, args)

    assert result.exit_code != 0
    assert "cycle" in result.output.lower()
    assert "seca" in result.output


def test_internal_station_depths_raises_on_cyclic_section():
    """The section size estimator rejects a section whose internal edges form a
    cycle rather than returning a distorted depth map. The parser gate keeps
    cyclic graphs away from this path, so it is exercised by calling the
    estimator directly on a graph with a section-internal cycle."""
    from nf_metro.layout.auto_layout import _internal_station_depths

    graph = parse_metro_mermaid(_WITHIN_SECTION_CYCLE)
    section_id = next(sid for sid, s in graph.sections.items() if "a1" in s.station_ids)

    with pytest.raises(CyclicGraphError, match="cyclic internal edges") as excinfo:
        _internal_station_depths(graph, section_id)

    assert "a1" in str(excinfo.value) and "a2" in str(excinfo.value)
