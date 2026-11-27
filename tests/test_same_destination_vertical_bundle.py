"""Same-destination H-V-H tails bundle on their shared vertical corridor."""

from __future__ import annotations

import copy
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import (
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    graph_offset_step,
)
from nf_metro.layout.lane_plan import build_lane_plan
from nf_metro.layout.routing import common as routing_common
from nf_metro.layout.routing import compute_station_offsets, normalize, route_edges
from nf_metro.layout.routing import invariants as routing_invariants
from nf_metro.layout.routing.common import OffsetRegime, RoutedPath
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.reserved_bands import ReservedCorridors
from nf_metro.parser.model import (
    Edge,
    LineSpread,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)
from nf_metro.render.svg import build_observed_render_plan

ROOT = Path(__file__).resolve().parent.parent
SEED_72 = ROOT / "tests" / "fixtures" / "hash_seed_determinism" / "seed_72.mmd"
CONVERGENCE = (
    ROOT / "examples" / "topologies" / "same_destination_vertical_convergence.mmd"
)


def _seed_routes():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(SEED_72.read_text(), source_dir=str(SEED_72.parent))
        observed = build_observed_render_plan(graph, resolve_theme(graph))
    return graph, {
        (route.edge.source, route.edge.target, route.line_id): route
        for route in observed.plan.routes
    }


def _arc_centre(route: RoutedPath, radius_index: int) -> tuple[float, float]:
    radii = resolve_curve_radii(route.points, route.curve_radii)
    x, y = route.points[radius_index + 1]
    return x + radii[radius_index], y + radii[radius_index]


def test_seed_72_bundles_same_destination_verticals_at_four_pixel_pitch() -> None:
    _graph, routes = _seed_routes()
    purple = routes[("__junction_14", "s4__entry_left_9", "l6")]
    blue = routes[("s3__exit_right_2", "s4__entry_left_9", "l0")]
    nearby = routes[("__junction_14", "s7__entry_right_10", "l0")]

    assert purple.points == (
        (200.0, 124.0),
        (256.0, 124.0),
        (256.0, 196.0),
        (564.0, 196.0),
        (564.0, 120.0),
        (662.0, 120.0),
    )
    assert purple.curve_radii == (18.0, 10.0, 10.0, 14.0)
    assert blue.points == (
        (530.0, 506.0),
        (568.0, 506.0),
        (568.0, 124.0),
        (662.0, 124.0),
    )
    assert blue.curve_radii == (14.0, 10.0)
    assert blue.points[1][0] - purple.points[3][0] == 4.0
    purple_straight = (
        purple.points[4][1] + purple.curve_radii[3],
        purple.points[3][1] - purple.curve_radii[2],
    )
    blue_straight = (
        blue.points[2][1] + blue.curve_radii[1],
        blue.points[1][1] - blue.curve_radii[0],
    )
    assert (
        max(purple_straight[0], blue_straight[0]),
        min(purple_straight[1], blue_straight[1]),
    ) == (134.0, 186.0)
    assert _arc_centre(purple, 3) == pytest.approx((578.0, 134.0))
    assert _arc_centre(blue, 1) == pytest.approx((578.0, 134.0))
    assert nearby.points[3][0] == 546.0


def _synthetic(
    side: PortSide,
    vertical_sign: int,
    *,
    separation: float = 10.0,
    short_suffix: bool = False,
    short_overlap: bool = False,
    counter_running: bool = False,
    different_target: bool = False,
    obstructed: bool = False,
    crossing_blocker: bool = False,
    preexisting_blocker: bool = False,
    planned: bool = False,
    rail: bool = False,
) -> tuple[MetroGraph, list[RoutedPath], SimpleNamespace]:
    left = side is PortSide.LEFT
    port_x = 220.0 if left else 100.0
    source_x = 100.0 if left else 220.0
    inner_x = port_x - 46.0 if left else port_x + 46.0
    outer_x = inner_x - separation if left else inner_x + separation
    if short_suffix:
        inner_x = port_x - 10.0 if left else port_x + 10.0
        outer_x = inner_x - separation if left else inner_x + separation
    target_box_x = 220.0 if left else 0.0
    source_box_x = 0.0 if left else 220.0
    graph = MetroGraph(
        sections={
            "source": Section(
                id="source",
                name="Source",
                direction="LR" if left else "RL",
                grid_col=0 if left else 1,
                grid_row=0,
                bbox_x=source_box_x,
                bbox_y=-20.0,
                bbox_w=100.0,
                bbox_h=260.0,
            ),
            "target": Section(
                id="target",
                name="Target",
                direction="LR" if left else "RL",
                grid_col=1 if left else 0,
                grid_row=0,
                bbox_x=target_box_x,
                bbox_y=-20.0,
                bbox_w=100.0,
                bbox_h=260.0,
            ),
        },
        stations={
            "source_a": Station("source_a", "", section_id="source"),
            "source_b": Station("source_b", "", section_id="source"),
            "entry": Station(
                "entry", "", section_id="target", is_port=True, x=port_x, y=100.0
            ),
        },
        ports={
            "entry": Port("entry", "target", side, is_entry=True),
        },
        edges=[
            Edge("source_a", "entry", "a"),
            Edge("source_b", "entry", "b"),
        ],
    )
    if different_target:
        graph.stations["entry_b"] = Station(
            "entry_b", "", section_id="target", is_port=True, x=port_x, y=104.0
        )
        graph.ports["entry_b"] = Port("entry_b", "target", side, is_entry=True)
        graph.edges[1] = Edge("source_b", "entry_b", "b")
    if obstructed:
        target_outer_x = inner_x - 4.0 if left else inner_x + 4.0
        graph.sections["obstacle"] = Section(
            id="obstacle",
            name="Obstacle",
            grid_col=99,
            grid_row=0,
            bbox_x=target_outer_x - 1.0,
            bbox_y=40.0,
            bbox_w=2.0,
            bbox_h=140.0,
        )
    if rail:
        graph.line_spread_overrides["source"] = LineSpread.RAILS

    if vertical_sign < 0:
        trunk_a = 110.0 if short_overlap else 196.0
        trunk_b = 200.0
    else:
        trunk_a = 90.0 if short_overlap else 0.0
        trunk_b = 0.0 if short_overlap else 4.0
    if counter_running:
        trunk_b = 200.0 if vertical_sign > 0 else 0.0
    lead_end = port_x
    routes = [
        RoutedPath(
            edge=graph.edges[0],
            line_id="a",
            points=[
                (source_x, trunk_a),
                (outer_x, trunk_a),
                (outer_x, 100.0),
                (lead_end, 100.0),
            ],
            is_inter_section=True,
            curve_radii=[7.0, 11.0],
        ),
        RoutedPath(
            edge=graph.edges[1],
            line_id="b",
            points=[
                (source_x, trunk_b),
                (inner_x, trunk_b),
                (inner_x, 104.0),
                (lead_end, 104.0),
            ],
            is_inter_section=True,
            curve_radii=[13.0, 17.0],
        ),
    ]
    if crossing_blocker or preexisting_blocker:
        blocker_x = 167.0 if crossing_blocker else 160.0
        graph.stations["blocker_source"] = Station(
            "blocker_source", "", section_id="source"
        )
        graph.stations["blocker_target"] = Station(
            "blocker_target", "", section_id="target"
        )
        blocker_edge = Edge("blocker_source", "blocker_target", "blocker")
        graph.edges.append(blocker_edge)
        routes.append(
            RoutedPath(
                edge=blocker_edge,
                line_id="blocker",
                points=[(blocker_x, 190.0), (blocker_x, 198.0)],
                is_inter_section=True,
                normalize_exempt=True,
                route_system_owned_segment_ranks=(0,),
            )
        )
    if planned:
        routes[0].route_system_owned_segment_ranks = (1,)
    ctx = SimpleNamespace(
        graph=graph,
        merge=SimpleNamespace(entry_port_for={}),
        offset_step=4.0,
        curve_radius=CURVE_RADIUS,
        station_offsets={("entry", "a"): 0.0, ("entry", "b"): 4.0},
        reversed_sections=set(),
        reserved_bands=ReservedCorridors(),
    )
    return graph, routes, ctx


@pytest.mark.parametrize("side", [PortSide.LEFT, PortSide.RIGHT])
@pytest.mark.parametrize("vertical_sign", [-1, 1], ids=["up", "down"])
def test_same_target_vertical_bundle_has_mirror_parity(
    side: PortSide, vertical_sign: int
) -> None:
    _graph, routes, ctx = _synthetic(side, vertical_sign)
    before_radii = [list(route.curve_radii or []) for route in routes]

    normalize._settle_same_destination_approach_bundles(routes, ctx)

    by_line = {route.line_id: route for route in routes}
    follows_port = vertical_sign == (-1 if side is PortSide.LEFT else 1)
    if side is PortSide.LEFT:
        expected = (
            {"a": 170.0, "b": 174.0} if follows_port else {"a": 174.0, "b": 170.0}
        )
    else:
        expected = (
            {"a": 146.0, "b": 150.0} if follows_port else {"a": 150.0, "b": 146.0}
        )
    assert {
        line_id: route.points[1][0] for line_id, route in by_line.items()
    } == expected
    assert [route.curve_radii for route in routes] == before_radii


@pytest.mark.parametrize(
    "case",
    [
        "planned",
        "obstructed",
        "short_suffix",
        "short_overlap",
        "counter_running",
        "different_target",
        "distant",
        "rail",
    ],
)
def test_ineligible_same_target_group_is_refused_atomically(case: str) -> None:
    kwargs = {
        "planned": {"planned": True},
        "obstructed": {"obstructed": True},
        "short_suffix": {"short_suffix": True},
        "short_overlap": {"short_overlap": True},
        "counter_running": {"counter_running": True},
        "different_target": {"different_target": True},
        "distant": {"separation": EDGE_TO_BUNDLE_CLEARANCE + 1.0},
        "rail": {"rail": True},
    }[case]
    _graph, routes, ctx = _synthetic(PortSide.LEFT, -1, **kwargs)
    before = copy.deepcopy(routes)

    normalize._settle_same_destination_approach_bundles(routes, ctx)

    assert routes == before
    checker = routing_invariants.check_same_destination_approach_bundle
    assert checker(_graph, routes) == []


def test_same_target_vertical_bundle_is_idempotent_across_freeze_calls() -> None:
    _graph, routes, ctx = _synthetic(PortSide.LEFT, -1)

    normalize._settle_same_destination_approach_bundles(routes, ctx)
    settled = [
        (copy.deepcopy(route.points), copy.deepcopy(route.curve_radii))
        for route in routes
    ]
    normalize._settle_same_destination_approach_bundles(routes, ctx)
    normalize._settle_same_destination_approach_bundles(
        routes, ctx, movable_route_ids=frozenset(id(route) for route in routes)
    )
    for route in routes:
        route.route_system_owned_segment_ranks = (1,)
    normalize._settle_same_destination_approach_bundles(routes, ctx)

    assert [(route.points, route.curve_radii) for route in routes] == settled


def _proposal_inputs(graph, routes, ctx):
    bundle = next(
        routing_common.iter_same_destination_approach_bundles(
            routes, graph, ctx.offset_step
        )
    )
    slots = routing_common.same_destination_approach_slots(
        bundle, graph, ctx.offset_step
    )
    return bundle, slots


def _route_geometry_state(routes):
    return tuple(
        (
            tuple(route.points),
            tuple(route.gap_slots),
            tuple(route.curve_radii or ()),
        )
        for route in routes
    )


def test_new_plan_owned_riser_conflict_refuses_tail_move_atomically() -> None:
    graph, routes, ctx = _synthetic(PortSide.LEFT, -1, crossing_blocker=True)
    bundle, slots = _proposal_inputs(graph, routes, ctx)
    moving_route = next(route for route in routes if route.line_id == "a")
    blocker = next(route for route in routes if route.line_id == "blocker")
    segment_rank = len(moving_route.points) - 3
    proposed_points = list(moving_route.points)
    proposed_x = slots["a"].peel_x
    proposed_points[segment_rank] = (
        proposed_x,
        proposed_points[segment_rank][1],
    )
    proposed_points[segment_rank + 1] = (
        proposed_x,
        proposed_points[segment_rank + 1][1],
    )
    relevant = frozenset({id(moving_route)})
    baseline_conflicts = routing_common._same_destination_conflicts(
        routes, {}, relevant
    )
    proposed_conflicts = routing_common._same_destination_conflicts(
        routes, {id(moving_route): proposed_points}, relevant
    )

    assert moving_route.offset_regime is OffsetRegime.DEFERRED
    assert blocker.normalize_exempt
    assert blocker.route_system_owned_segment_ranks == (0,)
    assert proposed_conflicts - baseline_conflicts
    assert (
        routing_common.feasible_same_destination_approach_proposals(
            graph, routes, bundle, slots
        )
        is None
    )

    before = _route_geometry_state(routes)
    normalize._settle_same_destination_approach_bundles(routes, ctx)
    assert _route_geometry_state(routes) == before
    normalize._settle_same_destination_approach_bundles(routes, ctx)
    assert _route_geometry_state(routes) == before


def test_preexisting_riser_conflict_does_not_veto_safe_tail_move() -> None:
    graph, routes, ctx = _synthetic(PortSide.LEFT, -1, preexisting_blocker=True)
    bundle, slots = _proposal_inputs(graph, routes, ctx)
    proposals = routing_common.feasible_same_destination_approach_proposals(
        graph, routes, bundle, slots
    )

    assert proposals is not None
    relevant = frozenset(id(proposal.route) for proposal in proposals)
    baseline_conflicts = routing_common._same_destination_conflicts(
        routes, {}, relevant
    )
    proposed_conflicts = routing_common._same_destination_conflicts(
        routes,
        {id(proposal.route): proposal.points for proposal in proposals},
        relevant,
    )
    assert baseline_conflicts
    assert not proposed_conflicts - baseline_conflicts

    normalize._settle_same_destination_approach_bundles(routes, ctx)
    assert next(route for route in routes if route.line_id == "a").points[1][0] == 170.0


def test_near_section_edge_refuses_tail_move_and_retry_is_stable() -> None:
    graph, routes, ctx = _synthetic(PortSide.LEFT, -1)
    bundle, slots = _proposal_inputs(graph, routes, ctx)
    target_x = slots["a"].peel_x
    graph.sections["near_edge"] = Section(
        id="near_edge",
        name="Near edge",
        grid_col=99,
        grid_row=0,
        bbox_x=target_x + EDGE_TO_BUNDLE_CLEARANCE - 1.0,
        bbox_y=40.0,
        bbox_w=20.0,
        bbox_h=140.0,
    )

    before = _route_geometry_state(routes)
    assert (
        routing_common.feasible_same_destination_approach_proposals(
            graph, routes, bundle, slots
        )
        is None
    )
    normalize._settle_same_destination_approach_bundles(routes, ctx)
    assert _route_geometry_state(routes) == before
    normalize._settle_same_destination_approach_bundles(routes, ctx)
    assert _route_geometry_state(routes) == before


def test_same_target_vertical_bundle_guard_is_fail_loud_and_non_vacuous() -> None:
    graph, routes, _ctx = _synthetic(PortSide.LEFT, -1)
    checker = getattr(
        routing_invariants, "check_same_destination_approach_bundle", None
    )
    assert checker is not None
    violations = checker(graph, routes)
    assert {violation.line_id for violation in violations} == {"a"}

    # The render guards read the lane plan a layout seals before routing.
    graph._lane_plan = build_lane_plan(graph)
    with pytest.raises(
        routing_invariants.CurveInvariantError,
        match="same-destination final vertical bundle",
    ):
        routing_invariants.assert_render_curve_invariants(graph, routes, {})


def test_same_target_vertical_bundle_guard_catches_seed_72_prefix() -> None:
    graph, routes = _seed_routes()
    observed_purple = routes[("__junction_14", "s4__entry_left_9", "l6")]
    observed_blue = routes[("s3__exit_right_2", "s4__entry_left_9", "l0")]
    purple = RoutedPath(
        edge=Edge("__junction_14", "s4__entry_left_9", "l6"),
        line_id="l6",
        points=list(observed_purple.points),
        is_inter_section=True,
        curve_radii=list(observed_purple.curve_radii),
    )
    blue = RoutedPath(
        edge=Edge("s3__exit_right_2", "s4__entry_left_9", "l0"),
        line_id="l0",
        points=list(observed_blue.points),
        is_inter_section=True,
        curve_radii=list(observed_blue.curve_radii),
    )
    purple.points[3] = (558.0, purple.points[3][1])
    purple.points[4] = (558.0, purple.points[4][1])
    checker = getattr(
        routing_invariants, "check_same_destination_approach_bundle", None
    )

    assert checker is not None
    violations = checker(graph, [purple, blue])
    assert [
        (violation.line_id, violation.actual_x, violation.expected_x)
        for violation in violations
    ] == [("l6", 558.0, 564.0)]


def _stepped_bundle(directive: str):
    """Route the convergence fixture under *directive*, with its one bundle.

    The directive prefix changes the graph's bundle pitch, which is what the
    approach slots and the closing guard both measure in.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(
            directive + "\n" + CONVERGENCE.read_text(),
            source_dir=str(CONVERGENCE.parent),
        )
        offsets = compute_station_offsets(graph)
        routes = route_edges(graph, station_offsets=offsets)
    step = graph_offset_step(graph)
    bundle = next(
        routing_common.iter_same_destination_approach_bundles(routes, graph, step)
    )
    slots = routing_common.same_destination_approach_slots(bundle, graph, step)
    return graph, routes, bundle, slots, step


@pytest.mark.parametrize(
    ("directive", "step"),
    [("%%metro track_gap: 0", 3.0), ("%%metro stroke_scale: 0.6", 2.4)],
)
def test_tighter_pitch_seats_the_approach_bundle(directive: str, step: float) -> None:
    """A graph nesting tighter than the default pitch gets a proposal.

    The slots are spaced on the graph's own step, so the clearance the pass
    demands between them has to be that step too.  Measured against the default
    instead, every pair of slots on a tighter map reads as too close and the pass
    vetoes its own proposal -- taking the closing guard, which skips a bundle it
    cannot propose for, inert with it.
    """
    graph, routes, bundle, slots, resolved = _stepped_bundle(directive)

    assert resolved == step
    assert len(bundle.entries) >= 2
    assert (
        routing_common.feasible_same_destination_approach_proposals(
            graph, routes, bundle, slots
        )
        is not None
    )


@pytest.mark.parametrize(
    "directive", ["%%metro track_gap: 0", "%%metro stroke_scale: 0.6"]
)
def test_tighter_pitch_guard_inspects_the_bundle(
    directive: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing guard reaches the bundle's tails on a tighter map.

    The guard skips any bundle the approach pass cannot propose for, so a pass
    that vetoes itself takes the guard with it.  Recording the gate's verdict
    shows the bundle is inspected rather than waved through, and the guard finds
    both tails already on the slots the graph's own pitch names.
    """
    graph, routes, bundle, slots, step = _stepped_bundle(directive)
    verdicts = []
    real = routing_common.feasible_same_destination_approach_proposals

    def spy(*args, **kwargs):
        proposals = real(*args, **kwargs)
        verdicts.append(proposals is not None)
        return proposals

    monkeypatch.setattr(
        routing_invariants, "feasible_same_destination_approach_proposals", spy
    )

    violations = routing_invariants.check_same_destination_approach_bundle(
        graph, routes
    )

    assert verdicts == [True]
    assert violations == []
    peels = sorted(slot.peel_x for slot in slots.values())
    assert [round(b - a, 6) for a, b in zip(peels, peels[1:])] == [step]
    assert sorted(tail.peel_x for _route, tail in bundle.entries) == peels
