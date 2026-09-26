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
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import (
    _v_segment_crosses_other_section,
    apply_route_offsets,
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid((TOPOLOGIES / name).read_text())
        compute_layout(graph, validate=True)
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
