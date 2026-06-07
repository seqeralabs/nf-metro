"""Unit tests for the graph-semantic validator (issue #559)."""

import dataclasses

import pytest

from nf_metro.parser import ERROR, WARNING, ValidationIssue, validate_graph
from nf_metro.parser.model import Edge, MetroGraph, MetroLine, Section, Station


def _graph_with_line() -> MetroGraph:
    graph = MetroGraph()
    graph.lines["rna"] = MetroLine(id="rna", display_name="RNA", color="#abcdef")
    graph.stations["a"] = Station(id="a", label="A")
    graph.stations["b"] = Station(id="b", label="B")
    return graph


def test_clean_graph_has_no_issues():
    graph = _graph_with_line()
    graph.edges.append(Edge(source="a", target="b", line_id="rna"))
    assert validate_graph(graph) == []


def test_default_line_edge_is_not_flagged():
    graph = _graph_with_line()
    graph.edges.append(Edge(source="a", target="b", line_id="default"))
    assert validate_graph(graph) == []


def test_undefined_line_reference_is_an_error():
    graph = _graph_with_line()
    graph.edges.append(Edge(source="a", target="b", line_id="missing"))

    issues = validate_graph(graph)

    assert issues == [
        ValidationIssue(
            ERROR,
            "Edge a -> b references undefined line 'missing'",
        )
    ]


def test_section_referencing_unknown_station_is_an_error():
    graph = _graph_with_line()
    graph.sections["sec"] = Section(
        id="sec", name="Section One", station_ids=["a", "ghost"]
    )

    issues = validate_graph(graph)

    assert issues == [
        ValidationIssue(
            ERROR,
            "Section 'Section One' references unknown station 'ghost'",
        )
    ]


def test_issues_are_frozen():
    issue = ValidationIssue(ERROR, "msg")
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.severity = WARNING  # type: ignore[misc]


def test_multiple_findings_are_all_collected():
    graph = _graph_with_line()
    graph.edges.append(Edge(source="a", target="b", line_id="missing"))
    graph.sections["sec"] = Section(id="sec", name="Section One", station_ids=["ghost"])

    issues = validate_graph(graph)

    assert len(issues) == 2
    assert all(issue.severity == ERROR for issue in issues)
