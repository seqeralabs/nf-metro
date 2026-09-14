from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    CURVE_RADIUS,
    graph_offset_step,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import (
    iter_horizontal_trunks,
    port_peeloff_tail,
)
from nf_metro.layout.routing.context import partial_flat_continuation_lines
from nf_metro.layout.routing.invariants import check_peeloff_concentric
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURE = EXAMPLES / "topologies" / "packed_cell_right_exit_left_entry_wrap.mmd"


def test_partial_flat_continuation_chooses_one_port_scoped_bundle() -> None:
    graph = MetroGraph(
        sections={
            section_id: Section(
                id=section_id,
                name=section_id,
                grid_col=0,
                grid_row=0,
            )
            for section_id in ("feeder", "consumer_a", "consumer_b")
        },
        stations={
            "exit": Station("exit", "", section_id="feeder", is_port=True, y=10),
            "entry_a": Station(
                "entry_a", "", section_id="consumer_a", is_port=True, x=20, y=10
            ),
            "entry_b": Station(
                "entry_b", "", section_id="consumer_b", is_port=True, x=30, y=10
            ),
            "junction_b": Station("junction_b", "", is_hidden=True),
            "junction_a": Station("junction_a", "", is_hidden=True),
        },
        ports={
            "exit": Port("exit", "feeder", PortSide.RIGHT, is_entry=False),
            "entry_a": Port("entry_a", "consumer_a", PortSide.LEFT),
            "entry_b": Port("entry_b", "consumer_b", PortSide.LEFT),
        },
        junctions=["junction_b", "junction_a"],
        edges=[
            Edge("exit", "junction_b", "c"),
            Edge("exit", "junction_a", "a"),
            Edge("junction_b", "entry_b", "c"),
            Edge("junction_b", "entry_b", "d"),
            Edge("junction_a", "entry_a", "a"),
            Edge("junction_a", "entry_a", "b"),
            Edge("junction_a", "entry_a", "foreign"),
        ],
        cell_packs={(0, 0): ["feeder", "consumer_a", "consumer_b"]},
    )

    lines = partial_flat_continuation_lines(graph, "exit", {"a", "b", "c", "d"})

    assert lines == {"a", "b"}


def _routes_into(port_id: str):
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    routes = [
        route
        for route in route_edges(graph, station_offsets=offsets)
        if route.edge.target == port_id
    ]
    return graph, routes


def _tail(route):
    tail = port_peeloff_tail(route)
    assert tail is not None
    trunk = list(iter_horizontal_trunks(route))[-1][1]
    return {
        "trunk": trunk,
        "trunk_y": trunk.y,
        "peel_x": tail.peel_x,
        "port_y": tail.port_y,
        "trunk_sign": tail.trunk_sign,
        "vertical_sign": tail.vertical_sign,
        "port_lead_sign": tail.port_lead_sign,
    }


@pytest.mark.parametrize(
    ("port_id", "line_ids"),
    [
        ("qc__entry_left_6", {"reference", "short"}),
        ("polish__entry_left_7", {"assembled", "short"}),
    ],
)
def test_same_destination_port_bundles_at_earliest_shared_corridor(
    port_id: str, line_ids: set[str]
) -> None:
    """Distinct lines sharing a destination-facing trunk overlap form one bundle."""
    graph, routes = _routes_into(port_id)
    assert {route.line_id for route in routes} == line_ids
    tails = {route.line_id: _tail(route) for route in routes}

    overlap_lo = max(tail["trunk"].x_lo for tail in tails.values())
    overlap_hi = min(tail["trunk"].x_hi for tail in tails.values())
    assert overlap_hi - overlap_lo >= 2 * CURVE_RADIUS

    trunk_ys = sorted(tail["trunk_y"] for tail in tails.values())
    step = graph_offset_step(graph)
    assert trunk_ys[-1] - trunk_ys[0] == pytest.approx(
        (len(trunk_ys) - 1) * step,
        abs=COORD_TOLERANCE,
    )
    assert all(
        b - a == pytest.approx(step, abs=COORD_TOLERANCE)
        for a, b in zip(trunk_ys, trunk_ys[1:])
    )


@pytest.mark.parametrize(
    "port_id",
    ["qc__entry_left_6", "polish__entry_left_7"],
)
def test_same_destination_port_tail_keeps_three_axis_order(port_id: str) -> None:
    """Trunk, approach, and port orders follow the tail's turn parity."""
    _graph, routes = _routes_into(port_id)
    tails = {route.line_id: _tail(route) for route in routes}

    shapes = {
        (
            tail["trunk_sign"],
            tail["vertical_sign"],
            tail["port_lead_sign"],
        )
        for tail in tails.values()
    }
    assert len(shapes) == 1
    trunk_sign, vertical_sign, port_lead_sign = shapes.pop()

    trunk_order = sorted(tails, key=lambda line_id: tails[line_id]["trunk_y"])
    peel_order = sorted(tails, key=lambda line_id: tails[line_id]["peel_x"])
    port_order = sorted(tails, key=lambda line_id: tails[line_id]["port_y"])

    expected_peel = (
        trunk_order if -vertical_sign == trunk_sign else list(reversed(trunk_order))
    )
    expected_port = (
        trunk_order if port_lead_sign == trunk_sign else list(reversed(trunk_order))
    )
    assert peel_order == expected_peel
    assert port_order == expected_port


def test_destination_tail_guard_reports_a_depth_inverted_bundle() -> None:
    """``check_peeloff_concentric`` names every line stacked against its order.

    Two lines converge on ``qc__entry_left_6`` from below-row trunks at
    different depths, and the settled routes put each on the depth its peel
    order earns.  Exchanging the two trunk Ys constructs the inversion the
    settling passes exist to prevent; the guard must then report both lines.

    This is detector arithmetic over a hand-built input.  No corpus fixture
    reaches the inversion through the routing pipeline, so the guard's ability
    to see one is not otherwise exercised.
    """
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    assert not check_peeloff_concentric(graph, routes)

    tails = [route for route in routes if route.edge.target == "qc__entry_left_6"]
    assert len(tails) == 2
    trunk_ys = [list(iter_horizontal_trunks(route))[-1][1].y for route in tails]
    for route, own_y, swapped_y in zip(tails, trunk_ys, reversed(trunk_ys)):
        route.points[:] = [(x, swapped_y if y == own_y else y) for x, y in route.points]

    messages = [
        violation.message() for violation in check_peeloff_concentric(graph, routes)
    ]
    assert len(messages) == len(tails)
    assert all("qc__entry_left_6" in message for message in messages)
