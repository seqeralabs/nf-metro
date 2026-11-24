"""Same-line legs fused onto one channel must share their turn radius.

:func:`_coincide_same_line_tracks` snaps several same-line vertical legs onto
one reference X, so they read as a single stroke.  Each leg carries the
flanking-corner radius its own handler assigned: a plain solo leg gets the base
radius, while a leg that is the outer member of a concentric multi-line bundle
gets a wider one.  Where the fused legs share a turn vertex the two arcs draw
concentrically a few pixels apart -- a doubled corner.

``check_concentric_bundle_corners`` deliberately skips this case (it tests
*offset* bundle-mates, which nest by design); ``check_coincident_corner_radii``
covers it. Planning freezes owned cohorts through
:func:`_unify_coincident_corner_radii`, while the post-emission call settles
only wholly unowned cohorts.

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
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import check_coincident_corner_radii
from nf_metro.layout.routing.normalize import _unify_coincident_corner_radii
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


def test_unify_uses_widest_radius_all_shared_legs_can_resolve() -> None:
    """A short lead limits the common radius instead of drawing a double arc."""
    from nf_metro.parser.model import MetroGraph

    short = _make_route("short", "x", 22.0)
    long = _make_route("long", "x", 22.0)
    short.points[0] = (80.0, 100.0)
    long.points[0] = (70.0, 100.0)
    routes = [short, long]
    graph = MetroGraph()

    assert [
        resolve_curve_radii(route.points, route.curve_radii)[0] for route in routes
    ] == [20.0, 22.0]
    assert check_coincident_corner_radii(graph, routes, {})

    _unify_coincident_corner_radii(routes)

    assert [
        resolve_curve_radii(route.points, route.curve_radii)[0] for route in routes
    ] == pytest.approx([20.0, 20.0])
    assert not check_coincident_corner_radii(graph, routes, {})


def test_unify_refreshes_a_shared_route_after_changing_one_corner() -> None:
    """Chained corners on one route settle within a single call.

    ``shared`` carries two coincident turns 30px apart, so its two corners
    compete for one segment budget: equalising the near corner against ``wide``
    changes what the far corner can draw against ``peer``.  A repeat call must
    be inert -- if it still moves a radius, the first call left the buckets
    disagreeing over that budget and the emitted arcs are a doubled corner
    apart.
    """
    shared = _make_route("shared", "x", 22.0)
    shared.points = [
        (0.0, 100.0),
        (100.0, 100.0),
        (100.0, 130.0),
        (200.0, 130.0),
    ]
    shared.curve_radii = [6.0, 22.0]

    wide = _make_route("wide", "x", 22.0)
    wide.points = [(0.0, 100.0), (100.0, 100.0), (100.0, 400.0)]

    peer = _make_route("peer", "x", 22.0)
    peer.points = [(0.0, 130.0), (100.0, 130.0), (100.0, 400.0)]

    routes = [peer, wide, shared]
    _unify_coincident_corner_radii(routes)

    assert shared.curve_radii == pytest.approx([8.0, 22.0])
    assert wide.curve_radii == pytest.approx([8.0])
    assert peer.curve_radii == pytest.approx([22.0])

    settled = [list(route.curve_radii or []) for route in routes]
    _unify_coincident_corner_radii(routes)
    assert [list(route.curve_radii or []) for route in routes] == settled


def test_owned_coincident_cohort_settles_only_during_planning() -> None:
    """Member, convergence, and exit ownership share one frozen corner."""
    member = _make_route("member", "x", 10.0)
    member.route_system_owned_segment_ranks = (0,)
    convergence = _make_route("convergence", "x", 14.0)
    convergence.convergence_owned_segment_ranks = (0,)
    exit_owned = _make_route("exit", "x", 12.0)
    exit_owned.exit_turn_axis_id = "axis"
    exit_owned.exit_turn_segment_rank = 1
    routes = [member, convergence, exit_owned]

    before = [route.curve_radii[:] for route in routes if route.curve_radii]
    _unify_coincident_corner_radii(routes)
    assert [route.curve_radii for route in routes] == before

    _unify_coincident_corner_radii(routes, include_owned=True)

    assert [route.curve_radii for route in routes] == [[14.0], [14.0], [14.0]]
    assert [route.concentric_corner_offsets_by_segment[1][0] for route in routes] == [
        0.0,
        0.0,
        0.0,
    ]
    assert [route.concentric_corner_bases_by_segment[1][0] for route in routes] == [
        14.0,
        14.0,
        14.0,
    ]
