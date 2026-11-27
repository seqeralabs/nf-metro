"""Junctions under a rigid downward translation of a grid row.

Translating a row (its sections, their stations and their ports) by a small
amount is layout-neutral: nothing inside the row moves relative to anything
else in it.  Junction stations, though, live outside any section, and their
coordinates are derived rather than authored - a fan-out junction is pinned to
the Y of the exit port that feeds it, a merge junction to the Y of the entry
port it feeds.  A translation that leaves a junction at its old Y strands it
off the bundle it belongs to, and the exit-port-to-junction connector
degenerates into a dogleg too short to carry a curve radius.

``examples/topologies/complex_multipath.mmd`` exposes this: its
``full_preprocess`` right-exit fan-out junction sits ``JUNCTION_MARGIN`` from
the exit port, so a few pixels of stranding is enough to turn a straight
connector into a sub-radius Z.

Layout's row translation carries the junctions with their anchors, and the
render routes the junctions as laid out: a graph that reaches it with one
stranded is an engine defect the render refuses rather than repairs.

A graph-wide rail layout is the one place where the port-derived placement is
not the authority: it seats junctions on per-line rail Ys instead, so the
derivation must leave those alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.phases.bbox import _shift_rows_from
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    _guard_fanout_junction_shares_exit_port_y,
    assert_junctions_on_their_anchors,
)
from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import _settled_render_graph, build_render_plan

FIXTURE = (
    Path(__file__).parent.parent / "examples" / "topologies" / "complex_multipath.mmd"
)

RAIL_DIVERGENCE = """%%metro title: Rail Divergence
%%metro line_spread: rails
%%metro line: a | A | #e63946
%%metro line: b | B | #2db572

graph LR
    subgraph s1 [One]
        n1[Start]
        n2[Split]
        n1 -->|a,b| n2
    end
    subgraph s2 [Two]
        n3[Left]
    end
    subgraph s3 [Three]
        n4[Right]
    end
    n2 -->|a| n3
    n2 -->|b| n4
"""


def _translate_rows_stranding_junctions(
    graph: MetroGraph, from_row: int, amount: float
) -> None:
    """Move sections at or below *from_row* down by *amount*, junctions aside.

    Deliberately narrower than :func:`_shift_rows_from`: omitting the junction
    re-derivation puts the render path in front of a graph whose junctions were
    left behind by whatever moved the sections.
    """
    for section in graph.sections.values():
        if section.grid_row < from_row:
            continue
        section.bbox_y += amount
        for station_id in section.station_ids:
            station = graph.stations.get(station_id)
            if station is not None:
                station.y += amount
            port = graph.ports.get(station_id)
            if port is not None:
                port.y += amount


def test_layout_leaves_every_junction_on_its_anchor() -> None:
    graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
    assert_junctions_on_their_anchors(graph)
    drawn = _settled_render_graph(graph, resolve_theme(graph))
    _guard_fanout_junction_shares_exit_port_y(drawn, "render")


@pytest.mark.parametrize("amount", range(1, 25))
def test_a_render_refuses_junctions_a_row_translation_stranded(amount: int) -> None:
    """The render reads junctions as laid out and refuses a stranded one.

    Re-seating it would repair, at render time, a placement the layout got
    wrong, so the render stops instead.
    """
    graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
    _translate_rows_stranding_junctions(graph, 1, amount)
    with pytest.raises(LayoutInvariantError, match="junctions off the anchors"):
        _settled_render_graph(graph, resolve_theme(graph))


@pytest.mark.parametrize("amount", [6.0, 17.5, 40.0])
def test_shift_rows_keeps_junctions_on_their_exit_ports(amount: float) -> None:
    graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
    _shift_rows_from(graph, 1, amount)
    _guard_fanout_junction_shares_exit_port_y(graph, "row-shift")
    assert_junctions_on_their_anchors(graph)


def test_rail_layout_junctions_keep_their_rail_y() -> None:
    graph = prepare_graph(RAIL_DIVERGENCE)
    junction_id = next(iter(graph.junctions))
    rail_xy = (graph.stations[junction_id].x, graph.stations[junction_id].y)
    plan = build_render_plan(graph, resolve_theme(graph))
    planned = plan.graph.stations[junction_id]
    assert (planned.x, planned.y) == rail_xy
