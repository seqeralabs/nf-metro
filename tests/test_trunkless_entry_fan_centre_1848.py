"""A trunkless multi-line entry-port fan, and the join its arms reconverge on,
both sit on the fan's served-span centreline.

When a section's single boundary entry port fans directly to several internal
targets with no unique trunk arm (no target carries every port line, no single
arm continues to the exit while the rest dead-end), ``center_ports`` has no 1:1
crossing to align the port against, so it must land on the midpoint of the span
of targets it serves rather than pinned to the topmost row.

The station where those fan arms reconverge is likewise the fan's centreline
join under ``diamond_style: symmetric``; it belongs on that same midpoint
instead of on whatever grid row its topological layer happened to occupy. The
arms reach the join through an intermediate relay, so the join's source set is
those relays rather than the port's direct targets, which is why the exact
fork-hub/join-source-set diamond detection misses this shape (issue #1848).
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / "trunkless_entry_fan_reconverge_centre.mmd"
)

TOL = 2.0


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    return graph


def _served_span_midpoint(graph, port_id, section):
    """Midpoint of the Y span of the port's direct in-section target stations."""
    ys = {
        target.y
        for edge in graph.edges_from(port_id)
        if not (target := graph.station_for_edge_target(edge)).is_port
        and target.section_id == section.id
    }
    return (min(ys) + max(ys)) / 2.0


def test_trunkless_entry_port_on_served_span_midpoint():
    graph = _layout()
    section = graph.sections["calling"]
    port_id = next(iter(section.entry_ports))

    midpoint = _served_span_midpoint(graph, port_id, section)
    port_y = graph.stations[port_id].y
    assert abs(port_y - midpoint) < TOL, (
        f"entry port y={port_y} not on served-span midpoint {midpoint}"
    )


def test_reconvergence_join_on_fan_centreline():
    graph = _layout()
    section = graph.sections["calling"]
    port_id = next(iter(section.entry_ports))

    midpoint = _served_span_midpoint(graph, port_id, section)
    merge_y = graph.stations["merge"].y
    assert abs(merge_y - midpoint) < TOL, (
        f"reconvergence join merge y={merge_y} not on fan centreline {midpoint}"
    )
