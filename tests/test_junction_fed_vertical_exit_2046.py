"""A vertical-flow section's exit feeding side entries through a divergence
junction keeps its own carrier's side of the section.

A TB/BT section's exit sits on a structural boundary, not on a row its
consumers share, so a LEFT/RIGHT entry it feeds anchors on its own consumer
rather than dragging the exit to the entry's height.  Reaching that entry
through a junction must not change the verdict: pulling the exit up to a
downstream TB entry's clamp height lifts it above its own carrier station, and
the exit leg then climbs back over the section's trunk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nf_metro.api import render_string
from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
TB_SIDE_EXIT = TOPOLOGIES / "tb_junction_side_exit_side_entries.mmd"

FIXTURES = {
    "tb_side_exit": TB_SIDE_EXIT.read_text(),
    "bt_top_exit": (TOPOLOGIES / "bt_junction_top_exit_side_entries.mmd").read_text(),
    "bt_side_exit": (TOPOLOGIES / "bt_junction_side_exit_side_entries.mmd").read_text(),
    "tb_side_exit_three_consumers": TB_SIDE_EXIT.read_text()
    + """    subgraph s4 [S4]
        %%metro direction: TB
        s4n0[S4N0]
        s4n1[S4N1]
        s4n0 -->|l0| s4n1
    end
    s1n4 -->|l0| s4n0
""",
}


def _layout(text: str, *, validate: bool) -> MetroGraph:
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=validate)
    return graph


def _junction_fed_vertical_side_exits(graph: MetroGraph) -> list[str]:
    return [
        pid
        for pid, port in graph.ports.items()
        if not port.is_entry
        and port.side in (PortSide.LEFT, PortSide.RIGHT)
        and lanes_run_along_x(graph.sections[port.section_id].direction)
        and any(e.target in graph.junction_ids for e in graph.edges_from(pid))
    ]


@pytest.mark.parametrize("text", FIXTURES.values(), ids=FIXTURES.keys())
def test_junction_fed_vertical_exit_lays_out_valid(text: str) -> None:
    _layout(text, validate=True)


@pytest.mark.parametrize(
    "fixture", ["tb_side_exit", "tb_side_exit_three_consumers", "bt_side_exit"]
)
def test_junction_fed_side_exit_stays_downstream_of_its_carrier(fixture: str) -> None:
    graph = _layout(FIXTURES[fixture], validate=False)
    exits = _junction_fed_vertical_side_exits(graph)
    assert exits
    for pid in exits:
        section = graph.sections[graph.ports[pid].section_id]
        sign = AxisFrame.flow_sign(section.direction)
        carrier_ys = [
            graph.stations[e.source].y
            for e in graph.edges_to(pid)
            if not graph.stations[e.source].is_port
        ]
        assert carrier_ys
        exit_y = graph.stations[pid].y
        assert all(sign * (exit_y - y) >= 0 for y in carrier_ys), (
            f"{pid} at y={exit_y} sits upstream of its carrier(s) at {carrier_ys}"
        )


def test_junction_fed_opposite_flow_seam_mirrors_like_a_direct_feed() -> None:
    """The BT feeder's RIGHT exit reaches the same-row TB consumer through a
    junction, and the consumer still mirrors it across the seam: its leading
    station sits level with the feeder's trailing one."""
    graph = _layout(FIXTURES["bt_side_exit"], validate=False)
    assert graph.stations["s2n0"].y == pytest.approx(graph.stations["s1n4"].y)


def _quadratic_corner_radii(svg: str) -> list[float]:
    radii = []
    for d in re.findall(r' d="([^"]*)"', svg):
        cur = (0.0, 0.0)
        for cmd, args in re.findall(r"([MLQ])([\d.\-,\s]+)", d):
            n = [float(v) for v in re.findall(r"-?[\d.]+", args)]
            if cmd == "Q":
                radii.append(
                    max(
                        abs(n[0] - cur[0]) + abs(n[1] - cur[1]),
                        abs(n[2] - n[0]) + abs(n[3] - n[1]),
                    )
                )
                cur = (n[2], n[3])
            else:
                cur = (n[0], n[1])
    return radii


@pytest.mark.parametrize("fixture", ["tb_side_exit", "bt_top_exit", "bt_side_exit"])
def test_junction_fed_branches_turn_at_full_radius(fixture: str) -> None:
    """Each single-line branch off the junction turns on a formed curve: a
    consumer entry seated a couple of pixels off the junction's run would
    force a sub-radius S-jog instead."""
    radii = _quadratic_corner_radii(render_string(FIXTURES[fixture]))
    assert radii
    assert min(radii) == pytest.approx(CURVE_RADIUS), sorted(radii)
