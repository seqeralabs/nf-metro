"""A bundle's radius set is shared across all its turns, not derived per corner.

Issue #1958.  A concentric bundle of ``n`` lines nesting one lane-step apart
draws one radius set at every 90-degree turn it makes together -- the innermost
line of that turn at ``CURVE_RADIUS`` and one step wider for each line further
out -- and only flips the inner/outer assignment as the turn direction changes.
The defect these tests lock is a bundle turning twice into a shared entry port
(right->down then down->right) that sizes each corner's reference independently,
seating two lines below the ``CURVE_RADIUS`` floor at one turn so the two turns
draw genuinely different radius sets rather than one set permuted between them.

The repro is a 3-line bundle (riboseq/rnaseq/tiseq) descending into MultiQC's
LEFT entry port.  The targeted tests pin both properties on that bundle;
``check_bundle_corner_radius_floor`` enforces the floor corollary corpus-wide
(the shared-vocabulary property itself does not yet hold corpus-wide -- other
passes still size a bundle's reference per corner, the remainder of #1958).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import check_bundle_corner_radius_floor
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REPRO = FIXTURES / "topologies" / "shared_entry_bundle_order_repro.mmd"
REPRO_PORT = "reporting__entry_left_7"
REPRO_LINES = {"riboseq", "rnaseq", "tiseq"}


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    graph.source_dir = str(path.parent)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


def test_repro_bundle_shares_one_radius_vocabulary_across_its_turns() -> None:
    """Every corner of the shared-entry bundle draws the same radius set.

    The defect seats the right->down corner at ``{2, 6, 10}`` -- two lines below
    the ``CURVE_RADIUS`` floor -- while the other corners hold ``{10, 14, 18}``.
    Each corner is asserted to be a permutation of one shared set.
    """
    _graph, routes, _offsets = _route(REPRO)
    bundle = [
        route
        for route in routes
        if route.edge.target == REPRO_PORT and route.line_id in REPRO_LINES
    ]
    assert {route.line_id for route in bundle} == REPRO_LINES

    corner_sets = set()
    for route in bundle:
        radii = resolve_curve_radii(route.points, route.curve_radii)
        assert all(r >= CURVE_RADIUS - 1.0 for r in radii), (
            f"{route.line_id} seats a corner below CURVE_RADIUS: {radii}"
        )
    per_corner: list[tuple[float, ...]] = []
    length = len(bundle[0].curve_radii or [])
    radii_by_line = {
        route.line_id: resolve_curve_radii(route.points, route.curve_radii)
        for route in bundle
    }
    for corner in range(length):
        per_corner.append(
            tuple(sorted(round(radii_by_line[line][corner], 1) for line in REPRO_LINES))
        )
    corner_sets = set(per_corner)
    assert len(corner_sets) == 1, (
        f"bundle corners draw differing radius sets: {sorted(corner_sets)}"
    )


def test_repro_bundle_passes_the_radius_floor_oracle() -> None:
    """The floor oracle finds no sub-floor bundle corner on the repro."""
    graph, routes, offsets = _route(REPRO)
    violations = check_bundle_corner_radius_floor(graph, routes, offsets)
    assert violations == [], "\n".join(v.message() for v in violations)


def _corpus_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    paths.extend(sorted((FIXTURES / "topologies").glob("*.mmd")))
    return paths


@pytest.mark.parametrize("fixture", _corpus_fixtures(), ids=lambda p: p.stem)
def test_no_shipped_fixture_seats_a_bundle_corner_below_the_floor(
    fixture: Path,
) -> None:
    """No shipped map seats a bundle-run corner below ``CURVE_RADIUS``.

    Parametrised over the whole corpus so RIGHT-side and TB/BT bundles reaching
    this code path are covered without a hand-mirrored fixture per orientation.
    """
    graph, routes, offsets = _route(fixture)
    violations = check_bundle_corner_radius_floor(graph, routes, offsets)
    assert violations == [], "\n".join(v.message() for v in violations)
