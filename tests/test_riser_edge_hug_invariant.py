"""Tests for the riser-hugs-section-edge invariant.

A TOP-entry lead-in that rises through the gap between two same-row sections
must climb the middle of the gap, not one wall.  When the minimal lead-in seats
the vertical riser one curve radius off the source box's exit edge, the line
renders as running up the outside of the box -- the ``riboseq_fold_two_dir_entry``
fold corner, where ``orf_calling -> psite_id`` climbed ``orf_calling``'s left edge.

Covers:

* Happy-path: every shipped topology and example routes with no riser hugging a
  section edge.
* Targeted: ``riboseq_fold_two_dir_entry`` seats its ``orf_calling -> psite_id``
  back-connection midway in the corridor between ``psite_id`` and ``orf_calling``.
* Meaningfulness: seating that same riser one curve radius off ``orf_calling``'s
  wall makes the checker report it, so the invariant genuinely encodes the bug.
  The hugging geometry is authored rather than coaxed out of the router, because
  several routing passes independently seat a riser clear of a wall and a proof
  of non-vacuity must not rest on defeating each of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    _min_riser_edge_clearance,
    check_no_riser_hugs_section_edge,
)
from nf_metro.parser.mermaid import parse_metro_mermaid_file
from nf_metro.parser.model import MetroGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIRS = (
    REPO_ROOT / "tests" / "fixtures" / "topologies",
    REPO_ROOT / "examples" / "topologies",
    REPO_ROOT / "examples",
)
RIBOSEQ = REPO_ROOT / "examples" / "topologies" / "riboseq_fold_two_dir_entry.mmd"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    for d in FIXTURE_DIRS:
        paths.extend(sorted(d.glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid_file(path)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_riser_hugs_section_edge_in_gallery(path: Path) -> None:
    """No TOP-entry riser climbs the outside of a section box in any fixture."""
    graph, routes, offsets = _route(path)
    violations = check_no_riser_hugs_section_edge(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def _riboseq_back_connection(
    graph: MetroGraph, routes: list[RoutedPath]
) -> tuple[RoutedPath, float, float]:
    """The ``orf_calling -> psite_id`` route and the corridor walls it rises in."""
    (route,) = [r for r in routes if r.edge.source == "orf_calling__exit_left_3"]
    psite = graph.sections["psite_id"]
    return route, psite.bbox_x + psite.bbox_w, graph.sections["orf_calling"].bbox_x


def test_riboseq_back_connection_rises_mid_corridor() -> None:
    """``orf_calling -> psite_id`` climbs the middle of the gap, not orf's edge."""
    graph, routes, offsets = _route(RIBOSEQ)
    route, gap_lo, gap_hi = _riboseq_back_connection(graph, routes)
    xs = {round(x, 1) for x, _y in route.points}
    riser_xs = [x for x in xs if gap_lo < x < gap_hi]
    assert riser_xs, "expected a riser in the psite/orf corridor"
    for x in riser_xs:
        assert (
            x - gap_lo >= _min_riser_edge_clearance()
            and gap_hi - x >= _min_riser_edge_clearance()
        ), f"riser at x={x} hugs a wall of gap [{gap_lo}, {gap_hi}]"


def test_checker_fires_on_a_wall_hugging_riser() -> None:
    """Seating the riboseq riser a curve radius off orf's wall makes the checker
    report it, proving the invariant is not vacuous."""
    graph, routes, offsets = _route(RIBOSEQ)
    route, gap_lo, gap_hi = _riboseq_back_connection(graph, routes)
    (riser_x,) = {x for x, _y in route.points if gap_lo < x < gap_hi}
    hugging = gap_hi - CURVE_RADIUS
    route.points = [
        (hugging if abs(x - riser_x) <= COORD_TOLERANCE else x, y)
        for x, y in route.points
    ]

    violations = check_no_riser_hugs_section_edge(graph, routes, offsets)

    assert [(v.line_id, v.section_id) for v in violations] == [("ribo", "orf_calling")]
    assert violations[0].clearance < _min_riser_edge_clearance()
