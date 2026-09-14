"""A route system decides the lanes of a channel two of its trunks would share.

Each convergence plan reads its trunk coordinate from a trial route produced with
no knowledge of its siblings, so two plans of one system taking the same channel
derive the same coordinate.  The decision belongs to the system: it lanes them by
``cotravelling_lane_clearance``, which separates a line from its own return leg by
a turn radius and fuses two trunks running the same way onto one stroke.

The same clearance bounds the other side of the decision.  Runs that co-travel a
corridor toward one local destination are lanes of one bundle, so a trial
coordinate leaving them further apart than their pitch is packed back onto it,
against a sibling trunk or against a frozen member run already in the corridor.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.geometry import cotravelling_lane_clearance
from nf_metro.layout.route_plan import (
    ConvergenceContinuation,
    ConvergenceDisposition,
    ConvergenceEndpointOwnership,
    ConvergenceEndpointRole,
    ConvergenceLanding,
    ConvergencePlan,
    ConvergencePlanId,
    ConvergenceTrunkAxis,
    ConvergenceTrunkReason,
    DemandAxis,
    DemandId,
    EmissionMemberId,
    RoutePlan,
    RouteSystemId,
    SharedReferenceId,
    TurnHandedness,
)
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.convergences import (
    _landing_cross_segment,
    _parallel_segments_conflict,
    _settle_landing_trunk_flanks,
    _settle_opposing_landing_channels,
    _settle_shared_trunk_channels,
    _trunk_run_travel_direction,
    _trunk_segments,
)
from nf_metro.parser.route_topology import (
    ConnectorId,
    ConvergenceId,
    EndpointGroupId,
    ResolvedEdge,
)
from nf_metro.render.svg import build_observed_render_plan

ROOT = Path(__file__).parents[1]

# Fixtures whose route systems carry more than one convergence trunk: one pair
# that counter-runs and has to be laned, and two that co-travel and have to stay
# fused.
SHARED_CHANNEL_FIXTURES = (
    ROOT / "examples" / "topologies" / "merge_around_below_leftmost.mmd",
    ROOT / "examples" / "topologies" / "fan_in_merge.mmd",
    ROOT / "examples" / "guide" / "03b_fan_in_merge.mmd",
)

LANE_CLEARANCE = cotravelling_lane_clearance(
    same_line=True, counter_running=True, curve_radius=CURVE_RADIUS
)


def _published_plan(path: Path) -> RoutePlan:
    """The route plan *path* publishes through the render chokepoint."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        plan = build_observed_render_plan(graph, resolve_theme(None, graph)).route_plan
    assert plan is not None
    return plan


def _systems_with_shared_trunks(
    path: Path,
) -> dict[str, list[ConvergencePlan]]:
    """*path*'s route systems that converge more than once, by system."""
    plan = _published_plan(path)
    by_system: dict[str, list[ConvergencePlan]] = {}
    for item in plan.convergence_plans:
        by_system.setdefault(str(item.system_id), []).append(item)
    return {key: items for key, items in by_system.items() if len(items) > 1}


def _overlap(first: ConvergenceTrunkAxis, second: ConvergenceTrunkAxis) -> float:
    return min(first.extent_end, second.extent_end) - max(
        first.extent_start, second.extent_start
    )


@pytest.mark.parametrize(
    "path", SHARED_CHANNEL_FIXTURES, ids=lambda item: Path(item).stem
)
def test_one_system_lanes_the_trunk_channel_its_plans_share(path: Path) -> None:
    systems = _systems_with_shared_trunks(path)
    assert systems, f"{path.name} publishes no system that converges twice"
    for plans in systems.values():
        assert {item.disposition for item in plans} == {
            ConvergenceDisposition.PLANNED
        }, "a shared trunk channel is a decision the system makes, not one it declines"
        axes = [item.trunk_axis for item in plans if item.trunk_axis is not None]
        for first, second in combinations(axes, 2):
            if first.axis is not second.axis or _overlap(first, second) <= (
                COORD_TOLERANCE
            ):
                continue
            separation = abs(first.coordinate - second.coordinate)
            if first.direction is second.direction:
                assert separation <= COORD_TOLERANCE, (
                    "trunks running one way along one channel draw one stroke"
                )
            else:
                assert separation >= LANE_CLEARANCE, (
                    f"a line and its return leg need {LANE_CLEARANCE}px between "
                    f"their lanes, not {separation}px"
                )


def _member_interior_runs(
    plan: RoutePlan, system_id: RouteSystemId
) -> list[tuple[float, float, float]]:
    """Every horizontal run *system_id*'s members hold between two turns."""
    runs: list[tuple[float, float, float]] = []
    for member in plan.member_geometry_plans:
        if member.system_id != system_id:
            continue
        points = member.points
        for rank in range(1, len(points) - 2):
            start, end = points[rank], points[rank + 1]
            before, after = points[rank - 1], points[rank + 2]
            if (
                abs(start[1] - end[1]) > COORD_TOLERANCE
                or abs(start[0] - end[0]) <= COORD_TOLERANCE
                or abs(before[0] - start[0]) > COORD_TOLERANCE
                or abs(after[0] - end[0]) > COORD_TOLERANCE
            ):
                continue
            runs.append((start[1], min(start[0], end[0]), max(start[0], end[0])))
    return runs


def test_distinct_line_trunks_heading_off_one_junction_hold_bundle_pitch() -> None:
    """Two trunks fed by the same junctions travel their gap as one bundle.

    They part only at the far end, where each turns off to its own target, so a
    trial coordinate that leaves them a whole bundle apart reads as two separate
    corridors crossing the map rather than one bundle of two lines.
    """
    plan = _published_plan(
        ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd"
    )
    trunks = {
        item.line_ids: item.trunk_axis
        for item in plan.convergence_plans
        if item.trunk_axis is not None
    }
    sheets, report = trunks[("sheets",)], trunks[("report",)]

    assert sheets.direction is report.direction
    assert abs(sheets.coordinate - report.coordinate) == pytest.approx(OFFSET_STEP)


def test_a_trunk_returning_to_an_entry_port_joins_the_member_run_already_there() -> (
    None
):
    """A trunk and a frozen member both ending at one entry port draw one stroke.

    The member's geometry is settled before the trunk's channel is decided, so
    the coordinate it holds is the one the trunk has to meet rather than a second
    lane for the same line to run in.
    """
    plan = _published_plan(ROOT / "examples" / "topologies" / "merge_right_entry.mmd")
    trunk = next(item for item in plan.convergence_plans if item.trunk_axis is not None)
    axis = trunk.trunk_axis
    assert axis is not None

    shared = [
        run
        for run in _member_interior_runs(plan, trunk.system_id)
        if min(run[2], axis.extent_end) - max(run[1], axis.extent_start) > CURVE_RADIUS
    ]
    assert shared, "the fixture no longer routes a member through the trunk's corridor"
    for coordinate, _lo, _hi in shared:
        assert coordinate == pytest.approx(axis.coordinate)


def _planned_plan(
    tag: str, axis: DemandAxis, direction: Direction, flank: float
) -> ConvergencePlan:
    """A minimal planned convergence whose only geometry is one trunk."""
    feeder = ResolvedEdge(f"{tag}_source", f"{tag}_merge", "a")
    outgoing = ResolvedEdge(f"{tag}_merge", f"{tag}_entry", "a")
    feeder_member = EmissionMemberId(f"{tag}_feeder")
    outgoing_member = EmissionMemberId(f"{tag}_outgoing")
    connector = ConnectorId(f"{tag}_connector")
    travelling_x = axis is DemandAxis.X
    join = (0.0, 50.0) if travelling_x else (50.0, 0.0)
    return ConvergencePlan(
        id=ConvergencePlanId(f"{tag}_plan"),
        system_id=RouteSystemId("system"),
        convergence_ids=(ConvergenceId(f"{tag}_convergence"),),
        entry_group_ids=(EndpointGroupId(f"{tag}_entry_group"),),
        merge_junction_ids=(f"{tag}_merge",),
        target_entry_port_ids=(f"{tag}_entry",),
        connector_ids=(connector,),
        member_ids=(feeder_member, outgoing_member),
        resolved_member_paths=((feeder, outgoing),),
        resolved_member_edges=(feeder, outgoing),
        line_ids=("a",),
        upstream_exit_turn_plan_ids=(),
        upstream_fan_plan_ids=(),
        primary_trunk_member_id=feeder_member,
        primary_trunk_reason=ConvergenceTrunkReason.LONGEST_BYPASS,
        trunk_axis=ConvergenceTrunkAxis(
            axis=axis,
            coordinate=50.0,
            extent_start=0.0,
            extent_end=100.0,
            direction=direction,
            source_flank_coordinate=flank,
            target_flank_coordinate=flank,
            claimant_member_ids=(feeder_member,),
        ),
        landings=(
            ConvergenceLanding(
                member_id=feeder_member,
                edge=feeder,
                source_junction_id=f"{tag}_source",
                approach_axis=DemandAxis.Y if travelling_x else DemandAxis.X,
                approach_direction=Direction.D if travelling_x else Direction.R,
                source_column=0,
                source_row=0,
                lane_rank=0,
                order=0,
                join_point=join,
                corner_handedness=None,
                minimum_runway=CURVE_RADIUS,
                opening_turn_coordinate=None,
                opening_turn_segment=None,
                bypass=True,
                long_haul=False,
                multiple_row=False,
            ),
        ),
        outgoing_continuations=(
            ConvergenceContinuation(
                member_id=outgoing_member,
                edge=outgoing,
                entry_port_id=f"{tag}_entry",
                lane_rank=0,
                start_point=(100.0, 50.0) if travelling_x else (50.0, 100.0),
                end_point=(110.0, 50.0) if travelling_x else (50.0, 110.0),
                covered_by_member_id=None,
            ),
        ),
        lane_order=("a",),
        endpoint_ownership=(
            ConvergenceEndpointOwnership(
                member_id=feeder_member,
                edge=feeder,
                connector_ids=(connector,),
                role=ConvergenceEndpointRole.TRUNK,
                endpoint=join,
            ),
            ConvergenceEndpointOwnership(
                member_id=outgoing_member,
                edge=outgoing,
                connector_ids=(connector,),
                role=ConvergenceEndpointRole.COVERED_CONTINUATION,
                endpoint=(110.0, 50.0) if travelling_x else (50.0, 110.0),
                covered_by_member_id=feeder_member,
            ),
        ),
        shared_reference_ids=(
            SharedReferenceId(f"{tag}_reference_0"),
            SharedReferenceId(f"{tag}_reference_1"),
        ),
        demand_ids=(DemandId(f"{tag}_demand"),),
        foreign_reference_ids=(),
        disposition=ConvergenceDisposition.PLANNED,
        legacy_reason=None,
    )


@pytest.mark.parametrize(
    ("axis", "forward", "backward"),
    (
        (DemandAxis.X, Direction.R, Direction.L),
        (DemandAxis.Y, Direction.D, Direction.U),
    ),
    ids=("travelling-x", "travelling-y"),
)
def test_the_lane_decision_reads_the_same_on_either_travel_axis(
    axis: DemandAxis, forward: Direction, backward: Direction
) -> None:
    """The channel is named by the axis its trunks travel, so a system rotated a
    quarter turn lanes by the same rule rather than by a copy of it.

    Both trunks arrive on one coordinate, as two trial routes through one channel
    do, and the second yields toward the side its own flanks lead.
    """
    plans = (
        _planned_plan("first", axis, forward, 10.0),
        _planned_plan("second", axis, backward, 90.0),
    )

    settled = _settle_shared_trunk_channels(plans, CURVE_RADIUS)

    axes = [item.trunk_axis for item in settled]
    assert axes[0] is not None and axes[1] is not None
    assert axes[0].coordinate == 50.0
    assert axes[1].coordinate == pytest.approx(50.0 + LANE_CLEARANCE)
    landing = settled[1].landings[0]
    across = 1 if axis is DemandAxis.X else 0
    assert landing.join_point[across] == pytest.approx(50.0 + LANE_CLEARANCE)


def _boxed_in_flank_pair() -> tuple[ConvergencePlan, ConvergencePlan]:
    """Two flanks of one line crowding one column, the second with no lane.

    Both turn out of the channel toward an endpoint barely a turn radius away,
    so every lane one clearance from the other flank costs the second plan the
    runway its own corner needs.  The first has room on the far side.
    """
    resident, newcomer = (
        _planned_plan("resident", DemandAxis.X, Direction.R, -30.0),
        _planned_plan("newcomer", DemandAxis.X, Direction.R, 130.0),
    )
    return (
        replace(
            resident,
            trunk_axis=replace(
                resident.trunk_axis,
                extent_start=0.0,
                source_endpoint_coordinate=5.0,
                target_endpoint_coordinate=105.0,
            ),
        ),
        replace(
            newcomer,
            trunk_axis=replace(
                newcomer.trunk_axis,
                coordinate=20.0,
                extent_start=-5.0,
                extent_end=400.0,
                source_endpoint_coordinate=-3.0,
                target_endpoint_coordinate=405.0,
            ),
        ),
    )


def test_a_flank_with_no_lane_of_its_own_is_given_way_to_by_the_resident() -> None:
    """The channel is the pair's to settle, so a boxed-in flank is not declined.

    One line's outward and return legs on one column is a doubled stroke, and
    the system has the geometry to avoid it either way round: where the arriving
    flank can reach no lane, the flank already seated takes one instead.
    """
    resident, newcomer = _boxed_in_flank_pair()

    settled = _settle_shared_trunk_channels((resident, newcomer), CURVE_RADIUS)

    columns = [item.trunk_axis.extent_start for item in settled]
    assert columns[1] == pytest.approx(-5.0), (
        "the flank with no reachable lane keeps its column"
    )
    assert abs(columns[0] - columns[1]) >= LANE_CLEARANCE, (
        "the resident gave way, so the two flanks stand a clearance apart"
    )


def _boxed_in_target_flank_pair() -> tuple[ConvergencePlan, ConvergencePlan]:
    """The target-side mirror of :func:`_boxed_in_flank_pair`.

    The two rank-3 flanks counter-run one column apart, and the arriving one
    turns onto an endpoint too close to take either lane beside the resident.
    """
    resident, newcomer = (
        _planned_plan("resident", DemandAxis.X, Direction.R, 130.0),
        _planned_plan("newcomer", DemandAxis.X, Direction.R, 60.0),
    )
    return (
        replace(
            resident,
            trunk_axis=replace(
                resident.trunk_axis,
                source_flank_coordinate=-30.0,
                target_endpoint_coordinate=95.0,
                source_endpoint_coordinate=5.0,
            ),
        ),
        replace(
            newcomer,
            trunk_axis=replace(
                newcomer.trunk_axis,
                coordinate=140.0,
                extent_start=-5.0,
                extent_end=105.0,
                source_flank_coordinate=200.0,
                source_endpoint_coordinate=-10.0,
                target_endpoint_coordinate=103.0,
            ),
        ),
    )


def test_a_boxed_in_target_flank_is_given_way_to_by_the_resident() -> None:
    """The give-way rule reads the same on the flank at either end of a trunk.

    A trunk states a flank at its source and one at its target, and the seat a
    resident occupies is what a give-way writes back to. Deriving that seat from
    the length of the list being appended to would address the source flank and
    the target flank alike, so the pair is settled from both ends.
    """
    resident, newcomer = _boxed_in_target_flank_pair()

    settled = _settle_shared_trunk_channels((resident, newcomer), CURVE_RADIUS)

    columns = [item.trunk_axis.extent_end for item in settled]
    assert columns[1] == pytest.approx(105.0), (
        "the flank with no reachable lane keeps its column"
    )
    assert abs(columns[0] - columns[1]) >= LANE_CLEARANCE, (
        "the resident gave way, so the two flanks stand a clearance apart"
    )


def _crowded_from_both_sides() -> tuple[ConvergencePlan, ...]:
    """Three trunks of one line where one resident is asked to give way twice.

    The first plan's flanks stand alone.  The second and third each counter-run
    it at both ends and can reach no lane of their own, so the first is the one
    that moves, once for each.
    """

    def _plan(
        tag: str,
        *,
        coordinate: float,
        start: float,
        end: float,
        flank: float,
        reach: float,
    ) -> ConvergencePlan:
        base = _planned_plan(tag, DemandAxis.X, Direction.R, flank)
        assert base.trunk_axis is not None
        return replace(
            base,
            trunk_axis=replace(
                base.trunk_axis,
                coordinate=coordinate,
                extent_start=start,
                extent_end=end,
                target_flank_coordinate=flank,
                source_endpoint_coordinate=start + reach,
                target_endpoint_coordinate=end + reach,
            ),
        )

    return (
        _plan("first", coordinate=50.0, start=0.0, end=300.0, flank=-30.0, reach=5.0),
        _plan("second", coordinate=20.0, start=-5.0, end=295.0, flank=130.0, reach=2.0),
        _plan("third", coordinate=20.0, start=-20.0, end=280.0, flank=130.0, reach=2.0),
    )


def test_a_resident_gives_way_to_a_lane_clear_of_the_flanks_around_it() -> None:
    """A give-way is a lane decision over the whole channel, not over one column.

    A resident asked to make room for an arrival is still standing among every
    other flank on that channel, so a lane chosen to clear the arrival alone can
    seat it on top of one of them.  The lane it takes clears them all.
    """
    settled = _settle_shared_trunk_channels(_crowded_from_both_sides(), CURVE_RADIUS)

    flanks = [
        (plan_rank, flank_rank, _trunk_segments(plan.trunk_axis)[flank_rank])
        for plan_rank, plan in enumerate(settled)
        if plan.trunk_axis is not None
        for flank_rank in (1, 3)
    ]
    for (left_plan, left_flank, left), (right_plan, right_flank, right) in combinations(
        flanks, 2
    ):
        if _trunk_run_travel_direction(
            settled[left_plan].trunk_axis, left_flank
        ) is _trunk_run_travel_direction(settled[right_plan].trunk_axis, right_flank):
            continue
        assert not _parallel_segments_conflict(left, right, LANE_CLEARANCE), (
            f"plan {left_plan} flank {left_flank} and plan {right_plan} flank "
            f"{right_flank} counter-run inside one clearance"
        )


def _landing_across_both_flanks() -> tuple[
    tuple[ConvergencePlan, ConvergencePlan], SimpleNamespace
]:
    """A landing whose cross run crowds both flanks of one short trunk.

    The trunk turns out of its channel on the same side at each end, so both its
    flanks travel one way and a single counter-running approach column stands
    within a radius of each.  Its extent spans less than two curve radii, which
    is what puts one column inside both.
    """
    landing_base = _planned_plan("landing", DemandAxis.X, Direction.R, -30.0)
    assert landing_base.trunk_axis is not None
    landing = replace(
        landing_base.landings[0],
        corner_handedness=TurnHandedness.CLOCKWISE,
        cross_run_start_coordinate=100.0,
        approach_axis=DemandAxis.X,
        approach_direction=Direction.R,
        join_point=(120.0, -20.0),
        minimum_runway=16.0,
    )
    landing_plan = replace(
        landing_base,
        trunk_axis=replace(
            landing_base.trunk_axis,
            coordinate=400.0,
            extent_start=300.0,
            extent_end=380.0,
        ),
        landings=(landing,),
        primary_trunk_member_id=EmissionMemberId("landing_outgoing"),
    )
    trunk_base = _planned_plan("trunk", DemandAxis.X, Direction.R, -30.0)
    assert trunk_base.trunk_axis is not None
    trunk_plan = replace(
        trunk_base,
        trunk_axis=replace(
            trunk_base.trunk_axis,
            coordinate=50.0,
            extent_start=100.0,
            extent_end=108.0,
            source_flank_coordinate=-30.0,
            target_flank_coordinate=130.0,
            source_endpoint_coordinate=102.0,
            target_endpoint_coordinate=110.0,
        ),
    )
    graph = SimpleNamespace(
        stations={"landing_source": SimpleNamespace(x=0.0, y=100.0)}
    )
    return (landing_plan, trunk_plan), graph


def test_a_landing_settles_against_each_flank_where_the_last_one_left_it() -> None:
    """A trunk states a flank at each end, and one landing can crowd both.

    Settling the first pair re-seats the landing, so the column the second pair
    is settled from is the one the landing now holds: reading the approach once
    per trunk would settle the second flank against a column nothing stands on
    and put the landing back beside the first.
    """
    plans, graph = _landing_across_both_flanks()

    settled = _settle_landing_trunk_flanks(plans, graph, CURVE_RADIUS)

    approach = _landing_cross_segment(settled[0].landings[0], graph)
    assert approach is not None
    axis = settled[1].trunk_axis
    assert axis is not None
    for flank_rank in (1, 3):
        flank = _trunk_segments(axis)[flank_rank]
        assert not _parallel_segments_conflict(approach, flank, CURVE_RADIUS), (
            f"the settled approach still crowds the flank at rank {flank_rank}"
        )


def _all_on_line(plan: ConvergencePlan, line_id: str) -> ConvergencePlan:
    """*plan* with every member of it carrying *line_id*."""

    def relined(edge: ResolvedEdge) -> ResolvedEdge:
        return ResolvedEdge(edge.source, edge.target, line_id)

    return replace(
        plan,
        resolved_member_paths=tuple(
            tuple(relined(edge) for edge in path) for path in plan.resolved_member_paths
        ),
        resolved_member_edges=tuple(
            relined(edge) for edge in plan.resolved_member_edges
        ),
        landings=tuple(
            replace(item, edge=relined(item.edge)) for item in plan.landings
        ),
        outgoing_continuations=tuple(
            replace(item, edge=relined(item.edge))
            for item in plan.outgoing_continuations
        ),
        endpoint_ownership=tuple(
            replace(item, edge=relined(item.edge)) for item in plan.endpoint_ownership
        ),
        line_ids=(line_id,),
        lane_order=(line_id,),
    )


def _crossing_approach(
    plan: ConvergencePlan,
    *,
    source_junction_id: str,
    source_row: float,
    direction: Direction,
    join_point: tuple[float, float],
    runway: float,
    opening_rows: tuple[float, float] | None = None,
) -> ConvergenceLanding:
    """*plan*'s landing restated as an approach crossing a column along X.

    The crossing stands on the column the runway reaches back to from the join.
    Its cross run begins on *source_row*, the row the feeder descends from.
    *opening_rows* states that column as an opening turn instead, spanning the
    two rows given, which is what makes the crossing follow a trunk flank.
    """
    opening_column = join_point[0] - direction.sign * runway
    return replace(
        plan.landings[0],
        source_junction_id=source_junction_id,
        approach_axis=DemandAxis.X,
        approach_direction=direction,
        join_point=join_point,
        corner_handedness=TurnHandedness.CLOCKWISE,
        cross_run_start_coordinate=source_row,
        minimum_runway=runway,
        opening_turn_coordinate=None if opening_rows is None else opening_column,
        opening_turn_segment=None
        if opening_rows is None
        else (
            (opening_column, opening_rows[0]),
            (opening_column, opening_rows[1]),
        ),
    )


def _landing_moved_by_a_neighbour() -> tuple[
    tuple[ConvergencePlan, ...], SimpleNamespace
]:
    """Three plans where laning one landing moves a second landing's column.

    The middle plan lands two feeders, one of each line.  Its trunk member's
    approach shares column 100 with the first plan's landing of the same line
    and counter-runs it, so laning the pair walks the trunk's source flank off
    that column.  Its other feeder opens its approach on that same flank, so it
    travels with it.  The third plan's landing counter-runs that follower and is
    laned after both.
    """
    obstacle_base = _planned_plan("obstacle", DemandAxis.Y, Direction.D, 10.0)
    obstacle = _all_on_line(
        replace(
            obstacle_base,
            landings=(
                _crossing_approach(
                    obstacle_base,
                    source_junction_id="obstacle_source",
                    source_row=60.0,
                    direction=Direction.R,
                    join_point=(120.0, 10.0),
                    runway=20.0,
                ),
            ),
        ),
        "b",
    )

    shared_base = _planned_plan("shared", DemandAxis.X, Direction.L, 10.0)
    assert shared_base.trunk_axis is not None
    trunk_member = EmissionMemberId("shared_trunk")
    trunk_edge = ResolvedEdge("shared_trunk_source", "shared_merge", "b")
    shared = replace(
        shared_base,
        member_ids=(*shared_base.member_ids, trunk_member),
        resolved_member_paths=(*shared_base.resolved_member_paths, (trunk_edge,)),
        resolved_member_edges=(*shared_base.resolved_member_edges, trunk_edge),
        line_ids=("a", "b"),
        lane_order=("a", "b"),
        primary_trunk_member_id=trunk_member,
        trunk_axis=replace(shared_base.trunk_axis, claimant_member_ids=(trunk_member,)),
        landings=(
            _crossing_approach(
                shared_base,
                source_junction_id="shared_source",
                source_row=200.0,
                direction=Direction.L,
                join_point=(60.0, 120.0),
                runway=40.0,
                opening_rows=(200.0, 120.0),
            ),
            replace(
                _crossing_approach(
                    shared_base,
                    source_junction_id="shared_trunk_source",
                    source_row=0.0,
                    direction=Direction.L,
                    join_point=(70.0, 50.0),
                    runway=30.0,
                ),
                member_id=trunk_member,
                edge=trunk_edge,
                lane_rank=1,
                order=1,
            ),
        ),
        endpoint_ownership=(
            *shared_base.endpoint_ownership,
            ConvergenceEndpointOwnership(
                member_id=trunk_member,
                edge=trunk_edge,
                connector_ids=shared_base.connector_ids,
                role=ConvergenceEndpointRole.TRUNK,
                endpoint=(70.0, 50.0),
            ),
        ),
    )

    arrival_base = _planned_plan("arrival", DemandAxis.Y, Direction.D, 10.0)
    arrival = replace(
        arrival_base,
        landings=(
            _crossing_approach(
                arrival_base,
                source_junction_id="arrival_source",
                source_row=100.0,
                direction=Direction.L,
                join_point=(60.0, 180.0),
                runway=32.0,
            ),
        ),
    )

    graph = SimpleNamespace(
        stations={
            "obstacle_source": SimpleNamespace(x=0.0, y=60.0),
            "shared_source": SimpleNamespace(x=0.0, y=200.0),
            "shared_trunk_source": SimpleNamespace(x=0.0, y=0.0),
            "arrival_source": SimpleNamespace(x=0.0, y=100.0),
        }
    )
    return (obstacle, shared, arrival), graph


def test_a_landing_lanes_against_the_column_a_resident_now_stands_on() -> None:
    """A resident of a channel is a seat on it, not the column it arrived on.

    Laning one landing walks its trunk's flank aside, and every landing of that
    plan opening on the flank travels with it.  A lane measured from the column
    such a landing held before that move clears nothing, because it seats the
    arrival on the column the resident actually stands on.
    """
    plans, graph = _landing_moved_by_a_neighbour()

    settled = _settle_opposing_landing_channels(plans, graph, (), CURVE_RADIUS)

    resident = _landing_cross_segment(settled[1].landings[0], graph)
    arrival = _landing_cross_segment(settled[2].landings[0], graph)
    assert resident is not None and arrival is not None
    assert not _parallel_segments_conflict(resident, arrival, LANE_CLEARANCE), (
        f"the arrival took column {arrival[0][0]} against a resident standing on "
        f"column {resident[0][0]}"
    )
