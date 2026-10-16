"""A fan-in branch arrives on the side its section's lanes stack.

In a vertical-flow section the lanes stack opposite to the axis the track
allocator grows along, so a branch carrying lines the join's trunk does not
carry must be reflected across the join or it crosses the trunk to reach its
lane.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import pytest
from orientation_transform import Orientation, all_orientations, transform_source

from nf_metro.api import render_string
from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.phases import single_section
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.validate import parse_route_polylines

FIXTURES = Path(__file__).parent / "fixtures" / "curve_invariant_repros"
SOURCE_BEYOND_CHANNEL = FIXTURES / "bt_bottom_entry_source_beyond_channel.mmd"
MERGE_LR = FIXTURES / "tb_lane_side_merge_lr.mmd"

# Route ends closer than this to a same-line neighbour are one join; those
# between the two bounds are a broken lane.
LANE_JOIN_PX = 0.5
LANE_BREAK_PX = 30.0
# A same-direction lateral step between these bounds is a jog.
JOG_MIN_PX = 0.05
JOG_MAX_PX = 12.0


def _laid_out(source: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(source)
        compute_layout(graph)
    return graph


def _proper_crossings(svg: str, box: tuple[float, float, float, float]) -> int:
    """Interior crossings between different lines' drawn segments inside *box*."""
    x0, y0, x1, y1 = box
    polys = []
    for line_id, subpaths in parse_route_polylines(svg):
        pts = [p for sub in subpaths for p in sub]
        polys.append((line_id, pts))
    count = 0
    for i, (la, a) in enumerate(polys):
        for lb, b in polys[i + 1 :]:
            if la == lb:
                continue
            for p, q in zip(a, a[1:]):
                for r, s in zip(b, b[1:]):
                    d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0])
                    if abs(d) < 1e-9:
                        continue
                    t = (
                        (r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])
                    ) / d
                    u = (
                        (r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])
                    ) / d
                    if 1e-3 < t < 1 - 1e-3 and 1e-3 < u < 1 - 1e-3:
                        x, y = p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])
                        if x0 <= x <= x1 and y0 <= y <= y1:
                            count += 1
    return count


def _section_box(graph, section_id: str) -> tuple[float, float, float, float]:
    s = graph.sections[section_id]
    return (s.bbox_x, s.bbox_y, s.bbox_x + s.bbox_w, s.bbox_y + s.bbox_h)


def test_source_beyond_channel_merge_does_not_cross_the_trunk() -> None:
    """Green and blue join red at Q2X2 from their own side: no crossing in Zone 2."""
    source = SOURCE_BEYOND_CHANNEL.read_text()
    graph = _laid_out(source)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        svg = render_string(source)
    assert _proper_crossings(svg, _section_box(graph, "z2")) == 0


def _lane_breaks_and_jogs(svg: str) -> tuple[int, int]:
    """Route ends that just miss a same-line neighbour, and small lateral steps."""
    polys = [
        (line_id, [p for sub in subpaths for p in sub])
        for line_id, subpaths in parse_route_polylines(svg)
    ]
    ends: dict[str, list[tuple[float, float]]] = {}
    for line_id, pts in polys:
        if len(pts) >= 2:
            ends.setdefault(line_id, []).extend([pts[0], pts[-1]])
    breaks = 0
    for pts in ends.values():
        for i, p in enumerate(pts):
            gaps = [math.dist(p, q) for j, q in enumerate(pts) if j != i]
            if gaps and LANE_JOIN_PX <= min(gaps) < LANE_BREAK_PX:
                breaks += 1
    jogs = 0
    for _line, pts in polys:
        for a, b, c, d in zip(pts, pts[1:], pts[2:], pts[3:]):
            v1, v3 = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
            vertical = abs(v1[0]) < 0.01 and abs(v3[0]) < 0.01
            horizontal = abs(v1[1]) < 0.01 and abs(v3[1]) < 0.01
            if not (vertical or horizontal) or v1[0] * v3[0] + v1[1] * v3[1] <= 0:
                continue
            lateral = abs(c[0] - b[0]) if vertical else abs(c[1] - b[1])
            if JOG_MIN_PX < lateral < JOG_MAX_PX:
                jogs += 1
    return breaks, jogs


def test_source_beyond_channel_keeps_its_seams_whole() -> None:
    """Re-seating Zone 2's branches leaves every lane continuous across its seams."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        svg = render_string(SOURCE_BEYOND_CHANNEL.read_text())
    assert _lane_breaks_and_jogs(svg) == (0, 0)


@pytest.mark.parametrize("orientation", list(all_orientations()), ids=lambda o: o.name)
def test_merge_lands_without_crossing_in_every_orientation(
    orientation: Orientation,
) -> None:
    source = transform_source(MERGE_LR.read_text(), orientation)
    graph = _laid_out(source)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        svg = render_string(source)
    assert _proper_crossings(svg, _section_box(graph, "m")) == 0


# A clockwise quarter turn maps screen (dx, dy) to (-dy, dx).
def _turn(vec: tuple[float, float], quarter_turns: int) -> tuple[float, float]:
    dx, dy = vec
    for _ in range(quarter_turns):
        dx, dy = -dy, dx
    return dx, dy


@pytest.mark.parametrize("quarter_turns", [1, 3], ids=["r90_tb", "r270_bt"])
def test_vertical_merge_seats_branches_as_the_rotated_lr_reference(
    quarter_turns: int,
) -> None:
    """TB/BT is LR rotated: each merge branch sits where the turned LR branch sits."""
    reference = _laid_out(MERGE_LR.read_text())
    image = _laid_out(
        transform_source(MERGE_LR.read_text(), Orientation(quarter_turns, False))
    )
    for branch in ("b1", "c1"):
        ref = reference.stations[branch]
        ref_join = reference.stations["mm"]
        want = _turn((ref.x - ref_join.x, ref.y - ref_join.y), quarter_turns)
        img, img_join = image.stations[branch], image.stations["mm"]
        got = (img.x - img_join.x, img.y - img_join.y)
        # Compare the lane-axis component only; the flow-axis spacing follows
        # label metrics, which a rotation does not carry.
        assert (want[0] > 0) == (got[0] > 0), (branch, want, got)


def _seated_positions(source: str, *, seat: bool, monkeypatch) -> dict:
    """Every station's position, with the fan-in seating pass on or off."""
    if not seat:
        monkeypatch.setattr(
            single_section, "_seat_fan_in_branches_on_lane_side", lambda *a, **k: None
        )
    graph = _laid_out(source)
    monkeypatch.undo()
    return {sid: (st.x, st.y) for sid, st in graph.stations.items()}


def _seating_moves_something(source: str, quarter_turns: int, monkeypatch) -> bool:
    turned = transform_source(source, Orientation(quarter_turns, False))
    with_pass = _seated_positions(turned, seat=True, monkeypatch=monkeypatch)
    without = _seated_positions(turned, seat=False, monkeypatch=monkeypatch)
    return with_pass != without


_MERGE = MERGE_LR.read_text()
# One variant per condition that stops a branch being reflected.
SEATING_BAILS = {
    "second_seam_line": _MERGE.replace(
        "    f1 -->|r| a\n", "    f1 -->|r| a\n    f1 -->|g| b1\n"
    ),
    "branches_fork": _MERGE.replace(
        "        mm -->|r,g,b| z\n",
        "        mm -->|r,g,b| z\n"
        "        w[W]\n        b1 -->|g| w\n        c1 -->|b| w\n",
    ),
    "branches_share_a_line": _MERGE.replace("b1 -->|g| mm", "b1 -->|g,b| mm"),
}


def test_seating_reflects_a_branch_in_the_baseline_map(monkeypatch) -> None:
    assert _seating_moves_something(_MERGE, 1, monkeypatch)


def test_seating_reflects_a_branch_fed_through_a_chain(monkeypatch) -> None:
    chained = _MERGE.replace(
        "        c1 -->|b| mm\n",
        "        c0[C0]\n        c0 -->|b| c1\n        c1 -->|b| mm\n",
    )
    assert _seating_moves_something(chained, 1, monkeypatch)


@pytest.mark.parametrize("name", SEATING_BAILS)
def test_seating_leaves_the_section_alone_when_a_condition_fails(
    name: str, monkeypatch
) -> None:
    assert not _seating_moves_something(SEATING_BAILS[name], 1, monkeypatch)


def test_seating_leaves_a_section_whose_lanes_stack_forward(monkeypatch) -> None:
    assert not _seating_moves_something(_MERGE, 0, monkeypatch)


def test_seating_leaves_a_branch_whose_reflected_slot_is_taken(monkeypatch) -> None:
    turned = transform_source(_MERGE, Orientation(1, False))
    monkeypatch.setattr(single_section, "_slot_occupied", lambda *a, **k: True)
    blocked = _seated_positions(turned, seat=True, monkeypatch=monkeypatch)
    unseated = _seated_positions(turned, seat=False, monkeypatch=monkeypatch)
    assert blocked == unseated


_BRANCH_MAP = """\
%%metro line: r | R | #e6194b
%%metro line: g | G | #3cb44b

graph LR
    subgraph s [S]
        a[A]
        b[B]
        j[J]
        c[C]
        k[K]
        u[U]
        v[V]
        m[M]
        p[P]
        a -->|r| b
        b -->|r| j
        c -->|g| j
        c -->|g| k
        u -->|r| m
        v -->|g| m
        m -->|r| j
        j -->|r| p
        p -->|r| j
    end
"""


def _parsed(source: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return parse_metro_mermaid(source)


def test_fan_in_branch_chain_runs_back_to_the_free_standing_head() -> None:
    graph = _parsed(_BRANCH_MAP)
    assert single_section._fan_in_branch_chain(graph, "b", "j") == ["b", "a"]
    assert single_section._fan_in_branch_chain(graph, "a", "b") == ["a"]


def test_fan_in_branch_chain_rejects_a_fork() -> None:
    graph = _parsed(_BRANCH_MAP)
    assert single_section._fan_in_branch_chain(graph, "c", "j") is None


def test_fan_in_branch_chain_rejects_a_station_fed_from_two_places() -> None:
    graph = _parsed(_BRANCH_MAP)
    assert single_section._fan_in_branch_chain(graph, "m", "j") is None


def test_fan_in_branch_chain_rejects_a_chain_that_loops_back_through_the_join() -> None:
    graph = _parsed(_BRANCH_MAP)
    assert single_section._fan_in_branch_chain(graph, "p", "j") is None


_TWO_SECTIONS = """\
%%metro line: r | R | #e6194b
%%metro line: g | G | #3cb44b

graph LR
    subgraph s [S]
        a[A]
        b[B]
        a -->|r| b
        a -->|g| b
    end
    subgraph t [T]
        q[Q]
    end
    b -->|r| q
"""


def test_in_section_sources_lists_each_in_edge_and_skips_ports() -> None:
    graph = _laid_out(_TWO_SECTIONS)
    assert single_section._in_section_sources(graph, "b") == ["a", "a"]
    entering = [e.source for e in graph.edges_to("q")]
    assert entering and all(graph.stations[src].is_port for src in entering)
    assert single_section._in_section_sources(graph, "q") == []


def _slot_probe(source: str):
    graph = _parsed(source)
    frame = AxisFrame.for_direction("TB", 60.0, 40.0)
    for sid, (flow, lane) in {
        "a": (100.0, 10.0),
        "b": (140.0, 10.0),
        "c": (100.0, 70.0),
    }.items():
        frame.primary.set(graph.stations[sid], flow)
        frame.secondary.set(graph.stations[sid], lane)
    return graph, frame


@pytest.mark.parametrize(
    ("lane", "chain", "expected"),
    [
        (70.0, ["a"], True),
        (70.0 + SAME_COORD_TOLERANCE - 0.01, ["a"], True),
        (70.0 + SAME_COORD_TOLERANCE, ["a"], False),
        (70.0, ["a", "c"], False),
        (10.0, ["a"], False),
    ],
    ids=[
        "same_slot",
        "inside_tolerance",
        "at_tolerance",
        "own_chain",
        "other_flow_row",
    ],
)
def test_slot_occupied_matches_an_outside_station_at_the_same_flow_position(
    lane: float, chain: list[str], expected: bool
) -> None:
    graph, frame = _slot_probe(_BRANCH_MAP)
    assert single_section._slot_occupied(graph, frame, "a", lane, chain) is expected
