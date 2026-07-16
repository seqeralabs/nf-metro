"""Runtime guard: an entry port never sits opposite the stations it feeds (#1363).

`_guard_entry_port_not_opposite_targets` fails loudly when a flow-axis entry is
placed on the section's flow-END edge while the consumers it feeds cluster
toward the flow-START edge -- the fold-back a contradictory entry hint produced
before the collapse rules. The synthetic cases prove the guard is not a no-op:
it fires on a hand-built opposite-edge graph and stays silent on the legitimate
shapes it must not flag (flow-start entry, reversed flow entered on its own
start edge, a perpendicular drop-in).
"""

from __future__ import annotations

import warnings

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import line_forks_within_section
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_entry_port_not_opposite_targets,
)
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station


def _section_with_entry(
    direction: str, port_side: PortSide, port_x: float, consumer_x: float
) -> MetroGraph:
    """One horizontal-flow section spanning x in [0, 200] with a single entry.

    The consumer station sits at ``consumer_x`` inside the box; the entry port
    sits at ``port_x`` on ``port_side`` and feeds the consumer.
    """
    graph = MetroGraph()
    section = Section(id="s", name="s", direction=direction)
    section.bbox_x, section.bbox_y, section.bbox_w, section.bbox_h = (
        0.0,
        0.0,
        200.0,
        100.0,
    )
    section.station_ids = ["c", "p"]
    section.entry_ports = ["p"]
    graph.sections["s"] = section
    graph.stations["c"] = Station(
        id="c", label="C", section_id="s", x=consumer_x, y=50.0
    )
    graph.stations["p"] = Station(
        id="p", label="", section_id="s", is_port=True, x=port_x, y=50.0
    )
    graph.ports["p"] = Port(
        id="p", section_id="s", side=port_side, is_entry=True, x=port_x, y=50.0
    )
    graph.edges = [Edge(source="p", target="c", line_id="x")]
    return graph


def test_guard_fires_on_flow_end_entry_opposite_consumers() -> None:
    """An LR section entered on its RIGHT (flow-end) edge feeding a left-clustered
    consumer trips the guard -- the line would double back over the flow."""
    graph = _section_with_entry("LR", PortSide.RIGHT, port_x=200.0, consumer_x=20.0)
    with pytest.raises(PhaseInvariantError, match="opposite|flow-END|double back"):
        _guard_entry_port_not_opposite_targets(graph, "test")


def test_guard_silent_on_flow_start_entry() -> None:
    """An LR section entered on its LEFT (flow-start) edge feeding a downstream
    consumer is legitimate: the line enters at the start and flows with it."""
    graph = _section_with_entry("LR", PortSide.LEFT, port_x=0.0, consumer_x=180.0)
    _guard_entry_port_not_opposite_targets(graph, "test")


def test_guard_silent_on_reversed_flow_own_start_edge() -> None:
    """A reversed-flow RL section entered on its RIGHT (its own flow-start) edge
    stays valid -- the guard is target-relative, not a blanket ban on RIGHT."""
    graph = _section_with_entry("RL", PortSide.RIGHT, port_x=200.0, consumer_x=180.0)
    _guard_entry_port_not_opposite_targets(graph, "test")


def test_guard_silent_on_perpendicular_entry() -> None:
    """A TOP entry into an LR section is a perpendicular drop-in, not a flow-axis
    entry, so it is out of scope even when it feeds a far consumer."""
    graph = _section_with_entry("LR", PortSide.LEFT, port_x=0.0, consumer_x=20.0)
    graph.ports["p"].side = PortSide.TOP
    graph.stations["p"].x = 100.0
    graph.stations["p"].y = 0.0
    graph.ports["p"].x, graph.ports["p"].y = 100.0, 0.0
    _guard_entry_port_not_opposite_targets(graph, "test")


def test_serpentine_validates_without_permissive() -> None:
    """The #1363 integration fixture lays out with validate=True (no abort).

    The packed serpentine grid braids all the entry-collapse and sibling-fix
    idioms at once; a clean validate run certifies the entry hints collapse to
    fed, hinted sides with no line folding back over its own track in sec_g.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(
            open("examples/topologies/packed_multiline_serpentine_grid.mmd").read()
        )
        compute_layout(graph, validate=True)


def test_forked_corridor_solo_anchors_entry_leaves_fan_to_straddle() -> None:
    """A forked single-line corridor-solo anchors its entry port on the trunk
    lane but leaves the fan branches free to straddle their fork origin.

    sec_e carries only l1 but forks e3 -> {e4, e5}.  The corridor re-anchor
    pins the entry port to offset 0 (so it is not dragged onto an upstream
    lane), while the fork arms e4/e5 straddle their fork origin e3 symmetrically
    -- they are not forced onto offset 0, which would collapse the fan.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(
            open("examples/topologies/packed_multiline_serpentine_grid.mmd").read()
        )
        compute_layout(graph, validate=True)
    sec_e = graph.sections["sec_e"]
    assert line_forks_within_section(graph, sec_e, "l1")
    (port_id,) = sec_e.entry_ports
    offsets = compute_station_offsets(graph)
    assert abs(offsets.get((port_id, "l1"), 0.0)) < 1.0, (
        "entry port not anchored to trunk"
    )
    fork_y = graph.stations["e3"].y
    e4_y, e5_y = graph.stations["e4"].y, graph.stations["e5"].y
    assert min(e4_y, e5_y) < fork_y < max(e4_y, e5_y), (
        f"fan arms e4={e4_y} e5={e5_y} do not straddle fork origin e3={fork_y}"
    )
    assert abs((e4_y - fork_y) + (e5_y - fork_y)) < 2.0, (
        f"fan arms not symmetric about fork origin {fork_y}: e4={e4_y} e5={e5_y}"
    )
