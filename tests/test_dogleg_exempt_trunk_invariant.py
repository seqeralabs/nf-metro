"""Tests for the reluctant-unbundling dogleg-off-exempt-trunk invariant.

A non-exempt bypass trunk cleared off an ``normalize_exempt`` run of a
different line must land on the side that keeps the two parallel.  Cleared to
the wrong side the movable trunk's riser pierces the exempt run -- and the
exempt riser pierces the movable run -- so the two colours cross twice instead
of running as a tight parallel bundle (issue #702).

Covers:

* Happy-path: every gallery example and topology fixture (including
  ``dogleg_exempt_distinct``, the reported defect) routes without a doglegged
  trunk crossing the exempt run it bundles with.
* Meaningfulness: the checker flags the reported crossing geometry and clears
  the parallel one, so the invariant genuinely encodes the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    check_no_dogleg_crosses_exempt_trunk,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
EXAMPLE_TOPOLOGIES = EXAMPLES / "topologies"
FIXTURE_TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
CURVE_REPROS = REPO_ROOT / "tests" / "fixtures" / "curve_invariant_repros"

# Named one by one rather than swept: the hash-seed corpus is a hundred fuzz
# renders, several of which fail unrelated guards, so only the seeds whose
# dogleg geometry is locked belong here.
EXTRA_FIXTURES = (
    REPO_ROOT / "tests" / "fixtures" / "hash_seed_determinism" / "seed_72.mmd",
)


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    paths.extend(sorted(EXAMPLES.glob("*.mmd")))
    paths.extend(sorted(EXAMPLE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(sorted(FIXTURE_TOPOLOGIES.glob("*.mmd")))
    paths.extend(EXTRA_FIXTURES)
    return paths


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    return graph, routes, offsets


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_dogleg_crosses_exempt_trunk_in_gallery(path: Path) -> None:
    """Every shipped example and topology clears a doglegged trunk to the side
    that keeps it parallel to the exempt run, never the side that crosses it."""
    graph, routes, offsets = _route(path)
    violations = check_no_dogleg_crosses_exempt_trunk(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


# Geometry lifted from the reported ``dogleg_exempt_distinct`` render: a blue
# exempt ``wrap`` trunk runs leftward at y=196; the red ``byp`` trunk bundles
# with it in the same inter-row channel.  Below it (y=199) byp's left riser
# pierces wrap's run and wrap's right riser pierces byp's run -- two crossings;
# above it (y=193) byp clears wrap entirely.
_WRAP = [
    (400.0, 298.0),
    (416.0, 298.0),
    (416.0, 196.0),
    (14.0, 196.0),
    (14.0, 120.0),
    (30.0, 120.0),
]
_BYP_BELOW = [
    (190.0, 120.0),
    (209.0, 120.0),
    (209.0, 199.0),
    (419.0, 199.0),
    (419.0, 298.0),
    (450.0, 298.0),
]
_BYP_ABOVE = [
    (190.0, 120.0),
    (209.0, 120.0),
    (209.0, 193.0),
    (419.0, 193.0),
    (419.0, 298.0),
    (450.0, 298.0),
]


def _routes(byp_points: list[tuple[float, float]]) -> list[RoutedPath]:
    return [
        RoutedPath(
            edge=Edge("rs2", "lt1", "wrap"),
            line_id="wrap",
            points=_WRAP,
            is_inter_section=True,
            normalize_exempt=True,
        ),
        RoutedPath(
            edge=Edge("lt2", "bs1", "byp"),
            line_id="byp",
            points=byp_points,
            is_inter_section=True,
        ),
    ]


def test_checker_flags_crossing_dogleg() -> None:
    """The checker fires when the movable trunk sits on the crossing side."""
    violations = check_no_dogleg_crosses_exempt_trunk(None, _routes(_BYP_BELOW), {})
    assert violations, "expected a dogleg crossing when byp runs below wrap"
    assert violations[0].line_id == "byp"
    assert violations[0].exempt_line == "wrap"


def test_checker_passes_parallel_dogleg() -> None:
    """The checker stays silent when the trunk clears to the parallel side."""
    violations = check_no_dogleg_crosses_exempt_trunk(None, _routes(_BYP_ABOVE), {})
    assert not violations, "parallel bundle above the exempt run must not flag"


# A movable trunk that originates above the exempt run and terminates below it,
# with both risers landing inside the run's X-span, cannot avoid the run: seated
# below, its up-riser at x=200 pierces the run; seated above, its down-riser at
# x=400 pierces it.  No parallel side is crossing-free, so the crossing is
# topological rather than a wrong-side dogleg.
_EXEMPT_TRANSIT = [
    (80.0, 180.0),
    (100.0, 180.0),
    (100.0, 200.0),
    (500.0, 200.0),
    (500.0, 220.0),
    (520.0, 220.0),
]
_TRANSIT_MOVABLE = [
    (180.0, 150.0),
    (200.0, 150.0),
    (200.0, 204.0),
    (400.0, 204.0),
    (400.0, 260.0),
    (420.0, 260.0),
]


def test_checker_allows_forced_transit_crossing() -> None:
    """A trunk crossing an exempt run it cannot dogleg around is not flagged.

    Both candidate placements (one corridor separation below and above the run)
    cross it, so the crossing is forced and the checker stays silent."""
    routes = [
        RoutedPath(
            edge=Edge("rs2", "lt1", "wrap"),
            line_id="wrap",
            points=_EXEMPT_TRANSIT,
            is_inter_section=True,
            normalize_exempt=True,
        ),
        RoutedPath(
            edge=Edge("lt2", "bs1", "byp"),
            line_id="byp",
            points=_TRANSIT_MOVABLE,
            is_inter_section=True,
        ),
    ]
    assert not check_no_dogleg_crosses_exempt_trunk(None, routes, {})


@pytest.mark.parametrize(
    "fixture",
    [
        "riboseq_inter_row_corridor.mmd",
        "rl_return_row_convergence.mmd",
    ],
)
def test_forced_transit_not_flagged(fixture: str) -> None:
    """A distinct-line trunk transiting an exempt run's span is not flagged.

    In ``riboseq_inter_row_corridor`` the pink ``riboseq`` trunk enters the
    ``annotation`` run's channel from above and leaves below it; in
    ``rl_return_row_convergence`` the ``bam`` trunk and the ``svvcf`` return run
    interlock at a fold corner.  In each the movable trunk crosses the exempt
    run from whichever side it takes, so no parallel lane clears it and the
    checker must stay silent."""
    graph, routes, offsets = _route(CURVE_REPROS / fixture)
    violations = check_no_dogleg_crosses_exempt_trunk(graph, routes, offsets)
    assert not violations, "\n".join(v.message() for v in violations)


def _same_source_corner_routes(radius: float) -> list[RoutedPath]:
    exempt = RoutedPath(
        edge=Edge("junction", "right_entry", "lower"),
        line_id="lower",
        points=[
            (283.5, 120.0),
            (317.5, 120.0),
            (317.5, 196.0),
            (645.0, 196.0),
            (645.0, 652.0),
            (617.0, 652.0),
        ],
        curve_radii=[radius] * 4,
        is_inter_section=True,
        normalize_exempt=True,
    )
    baseline = RoutedPath(
        edge=Edge("junction", "left_entry", "upper"),
        line_id="upper",
        points=[
            (283.5, 132.0),
            (309.5, 132.0),
            (309.5, 200.0),
            (651.3, 200.0),
            (651.3, 124.0),
            (677.0, 124.0),
        ],
        curve_radii=[radius] * 4,
        is_inter_section=True,
    )
    return [baseline, exempt]


def test_checker_allows_resolved_same_source_corner_arcs() -> None:
    """Two same-source corners whose arcs swallow the raw contact do not cross.

    Both trunks leave one junction and turn away from each other, so the arcs
    their resolved radii draw cover the point where the raw polylines touch and
    no ink crosses. No shipped fixture routes that geometry, so this hand-built
    pair is what holds the exemption: dropping it makes the checker report the
    raw contact as a crossing here.
    """
    assert not check_no_dogleg_crosses_exempt_trunk(
        None, _same_source_corner_routes(10.0), {}
    )


@pytest.mark.parametrize("radius", (0.0, 2.0))
def test_checker_reports_raw_crossings_without_sufficient_corner_arcs(
    radius: float,
) -> None:
    violations = check_no_dogleg_crosses_exempt_trunk(
        None, _same_source_corner_routes(radius), {}
    )

    assert [(violation.x, violation.y) for violation in violations] == [(645.0, 200.0)]


def test_natural_blocked_riser_records_metadata_backed_corner_radii() -> None:
    """Two same-source risers into one destination carry their corner records.

    ``upper`` and the exempt ``lower`` share a source junction and turn in the
    same inter-row channel, so each has to reach its shared turn on a resolved
    radius its concentric-corner metadata accounts for -- 10px inside a -4px
    offset for ``upper``, 14px on the reference for the exempt run -- and each
    turn belongs to a member-geometry plan.  The fixture then routes clear of
    the dogleg check.  It clears it because the two trunks never bring their
    axes into contact, not through the same-source corner exemption:
    ``test_checker_allows_resolved_same_source_corner_arcs`` is what covers
    that branch.
    """
    graph, routes, offsets = _route(
        EXAMPLE_TOPOLOGIES / "same_destination_vertical_convergence.mmd"
    )
    upper = next(
        route
        for route in routes
        if route.edge.source == "__junction_12"
        and route.line_id == "upper"
        and route.edge.target.startswith("target__entry_left")
    )
    exempt = next(
        route
        for route in routes
        if route.edge.source == "__junction_12"
        and route.edge.target == "s7__entry_right_9"
        and route.line_id == "lower"
    )

    assert resolve_curve_radii(upper.points, upper.curve_radii)[2] == 10.0
    assert resolve_curve_radii(exempt.points, exempt.curve_radii)[2] == 14.0
    assert upper.concentric_corner_offsets_by_segment[3] == (0.0, -4.0)
    assert upper.concentric_corner_bases_by_segment[3] == (10.0, 10.0)
    assert exempt.concentric_corner_offsets_by_segment[3] == (0.0, 0.0)
    assert exempt.concentric_corner_bases_by_segment[3] == (14.0, 14.0)
    assert upper.member_geometry_plan_id is not None
    assert exempt.member_geometry_plan_id is not None
    assert not check_no_dogleg_crosses_exempt_trunk(graph, routes, offsets)


def test_checker_flags_other_crossing_despite_coincident_end_axis() -> None:
    exempt = RoutedPath(
        edge=Edge("junction", "right_entry", "lower"),
        line_id="lower",
        points=[
            (80.0, 80.0),
            (100.0, 80.0),
            (100.0, 100.0),
            (200.0, 100.0),
            (200.0, 200.0),
            (220.0, 200.0),
        ],
        is_inter_section=True,
        normalize_exempt=True,
    )
    crossing = RoutedPath(
        edge=Edge("junction", "left_entry", "upper"),
        line_id="upper",
        points=[
            (120.0, 90.0),
            (150.0, 90.0),
            (150.0, 104.0),
            (200.0, 104.0),
            (200.0, 200.0),
            (220.0, 200.0),
        ],
        is_inter_section=True,
    )
    violations = check_no_dogleg_crosses_exempt_trunk(None, [crossing, exempt], {})
    assert [(violation.x, violation.y) for violation in violations] == [(150.0, 100.0)]
