"""A divergence junction fed down a vertical-flow section's own trailing exit.

A TB section leaves through its BOTTOM edge (a BT section through its TOP) and
the line forks at an off-box junction into two sections stacked in the column
beyond.  The feeder carries its lane across the port on X, so the fork's lanes
spread on X too: every branch must leave the junction exactly where its feeder
arrives, on the lane ``junction.x + lane_sign * offset`` that the feeding
section's own lane sign dictates, and a branch bound for the farther section
must reach it without ploughing through the nearer one.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import section_lane_sign
from nf_metro.layout.route_topology import divergence_junction_sources
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import (
    _v_segment_crosses_other_section,
    apply_route_offsets,
    lines_by_section,
    stack_diverted_junction_branches,
)
from nf_metro.layout.routing.normalize import _h_segment_crosses_other_section
from nf_metro.layout.routing.reversal import tb_positive_fan_sections
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

TOPOLOGIES = Path("examples/topologies")
FIXTURES = (
    "tb_bottom_exit_junction_stacked_fork.mmd",
    "tb_bottom_exit_junction_stacked_fork_two_lines.mmd",
    "bt_top_exit_junction_stacked_fork_two_lines.mmd",
    "bottom_exit_junction_collinear_top_entry.mmd",
    "bottom_exit_junction_offset_target.mmd",
)
TOLERANCE = 0.5


def _laid_out(name: str):
    return _laid_out_text((TOPOLOGIES / name).read_text(), validate=True)


def _laid_out_text(text: str, *, validate: bool = False):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(text)
        compute_layout(graph, validate=validate)
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
    polylines = [
        (route, apply_route_offsets(route, offsets)) for route in routes if route.points
    ]
    return graph, offsets, polylines


def _fork(graph):
    (junction_id,) = [jid for jid in graph.junctions if len(graph.edges_from(jid)) > 1]
    (feed,) = {edge.source for edge in graph.edges_to(junction_id)}
    return graph.stations[junction_id], graph.ports[feed]


@pytest.mark.parametrize("name", FIXTURES)
def test_renders(name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string((TOPOLOGIES / name).read_text())


@pytest.mark.parametrize("name", FIXTURES)
def test_branches_leave_the_junction_on_the_feeder_lane(name: str) -> None:
    graph, offsets, polylines = _laid_out(name)
    junction, port = _fork(graph)
    assert port.side in (PortSide.TOP, PortSide.BOTTOM)
    lane_sign = section_lane_sign(
        graph.sections[port.section_id], tb_positive_fan_sections(graph)
    )
    line_ids = {edge.line_id for edge in graph.edges_from(junction.id)}
    for line_id in line_ids:
        planned = (
            junction.x + lane_sign * offsets.get((junction.id, line_id), 0.0),
            junction.y,
        )
        arrivals = [
            points[-1]
            for route, points in polylines
            if route.edge.target == junction.id and route.line_id == line_id
        ]
        departures = [
            points[0]
            for route, points in polylines
            if route.edge.source == junction.id and route.line_id == line_id
        ]
        assert arrivals and departures
        for point in (*arrivals, *departures):
            assert abs(point[0] - planned[0]) <= TOLERANCE, (
                f"{line_id} meets the junction at x={point[0]}, off its planned "
                f"lane x={planned[0]}"
            )
            assert abs(point[1] - planned[1]) <= TOLERANCE, (
                f"{line_id} meets the junction at y={point[1]}, short of the "
                f"fork at y={planned[1]}"
            )


@pytest.mark.parametrize("name", FIXTURES)
def test_inter_section_legs_clear_other_sections(name: str) -> None:
    graph, _offsets, polylines = _laid_out(name)
    for route, points in polylines:
        if not route.is_inter_section:
            continue
        exclude = {
            graph.stations[station_id].section_id
            for station_id in (route.edge.source, route.edge.target)
        }
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if abs(x1 - x2) <= TOLERANCE:
                assert not _v_segment_crosses_other_section(
                    graph, x1, y1, y2, exclude
                ), f"{route.edge.source}->{route.edge.target} drops through a box"
            if abs(y1 - y2) <= TOLERANCE:
                assert not _h_segment_crosses_other_section(
                    graph, x1, x2, y1, exclude
                ), f"{route.edge.source}->{route.edge.target} runs through a box"


_TWO_BRANCHES_PAST_THE_STACK = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro line: l4 | Line 4 | #b13737
%%metro line_order: span
%%metro grid: a | 0,0
%%metro grid: b | 1,0
%%metro grid: x | 1,1
%%metro grid: t0 | 1,2
%%metro grid: t1 | 1,3
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: TB
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1,l4| b1
        b1 -->|l1,l4| b2
    end
    subgraph x [X]
        %%metro direction: TB
        xn0[XN0]
        xn1[XN1]
        xn0 -->|l4| xn1
    end
    subgraph t0 [T0]
        t0n0[T0N0]
        t0n1[T0N1]
        t0n0 -->|l1| t0n1
    end
    subgraph t1 [T1]
        t1n0[T1N0]
    end
    a0 -->|l1,l4| b0
    b2 -->|l1| t0n0
    b2 -->|l4| t1n0
    b0 -->|l4| xn0
"""

_SIBLING_LEAVES_THE_COLUMN = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro grid: a | 1,2
%%metro grid: b | 0,2
%%metro grid: x | 0,1
%%metro grid: t0 | 0,0
%%metro grid: t1 | 1,0
graph LR
    subgraph a [A]
        %%metro direction: RL
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: BT
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1| b1
        b1 -->|l1| b2
    end
    subgraph x [X]
        %%metro direction: RL
        xn0[XN0]
    end
    subgraph t0 [T0]
        %%metro direction: TB
        t0n0[T0N0]
    end
    subgraph t1 [T1]
        %%metro direction: TB
        t1n0[T1N0]
    end
    a0 -->|l1| b0
    b2 -->|l1| t0n0
    b2 -->|l1| t1n0
    a0 -->|l1| xn0
"""


def _stack_diversions(graph) -> dict[str, tuple[str, ...]]:
    section_lines = lines_by_section(graph)
    return {
        junction_id: tuple(
            edge.target
            for edge in stack_diverted_junction_branches(
                graph, junction_id, exit_port_id, section_lines
            )
        )
        for junction_id, exit_port_id in divergence_junction_sources(graph).items()
    }


@pytest.mark.parametrize("name", FIXTURES[:3])
def test_one_branch_diverts_round_the_stack(name: str) -> None:
    graph, _offsets, _polylines = _laid_out(name)
    junction, _port = _fork(graph)
    assert len(_stack_diversions(graph)[junction.id]) == 1


@pytest.mark.parametrize(
    "text",
    [_TWO_BRANCHES_PAST_THE_STACK, _SIBLING_LEAVES_THE_COLUMN],
    ids=["two-branches-past-the-stack", "sibling-leaves-the-column"],
)
def test_shared_stack_gap_keeps_the_straight_drops(text: str) -> None:
    """A diversion owns the gap beside the column, so none is taken when a
    second branch would divert too or a sibling leaves the column through it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string(text)
        graph = parse_metro_mermaid(text)
        compute_layout(graph)
    assert not any(_stack_diversions(graph).values())


_ADJACENT_ROW_TRAILING_EXIT = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro grid: s | 0,1
%%metro grid: b | 1,0
%%metro grid: x | 1,1
%%metro grid: t | 2,0
graph LR
    subgraph s [S]
        s0[S0]
    end
    subgraph b [B]
        %%metro direction: TB
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1| b1
        b1 -->|l1| b2
    end
    subgraph x [X]
        %%metro direction: BT
        xn0[XN0]
    end
    subgraph t [T]
        %%metro direction: BT
        tn0[TN0]
    end
    s0 -->|l1| xn0
    xn0 -->|l1| tn0
    s0 -->|l1| b0
"""


def test_adjacent_row_trailing_exit_turns_in_the_shared_gap() -> None:
    """A BT TOP exit feeding the next column's BOTTOM entry one row up has no
    row between them to divert round, so it turns in the gap the two rows share
    rather than detouring round the box above its own column."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string(_ADJACENT_ROW_TRAILING_EXIT)
        graph, _offsets, polylines = _laid_out_text(_ADJACENT_ROW_TRAILING_EXIT)
    (points,) = [
        points
        for route, points in polylines
        if route.edge.source == "x__exit_top_1" and route.edge.target.startswith("t__")
    ]
    x, t = graph.sections["x"], graph.sections["t"]
    assert all(
        t.bbox_y + t.bbox_h - TOLERANCE <= y <= x.bbox_y + TOLERANCE for _x, y in points
    )


_COLUMN_RECEIVER_THROUGH_JUNCTION = """\
%%metro line: l1 | Line 0 | #3779b1
%%metro line: l2 | Line 1 | #a66d13
%%metro grid: a | 0,0
%%metro grid: b | 1,0
%%metro grid: x | 1,1
%%metro grid: t0 | 1,2
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: TB
        b0[B0]
        b1[B1]
        b0 -->|l2| b1
    end
    subgraph x [X]
        %%metro direction: TB
        xn0[XN0]
    end
    subgraph t0 [T0]
        %%metro direction: BT
        t0n0[T0N0]
    end
    a0 -->|l2| b0
    b1 -->|l2| t0n0
    b0 -->|l2| xn0
"""


def test_column_receiver_behind_a_junction_renders() -> None:
    """A vertical-flow receiver's first station keeps its own trunk lane, so its
    facing entry takes no lane copied through the junction that would part the
    entry from that trunk inside a planned fan's boundary frame."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string(_COLUMN_RECEIVER_THROUGH_JUNCTION)


_PERPENDICULAR_TOP_EXIT_FORK = """\
%%metro line: l1 | Line 0 | #3779b1
%%metro line: l2 | Line 1 | #a66d13
%%metro line: l3 | Line 2 | #156075
%%metro grid: b | 1,2
%%metro grid: a | 0,2
%%metro grid: t0 | 2,1
%%metro grid: t1 | 1,0
graph LR
    subgraph b [B]
        %%metro exit: top | l1, l2
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1,l2| b1
        b1 -->|l1,l2| b2
    end
    subgraph a [A]
        %%metro direction: TB
        a0[A0]
    end
    subgraph t0 [T0]
        %%metro direction: TB
        %%metro entry: top | l1
        t0n0[T0N0]
    end
    subgraph t1 [T1]
        %%metro direction: RL
        t1n0[T1N0]
    end
    a0 -->|l1,l2,l3| b0
    b2 -->|l1| t0n0
    b2 -->|l2| t1n0
"""


def test_perpendicular_top_exit_junction_keeps_its_row_placement() -> None:
    """Only a vertical-flow section's trailing TOP exit stands its junction off
    along the column; an LR section's perpendicular TOP exit keeps the junction
    on the port's own row."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string(_PERPENDICULAR_TOP_EXIT_FORK)
        graph = parse_metro_mermaid(_PERPENDICULAR_TOP_EXIT_FORK)
        compute_layout(graph)
    ((junction_id, exit_port_id),) = divergence_junction_sources(graph).items()
    assert graph.ports[exit_port_id].side is PortSide.TOP
    port = graph.stations[exit_port_id]
    assert abs(graph.stations[junction_id].y - port.y) <= TOLERANCE
