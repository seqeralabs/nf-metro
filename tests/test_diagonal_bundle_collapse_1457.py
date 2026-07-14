"""Multi-line bundles keep distinct slots on steep diagonals (#1457).

A bundle's per-line offset is applied along one axis, so on a diagonal the
perpendicular separation shrinks to ``OFFSET_STEP * cos(theta)`` as the descent
steepens past 45 degrees.  When several distinct lines drop together from a
section trunk to a lower exit-port row over too short a horizontal run, the
diagonal turns near-vertical and the lines fuse into one stroke -- which
:func:`assert_render_curve_invariants` treats as a render-aborting defect.

The #1457 repro is a valid four-section map whose source section spans all
three grid rows and feeds three vertically stacked downstream sections through
one right-exit port; the three exiting lines descend together over a bypass of
the in-section ``align`` terminal.  The remaining fixtures are gallery
topologies with their own multi-line diagonal descents, so the invariant
generalises beyond the repro.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import check_collinear_distinct_lines
from nf_metro.parser.mermaid import parse_metro_mermaid

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"

FIXTURES = [
    TOPOLOGIES / "multirow_source_stacked_fan.mmd",
    TOPOLOGIES / "fold_double.mmd",
    TOPOLOGIES / "tb_trunk_through_fan.mmd",
    TOPOLOGIES / "wide_fan_in.mmd",
]
IDS = [p.stem for p in FIXTURES]


@pytest.mark.parametrize("path", FIXTURES, ids=IDS)
def test_no_diagonal_collinear_overlay(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    violations = check_collinear_distinct_lines(
        graph, routes, offsets, scopes=("diagonal",)
    )
    assert not violations, "\n".join(v.message() for v in violations)
