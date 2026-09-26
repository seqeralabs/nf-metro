"""Convergence trunks are published as corridor requests and granted back.

The adapter tests call :func:`convergence_corridor_requests` alone.  The trunk
skeleton it publishes is one connected six-point polyline of five runs -- the
central run the axis states, its two flank connectors, and the two flank runs --
whatever the plan contains.

The grant tests hand those requests' own recipes back to
:func:`apply_convergence_corridor_grants`, either directly with a chosen
coordinate or through a full render whose corridor preference is shifted by a
known amount: a corpus fixture's solve moves a trunk only onto a lane its own
bundle or a crossing leg forces, which settlement forces as well, so a clean
render diff says nothing about whether granted coordinates reach the drawing.
"""

import math
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    DIAGONAL_RUN,
    OFFSET_STEP,
)
from nf_metro.layout.route_plan import (
    ConvergenceEndpointRole,
    ConvergencePlan,
    ConvergenceTrunkAxis,
    DemandAxis,
    Direction,
    RoutePlan,
)
from nf_metro.layout.routing import (
    convergences,
    corridor_cohort_integration,
    corridor_cohorts,
    planning,
)
from nf_metro.layout.routing.common import RoutedPath, _points_coincide
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.convergences import (
    CONVERGENCE_CORRIDOR_GRANT_APPLIED,
    ConvergenceInvariantError,
    ConvergencePlanExecution,
    apply_convergence_corridor_grants,
    convergence_corridor_requests,
)
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortCompilationError,
    CorridorCrossingDisposition,
    CorridorScalarFixedPoint,
    CorridorScalarGrant,
    CorridorScalarOwnerKind,
    CorridorScalarRequest,
    _validate_control_recipe,
    build_corridor_footprint_witnesses,
)
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.render import svg
from nf_metro.render.svg import _convergence_decision

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


def _trunk_target_for(targets, request):
    return next(
        target for target in targets if target.member_id == request.variable.member_id
    )


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
    assert len(requests) == len(eligible)
    assert len({target.member_id for target in targets}) == len(targets)
    for request in requests:
        plan = eligible[request.variable.owner_id]
        target = _trunk_target_for(targets, request)
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
        for request in requests:
            seen_trunks += 1
            plan = eligible[request.variable.owner_id]
            target = _trunk_target_for(targets, request)
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
    assert seen_trunks == 32


def test_left_running_trunk_central_run_is_oriented_against_the_listing() -> None:
    for path in _corpus_paths():
        _graph, _ctx, execution, targets, requests = _requests_for(str(path))
        by_owner = _eligible_plans_by_owner(execution)
        for request in requests:
            axis = by_owner[request.variable.owner_id].trunk_axis
            if axis.direction is not Direction.L:
                continue
            target = _trunk_target_for(targets, request)
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
    target, _dependants, variable, recipe = convergences._convergence_corridor_target(
        plan, ctx
    )
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
    for request in requests:
        axis = by_owner[request.variable.owner_id].trunk_axis
        if _has_collapsed_flank(axis):
            continue
        target = _trunk_target_for(targets, request)
        points = target.route.points
        controlled = {
            (point.point_rank, point.axis)
            for point in request.control_recipe.controlled_points
            if point.member_id == request.variable.member_id
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


def test_recipe_controls_every_landing_join_opening_turn_and_continuation_start() -> (
    None
):
    execution, targets, requests = _corridor_fixture()
    by_owner = _eligible_plans_by_owner(execution)
    saw_fixed_landing_join = False
    saw_fixed_feeder_endpoint = False
    for request in requests:
        plan = by_owner[request.variable.owner_id]
        recipe = request.control_recipe
        variable_id = request.variable.variable_id

        _validate_control_recipe(request, targets)

        named = {point.role_id for point in recipe.controlled_points} | {
            point.role_id for point in recipe.fixed_points
        }
        assert named == convergences._convergence_recipe_role_ids(plan, variable_id)
        assert len(named) == len(recipe.controlled_points) + len(recipe.fixed_points)

        for landing in plan.landings:
            assert f"{variable_id}|landing:{landing.member_id}|join" in named
            if landing.opening_turn_segment is not None:
                assert f"{variable_id}|landing:{landing.member_id}|opening" in named
                assert f"{variable_id}|landing:{landing.member_id}|opening-end" in named
        for continuation in plan.outgoing_continuations:
            assert f"{variable_id}|continuation:{continuation.member_id}|start" in named
        for ownership in plan.endpoint_ownership:
            if ownership.role is ConvergenceEndpointRole.FEEDER:
                assert f"{variable_id}|feeder:{ownership.member_id}|endpoint" in named

        for role_id in named:
            pruned = replace(
                recipe,
                controlled_points=tuple(
                    point
                    for point in recipe.controlled_points
                    if point.role_id != role_id
                ),
                fixed_points=tuple(
                    point for point in recipe.fixed_points if point.role_id != role_id
                ),
            )
            with pytest.raises(
                ConvergenceInvariantError, match="incomplete control recipe"
            ):
                convergences._validate_convergence_recipe_completeness(
                    plan, variable_id, pruned
                )

        extra = replace(
            recipe,
            fixed_points=(
                *recipe.fixed_points,
                CorridorScalarFixedPoint(
                    member_id=f"{variable_id}|invented|anchor",
                    edge_key=(
                        f"{variable_id}|invented|source",
                        f"{variable_id}|invented|target",
                        f"{variable_id}|invented|line",
                    ),
                    connector_ids=request.variable.connector_ids,
                    point_rank=0,
                    axis=request.variable.axis,
                    coordinate=recipe.source_coordinate,
                    role_id=f"{variable_id}|invented",
                ),
            ),
        )
        with pytest.raises(
            ConvergenceInvariantError, match="incomplete control recipe"
        ):
            convergences._validate_convergence_recipe_completeness(
                plan, variable_id, extra
            )

        for fixed in recipe.fixed_points:
            if fixed.role_id.endswith("|join"):
                saw_fixed_landing_join = True
                assert fixed.coordinate != pytest.approx(recipe.source_coordinate)
            if fixed.role_id.endswith("|endpoint"):
                saw_fixed_feeder_endpoint = True
                assert fixed.coordinate != pytest.approx(recipe.source_coordinate)

    assert saw_fixed_landing_join
    assert saw_fixed_feeder_endpoint


def test_recipe_fails_closed_on_a_fixed_point_off_its_target() -> None:
    _execution, targets, requests = _corridor_fixture()
    request = next(
        request for request in requests if request.control_recipe.fixed_points
    )
    recipe = request.control_recipe
    corrupt = recipe.fixed_points[0]
    broken = replace(
        request,
        control_recipe=replace(
            recipe,
            fixed_points=tuple(
                replace(point, coordinate=point.coordinate + 100.0)
                if point is corrupt
                else point
                for point in recipe.fixed_points
            ),
        ),
    )
    with pytest.raises(CorridorCohortCompilationError, match="invalid fixed point"):
        _validate_control_recipe(broken, targets)


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


_FUNCPROFILER = "examples/topologies/funcprofiler_upstream.mmd"
_MERGE_RIGHT_ENTRY = "examples/topologies/merge_right_entry.mmd"
_FAN_IN_MERGE = "examples/topologies/fan_in_merge.mmd"
_MERGE_PULLAWAY = "examples/topologies/merge_pullaway.mmd"
_STACKED_COLLECTOR = "tests/fixtures/regressions/stacked_collector_fanin.mmd"
_CROSS_COLUMN = "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd"
_MOVABLE_FIXTURES = (_FUNCPROFILER, _MERGE_RIGHT_ENTRY, _FAN_IN_MERGE, _MERGE_PULLAWAY)
_STATION_PINNED_FIXTURES = (_STACKED_COLLECTOR, _CROSS_COLUMN)
_SHIFT = 12.0

_SKELETON_FIELDS = {
    0: "source_flank_coordinate",
    1: "source_flank_coordinate",
    2: "coordinate",
    3: "coordinate",
    4: "target_flank_coordinate",
    5: "target_flank_coordinate",
}

_PROXIMITY_AND_LEGACY_HELPERS = (
    "_move_trunk_axis",
    "_move_trunk_flank",
    "_reseated_runway",
    "_reseat_landing_opening",
    "_reseat_landing_cross",
    "_move_landing_opening",
    "point_to_polyline_distance",
    "_point_on_trunk_geometry",
    "_route_covers_segment",
    "_closest_point_on_polyline",
    "_on_central_run",
)


def _grant(request: CorridorScalarRequest, coordinate: float) -> CorridorScalarGrant:
    recipe = request.control_recipe
    assert recipe is not None
    return CorridorScalarGrant(
        request.variable.variable_id,
        request.variable.owner_kind,
        request.variable.owner_id,
        coordinate,
        coordinate - recipe.source_coordinate,
        recipe,
    )


def _grants(requests, shift: float = 0.0) -> tuple[CorridorScalarGrant, ...]:
    return tuple(_grant(item, item.variable.coordinate + shift) for item in requests)


def _unbounded(request: CorridorScalarRequest) -> CorridorScalarRequest:
    return replace(
        request,
        domain=replace(
            request.domain, minimum_coordinate=None, maximum_coordinate=None
        ),
    )


def _grant_fixture(fixture: str):
    """A fixture's execution and requests, with every request's domain lifted.

    Lifting the domain separates what the applier writes from whether the
    producer would have allowed the move; the domain is tested on its own.
    """
    _graph, _ctx, execution, _targets, requests = _requests_for(str(ROOT / fixture))
    return execution, tuple(_unbounded(request) for request in requests)


def _plan_scalars(plan: ConvergencePlan) -> dict[tuple[object, ...], float | None]:
    """Every coordinate-bearing scalar *plan* states, keyed by where it lives."""
    axis = plan.trunk_axis
    assert axis is not None
    values: dict[tuple[object, ...], float | None] = {
        ("trunk", name): getattr(axis, name)
        for name in (
            "coordinate",
            "extent_start",
            "extent_end",
            "source_flank_coordinate",
            "target_flank_coordinate",
            "source_endpoint_coordinate",
            "target_endpoint_coordinate",
        )
    }
    for landing in plan.landings:
        member = landing.member_id
        values[("landing", member, "runway")] = landing.minimum_runway
        values[("landing", member, "opening")] = landing.opening_turn_coordinate
        values[("landing", member, "cross_run_start")] = (
            landing.cross_run_start_coordinate
        )
        for index in (0, 1):
            values[("landing", member, "join", index)] = landing.join_point[index]
            if landing.opening_turn_segment is not None:
                start, end = landing.opening_turn_segment
                values[("landing", member, "opening_start", index)] = start[index]
                values[("landing", member, "opening_end", index)] = end[index]
    for item in plan.outgoing_continuations:
        for index in (0, 1):
            values[("continuation", item.member_id, "start", index)] = item.start_point[
                index
            ]
            values[("continuation", item.member_id, "end", index)] = item.end_point[
                index
            ]
    for item in plan.endpoint_ownership:
        for index in (0, 1):
            values[("ownership", item.member_id, item.role, index)] = item.endpoint[
                index
            ]
    return values


def _role_field(
    plan: ConvergencePlan, variable_id: str, role_id: str, axis: int
) -> tuple[object, ...]:
    """The one plan scalar a controlled role writes, per the recipe contract."""
    for rank, name in _SKELETON_FIELDS.items():
        if role_id == f"{variable_id}|point:{rank}":
            return ("trunk", name)
    for landing in plan.landings:
        member = landing.member_id
        if role_id == f"{variable_id}|landing:{member}|join":
            return ("landing", member, "join", axis)
        if role_id == f"{variable_id}|landing:{member}|opening-end":
            return ("landing", member, "opening_end", axis)
    for item in plan.outgoing_continuations:
        if role_id == f"{variable_id}|continuation:{item.member_id}|start":
            return ("continuation", item.member_id, "start", axis)
    for item in plan.endpoint_ownership:
        if role_id == f"{variable_id}|feeder:{item.member_id}|endpoint":
            return ("ownership", item.member_id, item.role, axis)
    raise AssertionError(f"role {role_id} names no writable field")


def _expected_writes(
    plan: ConvergencePlan, request: CorridorScalarRequest, grant: CorridorScalarGrant
) -> dict[tuple[object, ...], float]:
    recipe = request.control_recipe
    assert recipe is not None
    variable_id = request.variable.variable_id
    expected: dict[tuple[object, ...], float] = {}
    joins: dict[str, tuple[int, float]] = {}
    for point in recipe.controlled_points:
        value = grant.coordinate + point.source_offset
        expected[_role_field(plan, variable_id, point.role_id, point.axis)] = value
        joins[point.role_id] = (point.axis, value)
    for runway in recipe.directed_runways:
        join = joins.get(runway.controlled_role_id)
        member = next(
            (
                landing.member_id
                for landing in plan.landings
                if runway.controlled_role_id
                == f"{variable_id}|landing:{landing.member_id}|join"
            ),
            None,
        )
        if join is None or join[0] != runway.axis or member is None:
            continue
        assert runway.anchor_coordinate is not None
        expected[("landing", member, "runway")] = runway.direction_sign * (
            join[1] - runway.anchor_coordinate
        )
    return expected


def _plans_by_owner(execution: ConvergencePlanExecution) -> dict[str, ConvergencePlan]:
    return {str(plan.id): plan for plan in execution.plans}


@pytest.mark.parametrize("fixture", (*_MOVABLE_FIXTURES, *_STATION_PINNED_FIXTURES))
def test_grant_writes_exactly_its_recipes_controlled_fields(fixture: str) -> None:
    execution, requests = _grant_fixture(fixture)
    assert requests
    for request in requests:
        grants = tuple(
            _grant(
                item,
                item.variable.coordinate + (_SHIFT if item is request else 0.0),
            )
            for item in requests
        )
        applied = apply_convergence_corridor_grants(execution, requests, grants)
        before, after = _plans_by_owner(execution), _plans_by_owner(applied)
        assert before.keys() == after.keys()
        owner = request.variable.owner_id
        for plan_id, plan in before.items():
            if plan_id != owner:
                assert after[plan_id] is plan
        old, new = before[owner], after[owner]
        expected = _expected_writes(old, request, grants[requests.index(request)])
        old_values, new_values = _plan_scalars(old), _plan_scalars(new)
        assert old_values.keys() == new_values.keys()
        changed = {key for key in old_values if new_values[key] != old_values[key]}
        assert changed == set(expected)
        for key, value in expected.items():
            assert new_values[key] == value
        assert _convergence_decision(new) == _convergence_decision(old)
        assert new.trunk_axis.direction is old.trunk_axis.direction
        for old_landing, new_landing in zip(old.landings, new.landings, strict=True):
            assert new_landing.approach_direction is old_landing.approach_direction
            assert new_landing.corner_handedness is old_landing.corner_handedness


@pytest.mark.parametrize("shift", [0.0, 1e-9], ids=["zero", "float-noise"])
@pytest.mark.parametrize("fixture", (*_MOVABLE_FIXTURES, *_STATION_PINNED_FIXTURES))
def test_unmoved_grants_return_an_equal_execution(fixture: str, shift: float) -> None:
    execution, requests = _grant_fixture(fixture)
    applied = apply_convergence_corridor_grants(
        execution, requests, _grants(requests, shift)
    )
    assert applied == execution
    assert applied.plans == execution.plans
    assert not any(
        item.code == CONVERGENCE_CORRIDOR_GRANT_APPLIED for item in applied.diagnostics
    )


def test_reapplying_a_granted_execution_fails_on_its_stale_frame() -> None:
    execution, requests = _grant_fixture(_FUNCPROFILER)
    grants = _grants(requests, _SHIFT)
    applied = apply_convergence_corridor_grants(execution, requests, grants)
    assert applied != execution
    with pytest.raises(ConvergenceInvariantError, match="stale"):
        apply_convergence_corridor_grants(applied, requests, grants)


def test_opening_end_off_the_central_run_moves_with_its_landing() -> None:
    execution, requests = _grant_fixture(_MERGE_PULLAWAY)
    (request,) = requests
    plan = _plans_by_owner(execution)[request.variable.owner_id]
    axis = plan.trunk_axis
    run_start = min(axis.extent_start, axis.extent_end)
    landing = next(
        item
        for item in plan.landings
        if item.opening_turn_segment is not None
        and item.opening_turn_segment[1][0] < run_start - COORD_TOLERANCE
    )
    assert landing.opening_turn_segment[1][1] == axis.coordinate
    grant = _grant(request, axis.coordinate + _SHIFT)
    applied = apply_convergence_corridor_grants(execution, requests, (grant,))
    moved = next(
        item
        for item in _plans_by_owner(applied)[request.variable.owner_id].landings
        if item.member_id == landing.member_id
    )
    start, end = moved.opening_turn_segment
    assert start == landing.opening_turn_segment[0]
    assert end == (landing.opening_turn_segment[1][0], grant.coordinate)
    assert moved.join_point == (landing.join_point[0], grant.coordinate)


def test_controlled_point_beyond_the_run_moves_and_an_unnamed_one_on_it_stays() -> None:
    execution, requests = _grant_fixture(_FUNCPROFILER)
    (request,) = requests
    plan = _plans_by_owner(execution)[request.variable.owner_id]
    axis = plan.trunk_axis
    run_end = max(axis.extent_start, axis.extent_end)
    (continuation,) = plan.outgoing_continuations
    beyond = (run_end + 5 * COORD_TOLERANCE, axis.coordinate)
    on_run = ((axis.extent_start + axis.extent_end) / 2, axis.coordinate)
    probed = replace(
        plan,
        outgoing_continuations=(
            replace(continuation, start_point=beyond, end_point=on_run),
        ),
    )
    probed_execution = replace(execution, plans=(probed,))
    grant = _grant(request, axis.coordinate + _SHIFT)

    applied = apply_convergence_corridor_grants(probed_execution, requests, (grant,))

    (granted,) = _plans_by_owner(applied)[
        request.variable.owner_id
    ].outgoing_continuations
    assert granted.start_point == (beyond[0], grant.coordinate)
    assert granted.end_point == on_run
    legacy = convergences._move_trunk_axis(probed, grant.coordinate)
    assert legacy.outgoing_continuations[0].start_point == beyond


def test_a_trunk_sharing_the_granted_coordinate_stays_put() -> None:
    execution, requests = _grant_fixture(_FAN_IN_MERGE)
    first, second = requests
    assert first.variable.coordinate == second.variable.coordinate
    granted = first.variable.coordinate + _SHIFT
    grants = (_grant(first, granted), _grant(second, second.variable.coordinate))
    applied = apply_convergence_corridor_grants(execution, requests, grants)
    before, after = _plans_by_owner(execution), _plans_by_owner(applied)
    assert after[first.variable.owner_id].trunk_axis.coordinate == granted
    assert after[second.variable.owner_id] is before[second.variable.owner_id]


def _drop_last(requests, grants):
    return requests, grants[:-1]


def _duplicate_grant(requests, grants):
    return requests, (*grants, grants[0])


def _duplicate_request(requests, grants):
    return (*requests, requests[0]), grants


def _extra_grant(requests, grants):
    return requests, (
        *grants,
        replace(grants[0], variable_id="convergence-trunk|invented"),
    )


def _with_first(**changes) -> Callable:
    def mutate(requests, grants):
        return requests, (replace(grants[0], **changes), *grants[1:])

    return mutate


def _altered_recipe(requests, grants):
    recipe = grants[0].control_recipe
    first, *rest = recipe.controlled_points
    altered = replace(
        recipe,
        controlled_points=(
            replace(first, source_offset=first.source_offset + 1.0),
            *rest,
        ),
    )
    return requests, (replace(grants[0], control_recipe=altered), *grants[1:])


def _below_domain(requests, grants):
    request = requests[0]
    minimum = request.domain.minimum_coordinate
    assert minimum is not None
    return requests, (_grant(request, minimum - _SHIFT), *grants[1:])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(_drop_last, "do not complete", id="partial"),
        pytest.param(_duplicate_grant, "duplicated", id="duplicate-grant"),
        pytest.param(_duplicate_request, "duplicated", id="duplicate-request"),
        pytest.param(_extra_grant, "do not complete", id="extra"),
        pytest.param(
            _with_first(coordinate=math.inf), "non-finite", id="nonfinite-coordinate"
        ),
        pytest.param(
            _with_first(coordinate_delta=math.nan), "non-finite", id="nonfinite-delta"
        ),
        pytest.param(
            _with_first(coordinate_delta=_SHIFT + 1.0),
            "inconsistent",
            id="inconsistent-delta",
        ),
        pytest.param(
            _with_first(owner_id="convergence-plan|invented"),
            "wrong owner",
            id="wrong-owner-id",
        ),
        pytest.param(
            _with_first(owner_kind=CorridorScalarOwnerKind.MEMBER_CARRIER),
            "wrong owner",
            id="wrong-owner-kind",
        ),
        pytest.param(_altered_recipe, "control recipe", id="altered-recipe"),
        pytest.param(
            _with_first(control_recipe=None), "control recipe", id="missing-recipe"
        ),
        pytest.param(_below_domain, "outside its request's domain", id="domain"),
    ],
)
def test_invalid_grant_population_fails_before_publication(
    mutate: Callable, message: str
) -> None:
    _graph, _ctx, execution, _targets, requests = _requests_for(
        str(ROOT / _FAN_IN_MERGE)
    )
    assert len(requests) >= 2
    plans = execution.plans
    bad_requests, bad_grants = mutate(requests, _grants(requests, _SHIFT))
    with pytest.raises(ConvergenceInvariantError, match=message):
        apply_convergence_corridor_grants(execution, bad_requests, bad_grants)
    assert execution.plans is plans


def test_infeasible_directed_runway_raises_instead_of_flipping() -> None:
    execution, requests = _grant_fixture(_FUNCPROFILER)
    (request,) = requests
    runway = next(
        item
        for item in request.control_recipe.directed_runways
        if item.axis == request.variable.axis
    )
    assert runway.anchor_coordinate is not None
    short = runway.anchor_coordinate + runway.direction_sign * (
        runway.minimum_distance / 2
    )
    reversed_ = runway.anchor_coordinate - runway.direction_sign * _SHIFT
    for coordinate in (short, reversed_):
        with pytest.raises(ConvergenceInvariantError, match="directed runway"):
            apply_convergence_corridor_grants(
                execution, requests, (_grant(request, coordinate),)
            )


def test_opening_descent_that_would_collapse_or_reverse_raises_in_the_applier() -> None:
    execution, requests = _grant_fixture(_MERGE_PULLAWAY)
    (request,) = requests
    plan = _plans_by_owner(execution)[request.variable.owner_id]
    variable_id = request.variable.variable_id
    runways = {
        item.controlled_role_id: item
        for item in request.control_recipe.directed_runways
    }
    descending = [
        landing
        for landing in plan.landings
        if f"{variable_id}|landing:{landing.member_id}|opening-end" in runways
    ]
    assert descending
    for landing in descending:
        start, end = landing.opening_turn_segment
        runway = runways[f"{variable_id}|landing:{landing.member_id}|opening-end"]
        assert runway.anchor_coordinate == start[1]
        assert runway.direction_sign * (end[1] - start[1]) > runway.minimum_distance
        collapsed = start[1] + runway.direction_sign * runway.minimum_distance / 2
        reversed_ = start[1] - runway.direction_sign * _SHIFT
        for coordinate in (collapsed, reversed_):
            with pytest.raises(
                ConvergenceInvariantError, match=r"opening-end a \S+px directed runway"
            ):
                apply_convergence_corridor_grants(
                    execution, requests, (_grant(request, coordinate),)
                )


def test_a_second_grant_for_one_plan_raises_even_after_a_no_op_first() -> None:
    execution, requests = _grant_fixture(_FUNCPROFILER)
    (request,) = requests
    variable_id = request.variable.variable_id
    twin_id = f"{variable_id}|twin"

    def renamed(role_id: str | None) -> str | None:
        return None if role_id is None else role_id.replace(variable_id, twin_id, 1)

    recipe = request.control_recipe
    twin = replace(
        request,
        variable=replace(request.variable, variable_id=twin_id),
        control_recipe=replace(
            recipe,
            controlled_points=tuple(
                replace(item, role_id=renamed(item.role_id))
                for item in recipe.controlled_points
            ),
            fixed_points=tuple(
                replace(item, role_id=renamed(item.role_id))
                for item in recipe.fixed_points
            ),
            directed_runways=tuple(
                replace(
                    item,
                    controlled_role_id=renamed(item.controlled_role_id),
                    anchor_role_id=renamed(item.anchor_role_id),
                )
                for item in recipe.directed_runways
            ),
        ),
    )
    assert (
        apply_convergence_corridor_grants(
            execution, (twin,), (_grant(twin, twin.variable.coordinate + _SHIFT),)
        ).plans
        != execution.plans
    )
    grants = (
        _grant(request, request.variable.coordinate),
        _grant(twin, twin.variable.coordinate + _SHIFT),
    )
    with pytest.raises(ConvergenceInvariantError, match="more than one corridor grant"):
        apply_convergence_corridor_grants(execution, (request, twin), grants)


def test_grant_that_would_reverse_a_flank_connector_raises() -> None:
    execution, requests = _grant_fixture(_FUNCPROFILER)
    (request,) = requests
    request = replace(
        request,
        control_recipe=replace(request.control_recipe, directed_runways=()),
    )
    axis = _plans_by_owner(execution)[request.variable.owner_id].trunk_axis
    assert abs(axis.target_flank_coordinate - axis.coordinate) > COORD_TOLERANCE
    beyond = axis.target_flank_coordinate - math.copysign(
        _SHIFT, axis.coordinate - axis.target_flank_coordinate
    )
    with pytest.raises(ConvergenceInvariantError, match="reverse"):
        apply_convergence_corridor_grants(
            execution, (request,), (_grant(request, beyond),)
        )


@pytest.mark.parametrize("fixture", (*_MOVABLE_FIXTURES, *_STATION_PINNED_FIXTURES))
def test_grant_path_reaches_no_proximity_helper_legacy_setter_or_solver(
    fixture: str,
) -> None:
    execution, requests = _grant_fixture(fixture)
    grants = _grants(requests, _SHIFT)
    called: list[str] = []

    def forbidden(name: str) -> Callable:
        def raise_on_call(*_args, **_kwargs):
            called.append(name)
            raise AssertionError(f"the grant path called {name}")

        return raise_on_call

    with pytest.MonkeyPatch.context() as patch:
        for name in _PROXIMITY_AND_LEGACY_HELPERS:
            patch.setattr(convergences, name, forbidden(name))
        for module in (corridor_cohorts, corridor_cohort_integration):
            patch.setattr(
                module, "solve_corridor_cohorts", forbidden("solve_corridor_cohorts")
            )
        patch.setattr(
            corridor_cohort_integration,
            "compile_corridor_cohort_plan",
            forbidden("compile_corridor_cohort_plan"),
        )
        applied = apply_convergence_corridor_grants(execution, requests, grants)

    assert called == []
    assert {
        plan.trunk_axis.coordinate
        for plan in applied.plans
        if plan.trunk_axis is not None
    } == {request.variable.coordinate + _SHIFT for request in requests}


def test_opening_roles_state_the_column_and_the_lateral_end_on_either_axis() -> None:
    _graph, ctx, execution, _targets, requests = _requests_for(
        str(ROOT / _FUNCPROFILER)
    )
    plan = next(iter(_eligible_plans_by_owner(execution).values()))
    landing = next(item for item in plan.landings if item.opening_turn_segment)
    column = landing.opening_turn_coordinate

    def opening_roles(candidate: ConvergencePlan):
        _target, _dependants, variable, recipe = (
            convergences._convergence_corridor_target(candidate, ctx)
        )
        prefix = f"{variable.variable_id}|landing:{landing.member_id}|"
        points = {
            point.role_id.removeprefix(prefix): point
            for point in (*recipe.controlled_points, *recipe.fixed_points)
            if point.role_id.startswith(prefix)
        }
        return points["opening"], points["opening-end"]

    opening, end = opening_roles(plan)
    assert isinstance(opening, CorridorScalarFixedPoint)
    assert (opening.axis, opening.coordinate) == (0, column)
    assert not isinstance(end, CorridorScalarFixedPoint)
    assert end.axis == 1

    vertical_axis = ConvergenceTrunkAxis(
        axis=DemandAxis.Y,
        coordinate=column + 40.0,
        extent_start=landing.join_point[1] - 100.0,
        extent_end=landing.join_point[1] + 100.0,
        direction=Direction.D,
        source_flank_coordinate=column - 40.0,
        target_flank_coordinate=column + 80.0,
    )
    vertical = replace(plan, trunk_axis=vertical_axis)
    opening, end = opening_roles(vertical)
    assert isinstance(opening, CorridorScalarFixedPoint)
    assert (opening.axis, opening.coordinate) == (0, column)
    assert isinstance(end, CorridorScalarFixedPoint)
    assert (end.axis, end.coordinate) == (0, landing.opening_turn_segment[1][0])


@pytest.mark.parametrize("fixture", _STATION_PINNED_FIXTURES)
def test_trunk_on_an_entry_port_lane_is_pinned_to_its_coordinate(fixture: str) -> None:
    _graph, _ctx, execution, targets, requests = _requests_for(str(ROOT / fixture))
    by_owner = _eligible_plans_by_owner(execution)
    targets_by_identity = {(item.member_id, item.edge_key): item for item in targets}
    pinned = []
    for request in requests:
        plan = by_owner[request.variable.owner_id]
        on_port = any(
            _points_coincide(
                targets_by_identity[(point.member_id, point.edge_key)].route.points[
                    point.point_rank
                ],
                continuation.end_point,
            )
            for point in request.control_recipe.controlled_points
            for continuation in plan.outgoing_continuations
        )
        domain = request.domain
        is_pinned = (
            domain.minimum_coordinate
            == domain.maximum_coordinate
            == request.variable.coordinate
        )
        assert is_pinned == on_port
        if is_pinned:
            pinned.append(request)
    assert pinned
    with pytest.raises(ConvergenceInvariantError, match="outside its request's domain"):
        apply_convergence_corridor_grants(
            execution,
            requests,
            tuple(
                _grant(item, item.variable.coordinate + _SHIFT)
                if item is pinned[0]
                else _grant(item, item.variable.coordinate)
                for item in requests
            ),
        )


def _shift_corridor_preference(monkeypatch: pytest.MonkeyPatch, **domain) -> None:
    """Prefer every convergence trunk ``_SHIFT`` px past where it stands."""
    for name in ("_convergence_corridor_preference", "_pinned_convergence_preference"):
        real = getattr(convergences, name)

        def shifted(*args, _real=real, **kwargs):
            preferred, request_domain = _real(*args, **kwargs)
            return preferred + _SHIFT, replace(request_domain, **domain)

        monkeypatch.setattr(convergences, name, shifted)


def _render(
    fixture: str,
    monkeypatch: pytest.MonkeyPatch,
    layout_options: dict[str, object] | None = None,
) -> tuple[RoutePlan, list[RoutedPath]]:
    """Render *fixture*; return its published plan and the routes it drew."""
    settled: list[svg._SettledRenderGeometry] = []
    real_settle = svg._settle_render_geometry

    def settle(*args, **kwargs):
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(svg, "_settle_render_geometry", settle)
        path = ROOT / fixture
        graph = prepare_graph(
            path.read_text(),
            source_dir=str(path.parent),
            layout_options=layout_options,
        )
        observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))
    return observed.route_plan, settled[-1].routes


def _routes_by_edge(routes: list[RoutedPath]) -> dict[tuple[str, str, str], RoutedPath]:
    return {
        (route.edge.source, route.edge.target, route.line_id): route for route in routes
    }


@pytest.mark.parametrize(
    ("fixture", "granted"),
    # merge_right_entry's trunk is held on its same-line member's lane, which
    # outranks its shifted preference.
    [(_FUNCPROFILER, 466.0), (_MERGE_RIGHT_ENTRY, 346.0)],
)
def test_granted_trunk_is_drawn_and_published_and_nothing_else_moves(
    fixture: str, granted: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    _baseline_plan, baseline_routes = _render(fixture, monkeypatch)
    _shift_corridor_preference(monkeypatch)

    plan, routes = _render(fixture, monkeypatch)

    (trunk,) = (item for item in plan.convergence_plans if item.trunk_axis is not None)
    assert trunk.trunk_axis.coordinate == granted
    assert any(
        item.code == CONVERGENCE_CORRIDOR_GRANT_APPLIED
        and item.member_id == trunk.primary_trunk_member_id
        for item in plan.diagnostics
    )
    drawn, baseline = _routes_by_edge(routes), _routes_by_edge(baseline_routes)
    assert drawn.keys() == baseline.keys()
    trunk_route = next(
        route
        for route in routes
        if route.convergence_member_id == str(trunk.primary_trunk_member_id)
    )
    assert convergences._route_covers_trunk(trunk_route, trunk.trunk_axis)
    for key, route in drawn.items():
        if route.convergence_plan_id == str(trunk.id):
            continue
        assert route.points == baseline[key].points, key


def _re_seat_trunks_in_global_settlement(
    monkeypatch: pytest.MonkeyPatch, offset: float
) -> None:
    """Make the global settlement's last legacy pass move every trunk *offset*."""
    global_passes: list[None] = []
    real_settle = planning.settle_global_convergence_execution
    real_reconcile = convergences._reconcile_landing_handedness

    def settle(*args, **kwargs):
        global_passes.append(None)
        try:
            return real_settle(*args, **kwargs)
        finally:
            global_passes.pop()

    def reconcile(plans):
        settled = real_reconcile(plans)
        if not global_passes:
            return settled
        return tuple(
            plan
            if plan.trunk_axis is None
            else convergences._move_trunk_axis(
                plan, plan.trunk_axis.coordinate + offset
            )
            for plan in settled
        )

    monkeypatch.setattr(planning, "settle_global_convergence_execution", settle)
    monkeypatch.setattr(convergences, "_reconcile_landing_handedness", reconcile)


def test_a_grant_the_global_settlement_re_seats_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The applier writes the grant exactly, and a later re-seat of it raises.

    A legacy pass that re-seats the granted trunk after the applier would make
    the published grant name a coordinate the drawn trunk does not sit on.
    """
    applied: list[tuple[tuple[CorridorScalarGrant, ...], ConvergencePlanExecution]] = []
    real_apply = planning.apply_convergence_corridor_grants

    def spy(execution, requests, grants):
        result = real_apply(execution, requests, grants)
        applied.append((tuple(grants), result))
        return result

    monkeypatch.setattr(planning, "apply_convergence_corridor_grants", spy)
    _shift_corridor_preference(monkeypatch)
    _re_seat_trunks_in_global_settlement(monkeypatch, 4.0)

    with pytest.raises(ConvergenceInvariantError, match="re-settled off the 466"):
        _render(_FUNCPROFILER, monkeypatch)

    moving = [
        (grants, result)
        for grants, result in applied
        if any(abs(grant.coordinate_delta) > COORD_TOLERANCE_FINE for grant in grants)
    ]
    assert moving
    for grants, result in moving:
        by_owner = _plans_by_owner(result)
        (grant,) = grants
        assert grant.coordinate == 466.0
        assert by_owner[grant.owner_id].trunk_axis.coordinate == grant.coordinate


def test_an_unmoved_grant_the_global_settlement_re_seats_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant owns the coordinate it leaves in place as much as one it moves.

    funcprofiler's trunk is granted the coordinate it already holds, so the
    applier writes nothing, yet a legacy pass re-seating that trunk would
    publish a grant the drawn trunk does not sit on.
    """
    _re_seat_trunks_in_global_settlement(monkeypatch, 4.0)

    with pytest.raises(ConvergenceInvariantError, match="re-settled off the 454"):
        _render(_FUNCPROFILER, monkeypatch)


@pytest.mark.parametrize("fixture", _STATION_PINNED_FIXTURES)
def test_trunk_on_an_entry_port_cannot_be_drawn_off_it(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifting the pin draws a feeder into the port on the wrong approach.

    The moved trunk's lane no longer meets the port it enters, so emission has
    to turn onto the port and the landing's planned approach does not survive.
    """
    _shift_corridor_preference(
        monkeypatch, minimum_coordinate=None, maximum_coordinate=None
    )
    with pytest.raises(ConvergenceInvariantError, match="planned .* approach"):
        _render(fixture, monkeypatch)


_DISTINCT_LANE_SEPARATION_FIXTURES = (
    "examples/topologies/plan_owned_distinct_lane_separation.mmd",
    "tests/fixtures/regressions/plan_owned_distinct_lane_separation_reordered.mmd",
)


def _render_with_compiled_grants(
    fixture: str,
    monkeypatch: pytest.MonkeyPatch,
    layout_options: dict[str, object] | None = None,
) -> tuple[RoutePlan, tuple[CorridorScalarGrant, ...]]:
    """Render *fixture*; return its plan and the last compile's trunk grants."""
    compiled: list[tuple[CorridorScalarGrant, ...]] = []
    real_apply = planning.apply_convergence_corridor_grants

    def spy(execution, requests, grants):
        if grants:
            compiled.append(tuple(grants))
        return real_apply(execution, requests, grants)

    monkeypatch.setattr(planning, "apply_convergence_corridor_grants", spy)
    plan, _routes = _render(fixture, monkeypatch, layout_options)
    assert compiled
    return plan, compiled[-1]


@pytest.mark.parametrize("fixture", _DISTINCT_LANE_SEPARATION_FIXTURES)
def test_trunk_is_granted_clear_of_a_distinct_line_turning_off_its_lane(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant, not settlement, seats a trunk clear of a distinct line's turn.

    The ``secondary`` members run left on the ``primary`` trunks' lane and turn
    up out of it across the trunks, so both trunks take the nearest lane one
    pitch clear of that turn-off, and the drawn trunk is the granted one.
    """
    plan, grants = _render_with_compiled_grants(fixture, monkeypatch)

    assert len(grants) == 2
    assert {grant.coordinate for grant in grants} == {200.0}
    by_owner = {str(item.id): item for item in plan.convergence_plans}
    for grant in grants:
        assert by_owner[grant.owner_id].trunk_axis.coordinate == grant.coordinate


def test_trunk_is_granted_onto_the_same_line_run_it_bundles_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant, not settlement, fuses a trunk onto its own line's frozen run.

    The unowned ``main`` member into the sink's right entry already runs along
    the trunk's corridor one pitch away, so the two are one bundle drawn too
    wide: the trunk is granted the member's lane, and the drawn trunk is the
    granted one.
    """
    plan, grants = _render_with_compiled_grants(_MERGE_RIGHT_ENTRY, monkeypatch)

    (grant,) = grants
    assert grant.coordinate == 346.0
    by_owner = {str(item.id): item for item in plan.convergence_plans}
    assert by_owner[grant.owner_id].trunk_axis.coordinate == grant.coordinate


def test_only_a_trunk_grant_binds_its_plan_through_settlement() -> None:
    """The settlement guard holds a plan to its trunk grant alone.

    A member-carrier grant sharing the owner id is another owner's coordinate,
    so it can neither stand in for the trunk's nor overwrite it.
    """
    owner_id = "convergence-plan|shared"
    trunk = CorridorScalarGrant(
        "convergence-trunk|shared",
        CorridorScalarOwnerKind.CONVERGENCE_TRUNK,
        owner_id,
        200.0,
    )
    carrier = CorridorScalarGrant(
        "member-carrier|shared",
        CorridorScalarOwnerKind.MEMBER_CARRIER,
        owner_id,
        196.0,
    )
    cohorts = corridor_cohort_integration.CorridorCohortPlan(
        (), (), (), scalar_grants=(trunk, carrier)
    )
    member_geometry = replace(
        planning.empty_member_geometry_execution(), corridor_cohorts=cohorts
    )

    assert planning._granted_trunk_coordinates(member_geometry) == {owner_id: 200.0}


@pytest.mark.parametrize("stroke_scale", [0.6, 0.7])
@pytest.mark.parametrize("fixture", _DISTINCT_LANE_SEPARATION_FIXTURES)
def test_trunk_is_granted_clear_of_every_distinct_line_run_it_co_travels(
    fixture: str, stroke_scale: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A narrowed step spreads the ``secondary`` runs over two lanes.

    The grant clears both of them, not only the turn-off leg, so each trunk
    holds at least one step from every run of the distinct line it shares the
    corridor with, and the drawn trunk is the granted one.
    """
    plan, grants = _render_with_compiled_grants(
        fixture, monkeypatch, {"stroke_scale": stroke_scale}
    )

    step = OFFSET_STEP * stroke_scale
    by_owner = {str(item.id): item for item in plan.convergence_plans}
    for grant in grants:
        axis = by_owner[grant.owner_id].trunk_axis
        assert axis.coordinate == grant.coordinate
        secondary_runs = [
            start[1]
            for member in plan.member_geometry_plans
            if member.edge.line_id == "secondary"
            for start, end in zip(member.points[1:-2], member.points[2:-1], strict=True)
            if start[1] == end[1]
            and min(start[0], end[0]) < axis.extent_end
            and max(start[0], end[0]) > axis.extent_start
        ]
        assert len(set(secondary_runs)) == 2
        for coordinate in secondary_runs:
            assert abs(coordinate - grant.coordinate) >= step - COORD_TOLERANCE_FINE


def test_a_bundle_anchored_on_a_run_its_own_problem_moves_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle's run is a fixed obstacle, which a lane of the same problem is not.

    Re-anchoring merge_right_entry's bundle on the trunk's own carrier puts the
    reference inside the scalar's problem, which the compile refuses rather than
    seating the trunk against geometry it is itself moving.
    """
    real_bundles = corridor_cohort_integration._scalar_bundles

    def self_anchored(targets, runs, scalar_requests, scalar_carriers, *args):
        return tuple(
            replace(bundle, witness_id=scalar_carriers[bundle.variable_id].footprint_id)
            for bundle in real_bundles(
                targets, runs, scalar_requests, scalar_carriers, *args
            )
        )

    monkeypatch.setattr(corridor_cohort_integration, "_scalar_bundles", self_anchored)

    with pytest.raises(CorridorCohortCompilationError, match="anchors on a run"):
        _render(_MERGE_RIGHT_ENTRY, monkeypatch)
