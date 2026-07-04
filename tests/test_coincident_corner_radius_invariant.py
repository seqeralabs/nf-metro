"""Same-line legs fused onto one channel must share their turn radius.

:func:`_coincide_same_line_tracks` snaps several same-line vertical legs onto
one reference X, so they read as a single stroke.  Each leg carries the
flanking-corner radius its own handler assigned: a plain solo leg gets the base
radius, while a leg that is the outer member of a concentric multi-line bundle
gets a wider one.  Where the fused legs share a turn vertex the two arcs draw
concentrically a few pixels apart -- a doubled corner.

``check_concentric_bundle_corners`` deliberately skips this case (it tests
*offset* bundle-mates, which nest by design); ``check_coincident_corner_radii``
covers it, and :func:`_unify_coincident_corner_radii` snaps every such shared
turn to the widest coincident radius so the fused stroke is one clean arc.

Covers:

* Corpus: no shipped fixture routes a same-line turn shared by two legs with
  unequal resolved radii.
* Meaningfulness: a hand-planted radius mismatch at a coincident corner is
  caught, so the corpus check is not vacuous.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from layout_validator import shared_same_line_turn_vertices

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import check_coincident_corner_radii
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
EXAMPLES = REPO_ROOT / "examples"

# Fixtures that route a same-line turn shared by two legs, so they genuinely
# exercise the coincident-corner unification rather than passing vacuously.
COINCIDENT_CORNER_FIXTURES = [
    "examples/longread_variant_calling.mmd",
    "examples/topologies/fanout_bundle_plus_spurs.mmd",
    "examples/topologies/merge_trunk_out_of_range_section.mmd",
    "tests/fixtures/target_entry_runway_bypass.mmd",
]


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted((EXAMPLES / "topologies").glob("*.mmd")))
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_doubled_coincident_corner(path: Path) -> None:
    """No same-line turn is shared by two legs with unequal resolved radii."""
    graph, routes, offsets = _route(path)
    violations = check_coincident_corner_radii(graph, routes, offsets)
    assert not violations, (
        f"{path.name}: {len(violations)} doubled coincident corner(s); "
        f"first: {violations[0].message()}"
    )


@pytest.mark.parametrize("fixture", COINCIDENT_CORNER_FIXTURES)
def test_named_fixtures_have_a_coincident_turn(fixture: str) -> None:
    """The named fixtures genuinely route a shared same-line turn.

    Guards the corpus sweep against silently going vacuous if a layout change
    stops these fixtures from fusing same-line legs onto a shared corner: with
    no such corner the unification has nothing to equalise and a passing
    ``test_no_doubled_coincident_corner`` would prove nothing here.
    """
    _graph, routes, _offsets = _route(REPO_ROOT / fixture)
    assert shared_same_line_turn_vertices(routes), (
        f"{fixture} no longer routes a coincident same-line turn"
    )


def _make_route(source: str, target: str, radius: float):
    """An L-shaped same-line route turning at (100, 100) with the given radius."""
    from nf_metro.layout.routing import OffsetRegime
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.parser.model import Edge

    return RoutedPath(
        edge=Edge(source=source, target=target, line_id="l"),
        line_id="l",
        points=[(0.0, 100.0), (100.0, 100.0), (100.0, 300.0)],
        is_inter_section=True,
        offset_regime=OffsetRegime.BAKED,
        curve_radii=[radius],
    )


def test_check_reports_unequal_radii_at_shared_turn() -> None:
    """Two same-line routes turning at one vertex with unequal radii are caught.

    Proves the corpus sweep is not vacuous: the check fires on a hand-built
    doubled corner and stays silent once the radii match.
    """
    from nf_metro.parser.model import MetroGraph

    graph = MetroGraph()
    mismatched = [_make_route("a", "x", 10.0), _make_route("b", "x", 18.0)]
    assert check_coincident_corner_radii(graph, mismatched, {})

    matched = [_make_route("a", "x", 14.0), _make_route("b", "x", 14.0)]
    assert not check_coincident_corner_radii(graph, matched, {})
