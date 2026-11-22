"""Both sides of the coordinate thresholds that classify a route's approach.

Each classifier below reads a coordinate difference against a limit.  A case a
hair under the limit and one on it pin where the answer flips, so a refactor
that nudges a ``<`` to ``<=`` or swaps a sign is caught where it happens rather
than through a render that happens to sit far from the limit.
"""

from __future__ import annotations

import dataclasses
import warnings

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_RUN,
    INTER_ROW_HEADER_CLEARANCE,
    SAME_Y_TOLERANCE,
)
from nf_metro.layout.fan_ordering import fanout_divergence_peel_order
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.context import _build_routing_context, _RoutingCtx
from nf_metro.layout.routing.corners import outer_lane_radius
from nf_metro.layout.routing.inter_section_handlers import (
    _gap_above_channel_y,
    _gap_above_target_y,
    _perp_entry_source_beyond_channel,
)
from nf_metro.layout.routing.offsets import loops_round_entry_section
from nf_metro.parser.model import MetroGraph

# Two stacked LR sections: ``top`` over ``bottom``, joined by a bottom exit and
# a top entry, so the lower section has a section above its row and the upper
# one is the topmost.
STACKED = """\
%%metro title: Stacked
%%metro line: a | A | #e6007e
%%metro grid: top | 0,0
%%metro grid: bottom | 0,1
graph LR
    subgraph top [Top]
        %%metro exit: bottom | a
        t1[T1]
        t2[T2]
        t1 -->|a| t2
    end
    subgraph bottom [Bottom]
        %%metro entry: top | a
        b1[B1]
        b2[B2]
        b1 -->|a| b2
    end
    t2 -->|a| b1
"""

# A fan out of ``a`` into a TOP-entry section and a second entry that the
# per-case text edits below move to other sides and directions.
FAN_LR = """\
%%metro title: Fan
%%metro line: l0 | L0 | #e6194b
%%metro line: l1 | L1 | #3cb44b
%%metro line: l2 | L2 | #4363d8
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro entry: top | l0
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l2| b1
        b1 -->|l2| b2
    end
    subgraph c [C]
        %%metro entry: right | l1, l2
        c0[C0]
        c1[C1]
        c0 -->|l2| c1
    end
    a0 -->|l0| b0
    a0 -->|l1| c0
    b2 -->|l2| c0
"""

FAN_RL = """\
%%metro title: Fan RL
%%metro line: l0 | L0 | #e6194b
%%metro line: l1 | L1 | #3cb44b
%%metro line: l2 | L2 | #4363d8
%%metro grid: a | 2,0
%%metro grid: b | 1,0
%%metro grid: c | 0,0
graph LR
    subgraph a [A]
        %%metro direction: RL
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: RL
        %%metro entry: top | l0
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l2| b1
        b1 -->|l2| b2
    end
    subgraph c [C]
        %%metro direction: RL
        %%metro entry: right | l1, l2
        c0[C0]
        c1[C1]
        c0 -->|l2| c1
    end
    a0 -->|l0| b0
    a0 -->|l1| c0
    b2 -->|l2| c0
"""

_C_ENTRY = "entry: right | l1, l2"


def _laid_out(text: str) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return prepare_graph(text)


def _context(graph: MetroGraph) -> _RoutingCtx:
    return _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, None)


@pytest.fixture(scope="module")
def stacked() -> MetroGraph:
    return _laid_out(STACKED)


@pytest.mark.parametrize(
    ("direction", "axis", "sign"),
    [("LR", "x", 1), ("RL", "x", -1), ("TB", "y", 1), ("BT", "y", -1)],
)
@pytest.mark.parametrize(
    ("past", "loops"),
    [
        (-50.0, False),
        (0.0, False),
        (SAME_Y_TOLERANCE, False),
        (SAME_Y_TOLERANCE + 0.01, True),
        (50.0, True),
    ],
)
def test_source_loops_round_only_when_past_the_port_along_the_flow(
    direction: str,
    axis: str,
    sign: int,
    past: float,
    loops: bool,
) -> None:
    graph = _laid_out(STACKED)
    port_station = graph.stations["bottom__entry_top_1"]
    graph.sections["bottom"].direction = direction
    setattr(port_station, axis, 0.0)  # the limit is then exact in floating point
    source = dataclasses.replace(port_station, id="src", is_port=False)
    setattr(source, axis, sign * past)
    assert loops_round_entry_section(graph, source, port_station) is loops


def test_station_without_a_port_never_loops_round(stacked: MetroGraph) -> None:
    station = stacked.stations["b1"]
    source = dataclasses.replace(station, id="src", x=station.x + 100)
    assert not loops_round_entry_section(stacked, source, station)


@pytest.mark.parametrize("is_top", [True, False])
@pytest.mark.parametrize("lead", [Direction.R, Direction.L])
@pytest.mark.parametrize("n", [1, 3])
class TestSourceBeyondChannel:
    """``_perp_entry_source_beyond_channel`` against its three distance limits.

    A source stands *depth* outside the target box's entry edge; the channel
    sits two runways out.  The riser is too short once the source is within two
    runways of the channel (depth below four runways), the box must leave a
    full runway for the turn (depth at least one runway), and the landing column
    must lie a full runway along the lead.
    """

    @staticmethod
    def _call(
        graph: MetroGraph,
        is_top: bool,
        lead: Direction,
        n: int,
        *,
        depth: float,
        lead_gap: float | None = None,
        channel_depth: float | None = None,
        source_y: float | None = None,
    ) -> bool:
        ctx = _context(graph)
        runway = outer_lane_radius(n, ctx.curve_radius, ctx.offset_step)
        tgt_sec = graph.sections["bottom" if is_top else "top"]
        tgt = graph.stations["bottom__entry_top_1" if is_top else "top__exit_bottom_0"]
        edge = tgt_sec.bbox_y if is_top else tgt_sec.bbox_y + tgt_sec.bbox_h
        outward = -1.0 if is_top else 1.0
        final_x = tgt.x
        gap = runway if lead_gap is None else lead_gap
        src = dataclasses.replace(
            tgt,
            id="src",
            is_port=False,
            section_id=None,
            x=final_x - lead.sign * gap,
            y=edge + outward * depth if source_y is None else source_y,
        )
        channel_y = edge + outward * (
            2 * runway if channel_depth is None else channel_depth
        )
        return _perp_entry_source_beyond_channel(
            src, tgt, tgt_sec, final_x, channel_y, lead, n, ctx, is_top=is_top
        )

    @staticmethod
    def _runway(graph: MetroGraph, n: int) -> float:
        ctx = _context(graph)
        return outer_lane_radius(n, ctx.curve_radius, ctx.offset_step)

    def test_depth_within_two_runways_of_the_channel(
        self, stacked: MetroGraph, is_top: bool, lead: Direction, n: int
    ) -> None:
        runway = self._runway(stacked, n)
        assert self._call(stacked, is_top, lead, n, depth=4 * runway - 0.01)
        assert not self._call(stacked, is_top, lead, n, depth=4 * runway)

    def test_box_leaves_a_runway_for_the_turn(
        self, stacked: MetroGraph, is_top: bool, lead: Direction, n: int
    ) -> None:
        runway = self._runway(stacked, n)
        assert self._call(stacked, is_top, lead, n, depth=runway)
        assert not self._call(stacked, is_top, lead, n, depth=runway - 0.01)

    def test_landing_column_lies_a_runway_along_the_lead(
        self, stacked: MetroGraph, is_top: bool, lead: Direction, n: int
    ) -> None:
        runway = self._runway(stacked, n)
        landing = dict(depth=2 * runway)
        assert self._call(stacked, is_top, lead, n, lead_gap=runway, **landing)
        assert not self._call(
            stacked, is_top, lead, n, lead_gap=runway - 0.01, **landing
        )

    def test_traverse_crossing_another_section_is_refused(
        self, stacked: MetroGraph, is_top: bool, lead: Direction, n: int
    ) -> None:
        other = stacked.sections["top" if is_top else "bottom"]
        inside_other = other.bbox_y + other.bbox_h / 2
        clear = self._call(stacked, is_top, lead, n, depth=2 * self._runway(stacked, n))
        blocked = self._call(
            stacked,
            is_top,
            lead,
            n,
            depth=0.0,
            source_y=inside_other,
            channel_depth=2 * self._runway(stacked, n),
            lead_gap=other.bbox_w,
        )
        assert clear
        assert not blocked


def test_source_beyond_channel_needs_a_target_section(stacked: MetroGraph) -> None:
    ctx = _context(stacked)
    tgt = stacked.stations["bottom__entry_top_1"]
    src = dataclasses.replace(tgt, id="src", is_port=False, section_id=None, x=0.0)
    assert not _perp_entry_source_beyond_channel(
        src, tgt, None, tgt.x, tgt.y - 20, Direction.R, 1, ctx, is_top=True
    )


def test_topmost_row_traverse_rides_over_the_header_headroom(
    stacked: MetroGraph,
) -> None:
    ctx = _context(stacked)
    _gap_top, gap_bottom = _gap_above_target_y(stacked, 0)
    assert _gap_above_channel_y(ctx, 0) == (
        gap_bottom - INTER_ROW_HEADER_CLEARANCE - ctx.curve_radius
    )


def test_traverse_below_another_row_centres_in_the_gap(stacked: MetroGraph) -> None:
    ctx = _context(stacked)
    gap_top, gap_bottom = _gap_above_target_y(stacked, 1)
    channel = _gap_above_channel_y(ctx, 1)
    assert gap_top < channel < gap_bottom
    assert channel != gap_bottom - INTER_ROW_HEADER_CLEARANCE - ctx.curve_radius


def _peel_order(text: str) -> list[str] | None:
    graph = _laid_out(text)
    (junction_id,) = graph.junction_ids
    return fanout_divergence_peel_order(graph, junction_id, {})


@pytest.mark.parametrize(
    ("text", "order"),
    [
        pytest.param(FAN_LR, ["l1", "l0"], id="lr-top-and-right-entry-reaching-on"),
        pytest.param(
            FAN_LR.replace(_C_ENTRY, "entry: top | l1, l2"),
            ["l1", "l0"],
            id="lr-two-top-entries",
        ),
        pytest.param(
            FAN_LR.replace(_C_ENTRY, "entry: left | l1, l2"),
            None,
            id="lr-left-entry-reaching-on",
        ),
        pytest.param(
            FAN_RL.replace(_C_ENTRY, "entry: top | l1, l2"),
            ["l1", "l0"],
            id="rl-two-top-entries",
        ),
        pytest.param(FAN_RL, None, id="rl-right-entry-reaching-on"),
    ],
)
def test_same_row_fan_orders_only_when_every_branch_climbs_over_the_row(
    text: str, order: list[str] | None
) -> None:
    """A TOP entry always climbs; a flow-side entry only when it faces away.

    A RIGHT entry reached by a target to the right faces away from the source,
    so its branch loops over the row.  The same entry on a target to the left,
    or a LEFT entry on a target to the right, faces the source and is reached
    straight on, which leaves the fan no single side to order by.
    """
    assert _peel_order(text) == order
