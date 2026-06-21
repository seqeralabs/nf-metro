"""Tests for the convergent-climb concentricity invariant (issue #910).

Two lines that leave the same trunk row for a shared off-row port must climb as
a concentric bundle: the diagonals stay parallel and any order change is settled
on the flat trunk run, not by the two colours crossing inside the diagonal.

Covers:

* Happy-path: every gallery fixture and example routes without a convergent
  climb crossing.
* Targeted: the ``convergent_offrow_exit_climb`` fixture's multi-carrier exit
  (``bam`` + ``other`` off the same trunk row into ``preprocessing__exit_right_0``)
  keeps a consistent horizontal order from the trunk corner to the port, with a
  direct geometric crossing test that fails without the concentric-climb pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.invariants import (
    _route_render_points,
    _segment_interior_crossing,
    check_convergent_climb_concentric,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
EXAMPLES = REPO_ROOT / "examples"
CONVERGENT_FIXTURE = EXAMPLES / "topologies" / "convergent_offrow_exit_climb.mmd"


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_convergent_climb_crossing_in_gallery(path: Path) -> None:
    """Every shipped fixture routes its convergent climbs concentrically.

    A multi-carrier off-row port whose climbing diagonals are left non-parallel
    surfaces here: the perpendicular spread splays a convergent pair in the
    wrong rotational order, so the two colours cross inside the diagonal.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_convergent_climb_concentric(graph, routes, offsets)
    assert violations == [], (
        f"{path.name}: {len(violations)} convergent climb crossing(s); "
        f"first: {violations[0].message() if violations else ''}"
    )


def _exit_climb(line_id: str) -> list[tuple[float, float]]:
    graph = parse_metro_mermaid(CONVERGENT_FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    for rp in routes:
        if (
            rp.line_id == line_id
            and rp.edge.target == "preprocessing__exit_right_0"
            and len(rp.points) == 4
        ):
            return _route_render_points(rp, offsets)
    raise AssertionError(f"no 4-point exit climb found for line {line_id!r}")


def test_multicarrier_exit_climb_is_concentric() -> None:
    """``bam`` and ``other`` climb the shared exit port without crossing.

    The two lines coincide on the trunk and must keep one horizontal order from
    the diagonal's corner through to the port flat run; a swap would mean the
    diagonals cross (the pre-fix defect).
    """
    bam = _exit_climb("bam")
    other = _exit_climb("other")

    # Order at the trunk corner (start of the diagonal) and at the port-side
    # flat (end of the diagonal) must agree in sign: no swap across the climb.
    corner = bam[1][0] - other[1][0]
    port = bam[2][0] - other[2][0]
    assert corner * port > 0, (
        f"bam/other swap order across the climb: corner delta {corner:.1f}, "
        f"port delta {port:.1f}"
    )

    # The diagonal legs themselves must not intersect in their interior.
    assert _segment_interior_crossing(bam[1], bam[2], other[1], other[2]) is None
