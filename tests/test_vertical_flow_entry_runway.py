"""A line entering a vertical section through its flow-side port reaches its station.

The flow-side port of a TB section is its TOP edge and of a BT section its
BOTTOM edge, so a line coming in there travels along the flow axis like every
lane inside the box.  When its first station stands in another column, the line
has to change lanes on the way in.  The drawn route must start where the feeder
lands on the port, finish on the station's own lane travelling along the flow
axis (meeting the line's departure from that station), and stay clear of the
markers of stations it passes without stopping at.
"""

from __future__ import annotations

import pytest
from conftest import parse_and_layout
from orientation_signature import divergences
from orientation_transform import Orientation, transform_source, transformable_reason

from nf_metro.layout.geometry import (
    AxisFrame,
    axis_split,
    flow_port_sides,
    lanes_run_along_x,
)
from nf_metro.layout.labels import LabelPlacement, segment_strikes_label
from nf_metro.layout.pass_metrics import station_radius_approx
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

Point = tuple[float, float]

_ZONE = """
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231
%%metro line: m4 | L4 | #911eb4
{grid}
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x0 -->|m0,m1,m2,m3| q0x1
    end
    subgraph z1 [Zone 1]
        %%metro direction: {direction}
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x3[Q1X3]
        q1x0 -->|m1| q1x1
        q1x1 -->|m2| q1x3
    end
    subgraph z2 [Zone 2]
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x0 -->|m0,m1| q2x1
        q2x0 -->|m3| q2x2
        q2x1 -->|m0,m3,m4| q2x2
    end
    subgraph z3 [Zone 3]
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x2[Q3X2]
        q3x3[Q3X3]
        q3x0 -->|m0,m1,m4| q3x1
        q3x1 -->|m0,m1| q3x2
        q3x2 -->|m1| q3x3
    end
    q0x0 -->|m3| q2x1
    q0x1 -->|m1| q1x1
    q0x1 -->|m3| q1x3
    q0x1 -->|m0,m2| q2x0
    q1x1 -->|m1| q2x0
    q1x3 -->|m2,m3| q2x0
    q2x0 -->|m2| q3x0
    q2x1 -->|m1| q3x0
    q2x2 -->|m0,m4| q3x0
"""

_CHAIN = """
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231
{grid}
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: {direction}
        b0[B0]
        b1[B1]
        b3[B3]
        b0 -->|m1| b1
        b1 -->|m2| b3
    end
    subgraph c [C]
        c0[C0]
    end
    a0 -->|m1| b1
    a0 -->|m3| b3
    b1 -->|m1| c0
    b3 -->|m2,m3| c0
"""

_STACKED = "%%metro grid: {0} | 0,0\n%%metro grid: {1} | 0,1\n%%metro grid: {2} | 0,2"

MAPS = {
    "zone_bt": _ZONE.format(grid="", direction="BT"),
    "chain_bt": _CHAIN.format(grid="", direction="BT"),
    "chain_tb_stacked": _CHAIN.format(
        grid=_STACKED.format("a", "b", "c"), direction="TB"
    ),
}


def _drawn(graph: MetroGraph) -> dict[tuple[str, str, str], list[Point]]:
    plan = build_render_plan(graph, resolve_theme(None, graph))
    return {
        (r.edge.source, r.edge.target, r.line_id): apply_route_offsets(
            r, plan.station_offsets
        )
        for r in plan.routes
    }


def _drawn_and_labels(
    graph: MetroGraph,
) -> tuple[dict[tuple[str, str, str], list[Point]], dict[str, LabelPlacement]]:
    plan = build_render_plan(graph, resolve_theme(None, graph))
    drawn = {
        (r.edge.source, r.edge.target, r.line_id): [tuple(p) for p in points]
        for r, points in zip(plan.routes, plan.route_polylines)
    }
    labels = {
        rec.station_id: LabelPlacement(**dict(rec.values.entries))
        for rec in plan.labels
    }
    return drawn, labels


def _flow_entries(
    graph: MetroGraph, drawn: dict[tuple[str, str, str], list[Point]]
) -> list[tuple[str, str, str]]:
    keys = []
    for key in drawn:
        port = graph.ports.get(key[0])
        target = graph.stations[key[1]]
        if port is None or not port.is_entry or target.is_port:
            continue
        section = graph.sections[port.section_id]
        if not lanes_run_along_x(section.direction):
            continue
        if port.side != flow_port_sides(section.direction)[0]:
            continue
        keys.append(key)
    return keys


def _close(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < 0.5 and abs(a[1] - b[1]) < 0.5


@pytest.mark.parametrize("name", sorted(MAPS))
def test_flow_entry_route_meets_its_feeder_and_its_station(name: str) -> None:
    graph = parse_and_layout(MAPS[name])
    drawn = _drawn(graph)
    entries = _flow_entries(graph, drawn)
    assert entries, "fixture lost its flow-side entry"
    for port_id, target_id, line_id in entries:
        points = drawn[port_id, target_id, line_id]
        feeders = [
            pts
            for (src, tgt, lid), pts in drawn.items()
            if tgt == port_id and lid == line_id
        ]
        assert feeders, (port_id, line_id)
        assert any(_close(pts[-1], points[0]) for pts in feeders), (
            f"{line_id} {port_id}->{target_id} starts at {points[0]}, "
            f"feeder lands at {[pts[-1] for pts in feeders]}"
        )
        departures = [
            pts
            for (src, _tgt, lid), pts in drawn.items()
            if src == target_id and lid == line_id
        ]
        assert departures, (target_id, line_id)
        assert any(_close(pts[0], points[-1]) for pts in departures), (
            f"{line_id} arrives at {target_id} at {points[-1]}, "
            f"leaves it at {[pts[0] for pts in departures]}"
        )
        flow_axis, _lane_axis = AxisFrame.axes_for_direction(
            graph.sections[graph.stations[target_id].section_id].direction
        )
        (ax, ay), (bx, by) = points[-2], points[-1]
        lateral = abs(ax - bx) if flow_axis == "y" else abs(ay - by)
        assert lateral < 0.5, (
            f"{line_id} arrives at {target_id} across the flow axis: {points[-2:]}"
        )


def _lane_span(
    graph: MetroGraph,
    drawn: dict[tuple[str, str, str], list[Point]],
    station_id: str,
) -> tuple[float, float]:
    """Lane-axis extent of the lines drawn into and out of *station_id*."""
    station = graph.stations[station_id]
    _flow, lane_axis = AxisFrame.axes_for_direction(
        graph.sections[station.section_id].direction
    )
    index = 0 if lane_axis == "x" else 1
    coords = [
        pts[0][index] if src == station_id else pts[-1][index]
        for (src, tgt, _lid), pts in drawn.items()
        if station_id in (src, tgt)
    ]
    return min(coords), max(coords)


def _segment_hits_box(a: Point, b: Point, box: tuple[float, float, float, float]):
    x0, y0, x1, y1 = box
    steps = max(2, int(max(abs(a[0] - b[0]), abs(a[1] - b[1]))))
    for k in range(steps + 1):
        t = k / steps
        x = a[0] + (b[0] - a[0]) * t
        y = a[1] + (b[1] - a[1]) * t
        if x0 < x < x1 and y0 < y < y1:
            return True
    return False


@pytest.mark.parametrize("name", sorted(MAPS))
def test_flow_entry_route_clears_stations_it_passes(name: str) -> None:
    graph = parse_and_layout(MAPS[name])
    drawn = _drawn(graph)
    radius = station_radius_approx()
    for port_id, target_id, line_id in _flow_entries(graph, drawn):
        section = graph.sections[graph.ports[port_id].section_id]
        _flow, lane_axis = AxisFrame.axes_for_direction(section.direction)
        points = drawn[port_id, target_id, line_id]
        for station_id in section.station_ids:
            station = graph.stations[station_id]
            if station.is_port or station.is_hidden or station_id == target_id:
                continue
            if line_id in graph.station_lines(station_id):
                continue
            lo, hi = _lane_span(graph, drawn, station_id)
            if lane_axis == "x":
                box = (lo - radius, station.y - radius, hi + radius, station.y + radius)
            else:
                box = (station.x - radius, lo - radius, station.x + radius, hi + radius)
            assert not any(
                _segment_hits_box(a, b, box) for a, b in zip(points, points[1:])
            ), f"{line_id} {port_id}->{target_id} runs through {station_id}'s marker"


# A line entering a BT section at the bottom both to the first station of a
# chain and, bypassing it, to a later one.  The bypass leaves the port beside
# the chain's own run into its first station, so it must pass the stations it
# skips without crossing their markers (which would read as stopping there) or
# their labels, which sit on the low-x side of each pill.
_BYPASS_MAPS = {
    "fork_after_bypassed_station_bt": """
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231

graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x3[Q0X3]
    end
    subgraph z1 [Zone 1]
        %%metro direction: BT
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x1 -->|m0| q1x2
        q1x1 -->|m2| q1x3
        q1x2 -->|m0| q1x3
    end
    subgraph z2 [Zone 2]
        %%metro direction: BT
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x3[Q2X3]
        q2x1 -->|m3| q2x2
    end
    subgraph z3 [Zone 3]
        %%metro direction: BT
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x0 -->|m1| q3x1
    end
    q0x0 -->|m0| q1x0
    q0x3 -->|m0| q1x2
    q1x3 -->|m2| q2x0
    q2x0 -->|m1| q3x0
    q2x3 -->|m3| q3x0
""",
    "bypass_into_shared_column_bt": """
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231
%%metro line: m4 | L4 | #911eb4

graph LR
    subgraph z0 [Zone 0]
        q0x2[Q0X2]
    end
    subgraph z1 [Zone 1]
        %%metro direction: TB
        q1x0[Q1X0]
        q1x1[Q1X1]
    end
    subgraph z2 [Zone 2]
        %%metro direction: BT
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x3[Q2X3]
        q2x0 -->|m0| q2x1
        q2x2 -->|m4| q2x3
    end
    q0x2 -->|m4| q1x0
    q0x2 -->|m0| q2x1
    q1x1 -->|m0| q2x0
""",
}


def _line_also_reaches_target(
    graph: MetroGraph, port_id: str, target_id: str, line_id: str
) -> bool:
    """Whether *line_id* reaches *target_id* from a station as well as the port.

    Such a bypass runs with its own line's chain through the stations it
    skips (``test_flow_entry_bypass_crossings``), so passing them is not a
    marker strike.
    """
    return any(
        edge.line_id == line_id and edge.source != port_id
        for edge in graph.edges_to(target_id)
    )


@pytest.mark.parametrize("name", sorted(_BYPASS_MAPS))
def test_flow_entry_bypass_clears_skipped_markers_and_labels(name: str) -> None:
    graph = parse_and_layout(_BYPASS_MAPS[name])
    drawn, labels = _drawn_and_labels(graph)
    radius = station_radius_approx()
    entries = [
        key
        for key in _flow_entries(graph, drawn)
        if len(drawn[key]) > 2 and not _line_also_reaches_target(graph, *key)
    ]
    assert entries, "fixture lost its lane-changing flow-side entry"
    for port_id, target_id, line_id in entries:
        points = drawn[port_id, target_id, line_id]
        port = graph.stations[port_id]
        target = graph.stations[target_id]
        section = graph.sections[graph.ports[port_id].section_id]
        lo, hi = sorted((port.y, target.y))
        for station_id in section.station_ids:
            station = graph.stations[station_id]
            if station.is_port or station.is_hidden or station_id == target_id:
                continue
            if not lo < station.y < hi:
                continue
            xs = [
                pts[0][0] if src == station_id else pts[-1][0]
                for (src, tgt, _lid), pts in drawn.items()
                if station_id in (src, tgt)
            ]
            box = (
                min(xs) - radius,
                station.y - radius,
                max(xs) + radius,
                station.y + radius,
            )
            segments = list(zip(points, points[1:]))
            assert not any(_segment_hits_box(a, b, box) for a, b in segments), (
                f"{line_id} {port_id}->{target_id} runs through {station_id}'s "
                f"marker: {points}"
            )
            label = labels.get(station_id)
            assert label is None or not any(
                segment_strikes_label(a[0], a[1], b[0], b[1], label)
                for a, b in segments
            ), (
                f"{line_id} {port_id}->{target_id} strikes {station_id}'s label: "
                f"{points}"
            )


# The chain above with every direction, cell and port side stated, so that
# reflecting it top to bottom (BT -> TB) is a meaning-preserving rewrite.
_CHAIN_EXPLICIT_BT = """
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231
%%metro grid: a | 0,0
%%metro grid: b | 1,0
%%metro grid: c | 2,0
graph LR
    subgraph a [A]
        %%metro direction: LR
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: BT
        %%metro entry: bottom | m1, m3
        %%metro exit: top | m1, m2, m3
        b0[B0]
        b1[B1]
        b3[B3]
        b0 -->|m1| b1
        b1 -->|m2| b3
    end
    subgraph c [C]
        %%metro direction: LR
        c0[C0]
    end
    a0 -->|m1| b1
    a0 -->|m3| b3
    b1 -->|m1| c0
    b3 -->|m2,m3| c0
"""

_FLIP_TO_TB = Orientation(2, True)


def _flow_entry_profile(graph: MetroGraph) -> dict[tuple[str, str], tuple]:
    """Each flow-side entry's lane decision, in the entry's own frame.

    Every vertex is tagged with the lane it sits on (the feeder's landing lane,
    the line's departure lane from the station, or neither) and, but for the
    arrival, its distance along the flow axis from where the route leaves the
    port.
    """
    drawn = _drawn(graph)
    profile = {}
    for port_id, target_id, line_id in _flow_entries(graph, drawn):
        points = drawn[port_id, target_id, line_id]
        flow_axis, _lane_axis = AxisFrame.axes_for_direction(
            graph.sections[graph.ports[port_id].section_id].direction
        )
        (departure,) = [
            pts[0]
            for (src, _tgt, lid), pts in drawn.items()
            if src == target_id and lid == line_id
        ]
        entry_flow, entry_lane = axis_split(flow_axis, points[0])
        departure_lane = axis_split(flow_axis, departure)[1]

        def lane_of(p: Point) -> str:
            lane = axis_split(flow_axis, p)[1]
            if abs(lane - entry_lane) < 0.5:
                return "port"
            if abs(lane - departure_lane) < 0.5:
                return "station"
            return "between"

        flow_offsets = tuple(
            round(abs(axis_split(flow_axis, p)[0] - entry_flow), 1) for p in points[:-1]
        )
        profile[target_id, line_id] = (tuple(map(lane_of, points)), flow_offsets)
    return profile


def test_flow_entry_lane_decision_survives_flipping_bt_to_tb() -> None:
    reference = parse_and_layout(_CHAIN_EXPLICIT_BT)
    image = parse_and_layout(transform_source(_CHAIN_EXPLICIT_BT, _FLIP_TO_TB))
    assert transformable_reason(reference) is None
    assert {s.direction for s in image.sections.values()} == {"LR", "TB"}
    assert divergences(reference, image, _FLIP_TO_TB) == []

    expected = _flow_entry_profile(reference)
    assert expected[("b3", "m3")][0][-2:] == ("station", "station"), expected
    assert _flow_entry_profile(image) == expected
