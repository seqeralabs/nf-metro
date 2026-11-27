"""A bypass around a hidden station opens on the side its lines' lanes stack.

A branch that forks off a trunk station's feeder, carries a subset of the
trunk's lines past it and rejoins downstream rides the fork's bundle until it
peels away.  Opening on the side the section does not stack those lines on
forces both of its legs across the lines staying on the trunk.

The layout validator's crossing check exempts every edge touching a hidden
station, so the count here is taken over the drawn polylines directly.
"""

from __future__ import annotations

import itertools
import warnings
from pathlib import Path

import pytest
from layout_metrics import drawn_polylines, measured_geometry

from nf_metro.api import resolve_theme
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.lane_plan import recorded_lane_plan
from nf_metro.layout.routing.common import segments_properly_cross
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.svg import build_render_plan

ROOT = Path(__file__).parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"
ROTATIONS = ROOT / "tests" / "data" / "bypass_lane_side"

FIXTURES = [
    "bypass_hidden_lane_side.mmd",
    "bypass_hidden_lane_side_lr.mmd",
    "bypass_hidden_lane_side_rl.mmd",
    "bypass_hidden_lane_side_tb.mmd",
    "bypass_hidden_lane_side_bt.mmd",
    "bypass_hidden_lane_side_tb_seam.mmd",
    "bypass_hidden_lane_side_two.mmd",
    "bypass_hidden_lane_side_symmetric.mmd",
    "bypass_hidden_lane_side_symmetric_bt.mmd",
    "bypass_hidden_lane_side_symmetric_tb_seam.mmd",
    "bypass_hidden_lane_side_chain.mmd",
    "bypass_hidden_lane_side_two_symmetric.mmd",
    "bypass_hidden_lane_side_symmetric_dead_end.mmd",
]


def _drawn_crossings(path: Path) -> int:
    """Proper crossings between different lines' drawn segments, hidden edges too."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
        compute_layout(graph)
        plan = build_render_plan(graph, resolve_theme(None, graph))
    offsets, routes = measured_geometry(plan.graph, plan)
    polylines = drawn_polylines(routes, offsets)
    return sum(
        1
        for (route_a, points_a), (route_b, points_b) in itertools.combinations(
            polylines, 2
        )
        if route_a.line_id != route_b.line_id
        for seg_a in itertools.pairwise(points_a)
        for seg_b in itertools.pairwise(points_b)
        if segments_properly_cross(*seg_a, *seg_b, reject_collinear_touch=True)
        is not None
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_hidden_bypass_does_not_cross_the_trunk(name: str) -> None:
    assert _drawn_crossings(TOPOLOGIES / name) == 0


@pytest.mark.parametrize(
    "path", sorted(ROTATIONS.glob("*.mmd")), ids=lambda path: path.name
)
def test_a_turned_in_bypass_does_not_cross_the_trunk(path: Path) -> None:
    assert _drawn_crossings(path) == 0


def _lanes(path: Path, nodes: tuple[str, ...]) -> dict[tuple[str, str], int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
        compute_layout(graph)
    return {
        key: lane
        for key, lane in recorded_lane_plan(graph).tracks.lanes.items()
        if key[0] in nodes
    }


def test_a_lone_turned_in_line_that_bypasses_takes_the_bypass_side() -> None:
    """line1 turns in from the left of a TB run and bypasses on the right, the
    side its priority lane 0 stacks on, so it rides lane 0 rather than the
    port's side."""
    lanes = _lanes(
        TOPOLOGIES / "bypass_hidden_lane_side_tb_seam.mmd", ("step1", "step2")
    )
    assert lanes == {
        ("step1", "line1"): 0,
        ("step1", "line2"): 1,
        ("step2", "line1"): 0,
        ("step2", "line2"): 1,
    }


def test_a_lone_turned_in_line_without_a_bypass_keeps_its_port_side() -> None:
    lanes = _lanes(ROTATIONS / "no_bypass_tb_left.mmd", ("step1", "step3"))
    assert lanes == {
        ("step1", "line1"): 1,
        ("step1", "line2"): 0,
        ("step3", "line1"): 1,
        ("step3", "line2"): 0,
    }


def test_a_line_turned_in_with_others_keeps_its_port_side() -> None:
    """sec4's bottom entry delivers six lines, so its lanes do not follow line
    priority and l3, which turns in and detours via s4n1, keeps its turn-in key
    below l2."""
    lanes = _lanes(
        ROOT / "tests" / "data" / "gate_witnesses" / "f8400987.mmd", ("s4n0",)
    )
    assert (lanes[("s4n0", "l2")], lanes[("s4n0", "l3")]) == (3, 2)
