"""Corridor cohorts allocate from realised geometry and frozen witnesses."""

from __future__ import annotations

from dataclasses import replace
from itertools import permutations

from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationFailureReason,
    CorridorAllocationProblem,
    CorridorAllocationStatus,
    CorridorCoordinateDomain,
    CorridorDirectedSeparation,
    CorridorEquality,
    CorridorFixedEquality,
    CorridorForbiddenInterval,
    CorridorLane,
    CorridorObstacle,
    CorridorSeparation,
    solve_corridor_cohorts,
)


def _lane(
    member_id: str,
    corridor_owner_id: str,
    endpoint_owner_id: str,
    boundary_coordinate: float,
    planned_coordinate: float,
    *,
    span: tuple[float, float],
    line_rank: int,
    root_rank: int = 0,
) -> CorridorLane:
    return CorridorLane(
        member_id=member_id,
        cohort_id=corridor_owner_id,
        endpoint_owner_id=endpoint_owner_id,
        boundary_coordinate=boundary_coordinate,
        planned_coordinate=planned_coordinate,
        span_start=span[0],
        span_end=span[1],
        semantic_rank=(root_rank, line_rank),
    )


def _obstacle(
    obstacle_id: str,
    order_coordinate: float,
    realised_coordinate: float,
    *,
    span: tuple[float, float],
    semantic_rank: tuple[int, ...],
) -> CorridorObstacle:
    return CorridorObstacle(
        obstacle_id=obstacle_id,
        order_coordinate=order_coordinate,
        realised_coordinate=realised_coordinate,
        span_start=span[0],
        span_end=span[1],
        semantic_rank=semantic_rank,
    )


def _allocations(problem: CorridorAllocationProblem) -> dict[str, float]:
    result = solve_corridor_cohorts(problem)
    assert result.status is CorridorAllocationStatus.PLANNED
    assert result.reason is None
    return dict(result.allocations)


def _seed77_problem(
    lanes: tuple[CorridorLane, ...] | None = None,
) -> CorridorAllocationProblem:
    if lanes is None:
        lanes = (
            _lane(
                "s9:l1",
                "corridor:s9",
                "endpoint:s9",
                478.0,
                558.0,
                span=(5.0, 15.0),
                line_rank=1,
                root_rank=0,
            ),
            _lane(
                "s9:l3",
                "corridor:s9",
                "endpoint:s9",
                482.0,
                554.0,
                span=(5.0, 15.0),
                line_rank=3,
                root_rank=0,
            ),
            _lane(
                "s17:l0",
                "corridor:s17",
                "endpoint:s17",
                478.0,
                550.0,
                span=(0.0, 10.0),
                line_rank=0,
                root_rank=1,
            ),
            _lane(
                "s17:l3",
                "corridor:s17",
                "endpoint:s17",
                482.0,
                554.0,
                span=(20.0, 30.0),
                line_rank=3,
                root_rank=1,
            ),
        )
    return CorridorAllocationProblem(
        lanes=lanes,
        obstacles=(
            _obstacle(
                "realised:s10",
                550.0,
                554.0,
                span=(0.0, 10.0),
                semantic_rank=(2, 0),
            ),
        ),
        clearance=4.0,
    )


SEED77_EXPECTED = {
    "s9:l1": 542.0,
    "s9:l3": 546.0,
    "s17:l0": 550.0,
    "s17:l3": 554.0,
}


def test_seed77_shape_uses_boundary_offsets_and_realised_obstacle() -> None:
    assert _allocations(_seed77_problem()) == SEED77_EXPECTED


def test_seed77_all_lane_permutations_have_identical_allocations() -> None:
    lanes = _seed77_problem().lanes

    for order in permutations(lanes):
        assert _allocations(_seed77_problem(order)) == SEED77_EXPECTED


def test_allocation_is_invariant_under_coordinate_translation() -> None:
    offset = 80.0
    problem = _seed77_problem()
    translated = replace(
        problem,
        lanes=tuple(
            replace(
                lane,
                boundary_coordinate=lane.boundary_coordinate + offset,
                planned_coordinate=lane.planned_coordinate + offset,
            )
            for lane in problem.lanes
        ),
        obstacles=tuple(
            replace(
                obstacle,
                order_coordinate=obstacle.order_coordinate + offset,
                realised_coordinate=obstacle.realised_coordinate + offset,
            )
            for obstacle in problem.obstacles
        ),
    )

    assert _allocations(translated) == {
        member_id: coordinate + offset
        for member_id, coordinate in SEED77_EXPECTED.items()
    }


def test_fixed_equalities_pin_a_complete_rigid_bundle() -> None:
    lanes = tuple(
        _lane(
            f"lane:{rank}",
            "corridor",
            "endpoint",
            -4.0 * rank,
            12.0 - 4.0 * rank,
            span=(0.0, 10.0),
            line_rank=rank,
        )
        for rank in range(3)
    )
    obstacles = tuple(
        _obstacle(
            f"fixed:{rank}",
            12.0 - 4.0 * rank,
            12.0 - 4.0 * rank,
            span=(0.0, 10.0),
            semantic_rank=(rank,),
        )
        for rank in range(3)
    )
    problem = CorridorAllocationProblem(
        lanes=lanes,
        obstacles=obstacles,
        separations=tuple(
            CorridorSeparation(
                lane.member_id,
                obstacle.obstacle_id,
                (
                    0.0
                    if lane.member_id.split(":")[-1]
                    == obstacle.obstacle_id.split(":")[-1]
                    else 4.0
                ),
            )
            for lane in lanes
            for obstacle in obstacles
        ),
        fixed_equalities=tuple(
            CorridorFixedEquality(
                f"reservation-lane:{rank}",
                f"lane:{rank}",
                f"fixed:{rank}",
            )
            for rank in range(3)
        ),
    )

    expected = {
        "lane:0": 12.0,
        "lane:1": 8.0,
        "lane:2": 4.0,
    }
    assert _allocations(replace(problem, fixed_equalities=())) == expected
    assert _allocations(problem) == expected


def test_planned_coordinates_do_not_reorder_boundary_witnesses() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "early",
                "corridor:ordered",
                "endpoint:ordered",
                10.0,
                104.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "late",
                "corridor:ordered",
                "endpoint:ordered",
                14.0,
                100.0,
                span=(0.0, 10.0),
                line_rank=1,
            ),
        )
    )

    allocations = _allocations(problem)

    assert allocations["late"] - allocations["early"] == 4.0


def test_directed_separation_can_reverse_default_semantic_order() -> None:
    lanes = (
        _lane(
            "semantic-first",
            "corridor:first",
            "endpoint:first",
            0.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
        ),
        _lane(
            "semantic-second",
            "corridor:second",
            "endpoint:second",
            0.0,
            20.0,
            span=(0.0, 10.0),
            line_rank=1,
        ),
    )
    problem = CorridorAllocationProblem(
        lanes,
        directed_separations=(
            CorridorDirectedSeparation(
                "controlled-lead-order",
                "semantic-second",
                "semantic-first",
                4.0,
            ),
        ),
    )

    allocations = _allocations(problem)

    assert allocations["semantic-first"] - allocations["semantic-second"] == 4.0


def test_directed_separation_is_invariant_under_input_permutations() -> None:
    lanes = (
        _lane(
            "a",
            "corridor:a",
            "endpoint:a",
            0.0,
            30.0,
            span=(0.0, 10.0),
            line_rank=0,
        ),
        _lane(
            "b",
            "corridor:b",
            "endpoint:b",
            0.0,
            20.0,
            span=(10.0, 20.0),
            line_rank=1,
        ),
        _lane(
            "c",
            "corridor:c",
            "endpoint:c",
            0.0,
            10.0,
            span=(20.0, 30.0),
            line_rank=2,
        ),
    )
    separations = (
        CorridorDirectedSeparation("order:ba", "b", "a", 4.0),
        CorridorDirectedSeparation("order:cb", "c", "b", 4.0),
    )
    expected = None
    for lane_order in permutations(lanes):
        for separation_order in permutations(separations):
            allocations = _allocations(
                CorridorAllocationProblem(
                    lane_order,
                    directed_separations=separation_order,
                )
            )
            expected = allocations if expected is None else expected
            assert allocations == expected


def test_directed_separation_cycle_fails_with_owner_provenance() -> None:
    lanes = tuple(
        _lane(
            member_id,
            f"corridor:{member_id}",
            f"endpoint:{member_id}",
            0.0,
            coordinate,
            span=(rank * 10.0, (rank + 1) * 10.0),
            line_rank=rank,
        )
        for rank, (member_id, coordinate) in enumerate(
            (("a", 0.0), ("b", 10.0), ("c", 20.0))
        )
    )
    problem = CorridorAllocationProblem(
        lanes,
        directed_separations=(
            CorridorDirectedSeparation("order:ab", "a", "b", 4.0),
            CorridorDirectedSeparation("order:bc", "b", "c", 4.0),
            CorridorDirectedSeparation("order:ca", "c", "a", 4.0),
        ),
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.allocations == ()
    assert result.blocking_member_ids == ("a", "b", "c")
    assert result.blocking_order_owner_ids == ("order:ab", "order:bc", "order:ca")
    assert result.blocking_endpoint_owner_ids == (
        "endpoint:a",
        "endpoint:b",
        "endpoint:c",
    )


def test_forbidden_coordinate_interval_chooses_one_deterministic_side() -> None:
    lane = _lane(
        "movable",
        "corridor:movable",
        "endpoint:movable",
        0.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=1,
    )
    problem = CorridorAllocationProblem(
        (lane,),
        forbidden_intervals=(
            CorridorForbiddenInterval(
                "movable",
                "fixed-perpendicular-footprint",
                8.0,
                12.0,
                (0, 0),
            ),
        ),
    )

    assert _allocations(problem) == {"movable": 12.0}


def test_forbidden_coordinate_interval_is_equivariant_under_axis_reflection() -> None:
    lane = _lane(
        "movable",
        "corridor:movable",
        "endpoint:movable",
        0.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=1,
    )
    interval = CorridorForbiddenInterval(
        "movable",
        "fixed-perpendicular-footprint",
        8.0,
        12.0,
        (0, 0),
    )
    reflected_lane = replace(
        lane,
        boundary_coordinate=-lane.boundary_coordinate,
        planned_coordinate=-lane.planned_coordinate,
    )
    reflected_interval = replace(
        interval,
        minimum_coordinate=-interval.maximum_coordinate,
        maximum_coordinate=-interval.minimum_coordinate,
    )

    original = _allocations(
        CorridorAllocationProblem((lane,), forbidden_intervals=(interval,))
    )
    reflected = _allocations(
        CorridorAllocationProblem(
            (reflected_lane,),
            forbidden_intervals=(reflected_interval,),
            axis_sign=-1,
        )
    )

    assert reflected == {
        member_id: -coordinate for member_id, coordinate in original.items()
    }


def test_infeasible_forbidden_interval_reports_its_named_obstacle() -> None:
    lane = _lane(
        "movable",
        "corridor:movable",
        "endpoint:movable",
        0.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
    )
    problem = CorridorAllocationProblem(
        (lane,),
        domains=(
            CorridorCoordinateDomain(
                "movable",
                minimum_coordinate=9.0,
                maximum_coordinate=11.0,
            ),
        ),
        forbidden_intervals=(
            CorridorForbiddenInterval(
                "movable",
                "fixed-perpendicular-footprint",
                8.0,
                12.0,
                (0, 0),
            ),
        ),
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.blocking_member_ids == ("movable",)
    assert result.blocking_obstacle_ids == ("fixed-perpendicular-footprint",)


def test_opposite_running_members_are_not_implicitly_bundled_by_ordering() -> None:
    problem = CorridorAllocationProblem(
        (
            _lane(
                "right-running",
                "corridor:direction:R",
                "endpoint:right-running",
                0.0,
                0.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "left-running",
                "corridor:direction:L",
                "endpoint:left-running",
                0.0,
                10.0,
                span=(10.0, 20.0),
                line_rank=1,
            ),
        ),
        directed_separations=(
            CorridorDirectedSeparation(
                "counter-running-order",
                "right-running",
                "left-running",
                4.0,
            ),
        ),
    )

    allocations = _allocations(problem)

    assert allocations["left-running"] - allocations["right-running"] >= 4.0
    assert allocations["left-running"] != allocations["right-running"]


def test_same_line_label_does_not_join_distinct_corridor_owners() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "left:l3",
                "corridor:left",
                "endpoint:left",
                10.0,
                100.0,
                span=(0.0, 10.0),
                line_rank=3,
                root_rank=0,
            ),
            _lane(
                "right:l3",
                "corridor:right",
                "endpoint:right",
                10.0,
                200.0,
                span=(20.0, 30.0),
                line_rank=3,
                root_rank=1,
            ),
        )
    )

    assert _allocations(problem) == {"left:l3": 100.0, "right:l3": 200.0}


def test_explicit_equalities_do_not_collide_with_their_own_members() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "left",
                "corridor:left",
                "endpoint:left",
                10.0,
                10.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "right",
                "corridor:right",
                "endpoint:right",
                10.0,
                10.0,
                span=(0.0, 10.0),
                line_rank=1,
            ),
        ),
        equalities=(CorridorEquality("network:coincident", "left", "right", 0.0),),
    )

    assert _allocations(problem) == {"left": 10.0, "right": 10.0}


def test_cohort_chooses_one_obstacle_side_for_every_lane() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "low",
                "corridor:paired",
                "endpoint:paired",
                0.0,
                8.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "high",
                "corridor:paired",
                "endpoint:paired",
                4.0,
                12.0,
                span=(0.0, 10.0),
                line_rank=1,
            ),
        ),
        obstacles=(
            _obstacle(
                "obstacle:middle",
                10.0,
                10.0,
                span=(0.0, 10.0),
                semantic_rank=(1, 0),
            ),
        ),
        clearance=4.0,
    )

    assert _allocations(problem) == {"low": 2.0, "high": 6.0}


def test_realised_obstacle_coordinate_is_the_final_clearance_constraint() -> None:
    lane = _lane(
        "lane",
        "corridor:lane",
        "endpoint:lane",
        20.0,
        20.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=1,
    )
    before = _obstacle(
        "obstacle",
        20.0,
        18.0,
        span=(0.0, 10.0),
        semantic_rank=(0, 0),
    )
    after = replace(before, realised_coordinate=22.0)

    before_allocation = _allocations(
        CorridorAllocationProblem(lanes=(lane,), obstacles=(before,))
    )
    after_allocation = _allocations(
        CorridorAllocationProblem(lanes=(lane,), obstacles=(after,))
    )

    assert before_allocation == {"lane": 22.0}
    assert after_allocation == {"lane": 18.0}


def test_one_cohort_cannot_span_distinct_endpoint_owners() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "a",
                "corridor:shared",
                "endpoint:a",
                10.0,
                10.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "b",
                "corridor:shared",
                "endpoint:b",
                14.0,
                14.0,
                span=(20.0, 30.0),
                line_rank=1,
            ),
        )
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INVALID
    assert result.allocations == ()
    assert result.blocking_member_ids == ("a", "b")
    assert result.blocking_endpoint_owner_ids == ("endpoint:a", "endpoint:b")


def test_empty_lane_or_obstacle_semantic_rank_is_invalid() -> None:
    rankless_lane = CorridorLane(
        member_id="rankless",
        cohort_id="corridor:rankless",
        endpoint_owner_id="endpoint:rankless",
        boundary_coordinate=10.0,
        planned_coordinate=10.0,
        span_start=0.0,
        span_end=10.0,
        semantic_rank=(),
    )
    lane_result = solve_corridor_cohorts(
        CorridorAllocationProblem(lanes=(rankless_lane,))
    )

    assert lane_result.status is CorridorAllocationStatus.FAILURE
    assert lane_result.reason is CorridorAllocationFailureReason.INVALID
    assert lane_result.blocking_member_ids == ("rankless",)

    ranked_lane = replace(rankless_lane, semantic_rank=(0, 0))
    rankless_obstacle = _obstacle(
        "rankless-obstacle",
        10.0,
        10.0,
        span=(0.0, 10.0),
        semantic_rank=(),
    )
    obstacle_result = solve_corridor_cohorts(
        CorridorAllocationProblem(lanes=(ranked_lane,), obstacles=(rankless_obstacle,))
    )

    assert obstacle_result.status is CorridorAllocationStatus.FAILURE
    assert obstacle_result.reason is CorridorAllocationFailureReason.INVALID
    assert obstacle_result.blocking_obstacle_ids == ("rankless-obstacle",)


def test_spans_touching_at_one_endpoint_do_not_overlap() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "before",
                "corridor:before",
                "endpoint:before",
                10.0,
                10.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "after",
                "corridor:after",
                "endpoint:after",
                10.0,
                10.0,
                span=(10.0, 20.0),
                line_rank=1,
            ),
        )
    )

    assert _allocations(problem) == {"before": 10.0, "after": 10.0}


def test_exact_obstacle_tie_is_equivariant_under_axis_reflection() -> None:
    lane = _lane(
        "lane",
        "corridor:lane",
        "endpoint:lane",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=1,
    )
    obstacle = _obstacle(
        "obstacle",
        10.0,
        10.0,
        span=(0.0, 10.0),
        semantic_rank=(0, 0),
    )
    forward = _allocations(
        CorridorAllocationProblem(lanes=(lane,), obstacles=(obstacle,), axis_sign=1)
    )
    reflected_lane = CorridorLane(
        member_id=lane.member_id,
        cohort_id=lane.cohort_id,
        endpoint_owner_id=lane.endpoint_owner_id,
        boundary_coordinate=-lane.boundary_coordinate,
        planned_coordinate=-lane.planned_coordinate,
        span_start=lane.span_start,
        span_end=lane.span_end,
        semantic_rank=lane.semantic_rank,
    )
    reflected_obstacle = _obstacle(
        obstacle.obstacle_id,
        -obstacle.order_coordinate,
        -obstacle.realised_coordinate,
        span=(obstacle.span_start, obstacle.span_end),
        semantic_rank=obstacle.semantic_rank,
    )

    reflected = _allocations(
        CorridorAllocationProblem(
            lanes=(reflected_lane,), obstacles=(reflected_obstacle,), axis_sign=-1
        )
    )

    assert reflected == {
        member_id: -coordinate for member_id, coordinate in forward.items()
    }


def test_fixed_obstacle_side_is_chosen_by_semantic_order() -> None:
    lane = _lane(
        "lane",
        "corridor:lane",
        "endpoint:lane",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=0,
    )
    obstacle = _obstacle(
        "obstacle",
        10.0,
        10.0,
        span=(0.0, 10.0),
        semantic_rank=(1, 0),
    )

    assert _allocations(
        CorridorAllocationProblem(lanes=(lane,), obstacles=(obstacle,))
    ) == {"lane": 6.0}


def test_exact_clearance_boundary_preserves_preferred_coordinate() -> None:
    lane = _lane(
        "lane",
        "corridor:lane",
        "endpoint:lane",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
    )
    obstacle = _obstacle(
        "obstacle",
        14.0,
        14.0,
        span=(0.0, 10.0),
        semantic_rank=(1, 0),
    )

    assert _allocations(
        CorridorAllocationProblem(lanes=(lane,), obstacles=(obstacle,))
    ) == {"lane": 10.0}


def test_root_dag_resolves_obstacle_sides_as_one_plan() -> None:
    lanes = (
        _lane(
            "early",
            "corridor:early",
            "endpoint:early",
            0.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=0,
        ),
        _lane(
            "middle",
            "corridor:middle",
            "endpoint:middle",
            0.0,
            10.0,
            span=(5.0, 15.0),
            line_rank=0,
            root_rank=1,
        ),
        _lane(
            "late",
            "corridor:late",
            "endpoint:late",
            0.0,
            10.0,
            span=(10.0, 20.0),
            line_rank=0,
            root_rank=2,
        ),
    )
    obstacle = _obstacle(
        "fixed",
        14.0,
        14.0,
        span=(0.0, 20.0),
        semantic_rank=(3, 0),
    )

    problem = CorridorAllocationProblem(lanes=lanes, obstacles=(obstacle,))

    assert _allocations(problem) == {"early": 2.0, "middle": 6.0, "late": 10.0}
    for order in permutations(lanes):
        assert _allocations(replace(problem, lanes=order)) == {
            "early": 2.0,
            "middle": 6.0,
            "late": 10.0,
        }


def test_pair_separation_is_permutation_and_reflection_invariant() -> None:
    lanes = (
        _lane(
            "early",
            "corridor:early",
            "endpoint:early",
            0.0,
            0.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=0,
        ),
        _lane(
            "late",
            "corridor:late",
            "endpoint:late",
            4.0,
            4.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=1,
        ),
    )
    separation = CorridorSeparation("early", "late", 12.0)
    expected = {"early": -8.0, "late": 4.0}

    for order in permutations(lanes):
        assert (
            _allocations(
                CorridorAllocationProblem(
                    lanes=order,
                    separations=(separation,),
                )
            )
            == expected
        )

    exact = tuple(
        replace(lane, planned_coordinate=expected[lane.member_id]) for lane in lanes
    )
    assert (
        _allocations(CorridorAllocationProblem(lanes=exact, separations=(separation,)))
        == expected
    )

    reflected = tuple(
        replace(
            lane,
            boundary_coordinate=-lane.boundary_coordinate,
            planned_coordinate=-lane.planned_coordinate,
        )
        for lane in lanes
    )
    assert _allocations(
        CorridorAllocationProblem(
            lanes=reflected,
            separations=(separation,),
            axis_sign=-1,
        )
    ) == {member_id: -coordinate for member_id, coordinate in expected.items()}


def test_pair_separation_controls_fixed_obstacle_clearance() -> None:
    lane = _lane(
        "lane",
        "corridor:lane",
        "endpoint:lane",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
        root_rank=0,
    )
    obstacle = _obstacle(
        "obstacle",
        14.0,
        14.0,
        span=(0.0, 10.0),
        semantic_rank=(1, 0),
    )
    separation = CorridorSeparation("lane", "obstacle", 12.0)

    assert _allocations(
        CorridorAllocationProblem(
            lanes=(lane,),
            obstacles=(obstacle,),
            separations=(separation,),
        )
    ) == {"lane": 2.0}
    assert _allocations(
        CorridorAllocationProblem(
            lanes=(replace(lane, planned_coordinate=2.0),),
            obstacles=(obstacle,),
            separations=(separation,),
        )
    ) == {"lane": 2.0}


def test_zero_pair_separation_allows_same_track_coordinates() -> None:
    lanes = (
        _lane(
            "first",
            "corridor:first",
            "endpoint:first",
            10.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=0,
        ),
        _lane(
            "second",
            "corridor:second",
            "endpoint:second",
            10.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=1,
        ),
    )

    assert _allocations(
        CorridorAllocationProblem(
            lanes=lanes,
            separations=(CorridorSeparation("first", "second", 0.0),),
        )
    ) == {"first": 10.0, "second": 10.0}


def test_incomplete_witnesses_return_whole_problem_to_compatibility() -> None:
    problem = CorridorAllocationProblem(
        lanes=(
            _lane(
                "known",
                "corridor:partial",
                "endpoint:partial",
                10.0,
                10.0,
                span=(0.0, 10.0),
                line_rank=1,
            ),
        ),
        witnesses_complete=False,
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.COMPATIBILITY
    assert result.reason is None
    assert result.allocations == ()


def test_coordinate_domains_constrain_the_joint_solve_and_attribute_conflicts() -> None:
    lane = _lane(
        "movable",
        "cohort",
        "endpoint",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
    )
    bounded = solve_corridor_cohorts(
        CorridorAllocationProblem(
            (lane,),
            domains=(CorridorCoordinateDomain("movable", maximum_coordinate=8.0),),
        )
    )

    assert bounded.status is CorridorAllocationStatus.PLANNED
    assert bounded.allocations == (("movable", 8.0),)

    conflicting = solve_corridor_cohorts(
        CorridorAllocationProblem(
            (lane,),
            domains=(
                CorridorCoordinateDomain(
                    "movable",
                    minimum_coordinate=9.0,
                    obstacle_ids=("left-lead",),
                ),
                CorridorCoordinateDomain(
                    "movable",
                    maximum_coordinate=8.0,
                    obstacle_ids=("right-lead",),
                ),
            ),
        )
    )

    assert conflicting.status is CorridorAllocationStatus.FAILURE
    assert conflicting.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert conflicting.blocking_member_ids == ("movable",)
    assert conflicting.blocking_obstacle_ids == ("left-lead", "right-lead")


def test_coordinate_domains_report_order_propagation_conflicts() -> None:
    lanes = (
        _lane(
            "before",
            "corridor:before",
            "endpoint:before",
            10.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
        ),
        _lane(
            "after",
            "corridor:after",
            "endpoint:after",
            5.0,
            5.0,
            span=(0.0, 10.0),
            line_rank=1,
        ),
    )

    domains = (
        CorridorCoordinateDomain("before", minimum_coordinate=10.0),
        CorridorCoordinateDomain(
            "after",
            maximum_coordinate=5.0,
            obstacle_ids=("positive-side-blocker",),
        ),
        CorridorCoordinateDomain(
            "after",
            maximum_coordinate=100.0,
            obstacle_ids=("inactive-blocker",),
        ),
    )
    results = tuple(
        solve_corridor_cohorts(
            CorridorAllocationProblem(
                ordered_lanes,
                domains=domains,
                coordinate_axis=1,
            )
        )
        for ordered_lanes in (lanes, tuple(reversed(lanes)))
    )

    assert results[0] == results[1]
    result = results[0]
    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.allocations == ()
    assert result.clearance_shortfall is not None
    assert result.clearance_shortfall.claim_ids == ("before",)
    assert result.clearance_shortfall.blocking_obstacle_ids == (
        "positive-side-blocker",
    )
    assert result.clearance_shortfall.deficit == 9.0
    assert result.clearance_shortfall.axis == 1
    assert result.clearance_shortfall.required_shift_sign == 1


def test_non_boundary_domain_conflict_has_no_clearance_shortfall() -> None:
    lane = _lane(
        "movable",
        "cohort",
        "endpoint",
        10.0,
        10.0,
        span=(0.0, 10.0),
        line_rank=0,
    )

    result = solve_corridor_cohorts(
        CorridorAllocationProblem(
            (lane,),
            domains=(
                CorridorCoordinateDomain("movable", minimum_coordinate=10.0),
                CorridorCoordinateDomain("movable", maximum_coordinate=5.0),
            ),
        )
    )

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.clearance_shortfall is None


def test_contradictory_equalities_are_attributed_without_mutating_input() -> None:
    lanes = (
        _lane(
            "a",
            "corridor:a",
            "endpoint:a",
            10.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=1,
        ),
        _lane(
            "b",
            "corridor:b",
            "endpoint:b",
            14.0,
            14.0,
            span=(0.0, 10.0),
            line_rank=2,
        ),
    )
    equalities = (
        CorridorEquality("network:ab", "a", "b", 2.0),
        CorridorEquality("network:ab", "a", "b", 4.0),
    )
    problem = CorridorAllocationProblem(lanes=lanes, equalities=equalities)
    before = (problem.lanes, problem.obstacles, problem.equalities)

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.CONTRADICTION
    assert result.allocations == ()
    assert result.blocking_member_ids == ("a", "b")
    assert result.blocking_obstacle_ids == ()
    assert result.blocking_equality_owner_ids == ("network:ab",)
    assert (problem.lanes, problem.obstacles, problem.equalities) == before
