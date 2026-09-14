"""Column-span awareness of ``_bypass_has_intervening_section`` (#1882).

The predicate decides whether a section boxes in the two-legged bypass between a
source and a LEFT-entry target, so the inter-row wrap band gets reserved.  A
section intervenes when its full grid span - both axes, span-aware - overlaps the
open corridor between source and target; a section anchored at or left of the
corridor qualifies whenever its ``grid_col_span`` reaches into it.

These tests drive the predicate directly with grids that isolate the column-span
case, and drive the whole reservation through a real layout of the companion
fixture.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.section_placement import (
    _bypass_has_intervening_section,
    _inter_row_routing_minimums,
)
from nf_metro.parser.mermaid import parse_metro_mermaid_file
from nf_metro.parser.model import MetroGraph, Section

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "topologies"
    / "bypass_left_entry_colspan_intervener.mmd"
)


def _section(sid: str, col: int, row: int, *, col_span: int = 1) -> Section:
    return Section(
        id=sid,
        name=sid,
        grid_col=col,
        grid_row=row,
        grid_col_span=col_span,
        bbox_w=100.0,
    )


def test_colspan_reaching_into_corridor_intervenes():
    """A section starting at ``lo_col`` whose span reaches into the open column
    corridor is an intervener: only its ``grid_col_span`` puts it in the way."""
    src = _section("src", col=0, row=0)
    tgt = _section("tgt", col=3, row=2)
    spanner = _section("spanner", col=0, row=1, col_span=3)
    graph = MetroGraph()
    for sec in (src, tgt, spanner):
        graph.sections[sec.id] = sec

    assert _bypass_has_intervening_section(graph, src, tgt) is True


def test_start_column_alone_does_not_intervene():
    """A single-column section anchored at ``lo_col`` (outside the open column
    corridor) and not spanning into it does not intervene."""
    src = _section("src", col=0, row=0)
    tgt = _section("tgt", col=3, row=2)
    edge_col = _section("edge_col", col=0, row=1)
    graph = MetroGraph()
    for sec in (src, tgt, edge_col):
        graph.sections[sec.id] = sec

    assert _bypass_has_intervening_section(graph, src, tgt) is False


def test_row_outside_corridor_does_not_intervene():
    """A section inside the column corridor but on a row outside the open row
    corridor does not intervene."""
    src = _section("src", col=0, row=0)
    tgt = _section("tgt", col=3, row=2)
    same_row_as_src = _section("above", col=2, row=0)
    graph = MetroGraph()
    for sec in (src, tgt, same_row_as_src):
        graph.sections[sec.id] = sec

    assert _bypass_has_intervening_section(graph, src, tgt) is False


def test_fixture_reserves_inter_row_band():
    """The companion fixture drives the LEFT-entry two-legged bypass whose
    intervener qualifies only through its column span; the reservation pass
    claims the ``(1, 2)`` inter-row gap for the wrap bundle."""
    graph = parse_metro_mermaid_file(FIXTURE)
    compute_layout(graph)

    source = graph.sections["source"]
    target = graph.sections["target"]
    assert _bypass_has_intervening_section(graph, source, target) is True
    assert _inter_row_routing_minimums(graph).get((1, 2), 0.0) > 0.0
