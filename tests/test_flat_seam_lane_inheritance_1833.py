"""A flat inter-section seam must not carry an offset-sized lane jog (#1833).

When a single line crosses a flat (same-Y) section boundary, the receiving
section must meet it on the exact lane its feeder rides.  The reactive
trunk-anchoring path top-anchored such a lone entry to offset 0 regardless of
its feeder's lane, drawing an offset-sized diagonal at the seam where the line
should run straight through.  A corridor (off-Y) feeder is excluded: its
vertical leg absorbs the lane step with no jog, and the lone consumer then rides
the trunk by a separate invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import SAME_Y_TOLERANCE, graph_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import flow_port_sides
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

FIXTURES = Path(__file__).parent / "fixtures"
TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

# Committed maps whose flow-axis entry receives a single line across a flat seam
# from a feeder riding a non-trunk lane.
CASES = [
    FIXTURES / "curve_invariant_repros" / "riboseq_inter_row_corridor.mmd",
    TOPOLOGIES / "junction_entry_align.mmd",
]


def _flat_seam_jogs(path: Path) -> list[tuple[str, str, float]]:
    """``(entry_port, line, jog)`` for flat single-line crossings that step."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, x_spacing=70, validate=False)
    offsets = compute_station_offsets(graph)
    step = graph_offset_step(graph)
    jogs: list[tuple[str, str, float]] = []
    for pid, port in graph.ports.items():
        section = graph.sections.get(port.section_id)
        if section is None or not port.is_entry:
            continue
        flow_entry, _flow_exit = flow_port_sides(section.direction)
        if port.side is not flow_entry:
            continue
        lines = graph.station_lines(pid)
        if len(lines) != 1:
            continue
        line_id = lines[0]
        port_y = graph.stations[pid].y
        feeders = [
            edge.source for edge in graph.edges_to(pid) if edge.source in graph.stations
        ]
        if not feeders or any(
            abs(graph.stations[src].y - port_y) > SAME_Y_TOLERANCE for src in feeders
        ):
            continue
        entry_offset = offsets.get((pid, line_id), 0.0)
        for src in feeders:
            feeder_offset = offsets.get((src, line_id), 0.0)
            jog = abs(entry_offset - feeder_offset)
            if step / 2 <= jog:
                jogs.append((pid, line_id, round(jog, 2)))
    return jogs


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_flat_seam_single_line_crossing_is_level(path: Path) -> None:
    assert _flat_seam_jogs(path) == []
