"""Tests for the bundle-corner concentricity invariant.

Covers:

* Happy-path: every gallery fixture and example routes without a
  non-concentric wholesale bundle corner.
* Route-level positive/negative: hand-built bundles exercise the
  wholesale-vs-transition discriminator and the arc-centre test, so the
  invariant is shown to catch a real pinch rather than passing by accident.

The correctness check here is the one the corner-radius *source* ratchet
(``tests/test_corner_radius_ratchet.py``) explicitly cannot perform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import (
    OffsetRegime,
    compute_station_offsets,
    route_edges,
)
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.invariants import (
    check_concentric_bundle_corners,
    check_fanout_lane_continuity,
    check_standard_source_bundle_corner_inputs,
)
from nf_metro.layout.routing.normalize import _rederive_semantic_end_corners
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "tests" / "fixtures" / "topologies"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
EXAMPLES = REPO_ROOT / "examples"


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
def test_no_non_concentric_bundle_corners_in_gallery(path: Path) -> None:
    """Every shipped fixture must route with concentric wholesale corners.

    A handler that sizes a wholesale-translated bundle corner with a base
    or hand-signed radius (instead of the geometry-derived concentric one)
    surfaces here as a failing fixture, even when that radius traces to an
    approved helper and so slips past the source ratchet.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_concentric_bundle_corners(graph, routes, offsets)
    assert violations == [], (
        f"{path.name}: {len(violations)} non-concentric corner(s); "
        f"first: {violations[0].message() if violations else ''}"
    )
    standard_violations = check_standard_source_bundle_corner_inputs(routes, offsets)
    assert standard_violations == [], (
        f"{path.name}: {len(standard_violations)} non-standard source corner(s); "
        f"first: {standard_violations[0].message() if standard_violations else ''}"
    )


def test_source_seam_turns_are_concentric_across_destinations() -> None:
    """One planned exit bundle keeps one arc centre as its members split."""
    path = EXAMPLES / "topologies" / "merge_trunk_over_low_section.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    assert check_concentric_bundle_corners(graph, routes, offsets) == []
    source_turns = {
        route.line_id: route.curve_radii[route.exit_turn_segment_rank - 1]
        for route in routes
        if route.edge.source == "__junction_7"
        and route.exit_turn_segment_rank is not None
        and route.curve_radii is not None
    }
    assert source_turns == {"flow": 14.0, "side": 10.0}


def test_cross_system_landing_corners_are_concentric_across_route_shapes() -> None:
    """One target landing cohort keeps one centre across distinct systems."""
    path = EXAMPLES / "topologies" / "convergent_offrow_exit_climb.mmd"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    landing = {
        route.line_id: route
        for route in routes
        if route.edge.target == "cnv_calling__entry_left_10"
        and route.line_id in {"other", "snvvcf"}
    }

    assert {line_id: len(route.points) for line_id, route in landing.items()} == {
        "other": 6,
        "snvvcf": 4,
    }
    centres = {}
    for line_id, route in landing.items():
        assert route.curve_radii is not None
        corner_x, corner_y = route.points[-2]
        radius = route.curve_radii[-1]
        centres[line_id] = (corner_x + radius, corner_y - radius)
    assert centres["other"] == pytest.approx((760.0, 372.0))
    assert centres["snvvcf"] == pytest.approx(centres["other"])
    assert {
        line_id: route.concentric_corner_offsets_by_segment[len(route.points) - 2][0]
        for line_id, route in landing.items()
    } == {"other": -4.0, "snvvcf": 0.0}
    assert {
        line_id: route.concentric_corner_bases_by_segment[len(route.points) - 2][0]
        for line_id, route in landing.items()
    } == {"other": 10.0, "snvvcf": 10.0}
    assert check_concentric_bundle_corners(graph, routes, offsets) == []


def test_short_planned_source_leads_share_a_clamp_safe_reference_radius() -> None:
    """A compact planned fan keeps one centre without moving its frozen axes."""
    path = FIXTURES / "route_reservations" / "reportho.metro"
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    source_turns = {
        route.line_id: route
        for route in observation.routes
        if route.edge.source == "__junction_12" and route.line_id in {"main", "report"}
    }

    radii = {}
    centres = {}
    for line_id, route in source_turns.items():
        assert route.curve_radii is not None
        radius = route.curve_radii[0]
        radii[line_id] = radius
        centres[line_id] = (
            route.points[1][0] - radius,
            route.points[1][1] + radius,
        )
    assert radii == {"main": 14.0, "report": 10.0}
    assert centres["report"] == pytest.approx(centres["main"])
    assert {
        line_id: route.concentric_corner_offsets_by_segment[1][0]
        for line_id, route in source_turns.items()
    } == {"main": 4.0, "report": 0.0}
    assert {
        line_id: route.concentric_corner_bases_by_segment[1][0]
        for line_id, route in source_turns.items()
    } == {"main": 10.0, "report": 10.0}
    relative_shapes = {
        line_id: tuple(
            (
                round(x - route.points[0][0], 6),
                round(y - route.points[0][1], 6),
            )
            for x, y in route.points
        )
        for line_id, route in source_turns.items()
    }
    assert relative_shapes == {
        "main": ((0.0, 0.0), (16.0, 0.0), (16.0, 29.2), (42.0, 29.2)),
        "report": ((0.0, 0.0), (10.0, 0.0), (10.0, 222.0), (30.0, 222.0)),
    }
    incoming = {
        route.line_id: route
        for route in observation.routes
        if route.edge.target == "__junction_12" and route.line_id in {"main", "report"}
    }
    assert {
        line_id: (incoming[line_id].points[-1], source_turns[line_id].points[0])
        for line_id in source_turns
    } == {
        "main": ((1073.5, 266.0), (1073.5, 266.0)),
        "report": ((1075.5, 270.0), (1075.5, 270.0)),
    }
    assert check_fanout_lane_continuity(observation.routes, graph) == []
    main = source_turns["main"]
    main_plan = next(
        plan
        for plan in observation.plan.member_geometry_plans
        if str(plan.id) == main.member_geometry_plan_id
    )
    assert tuple(main.points) == main_plan.points
    assert tuple(main.curve_radii or ()) == main_plan.curve_radii
    assert main.concentric_corner_offsets_by_segment == dict(
        main_plan.concentric_corner_offsets_by_segment
    )
    assert main.concentric_corner_bases_by_segment == dict(
        main_plan.concentric_corner_bases_by_segment
    )
    assert source_turns["report"].member_geometry_plan_id is None
    assert check_concentric_bundle_corners(graph, observation.routes, offsets) == []

    source_turns["main"].curve_radii[0] += 1.0
    assert check_concentric_bundle_corners(graph, observation.routes, offsets)

    source_turns["report"].points[0] = (1075.5, 266.0)
    source_turns["report"].points[1] = (1085.5, 266.0)
    assert check_fanout_lane_continuity(observation.routes, graph)


def test_unowned_short_leads_share_a_bisected_clamp_safe_radius() -> None:
    routes = [
        _route(
            "main",
            [(1073.5, 270.0), (1081.5, 270.0), (1081.5, 295.2), (1115.5, 295.2)],
            [10.0, 14.0],
            source="junction",
            target="main-target",
        ),
        _route(
            "report",
            [(1075.5, 266.0), (1085.5, 266.0), (1085.5, 492.0), (1105.5, 492.0)],
            [14.0, 10.0],
            source="junction",
            target="report-target",
        ),
    ]

    _rederive_semantic_end_corners(routes, CURVE_RADIUS, {})

    assert [route.curve_radii[0] for route in routes if route.curve_radii] == [
        pytest.approx(6.01),
        pytest.approx(10.01),
    ]
    assert check_concentric_bundle_corners(None, routes, {}) == []


# ---------------------------------------------------------------------------
# Route-level positive/negative tests
# ---------------------------------------------------------------------------


def _route(
    line_id: str,
    points: list[tuple[float, float]],
    radii: list[float] | None = None,
    *,
    source: str = "__src__",
    target: str = "__tgt__",
    route_system_id: str | None = None,
) -> RoutedPath:
    """A bundled ``RoutedPath`` (shared src/tgt) with baked geometry."""
    return RoutedPath(
        edge=Edge(source=source, target=target, line_id=line_id),
        line_id=line_id,
        points=points,
        is_inter_section=True,
        offset_regime=OffsetRegime.BAKED,
        curve_radii=radii,
        route_system_id=route_system_id,
    )


# A down->right corner offset wholesale by (3, -3): the concentric radii are
# 10 (inner) and 7 (outer) so both arc centres land at (10, 90).
_CONCENTRIC_A = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
_CONCENTRIC_B = _route("b", [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)], [7.0])


def test_concentric_wholesale_corner_passes() -> None:
    """A wholesale-translated corner with correctly nested radii is clean."""
    assert (
        check_concentric_bundle_corners(None, [_CONCENTRIC_A, _CONCENTRIC_B], {}) == []
    )


def test_non_concentric_wholesale_corner_is_caught() -> None:
    """The same geometry with a base (un-nested) outer radius pinches."""
    bad_b = _route("b", [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)], [10.0])
    violations = check_concentric_bundle_corners(None, [_CONCENTRIC_A, bad_b], {})
    assert len(violations) == 1
    assert violations[0].centre_spread > 1.0


def test_same_edge_corner_matches_across_different_waypoint_counts() -> None:
    """A semantic edge cohort does not require equal waypoint structure."""
    a = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
    b = _route(
        "b",
        [(3.0, 0.0), (3.0, 40.0), (3.0, 97.0), (50.0, 97.0)],
        [0.0, 10.0],
    )

    violations = check_concentric_bundle_corners(None, [a, b], {})

    assert len(violations) == 1
    assert violations[0].edge_source == "__src__"
    assert violations[0].edge_target == "__tgt__"
    assert violations[0].centre_spread == pytest.approx(3.0 * 2**0.5)


def test_shared_target_corner_matches_across_route_systems() -> None:
    """A semantic landing cohort can span independently owned route systems."""
    a = _route(
        "a",
        [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)],
        [10.0],
        source="__source_a__",
        target="__landing__",
        route_system_id="system-a",
    )
    b = _route(
        "b",
        [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)],
        [10.0],
        source="__source_b__",
        target="__landing__",
        route_system_id="system-b",
    )

    violations = check_concentric_bundle_corners(None, [a, b], {})

    assert len(violations) == 1
    assert violations[0].edge_source == "target seam"
    assert violations[0].edge_target == "__landing__"
    assert violations[0].centre_spread == pytest.approx(3.0 * 2**0.5)


def test_non_concentric_source_seam_corner_is_caught_across_destinations() -> None:
    """A planned source bundle is one corner cohort across distinct targets."""
    a = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
    b = _route(
        "b",
        [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)],
        [10.0],
        target="__other__",
    )
    for route in (a, b):
        route.exit_turn_plan_id = "plan"
        route.exit_turn_segment_rank = 1
    violations = check_concentric_bundle_corners(None, [a, b], {})
    assert len(violations) == 1
    assert violations[0].edge_source == "__src__"
    assert violations[0].edge_target == "source seam"


def test_planned_source_bundle_requires_standard_corner_inputs() -> None:
    """Missing source-turn radius metadata fails closed at the render guard."""
    a = _route("a", [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)], [10.0])
    b = _route("b", [(3.0, 0.0), (3.0, 97.0), (50.0, 97.0)], [7.0])
    for route, offset in ((a, 0.0), (b, 3.0)):
        route.exit_turn_plan_id = "plan"
        route.exit_turn_segment_rank = 1
        route.concentric_corner_offsets_by_segment = {1: (offset, None)}
        route.concentric_corner_bases_by_segment = {1: (10.0, None)}
    assert check_standard_source_bundle_corner_inputs([a, b], {}) == []

    b.concentric_corner_offsets_by_segment.clear()
    violations = check_standard_source_bundle_corner_inputs([a, b], {})
    assert len(violations) == 1
    assert "no complete standard radius calculation" in violations[0].message()


def test_transition_corner_with_one_pinned_leg_is_skipped() -> None:
    """A converging corner (vertical legs offset, horizontals coincident) is
    a transition, not a wholesale translation, so non-concentric is allowed.
    """
    a = _route(
        "a",
        [(0.0, 0.0), (0.0, 100.0), (50.0, 100.0)],
        [10.0],
        source="__source_a__",
        target="__landing__",
        route_system_id="system-a",
    )
    # b's vertical leg is offset 3px but both horizontals share y=100.
    b = _route(
        "b",
        [(3.0, 0.0), (3.0, 100.0), (50.0, 100.0)],
        [10.0],
        source="__source_b__",
        target="__landing__",
        route_system_id="system-b",
    )
    assert check_concentric_bundle_corners(None, [a, b], {}) == []


def test_diagonal_leg_is_not_a_corner() -> None:
    """A 45-degree diagonal leg carries no orthogonal corner to nest."""
    a = _route("a", [(0.0, 0.0), (100.0, 0.0), (130.0, 117.0), (180.0, 117.0)])
    b = _route("b", [(0.0, 3.0), (97.0, 3.0), (127.0, 120.0), (180.0, 120.0)])
    assert check_concentric_bundle_corners(None, [a, b], {}) == []


def test_single_line_bundle_is_skipped() -> None:
    """One line has no bundle-mate to be concentric with."""
    assert check_concentric_bundle_corners(None, [_CONCENTRIC_A], {}) == []
