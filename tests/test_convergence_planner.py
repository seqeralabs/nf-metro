"""Immutable convergence plans own merge trunks before route emission."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import nf_metro.layout.routing.convergences as convergence_routing
import nf_metro.layout.routing.core as routing_core
from nf_metro.api import prepare_graph
from nf_metro.layout.geometry import point_to_polyline_distance
from nf_metro.layout.route_plan import (
    BindingKind,
    ConvergenceDisposition,
    ConvergenceEndpointRole,
    ConvergencePlanId,
    ConvergenceTrunkAxis,
    ConvergenceTrunkReason,
    CoordinateRegime,
    DemandAxis,
    DemandKind,
    KeepOutClass,
    RoutePlan,
    RouteSystemDisposition,
    RouteSystemId,
    SharedReferenceId,
    SharedReferenceKind,
    build_route_plan_query,
)
from nf_metro.layout.route_reservations import (
    expected_convergence_foreign_references,
)
from nf_metro.layout.routing import compute_station_offsets, observe_route_edges
from nf_metro.layout.routing.common import Direction, OffsetRegime, RoutedPath
from nf_metro.layout.routing.convergences import (
    ConvergenceInvariantError,
    ConvergencePlanningError,
    UnsupportedConvergenceError,
    _direct_axis,
    _seat_route_on_trunk_flanks,
    validate_convergence_plans,
)
from nf_metro.layout.routing.corners import concentric_corner_radius_at
from nf_metro.layout.routing.invariants import (
    check_merge_branches_meet_trunk,
    check_merge_feeders_land_on_trunk,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Station
from nf_metro.parser.route_topology import ResolvedEdge, build_route_topology_query

ROOT = Path(__file__).parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
GUIDE = ROOT / "examples" / "guide"
FROZEN = ROOT / "tests" / "fixtures" / "hash_seed_determinism"


def _observe(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    observed = observe_route_edges(graph, station_offsets=offsets)
    return graph, offsets, observed


def _edge_order(observed) -> tuple[ResolvedEdge, ...]:
    return tuple(member.edge for member in observed.plan.members)


def _observe_text(text: str):
    graph = prepare_graph(text)
    offsets = compute_station_offsets(graph)
    observed = observe_route_edges(graph, station_offsets=offsets)
    return graph, offsets, observed


@pytest.fixture(scope="module")
def three_column_route_plan() -> RoutePlan:
    return _observe(TOPOLOGIES / "merge_feeders_three_columns.mmd")[2].plan


@pytest.fixture(scope="module")
def right_entry_route_plan() -> RoutePlan:
    return _observe(TOPOLOGIES / "merge_right_entry.mmd")[2].plan


def test_three_column_merge_has_one_complete_planned_convergence() -> None:
    graph, _offsets, observed = _observe(TOPOLOGIES / "merge_feeders_three_columns.mmd")
    (plan,) = observed.plan.convergence_plans

    assert plan.disposition is ConvergenceDisposition.PLANNED
    assert plan.primary_trunk_reason is ConvergenceTrunkReason.LONGEST_BYPASS
    assert plan.primary_trunk_member_id in plan.member_ids
    assert plan.trunk_axis is not None
    assert plan.trunk_axis.axis is DemandAxis.X
    assert plan.trunk_axis.extent_start < plan.trunk_axis.extent_end
    topology = build_route_topology_query(graph)
    assert topology is not None
    assert plan.merge_junction_ids == tuple(
        item.junction_id for item in topology.convergences
    )
    assert len(plan.landings) == 3
    assert tuple(item.order for item in plan.landings) == (0, 1, 2)
    expected_lane_rank = plan.lane_order.index(plan.line_ids[0])
    assert tuple(item.lane_rank for item in plan.landings) == (
        expected_lane_rank,
        expected_lane_rank,
        expected_lane_rank,
    )
    assert len(plan.lane_order) > 1
    assert any(item.bypass for item in plan.landings)
    assert any(item.long_haul for item in plan.landings)
    assert plan.outgoing_continuations
    assert set(plan.member_ids) == {
        ownership.member_id for ownership in plan.endpoint_ownership
    }
    assert {ownership.role for ownership in plan.endpoint_ownership} >= {
        ConvergenceEndpointRole.FEEDER,
        ConvergenceEndpointRole.COVERED_CONTINUATION,
    }


@pytest.mark.parametrize(
    "fixture",
    (
        "exit_run_three_drop_columns.mmd",
        "funcprofiler_upstream.mmd",
        "merge_trunk_out_of_range_section.mmd",
    ),
)
def test_shared_channel_route_systems_are_planned_as_a_whole(fixture: str) -> None:
    _graph, _offsets, observed = _observe(TOPOLOGIES / fixture)

    assert observed.plan.convergence_plans
    assert {item.disposition for item in observed.plan.convergence_plans} == {
        ConvergenceDisposition.PLANNED
    }
    assert len({item.system_id for item in observed.plan.convergence_plans}) == 1
    assert {item.legacy_reason for item in observed.plan.convergence_plans} == {None}


@pytest.mark.parametrize(
    "path",
    (
        TOPOLOGIES / "merge_bottom_row_bypass.mmd",
        TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd",
        TOPOLOGIES / "merge_right_entry.mmd",
        ROOT / "examples" / "genomeassembly.mmd",
        ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd",
        ROOT / "tests" / "fixtures" / "ambiguous_exit_continuation.mmd",
    ),
)
def test_reviewed_shared_channels_are_complete_planned_systems(path: Path) -> None:
    _graph, _offsets, observed = _observe(path)

    assert observed.plan.convergence_plans
    assert all(item.owns_geometry for item in observed.plan.convergence_plans)
    assert {item.legacy_reason for item in observed.plan.convergence_plans} == {None}


@pytest.mark.parametrize(
    "path",
    (
        TOPOLOGIES / "merge_adjacent_feeder.mmd",
        TOPOLOGIES / "merge_trunk_over_low_section.mmd",
    ),
)
def test_non_conflicting_reviewed_systems_remain_planned(path: Path) -> None:
    _graph, _offsets, observed = _observe(path)

    assert observed.plan.convergence_plans
    assert all(item.owns_geometry for item in observed.plan.convergence_plans)


def test_convergence_plan_is_queryable_through_every_semantic_identity() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    (plan,) = observed.plan.convergence_plans
    query = build_route_plan_query(observed.plan)

    assert query.convergence_plan(plan.id) is plan
    assert query.convergence_plans_for_system(plan.system_id) == (plan,)
    for convergence_id in plan.convergence_ids:
        assert query.convergence_plans_for_convergence(convergence_id) == (plan,)
    for connector_id in plan.connector_ids:
        assert query.convergence_plans_for_connector(connector_id) == (plan,)
    for member_id in plan.member_ids:
        assert query.convergence_plans_for_member(member_id) == (plan,)
    for path in plan.resolved_member_paths:
        assert query.convergence_plans_for_resolved_path(path) == (plan,)


def test_planned_merge_does_not_depend_on_late_feeder_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routing_core,
        "_land_merge_feeders_on_trunk",
        lambda _routes, _ctx: None,
    )
    graph, offsets, observed = _observe(TOPOLOGIES / "merge_feeders_three_columns.mmd")

    assert not check_merge_feeders_land_on_trunk(graph, observed.routes, offsets)


def test_planned_coverage_does_not_depend_on_late_hop_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routing_core,
        "_drop_covered_merge_entry_hops",
        lambda _routes, _ctx, **_kwargs: (),
    )
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    continuation = plan.outgoing_continuations[0]
    query = build_route_plan_query(observed.plan)

    assert continuation.covered_by_member_id is not None
    assert query.bindings_for(continuation.member_id)[0].kind is BindingKind.MERGE_SKIP


def test_planned_convergence_publishes_trunk_and_landing_resources() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    (plan,) = observed.plan.convergence_plans
    references = {
        reference.id: reference for reference in observed.plan.shared_references
    }
    demands = {demand.id: demand for demand in observed.plan.demands}

    assert tuple(references[item].kind for item in plan.shared_reference_ids) == (
        SharedReferenceKind.TRUNK,
        SharedReferenceKind.LANDING_SEQUENCE,
    )
    assert all(demands[item].system_id == plan.system_id for item in plan.demand_ids)


def test_overlapping_foreign_convergence_axes_are_indexed() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    first = observed.plan.convergence_plans[0]
    second = replace(
        first,
        id=ConvergencePlanId("foreign-convergence"),
        system_id=RouteSystemId("foreign-system"),
        shared_reference_ids=(
            SharedReferenceId("foreign-trunk"),
            SharedReferenceId("foreign-landings"),
        ),
    )
    synthetic = replace(observed.plan, convergence_plans=(first, second))
    conflicts = expected_convergence_foreign_references(synthetic)

    assert conflicts[first.id] == (second.shared_reference_ids[0],)
    assert conflicts[second.id][0] == first.shared_reference_ids[0]
    assert any(
        reference_id.startswith("corridor-reference:")
        for reference_id in conflicts[second.id]
    )


@pytest.mark.parametrize(
    ("source_axis", "coordinate", "expected"),
    (
        (DemandAxis.X, 515.0, True),
        (DemandAxis.X, 1_000.0, False),
        (DemandAxis.Y, 198.0, True),
        (DemandAxis.Y, 1_000.0, False),
    ),
)
def test_foreign_exit_turn_conflicts_use_physical_trunk_geometry(
    source_axis: DemandAxis,
    coordinate: float,
    expected: bool,
) -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    convergence = observed.plan.convergence_plans[0]
    exit_turn = next(item for item in observed.plan.exit_turn_plans if item.axes)
    foreign_reference = SharedReferenceId("foreign-exit-turn")
    foreign_exit = replace(
        exit_turn,
        system_id=RouteSystemId("foreign-exit-system"),
        source_run_direction=(
            Direction.R if source_axis is DemandAxis.X else Direction.D
        ),
        source_axis=source_axis,
        axes=tuple(
            replace(axis, axis=source_axis, coordinate=coordinate)
            for axis in exit_turn.axes
        ),
        reference_id=foreign_reference,
    )
    synthetic = replace(observed.plan, exit_turn_plans=(foreign_exit,))

    conflicts = expected_convergence_foreign_references(synthetic)

    assert (foreign_reference in conflicts[convergence.id]) is expected


def test_convergence_endpoint_ownership_matches_final_bindings() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    (plan,) = observed.plan.convergence_plans
    query = build_route_plan_query(observed.plan)

    for ownership in plan.endpoint_ownership:
        (binding,) = query.bindings_for(ownership.member_id)
        if ownership.role is ConvergenceEndpointRole.COVERED_CONTINUATION:
            assert binding.kind in {
                BindingKind.MERGE_SKIP,
                BindingKind.COVERED_MERGE_HOP,
            }
        else:
            assert binding.kind is BindingKind.EMITTED


def test_every_feeder_join_connects_to_the_target_entry() -> None:
    graph, _offsets, observed = _observe(TOPOLOGIES / "merge_feeders_three_columns.mmd")
    (plan,) = observed.plan.convergence_plans
    routes = [
        route for route in observed.routes if route.convergence_plan_id == str(plan.id)
    ]
    entry_point = plan.outgoing_continuations[0].end_point
    neighbours: dict[int, set[int]] = {rank: set() for rank in range(len(routes))}
    for rank, route in enumerate(routes):
        for other_rank, other in enumerate(routes):
            if rank == other_rank:
                continue
            if any(
                point_to_polyline_distance(point, other.points) <= 1e-6
                for point in (route.points[0], route.points[-1])
            ):
                neighbours[rank].add(other_rank)
    entry_routes = {
        rank
        for rank, route in enumerate(routes)
        if point_to_polyline_distance(entry_point, route.points) <= 1e-6
    }

    assert entry_routes
    for landing in plan.landings:
        start = next(
            rank
            for rank, route in enumerate(routes)
            if route.convergence_member_id == str(landing.member_id)
        )
        reachable = {start}
        pending = [start]
        while pending:
            rank = pending.pop()
            for neighbour in neighbours[rank] - reachable:
                reachable.add(neighbour)
                pending.append(neighbour)
        assert reachable & entry_routes, landing


def test_multiple_lines_share_the_target_entry_bundle_order() -> None:
    _graph, _offsets, observed = _observe(FROZEN / "seed_15.mmd")
    plans_by_entry: dict[object, list] = {}
    for plan in observed.plan.convergence_plans:
        plans_by_entry.setdefault(plan.entry_group_ids[0], []).append(plan)
    plans = next(
        items
        for items in plans_by_entry.values()
        if len({plan.line_ids[0] for plan in items}) >= 3
    )
    lane_order = plans[0].lane_order

    assert all(plan.lane_order == lane_order for plan in plans)
    assert {plan.line_ids[0] for plan in plans}.issubset(lane_order)
    for plan in plans:
        expected_rank = lane_order.index(plan.line_ids[0])
        assert all(item.lane_rank == expected_rank for item in plan.landings)
        assert all(
            item.lane_rank == expected_rank for item in plan.outgoing_continuations
        )


@pytest.mark.parametrize("name", ("seed_15.mmd", "seed_41.mmd"))
def test_frozen_recovery_seeds_have_complete_planned_convergences(name: str) -> None:
    graph, offsets, observed = _observe(FROZEN / name)

    assert observed.plan.convergence_plans
    assert all(plan.owns_geometry for plan in observed.plan.convergence_plans)
    assert not check_merge_branches_meet_trunk(graph, observed.routes, offsets)
    assert not check_merge_feeders_land_on_trunk(graph, observed.routes, offsets)


def test_mixed_direct_bypass_and_multirow_approaches_are_frozen() -> None:
    _graph, _offsets, observed = _observe(FROZEN / "seed_15.mmd")
    landings = [
        landing for plan in observed.plan.convergence_plans for landing in plan.landings
    ]

    assert {landing.bypass for landing in landings} == {False, True}
    assert any(landing.long_haul for landing in landings)
    assert any(landing.multiple_row for landing in landings)


def test_packed_adjacency_convergences_are_planned() -> None:
    graph, offsets, observed = _observe(TOPOLOGIES / "merge_adjacent_feeder.mmd")

    assert observed.plan.convergence_plans
    assert all(plan.owns_geometry for plan in observed.plan.convergence_plans)
    assert not check_merge_branches_meet_trunk(graph, observed.routes, offsets)
    assert not check_merge_feeders_land_on_trunk(graph, observed.routes, offsets)


def test_fan_in_merge_settles_the_complete_system_before_emission() -> None:
    graph, offsets, observed = _observe(TOPOLOGIES / "fan_in_merge.mmd")

    assert observed.plan.convergence_plans
    assert all(plan.owns_geometry for plan in observed.plan.convergence_plans)
    assert all(plan.legacy_reason is None for plan in observed.plan.convergence_plans)
    assert not check_merge_branches_meet_trunk(graph, observed.routes, offsets)
    assert not check_merge_feeders_land_on_trunk(graph, observed.routes, offsets)


@pytest.mark.parametrize(
    ("direction", "side"),
    (
        ("LR", PortSide.LEFT),
        ("RL", PortSide.RIGHT),
        ("TB", PortSide.TOP),
        ("BT", PortSide.BOTTOM),
    ),
)
def test_target_section_orientations_use_one_convergence_model(
    direction: str,
    side: PortSide,
) -> None:
    source = (GUIDE / "03b_fan_in_merge.mmd").read_text()
    text = source.replace(
        "subgraph sink [Sink]",
        "subgraph sink [Sink]\n"
        f"        %%metro direction: {direction}\n"
        f"        %%metro entry: {side.value} | main, aux",
    )
    graph, offsets, observed = _observe_text(text)
    sink_plans = [
        plan
        for plan in observed.plan.convergence_plans
        if graph.ports[plan.target_entry_port_ids[0]].section_id == "sink"
    ]

    assert sink_plans
    assert graph.sections["sink"].direction == direction
    assert all(
        graph.ports[plan.target_entry_port_ids[0]].side is side for plan in sink_plans
    )
    assert all(plan.owns_geometry for plan in sink_plans)
    assert all(plan.legacy_reason is None for plan in sink_plans)
    assert not check_merge_feeders_land_on_trunk(graph, observed.routes, offsets)


def test_planned_opening_turns_remain_exact_after_normalization() -> None:
    from nf_metro.layout.routing.normalize import _opening_fanout_descent

    _graph, _offsets, observed = _observe(FROZEN / "seed_15.mmd")
    planned = {
        landing.member_id: landing
        for plan in observed.plan.convergence_plans
        if plan.owns_geometry
        for landing in plan.landings
        if landing.opening_turn_coordinate is not None
    }

    assert planned
    for route in observed.routes:
        if route.convergence_member_id not in {str(item) for item in planned}:
            continue
        landing = planned[
            next(
                member_id
                for member_id in planned
                if str(member_id) == route.convergence_member_id
            )
        ]
        opening = _opening_fanout_descent(route)
        assert opening is not None
        assert opening.x == pytest.approx(landing.opening_turn_coordinate)
        assert tuple(route.points[opening.idx : opening.idx + 2]) == (
            landing.opening_turn_segment
        )
        assert opening.idx in route.convergence_owned_segment_ranks


def test_runtime_guard_rejects_a_mutated_planned_opening_segment() -> None:
    _graph, _offsets, observed = _observe(FROZEN / "seed_15.mmd")
    from nf_metro.layout.routing.normalize import _opening_fanout_descent

    candidates = []
    for plan in observed.plan.convergence_plans:
        for landing in plan.landings:
            if (
                landing.opening_turn_segment is None
                or landing.member_id == plan.primary_trunk_member_id
            ):
                continue
            route = next(
                item
                for item in observed.routes
                if item.convergence_member_id == str(landing.member_id)
            )
            opening = _opening_fanout_descent(route)
            if opening is not None and opening.idx + 2 < len(route.points):
                candidates.append((plan, landing, route, opening))
    plan, _landing, route, opening = candidates[0]
    x, y = route.points[opening.idx + 1]
    route.points[opening.idx + 1] = (x, y + 3.0)
    execution = replace(
        convergence_routing.empty_convergence_plan_execution(),
        plans=(plan,),
        query=convergence_routing._query((plan,), _edge_order(observed)),
    )

    with pytest.raises(ConvergenceInvariantError, match="planned opening"):
        validate_convergence_plans(observed.routes, execution)


@pytest.mark.parametrize(
    ("axis", "route_points", "segment_rank"),
    (
        (
            ConvergenceTrunkAxis(
                DemandAxis.X,
                20.0,
                10.0,
                30.0,
                Direction.R,
                0.0,
                40.0,
            ),
            [
                (0.0, 0.0),
                (12.0, 0.0),
                (12.0, 20.0),
                (30.0, 20.0),
                (30.0, 40.0),
                (40.0, 40.0),
            ],
            1,
        ),
        (
            ConvergenceTrunkAxis(
                DemandAxis.Y,
                20.0,
                10.0,
                30.0,
                Direction.D,
                0.0,
                40.0,
            ),
            [
                (0.0, 0.0),
                (0.0, 12.0),
                (20.0, 12.0),
                (20.0, 30.0),
                (40.0, 30.0),
                (40.0, 40.0),
            ],
            1,
        ),
    ),
)
def test_trunk_flank_settlement_rederives_curve_radii(
    axis: ConvergenceTrunkAxis,
    route_points: list[tuple[float, float]],
    segment_rank: int,
) -> None:
    route = RoutedPath(
        Edge("source", "target", "line"),
        "line",
        route_points,
        is_inter_section=True,
        curve_radii=[99.0] * (len(route_points) - 2),
        offset_regime=OffsetRegime.BAKED,
    )

    _seat_route_on_trunk_flanks(route, axis, MetroGraph(), lane_offset=2.0)

    assert route.curve_radii is not None
    radii_and_offsets = (
        (segment_rank - 1, 2.0),
        (segment_rank, 0.0),
        (segment_rank + 1, 0.0),
        (segment_rank + 2, -2.0),
    )
    for radius_rank, offset in radii_and_offsets:
        assert route.curve_radii[radius_rank] == pytest.approx(
            concentric_corner_radius_at(
                *route.points[radius_rank : radius_rank + 3],
                offset,
                10.0,
            )
        )
        assert route.curve_radii[radius_rank] != 99.0


def test_perpendicular_entry_convergences_plan_one_trunk_per_crossing_column() -> None:
    """Three merges into one TOP port are one route system, and the planner owns it.

    Each merge stands on the vertical lead-in its perpendicular port receives, so
    its trunk runs the column that port's own crossing gives the merged line --
    the column its feeders descend, its sibling landing from outside the system
    descends, and the intra-section departure leaves from.  Every member of the
    system therefore terminates on one point per line, which is what lets each
    plan state a ``SHARED_TERMINAL_APPROACH`` trunk instead of falling back to the
    junction's own column and disagreeing with its own landings.

    The three lines' trunks are one offset step apart because that is the spacing
    the port lanes its crossings at, and each is the column the emitted geometry
    draws: a plan whose trunk a later fusion had to move would be refused.
    """
    path = (
        ROOT
        / "tests"
        / "fixtures"
        / "regressions"
        / "cross_column_perp_entry_overflow.mmd"
    )
    graph, offsets, observed = _observe(path)
    plans = observed.plan.convergence_plans

    assert len(plans) == 3
    assert len({plan.system_id for plan in plans}) == 1
    assert {plan.disposition for plan in plans} == {ConvergenceDisposition.PLANNED}
    assert {plan.conflict for plan in plans} == {None}
    assert {plan.primary_trunk_reason for plan in plans} == {
        ConvergenceTrunkReason.SHARED_TERMINAL_APPROACH
    }

    port = graph.stations["reporting__entry_top_7"]
    axis_by_line = {}
    for plan in plans:
        assert plan.owns_geometry
        axis = plan.trunk_axis
        assert axis is not None
        assert axis.axis is DemandAxis.Y
        assert axis.direction is Direction.D
        # The trunk ends on the port's edge, where its own landings join it.
        assert axis.extent_end == pytest.approx(port.y)
        for landing in plan.landings:
            assert landing.join_point == pytest.approx((axis.coordinate, port.y))
        axis_by_line[plan.line_ids[0]] = axis.coordinate

    # Each trunk stands in the column the port states for its line, so no two
    # of them share one.
    for line_id, coordinate in axis_by_line.items():
        assert coordinate == pytest.approx(port.x + offsets[(port.id, line_id)])
    assert len(set(axis_by_line.values())) == len(plans)


def test_a_planned_feeder_leaves_its_fan_on_its_own_line_s_column() -> None:
    """One line leaves one source on one column, plan or no plan.

    ``_divergent_source_groups`` fuses each line's descents at a shared source
    onto the column its bundled member holds there.  A plan that froze its
    feeder's own handler column would stand that descent one lane off its own
    colour: the line drawn twice, and drawn over the neighbouring line whose lane
    it took.
    """
    from nf_metro.layout.routing.normalize import _opening_fanout_descent

    path = (
        ROOT
        / "tests"
        / "fixtures"
        / "regressions"
        / "cross_column_perp_entry_overflow.mmd"
    )
    _graph, _offsets, observed = _observe(path)
    columns: dict[tuple[str, str], set[float]] = {}
    for route in observed.routes:
        channel = _opening_fanout_descent(route)
        if channel is None:
            continue
        columns.setdefault((route.edge.source, route.line_id), set()).add(
            round(channel.x, 3)
        )

    fan_sources = {
        source for source, _line in columns if source.startswith("__junction")
    }
    assert fan_sources
    for source in fan_sources:
        per_line = {
            line: lanes for (src, line), lanes in columns.items() if src == source
        }
        assert len(per_line) > 1
        for line, lanes in per_line.items():
            assert len(lanes) == 1, (source, line, sorted(lanes))
        held = [lanes.pop() for lanes in per_line.values()]
        assert len(set(held)) == len(held), (source, sorted(held))


def test_planned_landing_facts_match_emitted_terminal_geometry() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]

    for landing in plan.landings:
        route = next(
            item
            for item in observed.routes
            if item.convergence_member_id == str(landing.member_id)
        )
        approach = convergence_routing._landing_approach(route, landing.join_point)
        assert approach is not None
        direction, handedness, runway = approach
        assert direction is landing.approach_direction
        assert handedness is landing.corner_handedness
        assert runway >= landing.minimum_runway
        if landing.member_id != plan.primary_trunk_member_id:
            assert not route.normalize_exempt


def test_runtime_guard_rejects_reduced_planned_landing_runway() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    landing = next(
        item
        for item in plan.landings
        if item.member_id != plan.primary_trunk_member_id and item.minimum_runway > 2.0
    )
    route = next(
        item
        for item in observed.routes
        if item.convergence_member_id == str(landing.member_id)
    )
    join_x, join_y = landing.join_point
    if landing.approach_axis is DemandAxis.X:
        sign = 1 if landing.approach_direction is Direction.R else -1
        route.points[-2] = (join_x - sign * 2.0, join_y)
    else:
        sign = 1 if landing.approach_direction is Direction.D else -1
        route.points[-2] = (join_x, join_y - sign * 2.0)
    execution = replace(
        convergence_routing.empty_convergence_plan_execution(),
        plans=(plan,),
        query=convergence_routing._query((plan,), _edge_order(observed)),
    )

    with pytest.raises(ConvergenceInvariantError, match="runway"):
        validate_convergence_plans(observed.routes, execution)


@pytest.mark.parametrize(
    ("entry_point", "axis", "direction"),
    (
        ((20.0, 10.0), DemandAxis.X, "R"),
        ((0.0, 10.0), DemandAxis.X, "L"),
        ((10.0, 20.0), DemandAxis.Y, "D"),
        ((10.0, 0.0), DemandAxis.Y, "U"),
    ),
)
def test_direct_trunk_axis_rotates_and_reverses(
    entry_point: tuple[float, float],
    axis: DemandAxis,
    direction: str,
) -> None:
    merge = Station("merge", "", x=10.0, y=10.0)
    entry = Station("entry", "", x=entry_point[0], y=entry_point[1])
    trunk = _direct_axis(merge, entry)

    assert trunk.axis is axis
    assert trunk.direction.value == direction


def test_unregistered_convergence_failure_cannot_open_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args, **_kwargs):
        raise UnsupportedConvergenceError("synthetic unregistered convergence failure")

    monkeypatch.setattr(convergence_routing, "_build_planned_convergence", reject)
    with pytest.raises(
        ValueError,
        match="unregistered compatibility reason convergence-plan",
    ):
        _observe(FROZEN / "seed_15.mmd")


@pytest.mark.parametrize("error", (AssertionError("bug"), TypeError("bug")))
def test_programming_errors_do_not_silently_fall_back(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(convergence_routing, "_build_planned_convergence", reject)

    with pytest.raises(type(error), match="bug"):
        _observe(FROZEN / "seed_15.mmd")


def test_incomplete_semantic_membership_is_a_planning_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_args, **_kwargs):
        raise ConvergencePlanningError("missing member")

    monkeypatch.setattr(convergence_routing, "_plan_membership", reject)

    with pytest.raises(ConvergencePlanningError, match="missing member"):
        _observe(FROZEN / "seed_15.mmd")


def test_incompatible_custom_exit_spacing_uses_one_compatibility_system() -> None:
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph, offset_step=10.0)
    observed = observe_route_edges(
        graph,
        station_offsets=offsets,
        offset_step=10.0,
    )
    plans = observed.plan.convergence_plans

    assert plans
    assert {plan.disposition for plan in plans} == {ConvergenceDisposition.LEGACY}
    assert {plan.legacy_reason for plan in plans} == {
        "convergence landing conflicts with an upstream exit turn"
    }
    system = next(
        item for item in observed.plan.systems if item.id == plans[0].system_id
    )
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    assert {
        (reason.owner, reason.reason) for reason in system.compatibility_reasons
    } == {
        (
            "convergence-plan",
            "convergence landing conflicts with an upstream exit turn",
        )
    }


def test_runtime_guard_names_the_plan_member_and_broken_join() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    ownership = next(
        item
        for item in plan.endpoint_ownership
        if item.role is ConvergenceEndpointRole.FEEDER
        and plan.connector_ids[0] not in item.connector_ids
    )
    landing = next(
        item for item in plan.landings if item.member_id == ownership.member_id
    )
    route = next(
        item
        for item in observed.routes
        if item.convergence_member_id == str(landing.member_id)
    )
    route.points[-1] = (route.points[-1][0], route.points[-1][1] + 100.0)
    execution = replace(
        convergence_routing.empty_convergence_plan_execution(),
        plans=(plan,),
        query=convergence_routing._query((plan,), _edge_order(observed)),
    )

    with pytest.raises(ConvergenceInvariantError) as error:
        validate_convergence_plans(observed.routes, execution)

    message = str(error.value)
    assert str(plan.system_id) in message
    connector_set = ", ".join(
        str(connector_id) for connector_id in ownership.connector_ids
    )
    assert f"connectors {connector_set} member" in message
    assert str(landing.member_id) in message
    assert "planned join" in message
    assert "emitted endpoint" in message


def test_runtime_guard_rejects_a_disconnected_diagonal_trunk() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    route = next(
        item
        for item in observed.routes
        if item.convergence_member_id == str(plan.primary_trunk_member_id)
    )
    axis = plan.trunk_axis
    assert axis is not None
    segment_rank = next(
        rank
        for rank, (start, end) in enumerate(zip(route.points, route.points[1:]))
        if abs(start[1] - axis.coordinate) < 1e-6
        and abs(end[1] - axis.coordinate) < 1e-6
        and min(start[0], end[0]) <= axis.extent_start
        and max(start[0], end[0]) >= axis.extent_end
    )
    end = route.points[segment_rank + 1]
    route.points[segment_rank + 1] = (end[0], end[1] + 20.0)
    execution = replace(
        convergence_routing.empty_convergence_plan_execution(),
        plans=(plan,),
        query=convergence_routing._query((plan,), _edge_order(observed)),
    )

    with pytest.raises(ConvergenceInvariantError, match="does not emit planned"):
        validate_convergence_plans(observed.routes, execution)


def test_runtime_guard_rejects_a_missing_terminal_trunk_cap() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    route = next(
        item
        for item in observed.routes
        if item.convergence_member_id == str(plan.primary_trunk_member_id)
    )
    axis = plan.trunk_axis
    assert axis is not None
    assert axis.source_endpoint_coordinate is not None
    source_longitudinal = axis.extent_start
    segment_rank = next(
        rank
        for rank, (start, end) in enumerate(zip(route.points, route.points[1:]))
        if abs(start[1] - axis.source_flank_coordinate) < 1e-6
        and abs(end[1] - axis.source_flank_coordinate) < 1e-6
        and min(start[0], end[0]) <= axis.source_endpoint_coordinate
        and max(start[0], end[0]) >= source_longitudinal
    )
    route.points[segment_rank + 1] = route.points[segment_rank]
    execution = replace(
        convergence_routing.empty_convergence_plan_execution(),
        plans=(plan,),
        query=convergence_routing._query((plan,), _edge_order(observed)),
    )

    with pytest.raises(ConvergenceInvariantError, match="does not emit planned"):
        validate_convergence_plans(observed.routes, execution)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("entry_group_ids", ("unknown-entry",)),
        ("merge_junction_ids", ("unknown-merge",)),
        ("target_entry_port_ids", ("unknown-port",)),
    ),
)
def test_route_plan_rejects_mutated_convergence_semantic_identity(
    field: str, value: tuple[str, ...]
) -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    mutated = replace(plan, **{field: value})
    route_plan = replace(observed.plan, convergence_plans=(mutated,))

    with pytest.raises(ValueError, match="semantic fields"):
        build_route_plan_query(route_plan)


def test_route_plan_rejects_coverage_that_disagrees_with_binding() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    covered = next(
        item
        for item in plan.endpoint_ownership
        if item.role is ConvergenceEndpointRole.COVERED_CONTINUATION
    )
    ownership = tuple(
        replace(
            item,
            role=ConvergenceEndpointRole.CONTINUATION,
            covered_by_member_id=None,
        )
        if item.member_id == covered.member_id
        else item
        for item in plan.endpoint_ownership
    )
    mutated = replace(plan, endpoint_ownership=ownership)
    route_plan = replace(observed.plan, convergence_plans=(mutated,))

    with pytest.raises(ValueError, match="endpoint owner"):
        build_route_plan_query(route_plan)


def test_route_plan_rejects_endpoint_connectors_from_another_member() -> None:
    _graph, _offsets, observed = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    plan = observed.plan.convergence_plans[0]
    first, second = plan.endpoint_ownership[:2]
    assert first.connector_ids != second.connector_ids
    ownership = (
        replace(first, connector_ids=second.connector_ids),
        *plan.endpoint_ownership[1:],
    )
    mutated = replace(plan, endpoint_ownership=ownership)
    route_plan = replace(observed.plan, convergence_plans=(mutated,))

    with pytest.raises(ValueError, match="connectors disagree with member"):
        build_route_plan_query(route_plan)


@pytest.mark.parametrize(
    "changes",
    (
        {"kind": SharedReferenceKind.BAND},
        {"coordinate_regime": CoordinateRegime.RELATIVE_FRAME},
        {"claimant_member_ids": ()},
    ),
)
def test_route_plan_rejects_mutated_convergence_references(
    changes: dict[str, object],
    three_column_route_plan: RoutePlan,
) -> None:
    plan = three_column_route_plan.convergence_plans[0]
    reference_id = plan.shared_reference_ids[0]
    malformed = next(
        replace(item, **changes)
        for item in three_column_route_plan.shared_references
        if item.id == reference_id
    )
    references = tuple(
        malformed if item.id == reference_id else item
        for item in three_column_route_plan.shared_references
    )

    with pytest.raises(ValueError, match="shared references"):
        build_route_plan_query(
            replace(three_column_route_plan, shared_references=references)
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"kind": DemandKind.KEEP_OUT},
        {"axis": DemandAxis.BOTH},
        {"lane_count": 999},
        {"ordered_reference_ids": ()},
        {"keep_out_classes": (KeepOutClass.SECTION,)},
        {"claimant_member_ids": ()},
    ),
)
def test_route_plan_rejects_mutated_convergence_lane_demands(
    changes: dict[str, object],
    three_column_route_plan: RoutePlan,
) -> None:
    plan = three_column_route_plan.convergence_plans[0]
    demand_id = plan.demand_ids[0]
    malformed = next(
        replace(item, **changes)
        for item in three_column_route_plan.demands
        if item.id == demand_id
    )
    demands = tuple(
        malformed if item.id == demand_id else item
        for item in three_column_route_plan.demands
    )

    with pytest.raises(ValueError, match="symbolic demands"):
        build_route_plan_query(replace(three_column_route_plan, demands=demands))


def test_route_plan_rejects_mutated_convergence_runway_demand(
    three_column_route_plan: RoutePlan,
) -> None:
    plan = three_column_route_plan.convergence_plans[0]
    demand_id = plan.demand_ids[1]
    malformed = next(
        replace(item, minimum_size=item.minimum_size + 1.0)
        for item in three_column_route_plan.demands
        if item.id == demand_id and item.minimum_size is not None
    )
    demands = tuple(
        malformed if item.id == demand_id else item
        for item in three_column_route_plan.demands
    )

    with pytest.raises(ValueError, match="symbolic demands"):
        build_route_plan_query(replace(three_column_route_plan, demands=demands))


def test_route_plan_rejects_duplicate_semantic_convergence_coverage(
    right_entry_route_plan: RoutePlan,
) -> None:
    (plan,) = right_entry_route_plan.convergence_plans
    duplicate = replace(plan, id=ConvergencePlanId("duplicate-convergence-plan"))
    systems = tuple(
        replace(
            item,
            convergence_plan_ids=(*item.convergence_plan_ids, duplicate.id),
        )
        if item.id == plan.system_id
        else item
        for item in right_entry_route_plan.systems
    )
    route_plan = replace(
        right_entry_route_plan,
        systems=systems,
        convergence_plans=(plan, duplicate),
    )

    with pytest.raises(ValueError, match="resource identities|coverage"):
        build_route_plan_query(route_plan)


def test_route_plan_rejects_missing_semantic_convergence_coverage(
    right_entry_route_plan: RoutePlan,
) -> None:
    (plan,) = right_entry_route_plan.convergence_plans
    systems = tuple(
        replace(item, convergence_plan_ids=()) if item.id == plan.system_id else item
        for item in right_entry_route_plan.systems
    )

    with pytest.raises(ValueError, match="coverage"):
        build_route_plan_query(
            replace(right_entry_route_plan, systems=systems, convergence_plans=())
        )


def test_route_plan_rejects_incomplete_convergence_emission_membership(
    right_entry_route_plan: RoutePlan,
) -> None:
    (plan,) = right_entry_route_plan.convergence_plans
    remaining_paths = plan.resolved_member_paths[1:]
    remaining_edges = tuple(
        dict.fromkeys(edge for path in remaining_paths for edge in path)
    )
    member_by_edge = {item.edge: item.id for item in right_entry_route_plan.members}
    remaining_member_ids = tuple(member_by_edge[edge] for edge in remaining_edges)
    with pytest.raises(ValueError, match="ownership"):
        replace(
            plan,
            member_ids=remaining_member_ids,
            resolved_member_paths=remaining_paths,
            resolved_member_edges=remaining_edges,
            endpoint_ownership=tuple(
                item
                for item in plan.endpoint_ownership
                if item.member_id in remaining_member_ids
            ),
        )
