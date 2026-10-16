"""A bypass entering a vertical section along its flow axis crosses no line twice.

A line entering a BT section at its BOTTOM edge (a TB section at its TOP) and
skipping the first stations of the chain has to change lanes on the way to its
target.  If it changes lanes at the port, over the lanes of lines that later
leave the chain toward the same side, each of those lines crosses it a second
time on the way out.  A bypass whose own line also reaches the target through
the chain is one line arriving twice, so it runs with the chain into the target
rather than splitting from it and rejoining.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import parse_and_layout

from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

Point = tuple[float, float]

_TOPOLOGIES = Path(__file__).resolve().parent.parent / "examples" / "topologies"

_LATER_DEPARTURES = """
%%metro line: l0 | L0 | #e6194b
%%metro line: l1 | L1 | #3cb44b
%%metro line: l2 | L2 | #4363d8

graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: BT
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|l0,l1| b1
        b1 -->|l1| b2
        b0 -->|l2| b2
    end
    a0 -->|l0| b2
    a0 -->|l0,l1,l2| b0
"""

_SAME_LINE_CHAIN = """
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #4363d8

graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: BT
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|m0,m1| b1
        b1 -->|m0| b2
    end
    a0 -->|m0| b2
    a0 -->|m1| b0
"""

_SINGLE_BYPASS = """
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8
%%metro line: m3 | L3 | #f58231

graph LR
    subgraph z0 [Zone 0]
        q0x1[Q0X1]
    end
    subgraph z1 [Zone 1]
        %%metro direction: BT
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x0 -->|m1| q1x1
    end
    subgraph z2 [Zone 2]
        q2x0[Q2X0]
    end
    subgraph z3 [Zone 3]
        %%metro direction: TB
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x0 -->|m2| q3x1
    end
    q0x1 -->|m1| q1x0
    q0x1 -->|m1| q1x1
    q1x0 -->|m3| q2x0
"""

_TWO_COLUMN_CHAIN = """
%%metro line: m1 | L1 | #3cb44b
%%metro line: m2 | L2 | #4363d8

graph LR
    subgraph z0 [Zone 0]
        q0x2[Q0X2]
        q0x3[Q0X3]
    end
    subgraph z1 [Zone 1]
        %%metro direction: BT
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x0 -->|m1| q1x1
        q1x1 -->|m1| q1x2
    end
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x3[Q2X3]
        q2x1 -->|m1| q2x3
    end
    subgraph z3 [Zone 3]
        %%metro direction: RL
        q3x0[Q3X0]
    end
    q0x2 -->|m1| q1x2
    q0x3 -->|m1| q1x0
    q1x0 -->|m2| q3x0
    q1x2 -->|m1| q2x0
"""

_SAME_LINE_MAPS = {
    "chain_from_first_station_bt": _SAME_LINE_CHAIN,
    "chain_from_port_bt": _SINGLE_BYPASS,
    "chain_across_columns_bt": _TWO_COLUMN_CHAIN,
}

_CROSSING_MAPS = {
    "later_departures_bt": (_LATER_DEPARTURES, "b"),
    "bt_entry_bundle_bypass": (
        (_TOPOLOGIES / "bt_entry_bundle_bypass.mmd").read_text(),
        "work",
    ),
    "bt_entry_bypass_holds_port_lane": (
        (_TOPOLOGIES / "bt_entry_bypass_holds_port_lane.mmd").read_text(),
        "align",
    ),
}


def _drawn(graph: MetroGraph) -> dict[tuple[str, str, str], list[Point]]:
    plan = build_render_plan(graph, resolve_theme(None, graph))
    return {
        (r.edge.source, r.edge.target, r.line_id): [tuple(p) for p in points]
        for r, points in zip(plan.routes, plan.route_polylines)
    }


def _proper_crossing(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    def side(p: Point, q: Point, r: Point) -> int:
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)

    (p1, p2), (q1, q2) = a, b
    return (
        side(p1, p2, q1) * side(p1, p2, q2) < 0
        and side(q1, q2, p1) * side(q1, q2, p2) < 0
    )


@pytest.mark.parametrize("name", sorted(_CROSSING_MAPS))
def test_flow_entry_bypass_crosses_no_line_twice(name: str) -> None:
    source, section_id = _CROSSING_MAPS[name]
    graph = parse_and_layout(source)
    segments: dict[str, list[tuple[Point, Point]]] = {}
    for (src, tgt, line_id), points in _drawn(graph).items():
        if not (
            graph.stations[src].section_id
            == graph.stations[tgt].section_id
            == section_id
        ):
            continue
        segments.setdefault(line_id, []).extend(zip(points, points[1:]))
    lines = sorted(segments)
    for i, first in enumerate(lines):
        for second in lines[i + 1 :]:
            crossings = sum(
                _proper_crossing(a, b)
                for a in segments[first]
                for b in segments[second]
            )
            assert crossings <= 1, (
                f"{first} and {second} cross {crossings} times inside {section_id}"
            )


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    length = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1e-9)
    return (
        abs(cross) / length < 0.5
        and min(a[0], b[0]) - 0.5 <= p[0] <= max(a[0], b[0]) + 0.5
        and min(a[1], b[1]) - 0.5 <= p[1] <= max(a[1], b[1]) + 0.5
    )


@pytest.mark.parametrize("name", sorted(_SAME_LINE_MAPS))
def test_same_line_bypass_runs_with_its_chain_into_the_target(name: str) -> None:
    graph = parse_and_layout(_SAME_LINE_MAPS[name])
    drawn = _drawn(graph)
    checked = 0
    for key, bypass in drawn.items():
        port_id, target_id, line_id = key
        port = graph.ports.get(port_id)
        if port is None or not port.is_entry or graph.stations[target_id].is_port:
            continue
        if not any(
            e.line_id == line_id and e.source != port_id
            for e in graph.edges_to(target_id)
        ):
            continue
        own_line = [
            seg
            for other, pts in drawn.items()
            if other != key and other[2] == line_id
            for seg in zip(pts, pts[1:])
        ]
        assert abs(bypass[0][0] - bypass[1][0]) < 0.5, (
            f"{line_id} leaves the port's lane at once: {bypass}"
        )
        for a, b in list(zip(bypass, bypass[1:]))[1:]:
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            assert any(_on_segment(mid, *seg) for seg in own_line), (
                f"{line_id} {port_id}->{target_id} splits from its own line "
                f"on {a}->{b}: {bypass}"
            )
        checked += 1
    assert checked, "fixture lost its same-line bypass"
