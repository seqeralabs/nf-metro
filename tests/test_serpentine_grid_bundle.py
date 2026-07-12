"""Multi-line bundles fold cleanly through an L->R section-grid serpentine.

A 2+-line bundle that wraps from one section row to the next via the L->R
section-grid fold-back (exit the right of row *N*, re-enter the left of row
*N+1*) keeps its lines fanned on every leg of the wrap, on grids larger than
2x2:

* ``serpentine_grid_wide_bundle`` -- the wrap spans more than one column; its
  vertical channels stay fanned across the multi-column return.
* ``serpentine_grid_tall_bundle`` -- a second wrap stacks below the first; the
  two wraps occupy distinct inter-row gaps, so their return runs stay on
  separate channels.

See issue #1192 (companion #1193 for the ``direction: RL`` true-serpentine).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    assert_render_curve_invariants,
    check_bundle_order_preserved,
    check_collinear_distinct_lines,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

FIXTURES = [
    "serpentine_grid_wide_bundle",
    "serpentine_grid_tall_bundle",
]


def _laid_out(stem: str):
    graph = parse_metro_mermaid((TOPOLOGIES / f"{stem}.mmd").read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, offsets, routes


@pytest.mark.parametrize("stem", FIXTURES)
def test_serpentine_grid_bundle_renders_clean(stem):
    """The folded multi-line bundle draws without a curve defect."""
    graph, offsets, routes = _laid_out(stem)
    assert_render_curve_invariants(graph, routes, offsets)


@pytest.mark.parametrize("stem", FIXTURES)
def test_serpentine_grid_bundle_no_collinear_overlay(stem):
    """No two distinct lines of the wrap coincide on a shared channel."""
    graph, offsets, routes = _laid_out(stem)
    assert not check_collinear_distinct_lines(graph, routes, offsets, scopes=("inter",))


@pytest.mark.parametrize("stem", FIXTURES)
def test_serpentine_grid_bundle_corners_concentric(stem):
    """Every wrap corner nests concentrically and preserves bundle order."""
    graph, offsets, routes = _laid_out(stem)
    assert not check_concentric_bundle_corners(graph, routes, offsets)
    assert not check_bundle_order_preserved(routes)
