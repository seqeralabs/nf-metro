"""Corridor cohorts allocate from realised geometry and frozen witnesses."""

from __future__ import annotations

from dataclasses import replace
from itertools import permutations

import pytest

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
    turns: tuple[int, int] = (0, 0),
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
        start_turn_side=turns[0],
        end_turn_side=turns[1],
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


def _turning_lane(
    member_id: str,
    span: tuple[float, float],
    turns: tuple[int, int],
    line_rank: int,
) -> CorridorLane:
    return _lane(
        member_id,
        f"corridor:{member_id}",
        f"endpoint:{member_id}",
        0.0,
        0.0,
        span=span,
        line_rank=line_rank,
        turns=turns,
    )


def test_end_turn_inside_a_run_seats_the_lane_on_its_turn_side() -> None:
    lanes = (
        _turning_lane("semantic-first", (0.0, 100.0), (0, 1), 0),
        _turning_lane("semantic-second", (50.0, 200.0), (0, 0), 1),
    )
    for axis_sign in (1, -1):
        allocations = _allocations(
            CorridorAllocationProblem(lanes, axis_sign=axis_sign)
        )

        assert allocations["semantic-first"] - allocations["semantic-second"] == 4.0


def test_opposing_end_turns_leave_the_pair_in_semantic_order() -> None:
    lanes = (
        _turning_lane("semantic-first", (0.0, 100.0), (0, 1), 0),
        _turning_lane("semantic-second", (50.0, 200.0), (1, 0), 1),
    )

    allocations = _allocations(CorridorAllocationProblem(lanes))

    assert allocations["semantic-second"] - allocations["semantic-first"] == 4.0


def test_end_turn_votes_pool_over_every_member_pair_of_two_roots() -> None:
    lanes = (
        _turning_lane("outer-first", (0.0, 100.0), (0, 1), 0),
        _turning_lane("inner", (50.0, 300.0), (0, 1), 2),
        _turning_lane("outer-second", (200.0, 400.0), (0, 0), 1),
    )
    problem = CorridorAllocationProblem(
        lanes,
        equalities=(CorridorEquality("outer", "outer-first", "outer-second", 0.0),),
    )

    allocations = _allocations(problem)

    assert allocations["outer-first"] == allocations["outer-second"]
    assert allocations["inner"] - allocations["outer-first"] == 4.0


def test_end_turn_within_tolerance_of_the_other_span_end_does_not_vote() -> None:
    lanes = (
        _turning_lane("semantic-first", (0.0, 50.5), (0, 1), 0),
        _turning_lane("semantic-second", (50.0, 200.0), (0, 0), 1),
    )

    allocations = _allocations(CorridorAllocationProblem(lanes))

    assert allocations["semantic-second"] - allocations["semantic-first"] == 4.0


def _end_turn_chain() -> tuple[CorridorLane, ...]:
    """``a`` turns off inside ``b`` toward ``b``'s low side and ``b`` inside ``c``
    toward ``c``'s; ``a``'s two turns inside ``c`` oppose each other, and
    semantic rank alone would seat ``c`` below ``a``."""
    return (
        _turning_lane("a", (100.0, 200.0), (1, -1), 2),
        _turning_lane("b", (150.0, 500.0), (0, -1), 1),
        _turning_lane("c", (0.0, 1000.0), (0, 0), 0),
    )


def test_an_unordered_pair_follows_the_order_end_turns_force() -> None:
    for lanes in permutations(_end_turn_chain()):
        assert _allocations(CorridorAllocationProblem(lanes)) == {
            "a": -8.0,
            "b": -4.0,
            "c": 0.0,
        }


def test_end_turn_orders_contradicting_a_directed_separation_fail_closed() -> None:
    problem = CorridorAllocationProblem(
        _end_turn_chain(),
        directed_separations=(CorridorDirectedSeparation("order:ca", "c", "a", 4.0),),
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.allocations == ()
    assert result.blocking_member_ids == ("a", "b", "c")
    assert result.blocking_order_owner_ids == ("order:ca",)


def _pinned_pair_problem() -> CorridorAllocationProblem:
    """``free`` is drawn one pitch below ``pinned``, which a fixed equality holds
    at 100; nothing orders the pair, and semantic rank alone seats ``free``
    first."""
    return CorridorAllocationProblem(
        lanes=(
            _lane(
                "free",
                "corridor:free",
                "endpoint:free",
                104.0,
                104.0,
                span=(0.0, 10.0),
                line_rank=0,
            ),
            _lane(
                "pinned",
                "corridor:pinned",
                "endpoint:pinned",
                100.0,
                100.0,
                span=(0.0, 10.0),
                line_rank=1,
            ),
        ),
        obstacles=(
            _obstacle("anchor", 100.0, 100.0, span=(0.0, 10.0), semantic_rank=(1,)),
        ),
        fixed_equalities=(CorridorFixedEquality("context", "pinned", "anchor"),),
    )


def test_an_unordered_pair_holding_a_fixed_lane_keeps_its_drawn_order() -> None:
    problem = _pinned_pair_problem()
    for lanes in permutations(problem.lanes):
        assert _allocations(replace(problem, lanes=lanes)) == {
            "free": 104.0,
            "pinned": 100.0,
        }


def test_a_movable_pair_seated_clear_keeps_its_seated_order() -> None:
    problem = replace(_pinned_pair_problem(), obstacles=(), fixed_equalities=())
    for lanes in permutations(problem.lanes):
        assert _allocations(replace(problem, lanes=lanes)) == {
            "free": 104.0,
            "pinned": 100.0,
        }


@pytest.mark.parametrize("free_coordinate", (100.0, 102.0, 98.0))
def test_a_movable_pair_seated_inside_its_clearance_follows_semantic_rank(
    free_coordinate: float,
) -> None:
    """Seated coincident or closer than the 4.0 clearance, neither order is
    already clear, so ``free``'s semantic rank seats it first."""
    problem = replace(_pinned_pair_problem(), obstacles=(), fixed_equalities=())
    free, pinned = problem.lanes
    lanes = (
        replace(
            free,
            boundary_coordinate=free_coordinate,
            planned_coordinate=free_coordinate,
        ),
        pinned,
    )
    allocations = _allocations(replace(problem, lanes=lanes))
    assert allocations["pinned"] - allocations["free"] == 4.0


def test_a_pair_seated_clear_keeps_its_seated_order_over_opposing_end_turns() -> None:
    """The two end turns oppose, so every order crosses one of them; the pair
    is already seated a clearance apart with ``semantic-first`` above, and
    reordering it would only add a move."""
    lanes = (
        replace(
            _turning_lane("semantic-first", (0.0, 100.0), (0, 1), 0),
            planned_coordinate=10.0,
        ),
        _turning_lane("semantic-second", (50.0, 200.0), (1, 0), 1),
    )
    for ordered in permutations(lanes):
        assert _allocations(CorridorAllocationProblem(ordered)) == {
            "semantic-first": 10.0,
            "semantic-second": 0.0,
        }


def test_a_pair_seated_clear_keeps_its_seated_order_over_an_end_turn_vote() -> None:
    """``semantic-first`` turns toward ``semantic-second``'s positive side
    inside its run, but is seated a clearance below it already."""
    lanes = (
        _turning_lane("semantic-first", (0.0, 100.0), (0, 1), 0),
        replace(
            _turning_lane("semantic-second", (50.0, 200.0), (0, 0), 1),
            planned_coordinate=10.0,
        ),
    )
    for axis_sign in (1, -1):
        assert _allocations(CorridorAllocationProblem(lanes, axis_sign=axis_sign)) == {
            "semantic-first": 0.0,
            "semantic-second": 10.0,
        }


def test_seated_order_reads_a_cohort_at_its_rigid_offsets() -> None:
    """``x:0`` and ``x:1`` are one rigid cohort 4.0 apart but drawn 10.0 apart
    the other way round; the cohort seats at ``x:0`` = 6.0 from the lower median
    of its members' drawn bases.  ``y`` is drawn 4.0 below ``x:0``'s drawn
    coordinate, yet seated 10.0 above ``x:0``'s seat, and it is the seat that
    decides the order; semantic rank would seat ``y`` first."""
    lanes = (
        _lane(
            "x:0",
            "corridor:x",
            "endpoint:x",
            0.0,
            20.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=1,
        ),
        _lane(
            "x:1",
            "corridor:x",
            "endpoint:x",
            4.0,
            10.0,
            span=(20.0, 30.0),
            line_rank=1,
            root_rank=1,
        ),
        _lane(
            "y", "corridor:y", "endpoint:y", 16.0, 16.0, span=(0.0, 10.0), line_rank=0
        ),
    )
    for ordered in permutations(lanes):
        assert _allocations(CorridorAllocationProblem(ordered)) == {
            "x:0": 6.0,
            "x:1": 10.0,
            "y": 16.0,
        }


def test_a_pair_seated_clear_on_both_sides_is_left_to_semantic_rank() -> None:
    """``x`` is seated a clearance below ``y`` on the first span and above it on
    the second, so neither order is already clear; ``y``'s semantic rank seats
    it first."""
    lanes = (
        _lane(
            "x:0", "corridor:x", "endpoint:x", 0.0, 0.0, span=(0.0, 10.0), line_rank=0
        ),
        _lane(
            "x:1",
            "corridor:x",
            "endpoint:x",
            20.0,
            20.0,
            span=(20.0, 30.0),
            line_rank=1,
        ),
        _lane(
            "y:0",
            "corridor:y",
            "endpoint:y",
            10.0,
            10.0,
            span=(0.0, 10.0),
            line_rank=0,
            root_rank=-1,
        ),
        _lane(
            "y:1",
            "corridor:y",
            "endpoint:y",
            10.0,
            10.0,
            span=(20.0, 30.0),
            line_rank=1,
            root_rank=-1,
        ),
    )
    for ordered in permutations(lanes):
        assert _allocations(CorridorAllocationProblem(ordered)) == {
            "x:0": 0.0,
            "x:1": 20.0,
            "y:0": -4.0,
            "y:1": -4.0,
        }


def _seated_chain() -> tuple[CorridorLane, ...]:
    """Three single-lane roots seated one after another a pitch apart, so the
    seats alone order ``a < b < c``."""
    return tuple(
        _lane(
            member_id,
            f"corridor:{member_id}",
            f"endpoint:{member_id}",
            coordinate,
            coordinate,
            span=(0.0, 100.0),
            line_rank=0,
            root_rank=rank,
        )
        for rank, (member_id, coordinate) in enumerate(
            (("a", 0.0), ("b", 10.0), ("c", 20.0))
        )
    )


def test_seated_orders_cycling_through_a_directed_separation_give_way() -> None:
    """The separation seats ``c`` below ``a``, which no order keeping both
    seated orders can, so the seated orders give way and semantic rank orders
    every pair the separation leaves free."""
    for lanes in permutations(_seated_chain()):
        problem = CorridorAllocationProblem(
            lanes,
            directed_separations=(
                CorridorDirectedSeparation("order:ca", "c", "a", 4.0),
            ),
        )
        assert _allocations(problem) == {"a": 0.0, "b": -8.0, "c": -4.0}


def test_seated_orders_that_cycle_among_braided_cohorts_give_way() -> None:
    """Each pair of the three rigid cohorts is seated clear on the one span the
    pair shares, and those seated orders cycle: ``a`` below ``b`` below ``c``
    below ``a``.  One order of roots cannot keep all three, so the seated
    orders give way to semantic rank."""
    lanes = (
        _lane("a:0", "c1", "e1", 0.0, 0.0, span=(0.0, 10.0), line_rank=0),
        _lane("a:1", "c1", "e1", 10.0, 10.0, span=(40.0, 50.0), line_rank=1),
        _lane(
            "b:0", "c2", "e2", 10.0, 10.0, span=(0.0, 10.0), line_rank=0, root_rank=1
        ),
        _lane("b:1", "c2", "e2", 0.0, 0.0, span=(20.0, 30.0), line_rank=1, root_rank=1),
        _lane(
            "c:0", "c3", "e3", 10.0, 10.0, span=(20.0, 30.0), line_rank=0, root_rank=2
        ),
        _lane("c:1", "c3", "e3", 0.0, 0.0, span=(40.0, 50.0), line_rank=1, root_rank=2),
    )
    for ordered in permutations(lanes):
        assert _allocations(CorridorAllocationProblem(ordered)) == {
            "a:0": -14.0,
            "a:1": -4.0,
            "b:0": 10.0,
            "b:1": 0.0,
            "c:0": 10.0,
            "c:1": 0.0,
        }


def _straddled_fixed_lane_problem(
    a_turns: tuple[int, int] = (0, 0),
    directed_separations: tuple[CorridorDirectedSeparation, ...] = (),
) -> CorridorAllocationProblem:
    """Movable ``a`` drawn at 9 and ``b`` at 6 straddle ``f``, which a fixed
    equality holds at 8; every pair is drawn inside its 4.0 clearance, so none
    is seated clear, and semantic rank puts ``a`` first."""
    return CorridorAllocationProblem(
        (
            _lane(
                "a", "ca", "ea", 9.0, 9.0, span=(0.0, 50.0), line_rank=0, turns=a_turns
            ),
            _lane(
                "b", "cb", "eb", 6.0, 6.0, span=(0.0, 100.0), line_rank=0, root_rank=1
            ),
            _lane(
                "f", "cf", "ef", 8.0, 8.0, span=(0.0, 100.0), line_rank=0, root_rank=2
            ),
        ),
        obstacles=(
            _obstacle("anchor", 8.0, 8.0, span=(0.0, 100.0), semantic_rank=(9,)),
        ),
        fixed_equalities=(CorridorFixedEquality("context", "f", "anchor"),),
        directed_separations=directed_separations,
    )


def test_movable_lanes_straddling_a_fixed_lane_keep_their_drawn_sides() -> None:
    problem = _straddled_fixed_lane_problem()
    for lanes in permutations(problem.lanes):
        assert _allocations(replace(problem, lanes=lanes)) == {
            "a": 12.0,
            "b": 4.0,
            "f": 8.0,
        }


@pytest.mark.parametrize(
    "problem",
    (
        _straddled_fixed_lane_problem(
            directed_separations=(
                CorridorDirectedSeparation("order:ab", "a", "b", 4.0),
            ),
        ),
        _straddled_fixed_lane_problem(a_turns=(0, -1)),
    ),
    ids=("directed-separation", "end-turn"),
)
def test_fixed_lane_drawn_orders_cycling_through_a_forced_order_give_way(
    problem: CorridorAllocationProblem,
) -> None:
    """Seating ``a`` below ``b`` leaves ``f``'s drawn sides, ``b`` below it and
    ``a`` above, no order of roots; the fixed lane's drawn orders give way and
    semantic rank orders the pairs through it."""
    for lanes in permutations(problem.lanes):
        assert _allocations(replace(problem, lanes=lanes)) == {
            "a": 0.0,
            "b": 4.0,
            "f": 8.0,
        }


def test_a_seated_order_that_cannot_seat_gives_way() -> None:
    """``z`` is drawn a clearance above ``y``, which a domain holds at or above
    its drawn 12, and a separation holds ``q`` a clearance above ``z``.  Keeping
    ``y`` below ``z`` would lift ``z`` and ``q`` off where they are drawn, so
    the seated order gives way and semantic rank seats ``z`` below ``y``."""
    for lanes in permutations(
        (
            _lane(
                "y", "cy", "ey", 12.0, 12.0, span=(0.0, 50.0), line_rank=0, root_rank=2
            ),
            _lane(
                "z", "cz", "ez", 16.0, 16.0, span=(0.0, 100.0), line_rank=0, root_rank=1
            ),
            _lane(
                "q",
                "cq",
                "eq",
                17.0,
                17.0,
                span=(60.0, 100.0),
                line_rank=0,
                root_rank=3,
            ),
        )
    ):
        problem = CorridorAllocationProblem(
            lanes,
            domains=(CorridorCoordinateDomain("y", minimum_coordinate=12.0),),
            directed_separations=(
                CorridorDirectedSeparation("order:zq", "z", "q", 4.0),
            ),
        )
        assert _allocations(problem) == {"q": 17.0, "y": 12.0, "z": 8.0}


def _lifted_fixed_lane_problem(
    z_coordinate: float,
    directed_separations: tuple[CorridorDirectedSeparation, ...] = (),
) -> CorridorAllocationProblem:
    """A fixed equality holds ``x`` at 8, drawn below ``y`` at 10, so ``y``
    clears ``x`` only at 12 or above; ``z`` overlaps ``y`` alone."""
    return CorridorAllocationProblem(
        (
            _lane(
                "y", "cy", "ey", 10.0, 10.0, span=(0.0, 100.0), line_rank=0, root_rank=1
            ),
            _lane(
                "z",
                "cz",
                "ez",
                z_coordinate,
                z_coordinate,
                span=(60.0, 100.0),
                line_rank=0,
                root_rank=2,
            ),
            _lane(
                "x", "cx", "ex", 8.0, 8.0, span=(0.0, 40.0), line_rank=0, root_rank=3
            ),
        ),
        obstacles=(
            _obstacle("anchor", 8.0, 8.0, span=(0.0, 40.0), semantic_rank=(9,)),
        ),
        fixed_equalities=(CorridorFixedEquality("context", "x", "anchor"),),
        directed_separations=directed_separations,
    )


@pytest.mark.parametrize(
    ("problem", "expected"),
    (
        (_lifted_fixed_lane_problem(12.0), {"x": 8.0, "y": 12.0, "z": 8.0}),
        (_lifted_fixed_lane_problem(14.0), {"x": 8.0, "y": 12.0, "z": 8.0}),
        (
            _lifted_fixed_lane_problem(
                12.0,
                directed_separations=(
                    CorridorDirectedSeparation("order:yz", "y", "z", 4.0),
                ),
            ),
            {"x": 8.0, "y": 4.0, "z": 12.0},
        ),
    ),
    ids=("semantic-rank", "seated-clear", "directed-separation"),
)
def test_preference_orders_that_cannot_seat_give_way_in_turn(
    problem: CorridorAllocationProblem, expected: dict[str, float]
) -> None:
    """Semantic rank seats ``z`` below ``y``, and ``y`` lifts clear of ``x``.
    Seated a clearance above ``y``, ``z`` would hold ``y`` below 10 and so onto
    ``x``; the seated order gives way first.  A separation holding ``z`` above
    ``y`` instead leaves ``x``'s drawn order, ``x`` below ``y``, no seating; the
    fixed-lane order gives way next, and ``y`` seats below ``x``."""
    for lanes in permutations(problem.lanes):
        assert _allocations(replace(problem, lanes=lanes)) == expected


def test_a_failure_no_order_can_seat_reports_the_fullest_orders() -> None:
    """A domain pins ``y`` at 12, and ``z`` lies between 10 and ``q``'s
    ceiling of 17 less the separation, so ``z`` clears ``y`` on neither side.
    With the seated order ``y`` fails against ``q``'s ceiling; without it
    ``z`` fails against its own floor instead."""
    for lanes in permutations(
        (
            _lane(
                "y", "cy", "ey", 12.0, 12.0, span=(0.0, 50.0), line_rank=0, root_rank=2
            ),
            _lane(
                "z", "cz", "ez", 16.0, 16.0, span=(0.0, 100.0), line_rank=0, root_rank=1
            ),
            _lane(
                "q",
                "cq",
                "eq",
                17.0,
                17.0,
                span=(60.0, 100.0),
                line_rank=0,
                root_rank=3,
            ),
        )
    ):
        problem = CorridorAllocationProblem(
            lanes,
            domains=(
                CorridorCoordinateDomain("y", 12.0, 12.0, ("pin:y",)),
                CorridorCoordinateDomain(
                    "z", minimum_coordinate=10.0, obstacle_ids=("floor:z",)
                ),
                CorridorCoordinateDomain(
                    "q", maximum_coordinate=17.0, obstacle_ids=("ceiling:q",)
                ),
            ),
            directed_separations=(
                CorridorDirectedSeparation("order:zq", "z", "q", 4.0),
            ),
        )

        result = solve_corridor_cohorts(problem)

        assert result.status is CorridorAllocationStatus.FAILURE
        assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
        assert result.blocking_member_ids == ("y",)
        assert result.blocking_obstacle_ids == ("ceiling:q", "pin:y")


def test_contradicting_directed_separations_fail_closed_whatever_gives_way() -> None:
    lanes = (
        _lane("a", "ca", "ea", 0.0, 0.0, span=(0.0, 100.0), line_rank=0, root_rank=1),
        _lane("b", "cb", "eb", 8.0, 8.0, span=(0.0, 100.0), line_rank=0, root_rank=2),
    )
    problem = CorridorAllocationProblem(
        lanes,
        directed_separations=(
            CorridorDirectedSeparation("order:ab", "a", "b", 4.0),
            CorridorDirectedSeparation("order:ba", "b", "a", 4.0),
        ),
    )

    result = solve_corridor_cohorts(problem)

    assert result.status is CorridorAllocationStatus.FAILURE
    assert result.reason is CorridorAllocationFailureReason.INFEASIBLE
    assert result.allocations == ()
    assert result.blocking_member_ids == ("a", "b")
    assert result.blocking_order_owner_ids == ("order:ab", "order:ba")


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
    """Drawn 2.0 apart, inside the clearance, the pair has no seated order and
    semantic rank seats ``before`` first, which its domain floor then pushes
    past ``after``'s ceiling."""
    lanes = (
        _lane(
            "before",
            "corridor:before",
            "endpoint:before",
            10.0,
            7.0,
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
