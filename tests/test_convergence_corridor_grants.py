"""The convergence adapter publishes each planned trunk skeleton as one request.

Every test here calls :func:`convergence_corridor_requests` and nothing that
solves or applies a request: this slice exposes the trunk skeleton, it does not
move it.  The skeleton is one connected six-point polyline of five runs -- the
central run the axis states, its two flank connectors, and the two flank runs --
whatever the plan contains.
"""

import math
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS, DIAGONAL_RUN
from nf_metro.layout.route_plan import ConvergenceTrunkAxis, DemandAxis, Direction
from nf_metro.layout.routing import convergences
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.convergences import (
    ConvergenceInvariantError,
    ConvergencePlanExecution,
    convergence_corridor_requests,
)
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortCompilationError,
    CorridorCrossingDisposition,
    build_corridor_footprint_witnesses,
)
from nf_metro.layout.routing.offsets import compute_station_offsets

ROOT = Path(__file__).parents[1]

_COLLAPSED_FIXTURES = (
    "examples/riboseq_metro.mmd",
    "examples/topologies/merge_bottom_row_bypass.mmd",
    "examples/topologies/merge_feeder_shared_channel_gap.mmd",
)


def _corpus_paths() -> tuple[Path, ...]:
    roots = (ROOT / "examples", ROOT / "examples" / "topologies")
    return tuple(sorted({path for root in roots for path in root.glob("*.mmd")}))


def _eligible_plans_by_owner(execution):
    return {
        str(plan.id): plan
        for plan in execution.plans
        if plan.owns_geometry and plan.trunk_axis is not None
    }


@lru_cache(maxsize=None)
def _requests_for(path_str: str):
    path = Path(path_str)
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    station_offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=station_offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, station_offsets)
    plans = observation.plan.convergence_plans
    edge_order = tuple(member.edge for member in observation.plan.members)
    execution = ConvergencePlanExecution(
        plans,
        (),
        (),
        (),
        convergences._query(plans, edge_order),
    )
    targets, requests = convergence_corridor_requests(plans, graph, ctx)
    return graph, ctx, execution, targets, requests


def _controlled_points_map(requests):
    return {
        (point.member_id, point.edge_key, point.point_rank, point.axis): (
            request.variable.variable_id,
            point.source_offset,
        )
        for request in requests
        if request.control_recipe is not None
        for point in request.control_recipe.controlled_points
    }


def _witnesses(targets, requests):
    return build_corridor_footprint_witnesses(
        targets,
        tuple(request.variable for request in requests),
        None,
        _controlled_points_map(requests),
    )


def _corridor_fixture():
    _graph, _ctx, execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    assert len(requests) >= 2
    return execution, targets, requests


def test_adapter_exposes_every_planned_trunk_without_replacing_members() -> None:
    execution, targets, requests = _corridor_fixture()
    eligible = _eligible_plans_by_owner(execution)

    assert {request.variable.owner_id for request in requests} == set(eligible)
    assert len(targets) == len(requests) == len(eligible)
    assert len({target.member_id for target in targets}) == len(targets)
    for target, request in zip(targets, requests, strict=True):
        plan = eligible[request.variable.owner_id]
        assert target.mutable
        assert target.member_id == request.variable.member_id
        assert target.member_id != plan.primary_trunk_member_id
        assert request.variable.coordinate == plan.trunk_axis.coordinate
        assert request.domain.member_id == request.variable.variable_id


def test_every_eligible_trunk_is_a_closed_six_point_skeleton_across_both_corpora() -> (
    None
):
    seen_trunks = 0
    for path in _corpus_paths():
        _graph, _ctx, execution, targets, requests = _requests_for(str(path))
        eligible = _eligible_plans_by_owner(execution)
        assert {request.variable.owner_id for request in requests} == set(eligible)
        for target, request in zip(targets, requests, strict=True):
            seen_trunks += 1
            plan = eligible[request.variable.owner_id]
            axis = plan.trunk_axis
            points = target.route.points
            assert len(points) == 6
            assert request.variable.segment_rank == 2
            assert request.variable.coordinate == axis.coordinate
            central_start, central_end = points[2], points[3]
            moving = request.variable.axis
            longitudinal = 1 - moving
            assert abs(central_start[moving] - axis.coordinate) <= COORD_TOLERANCE
            assert abs(central_end[moving] - axis.coordinate) <= COORD_TOLERANCE
            forward = axis.direction in {Direction.R, Direction.D}
            if forward:
                assert central_start[longitudinal] < central_end[longitudinal]
            else:
                assert central_start[longitudinal] > central_end[longitudinal]
    assert seen_trunks == 31


def test_left_running_trunk_central_run_is_oriented_against_the_listing() -> None:
    for path in _corpus_paths():
        _graph, _ctx, execution, targets, requests = _requests_for(str(path))
        by_owner = _eligible_plans_by_owner(execution)
        for target, request in zip(targets, requests, strict=True):
            axis = by_owner[request.variable.owner_id].trunk_axis
            if axis.direction is not Direction.L:
                continue
            central_start, central_end = target.route.points[2], target.route.points[3]
            assert central_start[0] > central_end[0]
            return
    pytest.fail("no left-running trunk found in the corpora")


def test_flank_connectors_are_named_controlled_footprints() -> None:
    _graph, _ctx, _execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    witnesses = _witnesses(targets, requests)
    variable_ids = {request.variable.variable_id for request in requests}
    connectors = [
        witness
        for witness in witnesses
        if witness.segment_rank in (1, 3)
        and witness.axis
        != next(
            request.variable.axis
            for request in requests
            if request.variable.owner_id == witness.owner_id
        )
    ]
    assert connectors
    for connector in connectors:
        endpoint_variables = {connector.start_variable_id, connector.end_variable_id}
        assert endpoint_variables & variable_ids
        assert connector.coordinate_variable_id is None


def test_central_run_carries_the_scalar_variable_at_its_actual_rank() -> None:
    _graph, _ctx, _execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    witnesses = _witnesses(targets, requests)
    for request in requests:
        central = [
            witness
            for witness in witnesses
            if witness.owner_id == request.variable.owner_id
            and witness.segment_rank == 2
        ]
        assert len(central) == 1
        assert central[0].coordinate_variable_id == request.variable.variable_id


@pytest.mark.parametrize("fixture", _COLLAPSED_FIXTURES)
def test_collapsed_flank_never_stands_uncontrolled_on_the_moving_coordinate(
    fixture: str,
) -> None:
    _graph, _ctx, execution, targets, requests = _requests_for(str(ROOT / fixture))
    by_owner = _eligible_plans_by_owner(execution)
    witnesses = _witnesses(targets, requests)
    collapsed_owners = {
        request.variable.owner_id
        for request in requests
        if _has_collapsed_flank(by_owner[request.variable.owner_id].trunk_axis)
    }
    assert collapsed_owners
    for witness in witnesses:
        variable = next(
            request.variable
            for request in requests
            if request.variable.owner_id == witness.owner_id
        )
        stands_on_moving = (
            witness.axis == variable.axis
            and abs(witness.coordinate - variable.coordinate) <= COORD_TOLERANCE
        )
        if stands_on_moving:
            assert witness.coordinate_variable_id == variable.variable_id


def _has_collapsed_flank(axis: ConvergenceTrunkAxis) -> bool:
    return (
        abs(axis.source_flank_coordinate - axis.coordinate) <= COORD_TOLERANCE
        or abs(axis.target_flank_coordinate - axis.coordinate) <= COORD_TOLERANCE
    )


def _synthetic_plan(axis: ConvergenceTrunkAxis):
    _graph, ctx, execution, _targets, _requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    plan = next(iter(_eligible_plans_by_owner(execution).values()))
    return replace(plan, trunk_axis=axis), ctx


def test_non_degenerate_collapsed_flank_run_moves_with_the_trunk() -> None:
    axis = ConvergenceTrunkAxis(
        axis=DemandAxis.X,
        coordinate=100.0,
        extent_start=200.0,
        extent_end=400.0,
        direction=Direction.R,
        source_flank_coordinate=100.0,
        target_flank_coordinate=50.0,
        source_endpoint_coordinate=150.0,
        target_endpoint_coordinate=450.0,
    )
    plan, ctx = _synthetic_plan(axis)
    target, variable, recipe = convergences._convergence_corridor_target(plan, ctx)
    witnesses = build_corridor_footprint_witnesses(
        (target,),
        (variable,),
        None,
        {
            (point.member_id, point.edge_key, point.point_rank, point.axis): (
                variable.variable_id,
                point.source_offset,
            )
            for point in recipe.controlled_points
        },
    )
    source_flank = next(w for w in witnesses if w.segment_rank == 0)
    target_flank = next(w for w in witnesses if w.segment_rank == 4)
    assert abs(source_flank.coordinate - variable.coordinate) <= COORD_TOLERANCE
    assert source_flank.coordinate_variable_id == variable.variable_id
    assert (
        source_flank.crossing_disposition is CorridorCrossingDisposition.LEGAL_CROSSING
    )
    assert target_flank.coordinate_variable_id is None
    assert target_flank.crossing_disposition is CorridorCrossingDisposition.FIXED_DOGLEG


def test_crossings_and_doglegs_are_distinguished_per_segment() -> None:
    _graph, _ctx, _execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "merge_bottom_row_bypass.mmd")
    )
    witnesses = _witnesses(targets, requests)
    dispositions = {witness.crossing_disposition for witness in witnesses}
    assert CorridorCrossingDisposition.LEGAL_CROSSING in dispositions
    assert CorridorCrossingDisposition.FIXED_DOGLEG in dispositions
    central = next(w for w in witnesses if w.segment_rank == 2)
    assert central.crossing_disposition is CorridorCrossingDisposition.LEGAL_CROSSING
    fixed_flank = next(
        w
        for w in witnesses
        if w.crossing_disposition is CorridorCrossingDisposition.FIXED_DOGLEG
    )
    assert fixed_flank.coordinate_variable_id is None


@pytest.mark.parametrize("direction", [Direction.D, Direction.U], ids=["down", "up"])
def test_vertical_trunk_skeleton_closes_for_both_travel_directions(
    direction: Direction,
) -> None:
    axis = ConvergenceTrunkAxis(
        axis=DemandAxis.Y,
        coordinate=100.0,
        extent_start=200.0,
        extent_end=400.0,
        direction=direction,
        source_flank_coordinate=60.0,
        target_flank_coordinate=140.0,
        source_endpoint_coordinate=180.0,
        target_endpoint_coordinate=420.0,
    )
    points = convergences._trunk_skeleton_travel_points(axis)
    assert len(points) == 6
    central_start, central_end = points[2], points[3]
    assert abs(central_start[0] - axis.coordinate) <= COORD_TOLERANCE
    assert abs(central_end[0] - axis.coordinate) <= COORD_TOLERANCE
    if direction is Direction.D:
        assert central_start[1] < central_end[1]
    else:
        assert central_start[1] > central_end[1]


def test_requests_fail_closed_on_duplicate_plan_owners() -> None:
    graph, ctx, execution, _targets, _requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    plan = next(iter(_eligible_plans_by_owner(execution).values()))
    with pytest.raises(ConvergenceInvariantError, match="duplicate plan owners"):
        convergence_corridor_requests((plan, plan), graph, ctx)


def test_requests_fail_closed_on_incomplete_trunk_identity() -> None:
    graph, ctx, execution, _targets, _requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    stripped = replace(ctx, edge_by_key={})
    with pytest.raises(ConvergenceInvariantError, match="incomplete trunk identity"):
        convergence_corridor_requests(execution.plans, graph, stripped)


def test_nonfinite_scalar_coordinate_fails_closed() -> None:
    _graph, _ctx, _execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    variable = replace(requests[0].variable, coordinate=math.inf)
    with pytest.raises(CorridorCohortCompilationError):
        build_corridor_footprint_witnesses((targets[0],), (variable,))


def test_translating_the_recipe_moves_central_and_pivots_only() -> None:
    delta = 37.0
    _graph, _ctx, execution, targets, requests = _requests_for(
        str(ROOT / "examples" / "topologies" / "fan_in_merge.mmd")
    )
    by_owner = _eligible_plans_by_owner(execution)
    for target, request in zip(targets, requests, strict=True):
        axis = by_owner[request.variable.owner_id].trunk_axis
        if _has_collapsed_flank(axis):
            continue
        points = target.route.points
        controlled = {
            (point.point_rank, point.axis)
            for point in request.control_recipe.controlled_points
        }
        assert controlled == {(2, request.variable.axis), (3, request.variable.axis)}
        moved = _translate(points, controlled, delta)
        moving = request.variable.axis
        assert moved[2][moving] == pytest.approx(points[2][moving] + delta)
        assert moved[3][moving] == pytest.approx(points[3][moving] + delta)
        for flank_point in (0, 1, 4, 5):
            assert moved[flank_point] == points[flank_point]
        assert len(moved) == len(points)
        return
    pytest.fail("no non-collapsed trunk found in fan_in_merge")


def _translate(points, controlled, delta):
    moved = []
    for rank, coordinate in enumerate(points):
        shifted = list(coordinate)
        for axis in (0, 1):
            if (rank, axis) in controlled:
                shifted[axis] += delta
        moved.append(tuple(shifted))
    return moved


def test_opposite_running_trunks_stay_direction_qualified_and_unbundled() -> None:
    central_directions = set()
    for path in _corpus_paths():
        _graph, _ctx, _execution, targets, requests = _requests_for(str(path))
        witnesses = _witnesses(targets, requests)
        for witness in witnesses:
            if witness.segment_rank == 2:
                central_directions.add(witness.direction)
        member_ids = [target.member_id for target in targets]
        owner_ids = [request.variable.owner_id for request in requests]
        assert len(set(member_ids)) == len(member_ids)
        assert len(set(owner_ids)) == len(owner_ids)
    assert {Direction.R, Direction.L} <= central_directions
