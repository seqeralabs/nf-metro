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
carries an offset-free top/side entry and stands in as the generalisation guard
that the reconciliation does not perturb the zero-offset case.
"""

from __future__ import annotations

import pytest

from nf_metro import api
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import (
    check_perp_entry_boundary_consistent,
    check_seam_segments_meet_at_port,
)

FIXTURES = [
    "examples/topologies/top_entry_bundle_offset_seam.mmd",
    "examples/topologies/fold_left_exit_right_entry.mmd",
]

REPRO = "examples/topologies/top_entry_bundle_offset_seam.mmd"


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
    from nf_metro.layout.routing.common import apply_route_offsets

    landing_x = apply_route_offsets(descent, offsets)[-1][0]
    assert landing_x == pytest.approx(port.x, abs=1.0)
