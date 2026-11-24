"""A junction fan launches outside the section it leaves, and a route ending on
a station's drawn lane is anchored there, at any line width, gap and radius.

``packed_cell_right_exit_left_entry_wrap`` fans five lines out of one right
exit: once the fan's half-width passes the junction's margin from the wall, the
shared lead-in would launch inside the source box.  Its one-line ``hicreads``
station draws its marker on its own lane, a whole bundle offset off the
station's nominal centre, where its route begins.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from nf_metro.api import apply_theme_layout
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import check_no_hanging_routes
from nf_metro.layout.routing.offsets import drawn_lane_span
from nf_metro.layout.routing.reversal import tb_positive_fan_sections
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.themes import bound_stack

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "topologies"
    / "packed_cell_right_exit_left_entry_wrap.mmd"
)


def _laid_out(
    tmp_path: Path, line_width: float, track_gap: int, ratio: float | None
) -> MetroGraph:
    theme = tmp_path / "theme.yaml"
    layout = "" if ratio is None else f"layout:\n  curve_radius_ratio: {ratio}\n"
    theme.write_text(f"name: grid\nline_width: {line_width}\n{layout}")
    text = re.sub(
        r"%%metro track_gap: .*", f"%%metro track_gap: {track_gap}", FIXTURE.read_text()
    )
    graph = parse_metro_mermaid(text, max_station_columns=15)
    graph.source_dir = str(FIXTURE.parent)
    apply_theme_layout(graph, bound_stack(graph, str(theme)))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph, validate=True)
    return graph


def _launches_inside_source(graph: MetroGraph) -> list[str]:
    inside: list[str] = []
    for route in route_edges(graph):
        if route.edge.source not in graph.junctions:
            continue
        feeders = [
            graph.ports[edge.source]
            for edge in graph.edges_to(route.edge.source)
            if edge.source in graph.ports
        ]
        for port in feeders:
            wall = graph.stations[port.id].x
            launch = route.points[0][0]
            if (port.side is PortSide.RIGHT and launch < wall - COORD_TOLERANCE) or (
                port.side is PortSide.LEFT and launch > wall + COORD_TOLERANCE
            ):
                inside.append(f"{route.edge.source}->{route.edge.target} at {launch}")
    return inside


@pytest.mark.parametrize("ratio", [None, 2.5, 3.0])
@pytest.mark.parametrize("track_gap", [1, 2, 3, 4])
@pytest.mark.parametrize("line_width", [3.0, 4.0])
def test_wide_fan_lays_out_cleanly(
    tmp_path: Path, line_width: float, track_gap: int, ratio: float | None
) -> None:
    graph = _laid_out(tmp_path, line_width, track_gap, ratio)
    assert _launches_inside_source(graph) == []


def test_one_line_station_is_anchored_on_its_drawn_lane(tmp_path: Path) -> None:
    graph = _laid_out(tmp_path, 4.0, 3, None)
    offsets = compute_station_offsets(graph)
    station = graph.stations["hicreads"]
    lane = offsets[("hicreads", "hic")]
    assert lane > 0
    start, end = drawn_lane_span(
        graph, station, offsets, tb_positive_fan_sections(graph)
    )
    assert start == end == (station.x, station.y + lane)
    assert (
        check_no_hanging_routes(
            graph, route_edges(graph, station_offsets=offsets), offsets
        )
        == []
    )
