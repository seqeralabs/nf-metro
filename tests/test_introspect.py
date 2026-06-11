"""Tests for structured `nf-metro info` introspection (``nf_metro.introspect``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nf_metro.introspect import (
    build_info,
    format_info_json,
    format_info_text,
    station_kind,
)
from nf_metro.parser import parse_metro_mermaid

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# A spread of gallery fixtures: fully-inferred auto layout, a hand-tuned mix of
# explicit and inferred directives, and a couple of distinct topologies so the
# invariants generalise beyond a single .mmd.
FIXTURES = [
    "rnaseq_auto.mmd",
    "rnaseq_sections.mmd",
    "genomeassembly.mmd",
    "epitopeprediction.mmd",
]


def _graph(fixture: str):
    return parse_metro_mermaid((EXAMPLES_DIR / fixture).read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_info_has_all_top_level_keys(fixture: str) -> None:
    info = build_info(_graph(fixture))
    assert set(info) == {
        "title",
        "style",
        "warnings",
        "counts",
        "lines",
        "sections",
        "stations",
        "ports",
        "junctions",
        "section_dag",
        "layout",
    }


@pytest.mark.parametrize("fixture", FIXTURES)
def test_counts_match_graph(fixture: str) -> None:
    graph = _graph(fixture)
    counts = build_info(graph)["counts"]
    assert counts["stations"] == len(graph.stations)
    assert counts["edges"] == len(graph.edges)
    assert counts["lines"] == len(graph.lines)
    assert counts["sections"] == len(graph.sections)
    assert counts["ports"] == len(graph.ports)
    assert counts["junctions"] == len(graph.junctions)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_routes_exclude_synthetic_stations(fixture: str) -> None:
    """A line's ``route`` lists only authored stations, never ports/junctions."""
    graph = _graph(fixture)
    info = build_info(graph)
    for line in info["lines"]:
        for sid in line["route"]:
            assert sid not in graph.ports
            assert sid not in graph.junction_ids
            assert station_kind(graph, sid) == "station"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_synthetic_elements_surfaced(fixture: str) -> None:
    """Ports and junctions appear in the inventory with the right kind."""
    graph = _graph(fixture)
    info = build_info(graph)
    by_id = {st["id"]: st for st in info["stations"]}

    # Every junction is classified as a junction, not mislabelled a port, even
    # though its underlying station carries is_port=True.
    for jid in graph.junctions:
        assert by_id[jid]["kind"] == "junction"
    assert sorted(graph.junctions) == info["junctions"]

    # Every port is present and classified as a port.
    for pid in graph.ports:
        assert by_id[pid]["kind"] == "port"
    assert {p["id"] for p in info["ports"]} == set(graph.ports)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_section_dag_edges_match(fixture: str) -> None:
    graph = _graph(fixture)
    info = build_info(graph)
    reported = {(e["from"], e["to"]) for e in info["section_dag"]["edges"]}
    assert reported == set(graph.section_dag.section_edges)
    for edge in info["section_dag"]["edges"]:
        assert edge["lines"] == sorted(
            graph.section_dag.edge_lines[(edge["from"], edge["to"])]
        )


def test_inferred_when_no_directives() -> None:
    """An all-auto fixture reports every direction, grid, and port side inferred."""
    info = build_info(_graph("rnaseq_auto.mmd"))
    for sec in info["sections"]:
        assert sec["direction_inferred"] is True
        assert sec["grid_inferred"] is True
        assert sec["entry_sides_inferred"] is True
        assert sec["exit_sides_inferred"] is True
    for port in info["ports"]:
        assert port["side_inferred"] is True


def test_explicit_directives_reported_as_explicit() -> None:
    """Authored direction:/entry:/exit: directives are flagged explicit, not inferred.

    rnaseq_sections pins postprocessing to TB and qc_report to RL, and writes
    explicit entry/exit sides; the inferred sections around them stay inferred.
    """
    sections = {s["id"]: s for s in build_info(_graph("rnaseq_sections.mmd"))["sections"]}

    assert sections["postprocessing"]["direction"] == "TB"
    assert sections["postprocessing"]["direction_inferred"] is False
    assert sections["qc_report"]["direction"] == "RL"
    assert sections["qc_report"]["direction_inferred"] is False
    # A section without a direction: directive keeps the inferred default.
    assert sections["preprocessing"]["direction_inferred"] is True

    # preprocessing writes an explicit exit: but no entry: directive.
    assert sections["preprocessing"]["exit_sides_inferred"] is False
    assert sections["preprocessing"]["entry_sides_inferred"] is True


def test_port_side_inferred_tracks_section_directive() -> None:
    """A port's side_inferred mirrors whether its section authored that side."""
    graph = _graph("rnaseq_sections.mmd")
    info = build_info(graph)
    for port in info["ports"]:
        explicit = (
            graph._explicit_entry if port["is_entry"] else graph._explicit_exit
        )
        assert port["side_inferred"] is (port["section_id"] not in explicit)


def test_warnings_passed_through() -> None:
    info = build_info(_graph("rnaseq_auto.mmd"), ["something happened"])
    assert info["warnings"] == ["something happened"]


def test_layout_rows_reflect_folding() -> None:
    """sections_by_row partitions the real sections; folded iff >1 row."""
    graph = _graph("rnaseq_sections.mmd")
    info = build_info(graph)
    layout = info["layout"]
    placed = {sid for ids in layout["sections_by_row"].values() for sid in ids}
    real = {sid for sid, s in graph.sections.items() if not s.is_implicit}
    assert placed == real
    assert layout["rows"] == len(layout["sections_by_row"])
    assert layout["folded"] is (layout["rows"] > 1)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_json_round_trips(fixture: str) -> None:
    info = build_info(_graph(fixture))
    assert json.loads(format_info_json(info)) == info


@pytest.mark.parametrize("fixture", FIXTURES)
def test_default_text_is_a_prefix_of_verbose(fixture: str) -> None:
    """Verbose output extends, rather than rewrites, the stable summary."""
    info = build_info(_graph(fixture))
    plain = format_info_text(info, verbose=False)
    verbose = format_info_text(info, verbose=True)
    assert verbose.startswith(plain)
    assert "Section dependency graph:" not in plain
    assert "Section dependency graph:" in verbose
