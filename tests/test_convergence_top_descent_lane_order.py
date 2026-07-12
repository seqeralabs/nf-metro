"""A line descending from a section above into a shared LEFT entry port must
take the topmost lane, not dive under a same-row left feeder (#1410).

This is the non-compact counterpart of the compact convergence-sink ordering
locked by ``test_convergence_sink_above_lane_order`` (#1204).  A section fed
by two feeders at one LEFT entry port -- one arriving from a row above (a top
descent) and one arriving level from the row to its left -- slots the bundle by
line-declaration order by default.  When the descending line is declared last
it lands on the bottom lane, so it crosses under the left feeder at the
boundary and reads as the lower stroke through every internal branch.

The crossing-free lane order at such a convergence is by feeder source Y: the
feeder whose source sits highest takes the topmost (smallest-offset) lane.  The
junction variant exercises the same ordering when the descending feeder reaches
the port through a fan-out junction (as the riboseq map's ``te`` section does),
whose section is undefined and so is invisible to a distinct-section feeder
count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import assert_render_curve_invariants
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"
FIXTURES = [
    "top_descent_over_left_entry.mmd",
    "top_descent_over_left_entry_junction.mmd",
]


def _target_lanes(graph, offsets) -> list[tuple[str, float, float]]:
    """Return ``[(line_id, lane_offset, source_y)]`` at the target LEFT port.

    ``source_y`` is the topmost source feeding each converging line.
    """
    port = next(s for s in graph.stations if s.startswith("target__entry"))
    source_y: dict[str, float] = {}
    for edge in graph.edges_to(port):
        y = graph.stations[edge.source].y
        source_y[edge.line_id] = min(source_y.get(edge.line_id, y), y)
    return [(lid, offsets.get((port, lid), 0.0), source_y[lid]) for lid in source_y]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_top_descent_takes_top_lane(fixture: str) -> None:
    """Lanes at the target's entry port are ordered by feeder source Y: the
    descending feeder (highest source) takes the smallest (topmost) offset."""
    graph = parse_metro_mermaid((TOPOLOGIES / fixture).read_text())
    compute_layout(graph)
    lanes = _target_lanes(graph, compute_station_offsets(graph))
    assert len(lanes) >= 2, "expected a multi-feeder convergence"
    by_offset = sorted(lanes, key=lambda r: r[1])
    source_ys = [src_y for _lid, _off, src_y in by_offset]
    assert source_ys == sorted(source_ys), (
        "lane order is not monotonic in feeder source Y: "
        f"{[(lid, round(off, 1), round(sy, 1)) for lid, off, sy in by_offset]}"
    )


@pytest.mark.parametrize("fixture", FIXTURES)
def test_top_descent_fixture_renders(fixture: str) -> None:
    """The fixtures lay out without a curve abort."""
    graph = parse_metro_mermaid((TOPOLOGIES / fixture).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    assert_render_curve_invariants(graph, routes, offsets)
