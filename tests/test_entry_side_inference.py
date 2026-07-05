"""Geometry-aware entry-side inference (#1342).

A section is entered from one side (``_guard_no_mixed_entry_directions``), so
all its lines share a single entry side.  When that side is not fixed by an
agreeing hint it is chosen from where the feeds actually arrive, preferring the
flow-natural side when it is fed rather than collapsing blindly onto it.
"""

from __future__ import annotations

import glob
import warnings

import pytest

from nf_metro.parser import resolve
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide, Section, SectionDAG


def _parse(path: str) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return parse_metro_mermaid(open(path).read())


def _graph_with_hub(direction: str, feeds: dict[str, tuple[int, int]]) -> MetroGraph:
    """A ``hub`` section at (1, 1) fed by one section per entry in ``feeds``.

    ``feeds`` maps a source id to its (col, row) grid cell; every source feeds
    line ``x`` into the hub.  Grid cells are recorded in ``grid_overrides`` so
    ``_effective_grid_pos`` reads them at resolve time.
    """
    graph = MetroGraph()
    cells = {"hub": (1, 1), **feeds}
    for sid, (col, row) in cells.items():
        section = Section(
            id=sid, name=sid, direction=direction if sid == "hub" else "LR"
        )
        section.grid_col, section.grid_row = col, row
        graph.sections[sid] = section
        graph.grid_overrides[sid] = (col, row, 1, 1)
    graph.section_dag = SectionDAG(
        successors={src: {"hub"} for src in feeds},
        predecessors={"hub": set(feeds)},
        edge_lines={(src, "hub"): {"x"} for src in feeds},
    )
    return graph


def test_dominant_prefers_natural_side_when_it_is_fed() -> None:
    """An LR hub fed from the left keeps its flow-natural LEFT entry.

    A top feed also arrives, but the natural side receives a feed, so internal
    flow enters at its source and the top feed routes around.
    """
    graph = _graph_with_hub("LR", {"left_src": (0, 1), "top_src": (1, 0)})
    assert resolve._dominant_entry_side(graph, "hub") is PortSide.LEFT


def test_dominant_deviates_when_natural_side_is_unfed() -> None:
    """A TB hub whose natural TOP receives no feed takes a fed side instead.

    Feeds arrive from the left and right only; collapsing to the flow-natural
    TOP would put the entry port where nothing arrives, so the entry falls to a
    fed side.
    """
    graph = _graph_with_hub("TB", {"left_src": (0, 1), "right_src": (2, 1)})
    side = resolve._dominant_entry_side(graph, "hub")
    assert side is not PortSide.TOP
    assert side in (PortSide.LEFT, PortSide.RIGHT)


def test_build_mapping_shares_one_side_across_a_sections_lines() -> None:
    """Every line entering a section resolves to the same single side."""
    graph = _parse("examples/topologies/riboseq_fold_two_dir_entry_hintless.mmd")
    mapping = resolve._build_entry_side_mapping(graph)
    by_section: dict[str, set[PortSide]] = {}
    for (sec_id, _line), side in mapping.items():
        by_section.setdefault(sec_id, set()).add(side)
    for sec_id, sides in by_section.items():
        assert len(sides) == 1, f"{sec_id} resolved to multiple entry sides: {sides}"


_ALL_FIXTURES = sorted(glob.glob("examples/**/*.mmd", recursive=True))


@pytest.mark.parametrize("path", _ALL_FIXTURES, ids=lambda p: p.rsplit("/", 1)[-1])
def test_one_entry_port_per_line_per_section(path: str) -> None:
    """Every line enters a section through at most one entry port (#1341 veto)."""
    graph = _parse(path)
    for sec_id, section in graph.sections.items():
        by_line: dict[str, set[str]] = {}
        for pid in section.entry_ports:
            for line in graph.station_lines(pid):
                by_line.setdefault(line, set()).add(pid)
        for line, ports in by_line.items():
            assert len(ports) == 1, (
                f"{sec_id}/{line} enters via {len(ports)} ports in {path}"
            )
