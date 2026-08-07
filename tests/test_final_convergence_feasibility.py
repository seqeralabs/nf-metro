"""Final convergence settlement cannot publish infeasible planned geometry."""

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import nf_metro.layout.routing.convergences as convergences
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.geometry import cotravelling_lane_clearance, spans_share_corridor
from nf_metro.layout.route_plan import RouteSystemId
from nf_metro.layout.routing.convergences import (
    ConvergencePlanExecution,
    FinalConvergenceFeasibilityError,
    settle_global_convergence_execution,
)
from nf_metro.layout.routing.core import observe_route_edges
from nf_metro.layout.routing.member_geometry import empty_member_geometry_execution
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser.route_topology import ResolvedEdge

ROOT = Path(__file__).parents[1]


def test_same_source_channels_from_distinct_systems_never_share_a_carrier() -> None:
    first = convergences._PlanGapChannel(
        None,
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"line"}),
        frozenset({"first-member"}),
        frozenset({"source"}),
        frozenset({"connector"}),
        RouteSystemId("first-system"),
        frozenset({"source"}),
        False,
    )
    second = replace(
        first,
        claimant_member_ids=frozenset({"second-member"}),
        system_id=RouteSystemId("second-system"),
    )

    assert not convergences._channels_share_source_carrier(first, second)


def test_disjoint_same_system_exit_channels_do_not_share_a_carrier() -> None:
    first = convergences._PlanGapChannel(
        None,
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"line"}),
        frozenset({"first-member"}),
        frozenset({"first-exit"}),
        frozenset({"first-connector"}),
        RouteSystemId("system"),
        frozenset({"first-exit"}),
        False,
    )
    second = replace(
        first,
        claimant_member_ids=frozenset({"second-member"}),
        source_junction_ids=frozenset({"second-exit"}),
        connector_ids=frozenset({"second-connector"}),
        continuation_endpoint_ids=frozenset({"second-target"}),
    )

    assert not convergences._channels_share_source_carrier(first, second)


def test_same_direction_distinct_lines_require_the_full_lane_step() -> None:
    first = convergences._PlanGapChannel(
        None,
        100.0,
        0.0,
        100.0,
        False,
        (0, 0),
        frozenset({"first"}),
        frozenset({"first-member"}),
        frozenset({"source"}),
        frozenset({"first-connector"}),
        RouteSystemId("system"),
        frozenset({"target"}),
        False,
    )
    fused = replace(
        first,
        coordinate=first.coordinate + OFFSET_STEP - COORD_TOLERANCE,
        line_ids=frozenset({"second"}),
        claimant_member_ids=frozenset({"second-member"}),
        connector_ids=frozenset({"second-connector"}),
    )

    assert convergences._gap_channels_crowd(first, fused)
    assert not convergences._gap_channels_crowd(
        first, replace(fused, coordinate=first.coordinate + OFFSET_STEP)
    )


@pytest.mark.parametrize(
    "field",
    ("claimant_member_ids", "source_junction_ids", "connector_ids"),
)
def test_plan_gap_channel_rejects_missing_carrier_provenance(field: str) -> None:
    values = {
        "claimant_member_ids": frozenset({"member"}),
        "source_junction_ids": frozenset({"source"}),
        "connector_ids": frozenset({"connector"}),
    }
    values[field] = frozenset()

    with pytest.raises(ValueError, match="complete carrier provenance"):
        convergences._PlanGapChannel(
            None,
            100.0,
            0.0,
            100.0,
            True,
            (0, 0),
            frozenset({"line"}),
            values["claimant_member_ids"],
            values["source_junction_ids"],
            values["connector_ids"],
            RouteSystemId("system"),
            frozenset({"source"}),
            False,
        )


@pytest.mark.parametrize(
    "path",
    (
        ROOT / "tests" / "fixtures" / "ambiguous_exit_continuation.mmd",
        ROOT / "examples" / "topologies" / "merge_bottom_row_bypass.mmd",
        ROOT / "examples" / "topologies" / "merge_feeder_shared_channel_gap.mmd",
    ),
)
def test_distinct_source_convergences_keep_opposing_same_line_channels(path) -> None:
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    lookup = convergences.gap_lookup_geometry(graph)
    channels = tuple(
        (plan_rank, channel)
        for plan_rank, plan in enumerate(observation.plan.convergence_plans)
        for channel in convergences._plan_gap_channels(plan, graph, lookup)
    )
    opposing = tuple(
        (first, second)
        for rank, (first_plan_rank, first) in enumerate(channels)
        for second_plan_rank, second in channels[rank + 1 :]
        if first_plan_rank != second_plan_rank
        if first.line_ids & second.line_ids
        and first.source_junction_ids.isdisjoint(second.source_junction_ids)
        and first.down is not second.down
        and first.gap == second.gap
        and spans_share_corridor(first.y_lo, first.y_hi, second.y_lo, second.y_hi)
    )
    clearance = cotravelling_lane_clearance(
        same_line=True, counter_running=True, curve_radius=CURVE_RADIUS
    )

    assert opposing
    assert all(
        abs(first.coordinate - second.coordinate) >= clearance - COORD_TOLERANCE
        for first, second in opposing
    )


def test_shared_source_convergences_fuse_one_opening_carrier() -> None:
    path = ROOT / "examples" / "topologies" / "merge_around_below_leftmost.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    lookup = convergences.gap_lookup_geometry(graph)
    channels = tuple(
        (rank, channel)
        for rank, plan in enumerate(observation.plan.convergence_plans)
        for channel in convergences._plan_gap_channels(plan, graph, lookup)
        if channel.gap == (1, 0)
        and channel.source_junction_ids == frozenset({"__junction_4"})
    )

    assert {rank for rank, _channel in channels} == {0, 1}
    assert len({channel.coordinate for _rank, channel in channels}) == 1


def test_coupled_flank_moves_do_not_leave_order_dependent_resident_channels(
    monkeypatch,
) -> None:
    @dataclass(frozen=True)
    class Plan:
        id: str
        trunk_axis: object
        coordinate: float
        line_id: str
        tag: str

    axis = SimpleNamespace(axis=convergences.DemandAxis.X)

    def channels(plan, graph, lookup):
        return (
            convergences._PlanGapChannel(
                1,
                plan.coordinate,
                0.0,
                100.0,
                True,
                (0, 0),
                frozenset({plan.line_id}),
                frozenset({plan.tag}),
                frozenset({plan.line_id}),
                frozenset({f"{plan.line_id}-connector"}),
                RouteSystemId("system"),
                frozenset({plan.line_id}),
                False,
            ),
        )

    fixed = convergences._PlanGapChannel(
        None,
        100.0,
        0.0,
        100.0,
        False,
        (0, 0),
        frozenset({"fixed"}),
        frozenset({"fixed"}),
        frozenset({"fixed-source"}),
        frozenset({"fixed-connector"}),
        RouteSystemId("system"),
        frozenset({"fixed-source"}),
        True,
    )
    monkeypatch.setattr(convergences, "gap_lookup_geometry", lambda graph: None)
    monkeypatch.setattr(convergences, "_plan_gap_channels", channels)
    monkeypatch.setattr(
        convergences,
        "_gap_channels_crowd",
        lambda channel, obstacle: bool(
            channel.claimant_member_ids & {"trigger", "third"}
        ),
    )
    monkeypatch.setattr(
        convergences,
        "_move_trunk_flank",
        lambda plan, flank_rank, coordinate: replace(plan, coordinate=coordinate),
    )
    monkeypatch.setattr(
        convergences, "_landing_trunk_flank_conflict", lambda *args: None
    )

    def lane_coordinate(plan, flank_rank, coordinate, obstacles, *args, **kwargs):
        if plan.tag == "trigger":
            return 20.0
        return sum({obstacle.coordinate for obstacle in obstacles})

    monkeypatch.setattr(convergences, "_flank_lane_coordinate", lane_coordinate)

    def settle(first_coordinate: float) -> float:
        plans = (
            Plan("first", axis, first_coordinate, "shared", "first"),
            Plan("trigger", axis, first_coordinate, "shared", "trigger"),
            Plan("third", axis, 50.0, "third", "third"),
        )
        settled = convergences._settle_opposing_gap_flanks(
            plans,
            SimpleNamespace(),
            curve_radius=8.0,
            fixed_channels=(fixed,),
        )
        return settled[2].coordinate

    assert settle(3.0) == settle(7.0) == 120.0


def test_chained_source_exemption_does_not_hide_target_flank_collision(
    monkeypatch,
) -> None:
    system_id = RouteSystemId("system")
    landing = SimpleNamespace(
        edge=ResolvedEdge("source", "target", "line"),
        source_junction_id="source-junction",
    )
    landing_plan = SimpleNamespace(
        id="landing-plan",
        system_id=system_id,
        trunk_axis=None,
        landings=(landing,),
    )
    trunk_axis = convergences.ConvergenceTrunkAxis(
        convergences.DemandAxis.X,
        0.0,
        0.0,
        100.0,
        convergences.Direction.R,
        -10.0,
        10.0,
    )
    trunk_plan = SimpleNamespace(
        id="trunk-plan",
        system_id=system_id,
        trunk_axis=trunk_axis,
        line_ids=("line",),
        landings=(SimpleNamespace(source_junction_id="source-junction"),),
        primary_trunk_member_id=None,
        endpoint_ownership=(),
    )
    target_flank = convergences._trunk_segments(trunk_axis)[3]
    monkeypatch.setattr(
        convergences,
        "_landing_cross_segment",
        lambda candidate, graph: (
            (target_flank[1], target_flank[0]) if candidate is landing else None
        ),
    )

    conflict = convergences._landing_trunk_flank_conflict(
        (landing_plan, trunk_plan), SimpleNamespace(), curve_radius=8.0
    )

    assert conflict is not None


def test_fan_in_coupled_convergence_flanks_move_as_one_allocation() -> None:
    path = ROOT / "examples" / "topologies" / "fan_in_merge.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    member = next(
        plan
        for plan in observation.plan.member_geometry_plans
        if plan.edge == ResolvedEdge("__junction_6", "sink__entry_left_5", "aux")
    )
    member_channel = next(
        channel
        for channel in member.gap_channels
        if (channel.gap_lo_col, channel.row) == (0, 0)
    )
    lookup = convergences.gap_lookup_geometry(graph)
    convergence_columns = {
        channel.coordinate
        for plan in observation.plan.convergence_plans
        for channel in convergences._plan_gap_channels(plan, graph, lookup)
        if channel.gap == (0, 0) and channel.line_ids == frozenset({"main"})
    }

    assert member_channel.start[0] == member_channel.end[0] == 210.0
    assert convergence_columns == {222.0}
    assert (
        min(abs(column - member_channel.start[0]) for column in convergence_columns)
        == 12.0
    )
    assert all(plan.owns_geometry for plan in observation.plan.convergence_plans)


def test_genomic_pipeline_mirrored_bundles_keep_exact_line_channels() -> None:
    path = ROOT / "examples" / "genomic_pipeline.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    lookup = convergences.gap_lookup_geometry(graph)
    convergence_channels = tuple(
        (plan, channel)
        for plan in observation.plan.convergence_plans
        for channel in convergences._plan_gap_channels(plan, graph, lookup)
        if channel.gap == (0, 1)
    )
    member_channels = tuple(
        (plan, channel)
        for plan in observation.plan.member_geometry_plans
        for channel in plan.gap_channels
        if (channel.gap_lo_col, channel.row) == (0, 1)
    )

    assert convergence_channels
    assert all(plan.owns_geometry for plan, _channel in convergence_channels)
    for line_id in ("germline", "tumor_only", "somatic"):
        convergence_columns = {
            channel.coordinate
            for _plan, channel in convergence_channels
            if channel.line_ids == frozenset({line_id})
        }
        member_columns = {
            channel.start[0]
            for plan, channel in member_channels
            if plan.edge.line_id == line_id
        }
        assert convergence_columns == member_columns


def test_organellar_joint_allocation_freezes_member_before_emission() -> None:
    path = ROOT / "tests" / "fixtures" / "genomeassembly_organellar.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    member = next(
        plan
        for plan in observation.plan.member_geometry_plans
        if plan.edge
        == ResolvedEdge("__junction_9", "scaffolding__entry_left_5", "hic_reads")
    )
    (member_channel,) = tuple(
        channel for channel in member.gap_channels if channel.gap_lo_col == 0
    )
    route = next(
        route
        for route in observation.routes
        if ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        == member.edge
    )
    lookup = convergences.gap_lookup_geometry(graph)
    convergence_columns = {
        channel.coordinate
        for plan in observation.plan.convergence_plans
        for channel in convergences._plan_gap_channels(plan, graph, lookup)
        if channel.gap == (0, 0)
        and channel.line_ids == frozenset({"assemblies"})
        and convergences.spans_share_corridor(
            channel.y_lo,
            channel.y_hi,
            min(member_channel.start[1], member_channel.end[1]),
            max(member_channel.start[1], member_channel.end[1]),
        )
    }

    assert member_channel.start[0] == member_channel.end[0] == 246.0
    assert convergence_columns == {258.0}
    assert tuple(route.points) == member.points
    assert tuple(
        route.points[member_channel.segment_rank : member_channel.segment_rank + 2]
    ) == (member_channel.start, member_channel.end)


def test_starved_final_settlement_does_not_publish_crowded_plan(monkeypatch) -> None:
    path = ROOT / "examples" / "topologies" / "fan_in_merge.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    plan = observation.plan.convergence_plans[0]
    system_id = plan.system_id
    edge_order = tuple(member.edge for member in observation.plan.members)
    query = convergences._query((plan,), edge_order)
    execution = ConvergencePlanExecution((plan,), (), (), (), query)
    convergence_channel = convergences._PlanGapChannel(
        1,
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"convergence"}),
        frozenset({"convergence-member"}),
        frozenset({"convergence-source"}),
        frozenset({"convergence-connector"}),
        system_id,
        frozenset({"convergence-source"}),
        False,
    )
    member_channel = convergences._PlanGapChannel(
        None,
        102.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"member"}),
        frozenset({"member"}),
        frozenset({"member-source"}),
        frozenset({"member-connector"}),
        system_id,
        frozenset({"member-source"}),
        True,
    )

    monkeypatch.setattr(
        convergences,
        "_planned_member_gap_channels",
        lambda plans, member_geometry: (member_channel,),
    )
    monkeypatch.setattr(
        convergences,
        "_plan_gap_channels",
        lambda candidate, graph, lookup: (convergence_channel,),
    )
    monkeypatch.setattr(convergences, "gap_lookup_geometry", lambda graph: None)
    monkeypatch.setattr(convergences, "_system_conflict", lambda plans, ctx: None)
    for name in (
        "_settle_shared_trunk_channels",
        "_settle_shared_opening_pivots",
        "_settle_landing_trunk_flanks",
        "_settle_same_line_gap_flanks",
    ):
        monkeypatch.setattr(convergences, name, lambda plans, *args, **kwargs: plans)
    monkeypatch.setattr(
        convergences,
        "_settle_opposing_landing_channels",
        lambda plans, *args, **kwargs: plans,
    )
    monkeypatch.setattr(
        convergences,
        "_settle_opposing_gap_flanks",
        lambda plans, *args, **kwargs: plans,
    )

    with pytest.raises(
        FinalConvergenceFeasibilityError,
        match="crowds a planned member channel",
    ):
        settle_global_convergence_execution(
            execution,
            graph,
            SimpleNamespace(curve_radius=8.0),
            exit_turn_plans=(),
            member_geometry=empty_member_geometry_execution(),
            planned_system_ids=frozenset({system_id}),
            include_resources=False,
        )
