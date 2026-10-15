"""Tests for the direction-agnostic section lane arranger.

The arranger reduces a section's *boundary configuration* -- the order in which
lines cross its determining edge -- to a lane order, so that a line crossing at
edge-slot ``k`` rides lane ``k`` and the bundle runs parallel by construction.
Fan-out divergence reads its exit edge's peel order; reconvergence reads its
entry edge's primary-feeder order.

The unit tests pin the reduction itself; the fixture tests prove the reduction
is wired into the pipeline and drives the lane order of real shipped diagrams.
"""

from __future__ import annotations

import warnings
from itertools import pairwise
from pathlib import Path

import pytest

from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.arranger import (
    BoundaryConfig,
    lane_order,
    peel_lane_order,
)
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, split_guard_warnings

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_TOPOLOGIES = REPO_ROOT / "examples" / "topologies"


# ---------------------------------------------------------------------------
# Unit tests: the order-to-lanes reduction
# ---------------------------------------------------------------------------

PRIORITY = {"a": 0, "b": 1, "c": 2, "d": 3}


def test_determining_order_takes_the_front_lanes() -> None:
    """Lines crossing the determining edge ride the front lanes in edge order."""
    config = BoundaryConfig(present=("a", "b", "c"), determining=("c", "a"))
    assert lane_order(config, PRIORITY) == ("c", "a", "b")


def test_unconstrained_lines_fall_to_the_back_in_priority_order() -> None:
    """Lines the determining edge does not pin are appended by priority, not by
    their position in *present*."""
    config = BoundaryConfig(present=("d", "c", "b", "a"), determining=("d",))
    assert lane_order(config, PRIORITY) == ("d", "a", "b", "c")


def test_returns_none_when_already_priority_order() -> None:
    """A determining order that reproduces the plain priority order needs no
    re-slot, signalled by ``None``."""
    config = BoundaryConfig(present=("a", "b", "c"), determining=("a", "b"))
    assert lane_order(config, PRIORITY) is None


def test_determining_lines_absent_from_present_are_ignored() -> None:
    """An edge order naming a line the section does not carry is filtered out
    before it can claim a lane."""
    config = BoundaryConfig(present=("a", "b"), determining=("c", "b"))
    assert lane_order(config, PRIORITY) == ("b", "a")


def test_missing_priority_defaults_to_zero() -> None:
    """Among unconstrained lines, one absent from the priority map sorts as 0,
    ahead of a line with a positive priority."""
    config = BoundaryConfig(present=("p", "q", "z"), determining=("z",))
    # 'z' leads (determining); 'q' (default 0) precedes 'p' (priority 3).
    assert lane_order(config, {"p": 3}) == ("z", "q", "p")


# ---------------------------------------------------------------------------
# Fixture tests: the reduction drives real layout
# ---------------------------------------------------------------------------


def _section_lane_order(path: Path, sec_id: str) -> list[str]:
    """Lines of *sec_id* in lane order (ascending stored offset).

    Reads a representative multi-line station of the section after layout, so
    the order reflects the offsets the arranger assigned.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    for sid, station in graph.stations.items():
        if station.is_port or station.section_id != sec_id:
            continue
        lines = list(graph.station_lines(sid))
        if len(lines) >= 2:
            return sorted(lines, key=lambda lid: offsets.get((sid, lid), 0.0))
    raise AssertionError(f"no multi-line station found in section {sec_id!r}")


@pytest.mark.parametrize(
    ("fixture", "sec_id", "expected"),
    [
        ("dogleg_twoline_fanout.mmd", "left_tgt", ["to_new", "to_src"]),
        ("clear_channel_target_aware_push.mmd", "src", ["rna", "dna"]),
        ("reconverge_reversed_fold.mmd", "preprocessing", ["rna", "atac", "protein"]),
        ("reconverge_reversed_fold.mmd", "integration", ["rna", "atac", "protein"]),
    ],
)
def test_fanout_source_section_leaves_in_peel_order(
    fixture: str, sec_id: str, expected: list[str]
) -> None:
    """A section feeding a shared fan-out leaves its bundle in the peel order
    the arranger reads off the EXIT edge, so the lines descend without
    crossing."""
    assert _section_lane_order(EXAMPLE_TOPOLOGIES / fixture, sec_id) == expected


# ---------------------------------------------------------------------------
# Fan-out divergence: lines the exit fan does not carry
# ---------------------------------------------------------------------------


def test_peel_order_is_dealt_over_the_fan_lines_own_lanes() -> None:
    """Fan lines trade lanes among themselves; a line off the fan keeps its
    priority lane."""
    config = BoundaryConfig(present=("a", "b", "c"), determining=("c", "a"))
    assert peel_lane_order(config, PRIORITY) == ("c", "b", "a")


def test_peel_order_matching_priority_needs_no_reslot() -> None:
    config = BoundaryConfig(present=("a", "b", "c"), determining=("b", "c"))
    assert peel_lane_order(config, PRIORITY) is None


def test_line_pinned_by_another_crossing_leads_or_trails_the_bundle() -> None:
    config = BoundaryConfig(present=("a", "b", "c", "d"), determining=("b", "c"))
    assert peel_lane_order(config, PRIORITY, leading=("d",)) == ("d", "a", "b", "c")
    assert peel_lane_order(config, PRIORITY, trailing=("a",)) == ("b", "c", "d", "a")


def test_fan_line_is_never_pinned() -> None:
    config = BoundaryConfig(present=("a", "b", "c"), determining=("c", "b"))
    assert peel_lane_order(config, PRIORITY, leading=("c",), trailing=("b",)) == (
        "a",
        "c",
        "b",
    )


_HANDOFF_REVERSED_PEEL = """\
%%metro line: l3 | Line 3 | #156075
%%metro line: l1 | Line 1 | #3779b1
%%metro line: l2 | Line 2 | #a66d13
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1| b1
        b1 -->|l2,l3| b2
    end
    subgraph c [C]
        c0[C0]
    end
    subgraph d [D]
        d0[D0]
    end
    a0 -->|l1| b0
    b2 -->|l2| c0
    b2 -->|l3| d0
"""

_HANDOFF_RL = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro line: l2 | Line 2 | #a66d13
%%metro line: l3 | Line 3 | #156075
%%metro grid: a | 2,0
%%metro grid: b | 1,0
%%metro grid: c | 0,0
%%metro grid: d | 0,1
graph LR
    subgraph a [A]
        %%metro direction: RL
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: RL
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l1| b1
        b1 -->|l2| b2
    end
    subgraph c [C]
        %%metro direction: RL
        c0[C0]
    end
    subgraph d [D]
        %%metro direction: RL
        d0[D0]
    end
    a0 -->|l1| b0
    b2 -->|l2| c0
    b2 -->|l3| d0
"""


def _perpendicular_handoff(side: str, first: bool) -> str:
    """A handoff whose off-fan line ``l1`` enters section B through *side*."""
    lines = [
        "%%metro line: l1 | Line 1 | #3779b1",
        "%%metro line: l2 | Line 2 | #a66d13",
        "%%metro line: l3 | Line 3 | #156075",
    ]
    if not first:
        lines.append(lines.pop(0))
    a_row, b_row = (1, 0) if side == "bottom" else (0, 1)
    return "\n".join(
        [
            *lines,
            f"%%metro grid: a | 0,{a_row}",
            f"%%metro grid: b | 0,{b_row}",
            f"%%metro grid: c | 1,{b_row}",
            f"%%metro grid: d | 1,{b_row + 1}",
            "graph LR",
            "    subgraph a [A]",
            "        a0[A0]",
            "    end",
            "    subgraph b [B]",
            f"        %%metro entry: {side} | l1",
            "        b0[B0]",
            "        b1[B1]",
            "        b2[B2]",
            "        b0 -->|l1| b1",
            "        b1 -->|l2| b2",
            "    end",
            "    subgraph c [C]",
            "        c0[C0]",
            "    end",
            "    subgraph d [D]",
            "        d0[D0]",
            "    end",
            "    a0 -->|l1| b0",
            "    b2 -->|l2| c0",
            "    b2 -->|l3| d0",
            "",
        ]
    )


def _laid_out(text: str) -> tuple[MetroGraph, dict[tuple[str, str], float]]:
    graph = parse_metro_mermaid(text)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compute_layout(graph)
        offsets = compute_station_offsets(graph)
    guard_warnings, _ = split_guard_warnings(caught)
    assert not guard_warnings, [str(w.message) for w in guard_warnings]
    return graph, offsets


def _drawn_ys(
    graph: MetroGraph, offsets: dict[tuple[str, str], float], line_id: str
) -> set[float]:
    """Every Y the routes of *line_id* are drawn through, offsets applied."""
    return {
        round(y, 3)
        for route in route_edges(graph, station_offsets=offsets)
        if route.line_id == line_id
        for _x, y in apply_route_offsets(route, offsets)
    }


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            (EXAMPLE_TOPOLOGIES / "lr_line_handoff_fanning_exit.mmd").read_text(),
            id="handoff",
        ),
        pytest.param(_HANDOFF_REVERSED_PEEL, id="peel-reverses-priority"),
        pytest.param(_HANDOFF_RL, id="rl"),
    ],
)
def test_line_handed_off_before_a_fanning_exit_runs_level(text: str) -> None:
    """A line that ends before the section's exit fan opens, fed level from
    the section beside it, is not re-slotted by the fan's peel order: it
    rides straight in with no jog between its feeder and the handoff."""
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    assert len(_drawn_ys(graph, offsets, "l1")) == 1


def test_handoff_station_carries_no_dead_lane() -> None:
    """The handoff station's marker spans exactly the two lines meeting at it."""
    graph, offsets = _laid_out(
        (EXAMPLE_TOPOLOGIES / "lr_line_handoff_fanning_exit.mmd").read_text()
    )
    step = graph_offset_step(graph)
    lanes = sorted(offsets[("b1", lid)] for lid in graph.station_lines("b1"))
    assert lanes[-1] - lanes[0] == pytest.approx(step)


@pytest.mark.parametrize(
    "first", [True, False], ids=["declared-first", "declared-last"]
)
@pytest.mark.parametrize("side", ["top", "bottom"])
def test_line_entering_through_a_perpendicular_port_rides_that_side(
    side: str, first: bool
) -> None:
    """An off-fan line entering through a TOP/BOTTOM port rides the bundle's
    top/bottom lane at the handoff, whatever its declaration order, and the
    layout settles with no guard downgraded."""
    graph, offsets = _laid_out(_perpendicular_handoff(side, first))
    l1, l2 = offsets[("b1", "l1")], offsets[("b1", "l2")]
    assert (l1 > l2) if side == "bottom" else (l1 < l2)


_FREE_FEEDER = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro line: l2 | Line 2 | #a66d13
%%metro line: l3 | Line 3 | #156075
%%metro grid: f | 0,0
%%metro grid: s | 0,1
%%metro grid: c | 1,1
%%metro grid: d | 1,2
graph LR
    subgraph f [F]
        f0[F0]
        f1[F1]
        f2[F2]
        f0 -->|l1,l2,l3| f1
        f1 -->|l1| f2
    end
    subgraph s [S]
        %%metro entry: top | l2, l3
        s0[S0]
        s1[S1]
        s0 -->|l2,l3| s1
    end
    subgraph c [C]
        c0[C0]
    end
    subgraph d [D]
        d0[D0]
    end
    f1 -->|l2,l3| s0
    s1 -->|l2| c0
    s1 -->|l3| d0
"""


@pytest.mark.parametrize("line_id", ["l2", "l3"])
def test_free_feeder_drops_its_fan_lines_in_one_step(line_id: str) -> None:
    """A feeder re-slotted onto the downstream fan's peel order keeps its
    off-fan line on its own lane, so each fan line runs level on the trunk,
    drops once, and runs level into the exit with no step between."""
    graph, offsets = _laid_out(_FREE_FEEDER)
    levels = {
        round(y0, 3)
        for route in route_edges(graph, station_offsets=offsets)
        if route.line_id == line_id
        and graph.stations[route.edge.source].section_id == "f"
        and graph.stations[route.edge.target].section_id == "f"
        for (_x0, y0), (_x1, y1) in pairwise(apply_route_offsets(route, offsets))
        if abs(y1 - y0) <= 1e-6
    }
    assert len(levels) == 2


_SHARED_BOTTOM_ENTRY = """\
%%metro line: l1 | Line 1 | #3779b1
%%metro line: l2 | Line 2 | #a66d13
%%metro line: l3 | Line 3 | #156075
%%metro grid: f | 0,1
%%metro grid: s | 0,0
%%metro grid: c | 1,0
%%metro grid: d | 1,2
graph LR
    subgraph f [F]
        f0[F0]
        f1[F1]
        f0 -->|l1,l2,l3| f1
    end
    subgraph s [S]
        %%metro entry: bottom | l1, l2, l3
        s0[S0]
        s1[S1]
        s2[S2]
        s0 -->|l1,l2,l3| s1
        s1 -->|l2,l3| s2
    end
    subgraph c [C]
        c0[C0]
    end
    subgraph d [D]
        d0[D0]
    end
    f1 -->|l1,l2,l3| s0
    s2 -->|l2| c0
    s2 -->|l3| d0
"""


def test_off_fan_line_sharing_the_fans_entry_keeps_the_delivered_order() -> None:
    """An off-fan line that reaches the section through the same BOTTOM port as
    the fan lines arrives in one stream with them, so it is not pinned to the
    bottom edge: the order the feeder delivers is the order the entry keeps,
    with no transposition in the riser between them."""
    graph, offsets = _laid_out(_SHARED_BOTTOM_ENTRY)
    (exit_id,) = graph.sections["f"].exit_ports
    (entry_id,) = graph.sections["s"].entry_ports

    def order(port_id: str) -> list[str]:
        return sorted(
            graph.station_lines(port_id), key=lambda lid: offsets[(port_id, lid)]
        )

    assert order(entry_id) == order(exit_id)
