"""Invariant tests for a bundle crossing into a per-section rail-mode section.

A ``%%metro line_spread: rails | <section>`` section lays each of its lines on
its own horizontal rail, and a station several lines pass through renders as an
interchange pill spanning those rails.  A multi-line bundle arriving from an
adjacent bundled section must therefore separate on the way in so each line
meets the pill on *its own* rail, the way the lines leaving that pill each run
along their own rail to their downstream station.  Landing the whole bundle at
the pill's centre Y instead draws the incoming lines diving into the middle of
the pill body.

The oracle reconstructs the routed geometry through
:func:`layout_metrics.measured_geometry` and checks it independently of the
rail-boundary handler.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
from layout_metrics import drawn_polylines, measured_geometry

from nf_metro.layout import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, Station
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"

# Fixtures whose rail section is entered by an inter-section bundle.  A rail
# section with no edge crossing into it cannot exercise the boundary fan.
BOUNDARY_FIXTURES = [
    FIXTURES / "rail_marked_single_line.mmd",
    TOPOLOGIES / "rail_boundary_bundle_fan.mmd",
]

_Y_TOLERANCE = 1.0


@lru_cache(maxsize=None)
def _rendered(path: Path) -> MetroGraph:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)
    render_svg(graph, THEMES[graph.style if graph.style in THEMES else "default"])
    return graph


def _is_multi_rail_pill(graph: MetroGraph, station_id: str, line_id: str) -> bool:
    """Whether *station_id* draws a per-rail marker for *line_id*.

    An off-track feeder and a blank terminus converge their lines to a point
    rather than to one rail each, so neither offers a rail to land on.
    """
    station = graph.stations[station_id]
    if station.off_track or station.is_blank_terminus:
        return False
    if not graph.station_is_rail(station_id):
        return False
    served = graph.station_lines_ordered(station_id)
    return (
        len(served) > 1
        and line_id in served
        and len(station.rail_used_ys) == len(served)
    )


def _rail_y(graph: MetroGraph, station_id: str, line_id: str) -> float:
    """Y at which *line_id* meets the pill of rail station *station_id*.

    Reconstructed from the published span rather than by calling the router's
    own resolver, so the expectation is independent of the code under test.
    """
    station = graph.stations[station_id]
    served = graph.station_lines_ordered(station_id)
    return station.rail_used_ys[served.index(line_id)]


def _boundary_legs(
    graph: MetroGraph,
) -> list[tuple[Station, Station, str, list[tuple[float, float]]]]:
    """``(port, pill, line_id, polyline)`` for each leg across a rail boundary.

    A "boundary leg" is a drawn edge between a section port and a rail-laid
    interchange pill: the last leg a line travels before it meets the pill, or
    the first leg after it leaves.  The polyline is oriented port end first
    whichever way the edge runs, so a caller reads the two ends by position.
    """
    offsets, routes = measured_geometry(graph)
    out = []
    for route, points in drawn_polylines(routes, offsets):
        source = graph.stations[route.edge.source]
        target = graph.stations[route.edge.target]
        for port_end, station_end, oriented in (
            (source, target, points),
            (target, source, points[::-1]),
        ):
            if not port_end.is_port or station_end.is_port:
                continue
            if not _is_multi_rail_pill(graph, station_end.id, route.line_id):
                continue
            out.append((port_end, station_end, route.line_id, oriented))
    return out


def _boundary_approaches(graph: MetroGraph) -> list[tuple[str, str, float, float]]:
    """``(station_id, line_id, approach_y, rail_y)`` for each boundary leg."""
    return [
        (pill.id, line_id, points[-1][1], _rail_y(graph, pill.id, line_id))
        for _port, pill, line_id, points in _boundary_legs(graph)
    ]


@pytest.mark.parametrize("path", BOUNDARY_FIXTURES, ids=lambda p: p.stem)
def test_bundle_entering_rail_section_lands_on_each_lines_rail(path: Path) -> None:
    graph = _rendered(path)

    approaches = _boundary_approaches(graph)
    assert approaches, f"{path.name} has no port-to-rail-station leg to check"

    offenders = [
        (sid, lid, got, want)
        for sid, lid, got, want in approaches
        if abs(got - want) > _Y_TOLERANCE
    ]
    assert not offenders, (
        "line(s) meet a rail station away from their own rail: "
        + ", ".join(
            f"{sid}/{lid} at y={got:.1f} (rail y={want:.1f})"
            for sid, lid, got, want in offenders
        )
    )


@pytest.mark.parametrize("path", BOUNDARY_FIXTURES, ids=lambda p: p.stem)
def test_bundle_entering_rail_section_spans_its_rails(path: Path) -> None:
    """The entering bundle is spread across rails, not stacked in one lane.

    Complements the per-line rail check, which one member of a lane-packed
    bundle can satisfy by coincidence when its lane happens to fall on its rail.
    """
    graph = _rendered(path)

    by_station: dict[str, list[float]] = {}
    for sid, _lid, got, _want in _boundary_approaches(graph):
        by_station.setdefault(sid, []).append(got)
    assert by_station, f"{path.name} has no port-to-rail-pill leg to check"

    for sid, ys in by_station.items():
        station = graph.stations[sid]
        rail_span = max(station.rail_used_ys) - min(station.rail_used_ys)
        got_span = max(ys) - min(ys)
        assert got_span >= rail_span - _Y_TOLERANCE, (
            f"{sid}: entering bundle spans {got_span:.1f}px but its rails "
            f"span {rail_span:.1f}px - the lines are bunched, not fanned"
        )


@pytest.mark.parametrize("path", BOUNDARY_FIXTURES, ids=lambda p: p.stem)
def test_boundary_fan_straddles_its_port(path: Path) -> None:
    """The bundle's lanes sit either side of the port they arrive at.

    The port is the middle of the fan that opens out to the section's rails, so
    lanes hung below it leave that fan lopsided and give the lane nearest a rail
    a transition too short to carry its corner radii.
    """
    graph = _rendered(path)

    legs = _boundary_legs(graph)
    assert legs, f"{path.name} has no port-to-rail-pill leg to check"

    ports = {port.id: port for port, _pill, _line_id, _pts in legs}
    for port_id, port in ports.items():
        lane_ys = [pts[0][1] for p, _pill, _line_id, pts in legs if p.id == port_id]
        middle = (min(lane_ys) + max(lane_ys)) / 2.0
        assert abs(middle - port.y) <= _Y_TOLERANCE, (
            f"{port_id}: bundle lanes {sorted(round(y, 1) for y in lane_ys)} "
            f"centre on y={middle:.1f}, off the port's own y={port.y:.1f}"
        )


def test_boundary_leg_on_the_ports_own_rail_runs_straight() -> None:
    """A line whose rail is the port's own Y crosses the boundary dead straight.

    An interchange draws the line passing through it as a straight run and bends
    only the lines diverging from it.  Reaching that rail from an off-centre lane
    instead spends the leg flat and jogs onto the rail at the last moment, which
    reads as a kink beside the pill.
    """
    straight_legs = 0
    for path in BOUNDARY_FIXTURES:
        graph = _rendered(path)
        for port, pill, line_id, points in _boundary_legs(graph):
            if abs(_rail_y(graph, pill.id, line_id) - port.y) > _Y_TOLERANCE:
                continue
            drawn_ys = sorted({round(y, 3) for _x, y in points})
            assert len(drawn_ys) == 1, (
                f"{path.name}: {line_id} meets {pill.id} on the rail at its "
                f"port's own y={port.y:.1f}, yet its leg bends through {drawn_ys}"
            )
            straight_legs += 1
    assert straight_legs, (
        "no fixture places a line's rail on the Y of the port it enters by, so "
        "the through-line case is unexercised"
    )
