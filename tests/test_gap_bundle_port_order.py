"""A bundle's lane order carries the lane order of the entry port its lines
turn into, whichever axis the bundle runs along.

Lines that keep running side by side through non-crossing turns keep their
side of the direction of travel, so a line on the right-hand side of travel
where it enters the port is on the right-hand side all along the bundle.  An
inter-row trunk band into a LEFT/RIGHT entry and a column-gap bundle into a
TOP/BOTTOM entry are quarter-turn images of one another and read the same rule.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.routing.common import Direction, RoutedPath
from nf_metro.layout.routing.normalize import (
    _band_port_constraints,
    _coincident_trunk_slots,
    _distinct_line_order,
    _HTrunk,
    _layout_gap_bundle,
    _port_lane_order_pairs,
    _port_ordered_line_order,
    _slot_entry_reach,
    _VChannel,
)
from nf_metro.layout.routing.reserved_bands import ReservedCorridors
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section
from nf_metro.render.svg import build_observed_render_plan

PORT = "dst__entry_0"
OTHER_PORT = "alt__entry_0"
STEP = 4.0


def _graph(
    *edges: tuple[str, str, str],
    side: PortSide,
    direction: str,
) -> MetroGraph:
    graph = MetroGraph()
    graph.add_section(Section(id="dst", name="Dst", direction=direction))
    graph.add_port(Port(id=PORT, section_id="dst", side=side))
    for source, target, line in edges:
        graph.add_edge(Edge(source, target, line))
    return graph


def _ctx(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    positive_fan: set[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        graph=graph,
        station_offsets=offsets,
        positive_fan=positive_fan or set(),
        offset_step=STEP,
        curve_radius=CURVE_RADIUS,
        reserved_bands=ReservedCorridors(),
    )


def _port_offsets(**by_line: float) -> dict[tuple[str, str], float]:
    return {(PORT, line): offset for line, offset in by_line.items()}


def _channel(line: str, source: str, target: str, x: float, *, down: bool = True):
    """A vertical gap leg at *x* between a lead-in and a lead-out heading right."""
    points = [(x - 40.0, 0.0), (x, 0.0), (x, 100.0), (x + 40.0, 100.0)]
    if not down:
        points = [(x + 40.0, 100.0), (x, 100.0), (x, 0.0), (x - 40.0, 0.0)]
    return _VChannel(
        route=RoutedPath(edge=Edge(source, target, line), line_id=line, points=points),
        idx=1,
        x=x,
        y_lo=0.0,
        y_hi=100.0,
        down=down,
    )


def _trunk(line: str, source: str, target: str, travel: Direction) -> _HTrunk:
    sign_x = 1 if travel is Direction.R else -1
    x_lo, x_hi = 0.0, 300.0
    start, end = (x_lo, x_hi) if sign_x > 0 else (x_hi, x_lo)
    return _HTrunk(
        route=RoutedPath(
            edge=Edge(source, target, line),
            line_id=line,
            points=[(start, 20.0), (start, 100.0), (end, 100.0), (end, 20.0)],
        ),
        idx=1,
        y=100.0,
        x_lo=x_lo,
        x_hi=x_hi,
        dips_down=True,
        sign_x=sign_x,
    )


def _pairs(
    direction: str, side: PortSide, travel: Direction, positive_fan: bool = False
) -> list[tuple[int, int]]:
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"), side=side, direction=direction)
    ctx = _ctx(graph, _port_offsets(a=4.0, b=8.0), {"dst"} if positive_fan else None)
    if travel in (Direction.R, Direction.L):
        slots = _coincident_trunk_slots(
            [_trunk("a", "sa", PORT, travel), _trunk("b", "sb", PORT, travel)]
        )
        return _band_port_constraints(slots, ctx)
    down = travel is Direction.D
    reach = [
        _slot_entry_reach(graph, [_channel("a", "sa", PORT, 10.0, down=down)]),
        _slot_entry_reach(graph, [_channel("b", "sb", PORT, 14.0, down=down)]),
    ]
    return _port_lane_order_pairs(["a", "b"], reach, travel, ctx)


A_FIRST, B_FIRST = [(0, 1)], [(1, 0)]

# Offsets a=4, b=8.  LR/RL and BT, and a TB section drawing on its +x side,
# place a before b along the port edge; a default TB section places b first.
# The entered side keeps travel straight through (LEFT on R, TOP on D, ...)
# or turns it back on itself, which swaps the line on each side.
ROTATION_TABLE = [
    ("LR", False, PortSide.LEFT, Direction.R, A_FIRST),
    ("LR", False, PortSide.LEFT, Direction.L, B_FIRST),
    ("LR", False, PortSide.RIGHT, Direction.R, B_FIRST),
    ("LR", False, PortSide.RIGHT, Direction.L, A_FIRST),
    ("RL", False, PortSide.LEFT, Direction.R, A_FIRST),
    ("RL", False, PortSide.LEFT, Direction.L, B_FIRST),
    ("RL", False, PortSide.RIGHT, Direction.R, B_FIRST),
    ("RL", False, PortSide.RIGHT, Direction.L, A_FIRST),
    ("TB", False, PortSide.TOP, Direction.D, B_FIRST),
    ("TB", False, PortSide.TOP, Direction.U, A_FIRST),
    ("TB", False, PortSide.BOTTOM, Direction.D, A_FIRST),
    ("TB", False, PortSide.BOTTOM, Direction.U, B_FIRST),
    ("TB", True, PortSide.TOP, Direction.D, A_FIRST),
    ("TB", True, PortSide.TOP, Direction.U, B_FIRST),
    ("TB", True, PortSide.BOTTOM, Direction.D, B_FIRST),
    ("TB", True, PortSide.BOTTOM, Direction.U, A_FIRST),
    ("BT", False, PortSide.TOP, Direction.D, A_FIRST),
    ("BT", False, PortSide.TOP, Direction.U, B_FIRST),
    ("BT", False, PortSide.BOTTOM, Direction.D, B_FIRST),
    ("BT", False, PortSide.BOTTOM, Direction.U, A_FIRST),
]


@pytest.mark.parametrize(
    ("direction", "positive_fan", "side", "travel", "expected"),
    ROTATION_TABLE,
    ids=[
        f"{d}{'+fan' if fan else ''}-{side.value}-{travel.value}"
        for d, fan, side, travel, _ in ROTATION_TABLE
    ],
)
def test_each_line_keeps_its_side_of_travel_from_bundle_to_port(
    direction, positive_fan, side, travel, expected
):
    """Pairs list the line that comes first along the bundle's screen axis
    (top-to-bottom for a horizontal band, left-to-right for a vertical one)."""
    assert _pairs(direction, side, travel, positive_fan) == expected


QUARTER_TURN = {
    PortSide.LEFT: PortSide.TOP,
    PortSide.RIGHT: PortSide.BOTTOM,
    Direction.R: Direction.D,
    Direction.L: Direction.U,
}


@pytest.mark.parametrize("side", [PortSide.LEFT, PortSide.RIGHT])
@pytest.mark.parametrize("travel", [Direction.R, Direction.L])
def test_a_vertical_bundle_reads_the_quarter_turn_of_the_horizontal_rule(side, travel):
    """Turning an LR band a quarter clockwise gives a default TB bundle, and the
    top-to-bottom order maps onto right-to-left, so every pair reverses."""
    horizontal = _pairs("LR", side, travel)
    vertical = _pairs("TB", QUARTER_TURN[side], QUARTER_TURN[travel])
    assert horizontal
    assert vertical == [(j, i) for i, j in horizontal]


@pytest.mark.parametrize(
    ("side", "direction"),
    [(PortSide.LEFT, "LR"), (PortSide.TOP, "LR"), (PortSide.LEFT, "TB")],
    ids=["lr-side-entry", "lr-top-entry", "tb-side-entry"],
)
def test_a_port_whose_lanes_cross_the_bundle_decides_no_order(side, direction):
    """A vertical bundle takes its order only from a port whose lanes run along
    X, which is a TOP/BOTTOM flow entry of a TB/BT section."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"), side=side, direction=direction)
    reach = [{PORT: {"sa"}}, {PORT: {"sb"}}]
    ctx = _ctx(graph, _port_offsets(a=4.0, b=8.0))

    assert _port_lane_order_pairs(["a", "b"], reach, Direction.D, ctx) == []


def _bundle_xs(chans: list[_VChannel], ctx: SimpleNamespace) -> list[str]:
    _layout_gap_bundle([(chans[0].down, chans)], 200.0, 260.0, ctx)
    return [ch.route.line_id for ch in sorted(chans, key=lambda ch: ch.x)]


@pytest.mark.parametrize(
    ("side", "expected"),
    [(PortSide.TOP, ["b", "a"]), (PortSide.BOTTOM, ["a", "b"])],
)
def test_a_gap_bundle_lays_out_in_its_entry_port_order(side, expected):
    """``a`` and ``b`` tie on crossings, so the free order keeps ``a`` left.
    Descending into a default TB section's TOP entry keeps ``b`` on the left
    of travel, where the port has it; a BOTTOM entry turns the lines back up
    and swaps that side."""
    graph = _graph(("sa", PORT, "a"), ("sb", PORT, "b"), side=side, direction="TB")
    chans = [_channel("a", "sa", PORT, 210.0), _channel("b", "sb", PORT, 214.0)]
    assert _distinct_line_order(chans) == ["a", "b"]

    ctx = _ctx(graph, _port_offsets(a=4.0, b=8.0))
    assert _bundle_xs(chans, ctx) == expected


def test_the_bundle_reads_the_lane_side_its_section_draws_on():
    """A TB section drawing its lanes on the +x side puts ``a`` left at the port,
    so the same raw offsets order the bundle the other way."""
    graph = _graph(
        ("sa", PORT, "a"), ("sb", PORT, "b"), side=PortSide.TOP, direction="TB"
    )
    chans = [_channel("a", "sa", PORT, 210.0), _channel("b", "sb", PORT, 214.0)]
    offsets = _port_offsets(a=8.0, b=4.0)

    assert _port_ordered_line_order(chans, _ctx(graph, offsets)) == ["a", "b"]
    assert _port_ordered_line_order(chans, _ctx(graph, offsets, {"dst"})) == [
        "b",
        "a",
    ]


def test_a_port_one_source_feeds_two_lines_decides_no_order():
    """``sa`` sends ``a`` and ``c`` into the port as one bundle whose order
    ``sa`` pins, so the port's lane order is not the gap bundle's to follow."""
    graph = _graph(
        ("sa", PORT, "a"),
        ("sa", PORT, "c"),
        ("sb", PORT, "b"),
        side=PortSide.TOP,
        direction="TB",
    )
    chans = [_channel("a", "sa", PORT, 210.0), _channel("b", "sb", PORT, 214.0)]
    ctx = _ctx(graph, _port_offsets(a=4.0, b=8.0, c=12.0))

    assert _port_ordered_line_order(chans, ctx) == ["a", "b"]


def test_a_port_order_reversing_two_lines_from_one_source_keeps_the_free_order():
    """``a`` and ``c`` leave ``s1`` together.  The port puts ``b`` left of
    ``a``; the cheapest order doing so swaps ``a`` and ``c``, which twists the
    pair at their source, so the bundle keeps its free order."""
    graph = _graph(
        ("s1", PORT, "a"),
        ("s2", PORT, "b"),
        ("s1", "elsewhere", "c"),
        side=PortSide.TOP,
        direction="TB",
    )
    chans = [
        _channel("a", "s1", PORT, 210.0),
        _channel("c", "s1", "elsewhere", 214.0),
        _channel("b", "s2", PORT, 218.0),
    ]
    ctx = _ctx(graph, _port_offsets(a=4.0, b=8.0))

    assert _distinct_line_order(chans) == ["a", "c", "b"]
    assert _port_lane_order_pairs(
        ["a", "c", "b"],
        [_slot_entry_reach(graph, [ch]) for ch in chans],
        Direction.D,
        ctx,
    ) == [(2, 0)]
    assert _port_ordered_line_order(chans, ctx) == ["a", "c", "b"]


def test_a_port_order_splitting_two_lines_from_one_source_keeps_the_free_order():
    """``a`` and ``c`` leave ``s1`` together and ``b`` sits outside them.  A
    second port puts ``b`` left of ``c``; every order doing so without
    reversing the pair wedges ``b`` between them, so the bundle keeps its free
    order."""
    graph = _graph(
        ("s1", PORT, "a"),
        ("s1", OTHER_PORT, "c"),
        ("s2", OTHER_PORT, "b"),
        side=PortSide.TOP,
        direction="TB",
    )
    graph.add_section(Section(id="alt", name="Alt", direction="TB"))
    graph.add_port(Port(id=OTHER_PORT, section_id="alt", side=PortSide.TOP))
    chans = [
        _channel("a", "s1", PORT, 210.0),
        _channel("c", "s1", OTHER_PORT, 214.0),
        _channel("b", "s2", OTHER_PORT, 218.0),
    ]
    ctx = _ctx(graph, {(OTHER_PORT, "c"): 4.0, (OTHER_PORT, "b"): 8.0})

    assert _distinct_line_order(chans) == ["a", "c", "b"]
    assert _port_lane_order_pairs(
        ["a", "c", "b"],
        [_slot_entry_reach(graph, [ch]) for ch in chans],
        Direction.D,
        ctx,
    ) == [(2, 1)]
    assert _port_ordered_line_order(chans, ctx) == ["a", "c", "b"]


# A BT section's BOTTOM entry fed by m1 off one junction and by m2 through a
# merge from another, with the two lines rising side by side in the column
# east of Zone 2 before they part.
BT_BOTTOM_ENTRY_MAP = """\
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8

graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x0 -->|m0| q0x1
        q0x1 -->|m0| q0x2
    end
    subgraph z1 [Zone 1]
        %%metro direction: TB
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x0 -->|m0| q1x1
        q1x0 -->|m1| q1x2
        q1x1 -->|m0,m1| q1x2
        q1x1 -->|m2| q1x3
        q1x2 -->|m1| q1x3
    end
    subgraph z2 [Zone 2]
        %%metro direction: BT
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x0 -->|m2| q2x1
        q2x1 -->|m2| q2x2
    end
    subgraph z3 [Zone 3]
        q3x0[Q3X0]
    end
    q0x0 -->|m2| q1x1
    q0x0 -->|m2| q2x0
    q0x2 -->|m0| q1x0
    q0x2 -->|m1| q1x1
    q1x3 -->|m1,m2| q2x0
    q1x3 -->|m1| q3x0
    q2x0 -->|m1| q3x0
"""


def _drawn_vertical_runs(plan) -> list[tuple[str, float, float, float, bool]]:
    """``(line, x, y_lo, y_hi, up)`` for every drawn axis-aligned vertical run."""
    runs = []
    for route, polyline in zip(plan.routes, plan.route_polylines):
        for (x0, y0), (x1, y1) in zip(polyline, polyline[1:]):
            if abs(x0 - x1) < 0.01 and abs(y0 - y1) > 0.01:
                runs.append((route.line_id, x0, min(y0, y1), max(y0, y1), y1 < y0))
    return runs


def test_lines_rising_together_into_a_bt_bottom_entry_hold_its_lane_order():
    """m1 and m2 both rise into Zone 2's BOTTOM entry, which they enter
    travelling up, so every stretch where they rise side by side carries them
    in the order the port does."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(BT_BOTTOM_ENTRY_MAP)
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).plan

    port = next(
        pid
        for pid, p in graph.ports.items()
        if p.section_id == "z2" and p.is_entry and p.side is PortSide.BOTTOM
    )
    port_x = {
        route.line_id: polyline[-1][0]
        for route, polyline in zip(plan.routes, plan.route_polylines)
        if route.edge.target == port
    }
    assert set(port_x) == {"m1", "m2"}
    port_m1_left = port_x["m1"] < port_x["m2"]

    runs = _drawn_vertical_runs(plan)
    side_by_side = [
        (m1[1], m2[1])
        for m1 in runs
        if m1[0] == "m1" and m1[4]
        for m2 in runs
        if m2[0] == "m2"
        and m2[4]
        and min(m1[3], m2[3]) - max(m1[2], m2[2]) > 1.0
        and abs(m1[1] - m2[1]) <= 2 * STEP + 0.01
    ]
    assert len(side_by_side) >= 2
    assert all((x1 < x2) == port_m1_left for x1, x2 in side_by_side), side_by_side
