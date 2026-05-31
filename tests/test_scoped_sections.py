"""Unit tests for the row-scoping helpers used by the row-major Pass C sweep."""

from __future__ import annotations

import pytest

from nf_metro.layout.phases._common import (
    _grid_rows_top_to_bottom,
    _scoped_sections,
)
from nf_metro.parser.model import MetroGraph, Section


def _graph_with_sections() -> MetroGraph:
    graph = MetroGraph()
    graph.sections = {
        "a": Section(id="a", name="A", grid_row=0, grid_col=0, bbox_h=50),
        "b": Section(id="b", name="B", grid_row=0, grid_col=1, bbox_h=50),
        "c": Section(id="c", name="C", grid_row=1, grid_col=0, bbox_h=50),
    }
    return graph


def test_scoped_sections_restricts_then_restores():
    graph = _graph_with_sections()
    original = graph.sections

    with _scoped_sections(graph, ["a", "c"]):
        assert set(graph.sections) == {"a", "c"}
        assert graph.sections["a"] is original["a"]

    assert graph.sections is original
    assert set(graph.sections) == {"a", "b", "c"}


def test_scoped_sections_ignores_unknown_ids():
    graph = _graph_with_sections()
    with _scoped_sections(graph, ["a", "missing"]):
        assert set(graph.sections) == {"a"}


def test_scoped_sections_restores_on_exception():
    graph = _graph_with_sections()
    original = graph.sections

    with pytest.raises(RuntimeError):
        with _scoped_sections(graph, ["b"]):
            raise RuntimeError("boom")

    assert graph.sections is original
    assert set(graph.sections) == {"a", "b", "c"}


def test_grid_rows_top_to_bottom_groups_and_orders():
    graph = _graph_with_sections()
    assert _grid_rows_top_to_bottom(graph) == [["a", "b"], ["c"]]


def test_grid_rows_top_to_bottom_skips_empty_and_unassigned():
    graph = _graph_with_sections()
    graph.sections["d"] = Section(id="d", name="D", grid_row=2, grid_col=0, bbox_h=0)
    graph.sections["e"] = Section(id="e", name="E", grid_row=-1, grid_col=0, bbox_h=50)
    assert _grid_rows_top_to_bottom(graph) == [["a", "b"], ["c"]]
