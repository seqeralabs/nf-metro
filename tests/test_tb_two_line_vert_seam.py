"""A LEFT/RIGHT entry into a vertical-flow section enters level, then drops.

When a vertical-flow (TB/BT) section's LEFT/RIGHT exit feeds another
vertical-flow section's LEFT/RIGHT entry with a multi-line bundle, the entry is
perpendicular to the destination trunk.  Pinning the entry port to its
consumer's own Y (the trunk head) leaves no vertical drop room, so the
staggered (non-zero-offset) line slants diagonally into the trunk instead of
entering horizontally and then dropping onto its lane (#1054).

The fix seats such an entry a station gap above the trunk head.  Encoded two
ways: the targeted fixture's entry edges are axis-aligned (a horizontal lead-in
then a vertical drop, never a shallow slant), and across every topology fixture
no LEFT/RIGHT entry port into a vertical-flow section shares a Y with an
internal station of that section (which would route the line through the marker
and rob the turn-in of drop room).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import lanes_run_along_x
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"
TOPOLOGY_FILES = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
TOPOLOGY_IDS = [f.stem for f in TOPOLOGY_FILES]

_AXIS_TOL = 0.5


def _internal_station_ys(graph, section_id: str) -> list[float]:
    section = graph.sections[section_id]
    ports = set(section.entry_ports) | set(section.exit_ports)
    return [
        st.y
        for sid in section.station_ids
        if sid not in ports and (st := graph.stations.get(sid)) is not None
    ]


def test_two_line_vert_seam_enters_level_then_drops() -> None:
    """Each two-line entry segment is axis-aligned, never a shallow slant."""
    text = (TOPOLOGIES_DIR / "tb_two_line_vert_seam.mmd").read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)

    port = graph.stations["down_sec__entry_left_1"]
    head = graph.stations["d1"]
    assert port.y < head.y - _AXIS_TOL, (
        f"entry port y={port.y:.1f} must sit above the trunk head d1 "
        f"y={head.y:.1f} for the turn-in to have drop room"
    )

    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    entry_routes = [
        r
        for r in routes
        if r.edge.source == "down_sec__entry_left_1" and r.edge.target == "d1"
    ]
    assert entry_routes, "no entry lead-in routes found"
    for route in entry_routes:
        pts = apply_route_offsets(route, offsets)
        for k in range(len(pts) - 1):
            dx = abs(pts[k + 1][0] - pts[k][0])
            dy = abs(pts[k + 1][1] - pts[k][1])
            assert dx <= _AXIS_TOL or dy <= _AXIS_TOL, (
                f"entry segment {k} (line={route.line_id}) is a slant: "
                f"dx={dx:.1f}, dy={dy:.1f}; the lead-in must be horizontal and "
                f"the drop vertical"
            )


@pytest.mark.parametrize("path", TOPOLOGY_FILES, ids=TOPOLOGY_IDS)
def test_perp_entry_into_vertical_section_clears_trunk_head(path: Path) -> None:
    """No LEFT/RIGHT entry into a vertical-flow section sits on an internal row."""
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    for pid, port in graph.ports.items():
        if not port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if section is None or not lanes_run_along_x(section.direction):
            continue
        port_y = graph.stations[pid].y
        for sy in _internal_station_ys(graph, section.id):
            assert abs(port_y - sy) > _AXIS_TOL, (
                f"{pid} (y={port_y:.1f}) shares Y with an internal station of "
                f"{section.id} (y={sy:.1f}); the line would route through the "
                f"marker with no room for a level turn-in"
            )
