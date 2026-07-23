"""A top-entry line carrying a bundle offset must meet its port at one X.

When a line splits off a shared multi-line trunk it carries a non-zero
within-bundle offset.  If that line then drops into a section through a
``entry: top`` port, its inter-section descent and the section's intra-section
drop share that single port marker.  The descent must therefore taper the
inbound offset away before the boundary and land on the port's own X; keeping
the offset parts the two legs at the top edge by that offset -- a boundary
jitter where the stroke steps sideways as it crosses the section's top edge.

``top_entry_bundle_offset_seam`` is the committed minimal fixture: line ``b``
splits off the ``a,b,c`` trunk at a junction (giving it a non-zero offset) and
drops into ``dst`` through its ``entry: top`` port.  ``fold_left_exit_right_entry``
carries an offset-free top/side entry and guards the zero-offset case: an entry
with no inbound bundle offset must land directly on the port's X.
"""

from __future__ import annotations

import pytest

from nf_metro import api
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.layout.routing.invariants import (
    check_perp_entry_boundary_consistent,
    check_seam_segments_meet_at_port,
)

FIXTURES = [
    "examples/topologies/top_entry_bundle_offset_seam.mmd",
    "examples/topologies/fold_left_exit_right_entry.mmd",
    "examples/topologies/straight_drop_below.mmd",
]

REPRO = "examples/topologies/top_entry_bundle_offset_seam.mmd"

# A junction feeding a TOP entry port directly below it, in a column that
# clears both gap walls: the drop descends as one constant-X vertical into the
# port, with no lateral lead-out-and-jog straddling the section boundary.
STRAIGHT_DROP = "examples/topologies/straight_drop_below.mmd"


def _route(path: str):
    graph = api.prepare_graph(open(path).read())
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize("path", FIXTURES)
def test_seams_meet_at_port(path: str) -> None:
    graph, routes, offsets = _route(path)
    gaps = check_seam_segments_meet_at_port(graph, routes, offsets)
    assert not gaps, "\n".join(g.message() for g in gaps)


@pytest.mark.parametrize("path", FIXTURES)
def test_perp_entry_boundary_consistent(path: str) -> None:
    graph, routes, _offsets = _route(path)
    violations = check_perp_entry_boundary_consistent(graph, routes)
    assert not violations, "\n".join(v.message() for v in violations)


def test_top_entry_descent_lands_on_port_x() -> None:
    """The descent into ``dst``'s top port ends at the port's own X.

    Line ``b`` reaches the port through ``s1 -> d1`` (its offset-bearing
    inter-section descent).  With the offset tapered away its final vertical
    leg shares the port marker's X, so the intra-section drop out of the port
    continues the same stroke.
    """
    graph, routes, offsets = _route(REPRO)
    port = graph.ports["dst__entry_top_3"]
    descent = next(
        r for r in routes if r.line_id == "b" and r.edge.target == "dst__entry_top_3"
    )
    landing_x = apply_route_offsets(descent, offsets)[-1][0]
    assert landing_x == pytest.approx(port.x, abs=1.0)


def test_straight_drop_below_is_one_vertical_run() -> None:
    """A junction's drop into the port directly below is one vertical run.

    The branch reaches ``bottom__entry_top_1`` straight below its feeding
    junction.  Every point from where the descent turns vertical down to the
    port shares the port's X, so the line enters the TOP port from directly
    above -- no lateral lead-out to a parallel channel and jog back onto the
    port marker.
    """
    graph, routes, offsets = _route(STRAIGHT_DROP)
    port = graph.ports["bottom__entry_top_1"]
    drop = next(
        r
        for r in routes
        if r.line_id == "branch" and r.edge.target == "bottom__entry_top_1"
    )
    pts = apply_route_offsets(drop, offsets)
    assert pts[-1] == pytest.approx((port.x, port.y), abs=1.0)
    # From the first point on the port's column, the run stays on that column.
    descent = [p for p in pts if p[0] == pytest.approx(port.x, abs=1.0)]
    assert len(descent) >= 2
    assert all(x == pytest.approx(port.x, abs=1.0) for x, _ in descent)
    # No point sits on the far side of the port's column (an out-and-back).
    assert max(x for x, _ in pts) <= port.x + 1.0
