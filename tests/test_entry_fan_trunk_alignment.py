"""A section-entry port feeding multiple direct targets must sit flush with
whichever target is the fan's own trunk, not with an arbitrary predecessor.

When a single entry port fans out to more than one station directly (e.g. one
target carrying every line the port carries, plus one or more single-line
branches), the port's Y must match that trunk target's Y. Blindly copying the
Y of whichever upstream predecessor happens to be visited first is fragile: it
only looks right when that predecessor's row coincidentally lines up with the
fan's trunk, and drifts onto an unrelated row otherwise, forcing every
incoming line into a dogleg right at the section boundary.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

FIXTURE = Path(__file__).parent / "fixtures" / "target_entry_runway_bypass.mmd"

TOL = 2.0


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    return graph


def test_target_entry_port_flush_with_fan_trunk():
    graph = _layout()
    st = graph.stations

    section = graph.sections["target"]
    entry_ports = [
        pid
        for pid in section.entry_ports
        if (port := graph.ports.get(pid)) and port.side == PortSide.LEFT
    ]
    assert len(entry_ports) == 1, entry_ports
    port_y = st[entry_ports[0]].y

    # Station C carries both lines directly from the entry port (the fan's
    # trunk); Station A carries only one, so it is a branch.
    assert abs(st["sc"].y - port_y) < TOL, (
        f"entry port y={port_y} not flush with trunk station sc y={st['sc'].y}"
    )
    assert abs(st["sa"].y - port_y) > TOL, (
        f"branch station sa unexpectedly flush with entry port y={port_y}"
    )
