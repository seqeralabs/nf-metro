"""A convergence sink placed above one of its feeder sections must slot its
entry bundle by feeder approach, not by line-declaration order (#1204).

When a section is placed above its convergence-sink target in the grid but its
line is declared last, the base offsets give it the bottom-most lane at the
shared entry port.  Its exit then sits above that lane, so the line runs down
through the inter-column gap to reach it -- a run that crosses its bundle-mates
and, in compact mode, lands in a gap with no declared channel and aborts the
render.

The crossing-free lane order at a LEFT-entry convergence is by feeder approach:
the feeder whose source sits highest takes the topmost lane.  These tests pin
that order and that the fixture renders without a curve abort.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import assert_render_curve_invariants
from nf_metro.parser.mermaid import parse_metro_mermaid

FIXTURE = (
    Path(__file__).parent.parent
    / "examples"
    / "topologies"
    / ("convergence_sink_above.mmd")
)


def _sink_lanes(graph, offsets) -> list[tuple[str, float, float]]:
    """Return ``[(line_id, lane_offset, source_y)]`` at the sink's LEFT port.

    ``source_y`` is the topmost source feeding each converging line.
    """
    port = next(s for s in graph.stations if s.startswith("sink__entry"))
    source_y: dict[str, float] = {}
    for edge in graph.edges_to(port):
        y = graph.stations[edge.source].y
        source_y[edge.line_id] = min(source_y.get(edge.line_id, y), y)
    return [(lid, offsets.get((port, lid), 0.0), source_y[lid]) for lid in source_y]


def test_convergence_lane_order_follows_source_position() -> None:
    """Lanes at the sink's entry port are ordered by feeder source Y: the
    feeder whose source sits highest takes the smallest (topmost) offset."""
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    lanes = _sink_lanes(graph, compute_station_offsets(graph))
    assert len(lanes) >= 2, "expected a multi-feeder convergence"
    by_offset = sorted(lanes, key=lambda r: r[1])
    source_ys = [src_y for _lid, _off, src_y in by_offset]
    assert source_ys == sorted(source_ys), (
        "lane order is not monotonic in feeder source Y: "
        f"{[(lid, round(off, 1), round(sy, 1)) for lid, off, sy in by_offset]}"
    )


def test_convergence_sink_above_renders() -> None:
    """The fixture lays out without the gap-channel curve abort."""
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    assert_render_curve_invariants(graph, routes, offsets)
