"""``lane_x`` accessor properties and the seam approach==departure oracle.

``lane_x`` is the single source of truth for where a line draws inside a
section: its lane (secondary) axis coordinate, derived from the section's
:class:`AxisFrame` (lane axis + sign) and the line's arrival order at its entry
port.  A vertical section is the 90-degree rotation image of a horizontal one,
so the same offsets fan to the opposite screen side.

:func:`check_seam_approach_equals_departure` is the oracle the rotation series
is verified against: at a continuation seam the inter-section approach must land
each line on the coordinate ``lane_x`` assigns it.  Horizontal sections already
satisfy this; vertical sections whose draw reflects rather than rotates the lane
fan are catalogued here as strict xfails until #1041 migrates the draw onto
``lane_x``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.context import (
    lane_x,
    port_arrival_order,
    port_lane_coord,
)
from nf_metro.layout.routing.invariants import check_seam_approach_equals_departure
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station

TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"

# TB sections draw their lane fan by reflection rather than rotation today, so
# every TB continuation seam lands its lines on the opposite side of the column
# from lane_x.  #1041 migrates the section draw onto lane_x, which flips each of
# these from xfail to pass (a strict xpass then reds CI, prompting removal).
_SEAM_XFAILS = {
    "lr_to_tb_top_two_lines",
    "lr_to_tb_top_drop_two_lines",
    "tb_internal_diagonal",
    "tb_lr_exit_left",
    "tb_lr_exit_right",
    "tb_right_entry_stack",
}


# --------------------------------------------------------------------------- #
# lane_x accessor unit properties
# --------------------------------------------------------------------------- #


def _mini_graph(direction: str) -> tuple[MetroGraph, dict[tuple[str, str], float]]:
    """A one-section graph: two lines a,b through an entry port to an exit port.

    ``a`` carries offset 0 and ``b`` offset 4 at every station, so the two lines
    ride adjacent lanes.  The entry port anchors at (100, 50); the internal
    station and exit port advance along the flow axis (X for LR/RL, Y for
    TB/BT), keeping the lane (secondary) coordinate constant down the section.
    """
    vertical = direction in ("TB", "BT")
    side_in = PortSide.TOP if vertical else PortSide.LEFT
    side_out = PortSide.BOTTOM if vertical else PortSide.RIGHT
    # advance along the flow axis only, so entry/exit share the lane baseline
    mid = (100.0, 120.0) if vertical else (170.0, 50.0)
    out = (100.0, 200.0) if vertical else (240.0, 50.0)
    graph = MetroGraph()
    graph.lines = ["a", "b"]
    graph.ports = {
        "s__entry": Port("s__entry", "s", side_in, is_entry=True, x=100.0, y=50.0),
        "s__exit": Port("s__exit", "s", side_out, is_entry=False, x=out[0], y=out[1]),
    }
    graph.sections = {
        "s": Section(
            id="s",
            name="S",
            direction=direction,
            station_ids=["s__entry", "n1", "s__exit"],
            entry_ports=["s__entry"],
            exit_ports=["s__exit"],
        )
    }
    graph.stations = {
        "s__entry": Station("s__entry", "", "s", is_port=True, x=100.0, y=50.0),
        "n1": Station("n1", "N", "s", x=mid[0], y=mid[1]),
        "s__exit": Station("s__exit", "", "s", is_port=True, x=out[0], y=out[1]),
    }
    graph.edges = [
        Edge("s__entry", "n1", "a"),
        Edge("s__entry", "n1", "b"),
        Edge("n1", "s__exit", "a"),
        Edge("n1", "s__exit", "b"),
    ]
    offsets = {
        (sid, lid): (0.0 if lid == "a" else 4.0)
        for sid in graph.stations
        for lid in ("a", "b")
    }
    return graph, offsets


def test_lane_x_rotates_with_section_direction() -> None:
    """A vertical section is the rotation image of a horizontal one.

    The offset-0 line draws on the anchor; the offset-4 line fans to +y for a
    horizontal section and to -x for a vertical one, so the lane order along the
    screen axis is mirrored while the lane pitch is identical.
    """
    lr, lr_off = _mini_graph("LR")
    tb, tb_off = _mini_graph("TB")
    lr_sec, tb_sec = lr.sections["s"], tb.sections["s"]

    # offset-0 line sits on the anchor in both orientations
    assert lane_x(lr, lr_sec, "a", lr_off) == 50.0  # anchor.y
    assert lane_x(tb, tb_sec, "a", tb_off) == 100.0  # anchor.x

    # offset-4 line fans +y for LR, -x for TB -- opposite screen sides
    assert lane_x(lr, lr_sec, "b", lr_off) == 54.0
    assert lane_x(tb, tb_sec, "b", tb_off) == 96.0

    # same lane pitch, mirrored order
    assert lane_x(lr, lr_sec, "a", lr_off) < lane_x(lr, lr_sec, "b", lr_off)
    assert lane_x(tb, tb_sec, "a", tb_off) > lane_x(tb, tb_sec, "b", tb_off)


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_lane_x_order_matches_arrival_order(direction: str) -> None:
    """Lane order in == lane order down the column == lane order out.

    The order lines cross the entry edge equals the order they ride the section
    (by ``lane_x``) equals the order they cross the exit edge.
    """
    graph, offsets = _mini_graph(direction)
    section = graph.sections["s"]
    entry = graph.stations["s__entry"]
    exit_ = graph.stations["s__exit"]

    arrival = port_arrival_order(graph, entry, offsets)
    by_lane = sorted(("a", "b"), key=lambda lid: lane_x(graph, section, lid, offsets))
    departure = port_arrival_order(graph, exit_, offsets)

    assert arrival == by_lane == departure


def test_port_arrival_order_breaks_ties_on_line_id() -> None:
    """Lines sharing a lane coordinate order by line id, not input listing."""
    graph, _ = _mini_graph("TB")
    entry = graph.stations["s__entry"]
    tied = {("s__entry", lid): 4.0 for lid in ("a", "b")}
    assert port_arrival_order(graph, entry, tied) == ["a", "b"]


@pytest.mark.parametrize("direction", ["LR", "RL", "TB", "BT"])
def test_lane_x_collinear_continuation_keeps_lane(direction: str) -> None:
    """A line running straight through keeps one lane from entry to exit."""
    graph, offsets = _mini_graph(direction)
    section = graph.sections["s"]
    exit_ = graph.stations["s__exit"]
    for lid in ("a", "b"):
        assert lane_x(graph, section, lid, offsets) == pytest.approx(
            port_lane_coord(graph, exit_, lid, offsets)
        )


# --------------------------------------------------------------------------- #
# Seam approach == departure oracle over the corpus
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=None)
def _seam_mismatches(name: str) -> tuple[str, ...]:
    graph = parse_metro_mermaid((TOPOLOGIES / f"{name}.mmd").read_text())
    compute_layout(graph, validate=False)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return tuple(
        m.message()
        for m in check_seam_approach_equals_departure(graph, routes, offsets)
    )


def _seam_params() -> list:
    params = []
    for path in sorted(TOPOLOGIES.glob("*.mmd")):
        name = path.stem
        marks = (
            (
                pytest.mark.xfail(
                    reason="#1041 migrates TB draw onto lane_x", strict=True
                ),
            )
            if name in _SEAM_XFAILS
            else ()
        )
        params.append(pytest.param(name, id=name, marks=marks))
    return params


@pytest.mark.parametrize("name", _seam_params())
def test_seam_approach_equals_departure(name: str) -> None:
    """Every continuation seam lands each line on its section's lane coordinate."""
    mismatches = _seam_mismatches(name)
    assert not mismatches, "\n".join(mismatches)
