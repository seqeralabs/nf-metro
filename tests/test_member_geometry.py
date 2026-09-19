"""Non-convergence member geometry is planned once and emitted exactly."""

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import nf_metro.layout.routing.member_geometry as member_geometry
from nf_metro.api import RenderConfig, prepare_graph, render_graph, resolve_theme
from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN, OFFSET_STEP
from nf_metro.layout.route_plan import (
    BindingKind,
    ConvergenceEndpointRole,
    EmissionMemberId,
    RouteMemberGapChannel,
    RouteMemberGeometryPlan,
    RouteMemberGeometryPlanId,
    RouteSystemId,
    build_route_plan_query,
    build_route_semantic_scaffold,
    serialize_route_plan,
)
from nf_metro.layout.route_reservations import ColumnGapRegion, CorridorOrientation
from nf_metro.layout.routing.common import Direction, GapSlot, OffsetRegime, RoutedPath
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.core import _route_edges, observe_route_edges
from nf_metro.layout.routing.corners import (
    _corner_travel_units,
    concentric_corner_radius_at,
)
from nf_metro.layout.routing.corridor_cohort_integration import (
    CorridorCohortCompilationError,
    CorridorCohortFailure,
    CorridorCohortLedger,
    CorridorCohortLedgerClaim,
    CorridorCohortObstacleProvenance,
    CorridorCohortTarget,
)
from nf_metro.layout.routing.corridor_cohorts import (
    CorridorAllocationFailureReason,
    CorridorClearanceShortfall,
)
from nf_metro.layout.routing.families import RouteFamilyId
from nf_metro.layout.routing.normalize import _rederive_semantic_end_corners, _VChannel
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.routing.planning import _allocation_eligible_system_ids
from nf_metro.layout.routing.reserved_bands import (
    ReservedBand,
    ReservedCorridors,
    build_reserved_corridors,
)
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceRequirement,
    BoundaryClearanceRequirementKind,
    SettlementAxis,
)
from nf_metro.parser.model import Edge, MetroGraph, Section
from nf_metro.parser.route_topology import ConnectorId, ResolvedEdge

ROOT = Path(__file__).parents[1]

OWNED_CORNER_RENDER_FIXTURES = (
    "examples/topologies/convergent_offrow_exit_climb.mmd",
    "examples/topologies/same_line_fan_distinct_descent.mmd",
    "examples/genomic_pipeline.mmd",
    "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd",
    "examples/topologies/plan_owned_distinct_lane_separation.mmd",
)


@pytest.mark.parametrize(
    ("claim_system", "claim_sources"),
    (
        (RouteSystemId("other-system"), frozenset({"source"})),
        (RouteSystemId("other-system"), frozenset()),
        (RouteSystemId("system"), frozenset({"other-source"})),
    ),
)
def test_preliminary_claim_requires_same_system_source_carrier(
    claim_system, claim_sources
) -> None:
    item = SimpleNamespace(
        candidate=SimpleNamespace(
            system_id=RouteSystemId("system"),
            route=SimpleNamespace(edge=Edge("source", "target", "line")),
            connector_ids=(),
        )
    )
    claim = member_geometry.PreliminaryGapChannelClaim(
        claim_system,
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"line"}),
        claim_sources,
    )
    assert not member_geometry._claim_source_compatible(item, claim)


def test_exit_port_does_not_bridge_disjoint_same_system_carriers() -> None:
    item = SimpleNamespace(
        candidate=SimpleNamespace(
            system_id=RouteSystemId("system"),
            route=SimpleNamespace(edge=Edge("exit", "target", "line"), line_id="line"),
            connector_ids=("member-connector",),
        )
    )
    claim = member_geometry.PreliminaryGapChannelClaim(
        RouteSystemId("system"),
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"line"}),
        frozenset({"other-source"}),
        frozenset({"claim-connector"}),
    )
    assert not member_geometry._claim_source_compatible(item, claim)


def test_connector_identity_can_extend_a_same_system_carrier() -> None:
    item = SimpleNamespace(
        candidate=SimpleNamespace(
            system_id=RouteSystemId("system"),
            route=SimpleNamespace(edge=Edge("member-source", "target", "line")),
            connector_ids=("shared-connector",),
        )
    )
    claim = member_geometry.PreliminaryGapChannelClaim(
        RouteSystemId("system"),
        100.0,
        0.0,
        100.0,
        True,
        (0, 0),
        frozenset({"line"}),
        frozenset({"claim-source"}),
        frozenset({"shared-connector"}),
    )

    assert member_geometry._claim_source_compatible(item, claim)


def _materialized_test_channel(
    name: str,
    carrier_id: str,
    y_lo: float,
    y_hi: float,
) -> member_geometry._MaterializedChannel:
    route = RoutedPath(
        Edge(name, f"{name}-target", name),
        name,
        [(0.0, y_lo), (10.0, y_lo), (10.0, y_hi), (20.0, y_hi)],
        is_inter_section=True,
    )
    candidate = member_geometry._MemberCandidate(
        route,
        RouteFamilyId.STANDARD_L_SHAPE,
        RouteSystemId("system"),
        carrier_id,
        (f"connector-{name}",),
    )
    slot = GapSlot(0, 1, 0, Direction.D, 0, 1)
    channel = _VChannel(route, 1, 10.0, y_lo, y_hi, True)
    return member_geometry._MaterializedChannel(candidate, channel, slot)


def test_channel_bundles_do_not_join_transitive_independent_carriers() -> None:
    channels = (
        _materialized_test_channel("a", "carrier-a", 0.0, 50.0),
        _materialized_test_channel("b", "carrier-b", 40.0, 90.0),
        _materialized_test_channel("c", "carrier-c", 80.0, 130.0),
    )

    bundles = member_geometry._channel_bundles(channels)

    assert tuple(len(bundle) for bundle in bundles) == (1, 1, 1)


def test_channel_bundles_keep_one_semantic_carrier_atomic() -> None:
    channels = (
        _materialized_test_channel("a", "shared-carrier", 0.0, 50.0),
        _materialized_test_channel("b", "shared-carrier", 0.0, 50.0),
        _materialized_test_channel("c", "shared-carrier", 0.0, 50.0),
    )

    bundles = member_geometry._channel_bundles(channels)

    assert len(bundles) == 1
    assert len(bundles[0]) == 3


def _observe(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observation = observe_route_edges(
        graph, station_offsets=compute_station_offsets(graph)
    )
    return graph, observation


def _route_for_plan(observation, plan):
    return next(
        route
        for route in observation.routes
        if ResolvedEdge(route.edge.source, route.edge.target, route.line_id)
        == plan.edge
    )


def _assert_channels_equal_emission(observation, plan) -> None:
    route = _route_for_plan(observation, plan)
    assert route.route_system_disposition == "planned"
    assert str(plan.id) in route.route_plan_ids
    assert route.route_system_owned_segment_ranks == tuple(
        dict.fromkeys(channel.segment_rank for channel in plan.gap_channels)
    )
    assert tuple(route.gap_slots) == plan.gap_slots
    for channel in plan.gap_channels:
        assert tuple(route.points[channel.segment_rank : channel.segment_rank + 2]) == (
            channel.start,
            channel.end,
        )


def test_live_claim_index_exposes_only_eligible_prior_systems_in_order() -> None:
    failed = RouteSystemId("failed")
    survivor = RouteSystemId("survivor")
    future = RouteSystemId("future")
    claims = tuple(
        member_geometry.PreliminaryGapChannelClaim(
            system_id,
            coordinate,
            0.0,
            100.0,
            True,
            (0, 0),
            frozenset({line_id}),
        )
        for system_id, coordinate, line_id in (
            (failed, 100.0, "failed-line"),
            (survivor, 112.0, "survivor-line"),
            (future, 124.0, "future-line"),
        )
    )
    failures = MappingProxyType({failed: "canonical-template-declined-member"})
    eligible_claims = member_geometry._eligible_preliminary_gap_claims(claims, failures)
    visible = member_geometry._visible_claims_by_system_gap(
        eligible_claims,
        {failed: 0, survivor: 1, future: 2},
        ((0, 1),),
    )

    assert tuple(claim.system_id for claim in visible[(survivor, (0, 0))]) == (
        survivor,
    )
    assert tuple(claim.system_id for claim in visible[(future, (0, 0))]) == (
        survivor,
        future,
    )
    assert tuple(claim.system_id for claim in visible[(future, (0, 1))]) == (
        survivor,
        future,
    )
    assert _allocation_eligible_system_ids(
        frozenset({failed, survivor, future}), frozenset(failures)
    ) == frozenset({survivor, future})


def test_member_planning_has_no_compatibility_context() -> None:
    path = ROOT / "examples" / "topologies" / "aligner_row_pinned_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    ctx = _build_routing_context(
        graph,
        DIAGONAL_RUN,
        CURVE_RADIUS,
        compute_station_offsets(graph),
    )
    assert not hasattr(ctx, "compatibility_edges")
    assert not hasattr(member_geometry, "_append_compatibility_context")


def test_exit_turn_channel_is_a_published_member_geometry_decision() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "exit_run_three_drop_columns.mmd"
    )
    plan = next(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge == ResolvedEdge("__junction_9", "e__entry_left_5", "main")
    )

    assert len(plan.gap_channels) == 1
    assert plan.exit_turn_axis_id is not None
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )
    assert plan.id in system.member_geometry_plan_ids
    _assert_channels_equal_emission(observation, plan)


def test_multi_gap_wrap_channels_are_planned_without_exit_or_fan_ownership() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    plans = tuple(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge.source == "__junction_7"
        and item.edge.target == "Output__entry_left_5"
    )

    assert len(plans) == 7
    assert all(len(plan.gap_channels) == 2 for plan in plans)
    assert all(plan.exit_turn_axis_id is None for plan in plans)
    assert all(plan.fan_plan_id is None for plan in plans)
    for channel_rank in range(2):
        coordinates = [plan.gap_channels[channel_rank].start[0] for plan in plans]
        deltas = [
            following - preceding
            for preceding, following in zip(coordinates, coordinates[1:])
        ]
        assert len({delta > 0.0 for delta in deltas}) == 1
        assert all(abs(delta) == OFFSET_STEP for delta in deltas)
    for plan in plans:
        _assert_channels_equal_emission(observation, plan)


def test_same_line_fanout_opening_is_coincident_before_member_freeze() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "divergent_fanout_split.mmd"
    )
    plans = tuple(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge.source == "__junction_3"
    )

    assert len(plans) == 2
    assert len({plan.gap_channels[0].start[0] for plan in plans}) == 1
    for plan in plans:
        _assert_channels_equal_emission(observation, plan)


def _arc_centre(
    points: list[tuple[float, float]], corner: int, radius: float
) -> tuple[float, float]:
    """The centre of the arc rounding ``points[corner]`` at *radius*."""
    turn_in, turn_out = _corner_travel_units(*points[corner - 1 : corner + 2])
    return (
        points[corner][0] + radius * (turn_out[0] - turn_in[0]),
        points[corner][1] + radius * (turn_out[1] - turn_in[1]),
    )


def test_seating_a_claimed_bundle_carries_its_concentric_fan() -> None:
    """Two lanes seated together keep the arc centre their fan shares.

    Both lanes travel one displacement into their claimed bands, so each keeps
    the corner radii its own displacement from the bundle reference gave it.
    Re-deriving them at the base radius instead leaves the two lanes turning on
    separate centres, which is the bundle drawn with its fan flattened.
    """
    lanes = {
        "wide": RoutedPath(
            Edge("src", "tgt", "wide"),
            "wide",
            [(0.0, -4.0), (104.0, -4.0), (104.0, 196.0), (304.0, 196.0)],
            curve_radii=[CURVE_RADIUS + OFFSET_STEP, CURVE_RADIUS - OFFSET_STEP],
        ),
        "ref": RoutedPath(
            Edge("src", "tgt", "ref"),
            "ref",
            [(0.0, 0.0), (100.0, 0.0), (100.0, 200.0), (300.0, 200.0)],
            curve_radii=[CURVE_RADIUS, CURVE_RADIUS],
        ),
    }
    centres_before = {
        name: tuple(
            _arc_centre(route.points, corner, route.curve_radii[corner - 1])
            for corner in (1, 2)
        )
        for name, route in lanes.items()
    }
    assert centres_before["wide"] == centres_before["ref"]

    candidates = tuple(
        SimpleNamespace(
            route=route,
            system_id=RouteSystemId("system"),
            carrier_id="carrier",
        )
        for route in lanes.values()
    )
    ctx = SimpleNamespace(
        reserved_bands=ReservedCorridors(
            per_claim={
                ("src", "tgt", "wide", 1): ReservedBand(116.0, 200.0),
                ("src", "tgt", "ref", 1): ReservedBand(112.0, 200.0),
            }
        )
    )

    member_geometry._seat_claimed_segments_before_freeze(candidates, ctx)

    assert lanes["ref"].points[1:3] == [(112.0, 0.0), (112.0, 200.0)]
    assert lanes["wide"].points[1:3] == [(116.0, -4.0), (116.0, 196.0)]
    centres_after = {
        name: tuple(
            _arc_centre(route.points, corner, route.curve_radii[corner - 1])
            for corner in (1, 2)
        )
        for name, route in lanes.items()
    }
    assert centres_after["wide"] == centres_after["ref"]
    assert lanes["wide"].curve_radii == [
        CURVE_RADIUS + OFFSET_STEP,
        CURVE_RADIUS - OFFSET_STEP,
    ]


def test_seating_a_member_channel_preserves_both_concentric_inputs() -> None:
    points = [(0.0, 0.0), (50.0, 0.0), (50.0, 100.0), (150.0, 100.0)]
    offsets = (OFFSET_STEP, OFFSET_STEP)
    bases = (CURVE_RADIUS, CURVE_RADIUS + 2.0)
    route = RoutedPath(
        Edge("source", "target", "line"),
        "line",
        points,
        curve_radii=[
            concentric_corner_radius_at(
                *points[radius_index : radius_index + 3],
                offsets[radius_index],
                bases[radius_index],
            )
            for radius_index in range(2)
        ],
        concentric_corner_offsets_by_segment={1: offsets},
        concentric_corner_bases_by_segment={1: bases},
    )
    channel = _VChannel(route, 1, 50.0, 0.0, 100.0, True)

    member_geometry._seat_channel(channel, 60.0)

    assert route.points[1:3] == [(60.0, 0.0), (60.0, 100.0)]
    assert route.concentric_corner_offsets_by_segment[1] == offsets
    assert route.concentric_corner_bases_by_segment[1] == bases
    assert route.curve_radii == [
        concentric_corner_radius_at(
            *route.points[radius_index : radius_index + 3],
            offsets[radius_index],
            bases[radius_index],
        )
        for radius_index in range(2)
    ]


@pytest.mark.parametrize("radius_index", (0, 1), ids=("incoming", "outgoing"))
def test_member_geometry_validator_rejects_changed_flanking_radius(
    radius_index: int,
) -> None:
    points = ((0.0, 0.0), (50.0, 0.0), (50.0, 100.0), (150.0, 100.0))
    offsets = (OFFSET_STEP, OFFSET_STEP)
    bases = (CURVE_RADIUS, CURVE_RADIUS + 2.0)
    radii = tuple(
        concentric_corner_radius_at(
            *points[index : index + 3], offsets[index], bases[index]
        )
        for index in range(2)
    )
    channel = RouteMemberGapChannel(1, points[1], points[2], 0, 0, Direction.D)
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        points,
        radii,
        OffsetRegime.BAKED,
        False,
        (),
        None,
        (channel,),
        ((1, offsets),),
        ((1, bases),),
    )
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    route.route_system_disposition = "planned"
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )
    assert route.curve_radii is not None
    route.curve_radii[radius_index] += 1.0

    with pytest.raises(RuntimeError, match="differs from its concentric radius"):
        member_geometry.validate_member_geometry_emission([route], execution)


def test_member_geometry_validator_accepts_boundary_channel_radius() -> None:
    points = ((0.0, 0.0), (0.0, 100.0), (50.0, 100.0))
    radius = concentric_corner_radius_at(*points, OFFSET_STEP, base_radius=CURVE_RADIUS)
    channel = RouteMemberGapChannel(0, points[0], points[1], 0, 0, Direction.D)
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        points,
        (radius,),
        OffsetRegime.BAKED,
        False,
        (),
        None,
        (channel,),
        ((0, (None, OFFSET_STEP)),),
        ((0, (None, CURVE_RADIUS)),),
    )
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )

    member_geometry.validate_member_geometry_emission([route], execution)


@pytest.mark.parametrize(
    "missing",
    ("offsets", "bases", "offset", "base"),
)
def test_member_geometry_validator_rejects_missing_corner_inputs(
    missing: str,
) -> None:
    points = ((0.0, 0.0), (50.0, 0.0), (50.0, 100.0), (150.0, 100.0))
    channel = RouteMemberGapChannel(1, points[1], points[2], 0, 0, Direction.D)
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        points,
        (CURVE_RADIUS, CURVE_RADIUS),
        OffsetRegime.BAKED,
        False,
        (),
        None,
        (channel,),
        (
            ()
            if missing == "offsets"
            else ((1, (None if missing == "offset" else 0.0, 0.0)),)
        ),
        (
            ()
            if missing == "bases"
            else ((1, (None if missing == "base" else CURVE_RADIUS, CURVE_RADIUS)),)
        ),
    )
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )

    with pytest.raises(RuntimeError, match="has no concentric inputs"):
        member_geometry.validate_member_geometry_emission([route], execution)


def test_member_geometry_validator_attributes_missing_corner_points() -> None:
    points = (
        (0.0, 0.0),
        (50.0, 0.0),
        (50.0, 100.0),
        (150.0, 100.0),
    )
    channel = RouteMemberGapChannel(1, points[1], points[2], 0, 0, Direction.D)
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        points,
        (CURVE_RADIUS, CURVE_RADIUS),
        OffsetRegime.BAKED,
        False,
        (),
        None,
        (channel,),
        ((1, (0.0, 0.0)), (2, (0.0, None))),
        ((1, (CURVE_RADIUS, CURVE_RADIUS)), (2, (CURVE_RADIUS, None))),
    )
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    route.points = route.points[:3]
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )

    with pytest.raises(RuntimeError, match="has no complete corner points"):
        member_geometry.validate_member_geometry_emission([route], execution)


def test_member_geometry_validator_attributes_missing_flanking_radius() -> None:
    points = ((0.0, 0.0), (50.0, 0.0), (50.0, 100.0), (150.0, 100.0))
    channel = RouteMemberGapChannel(1, points[1], points[2], 0, 0, Direction.D)
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        points,
        (CURVE_RADIUS, CURVE_RADIUS),
        OffsetRegime.BAKED,
        False,
        (),
        None,
        (channel,),
        ((1, (0.0, 0.0)), (2, (0.0, None))),
        ((1, (CURVE_RADIUS, CURVE_RADIUS)), (2, (CURVE_RADIUS, None))),
    )
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    route.curve_radii = [CURVE_RADIUS]
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )

    with pytest.raises(RuntimeError, match="lost corner radius at index 1"):
        member_geometry.validate_member_geometry_emission([route], execution)


def test_member_plans_persist_exact_connector_ownership() -> None:
    graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    scaffold = build_route_semantic_scaffold(graph)

    assert observation.plan.member_geometry_plans
    for plan in observation.plan.member_geometry_plans:
        assert plan.connector_ids == scaffold.connector_ids_for_edge(plan.edge)

    payload = json.loads(serialize_route_plan(observation.plan))
    encoded = {
        item["id"]: tuple(item["connector_ids"])
        for item in payload["member_geometry_plans"]
    }
    assert encoded == {
        plan.id: plan.connector_ids for plan in observation.plan.member_geometry_plans
    }


def test_reportho_owned_lead_is_frozen_at_its_concentric_radius() -> None:
    path = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"
    graph, observation = _observe(path)
    plan = next(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge.source == "__junction_12" and item.edge.line_id == "main"
    )
    route = _route_for_plan(observation, plan)
    fresh = member_geometry.fresh_member_route(
        plan,
        Edge(plan.edge.source, plan.edge.target, plan.edge.line_id),
    )

    assert route.curve_radii is not None
    assert (
        tuple(route.curve_radii) == plan.curve_radii == tuple(fresh.curve_radii or ())
    )
    assert route.curve_radii[0] == 14.0
    assert route.concentric_corner_offsets_by_segment == dict(
        plan.concentric_corner_offsets_by_segment
    )
    assert route.concentric_corner_bases_by_segment == dict(
        plan.concentric_corner_bases_by_segment
    )
    before = (
        tuple(route.curve_radii),
        dict(route.concentric_corner_offsets_by_segment),
        dict(route.concentric_corner_bases_by_segment),
    )
    _rederive_semantic_end_corners(
        observation.routes,
        CURVE_RADIUS,
        compute_station_offsets(graph),
    )
    assert (
        tuple(route.curve_radii),
        route.concentric_corner_offsets_by_segment,
        route.concentric_corner_bases_by_segment,
    ) == before


def test_owned_coincident_terminal_corners_match_their_frozen_plans() -> None:
    _graph, observation = _observe(ROOT / "examples" / "genomic_pipeline.mmd")
    plans = [
        plan
        for plan in observation.plan.member_geometry_plans
        if plan.edge.source == "annotation__exit_right_3"
        and plan.edge.target == "reporting__entry_left_7"
    ]

    assert {plan.edge.line_id for plan in plans} == {
        "germline",
        "somatic",
        "tumor_only",
    }
    for plan in plans:
        route = _route_for_plan(observation, plan)
        assert tuple(route.curve_radii or ()) == plan.curve_radii
        assert route.concentric_corner_offsets_by_segment == dict(
            plan.concentric_corner_offsets_by_segment
        )
        assert route.concentric_corner_bases_by_segment == dict(
            plan.concentric_corner_bases_by_segment
        )


@pytest.mark.parametrize("relative_path", OWNED_CORNER_RENDER_FIXTURES)
def test_owned_corner_preview_regressions_match_plans_and_render(
    relative_path: str,
) -> None:
    path = ROOT / relative_path
    graph, observation = _observe(path)

    assert observation.plan.member_geometry_plans
    for plan in observation.plan.member_geometry_plans:
        route = _route_for_plan(observation, plan)
        assert (
            None if route.curve_radii is None else tuple(route.curve_radii)
        ) == plan.curve_radii
        assert route.concentric_corner_offsets_by_segment == dict(
            plan.concentric_corner_offsets_by_segment
        )
        assert route.concentric_corner_bases_by_segment == dict(
            plan.concentric_corner_bases_by_segment
        )

    assert (
        render_graph(
            graph,
            resolve_theme(None, graph),
            RenderConfig(chrome_css=False),
        ).find("<svg ")
        > 0
    )


def test_blocked_riser_members_publish_their_frozen_corner_templates() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "same_destination_vertical_convergence.mmd"
    )

    assert observation.plan.member_geometry_plans
    for plan in observation.plan.member_geometry_plans:
        route = _route_for_plan(observation, plan)
        assert (
            None if route.curve_radii is None else tuple(route.curve_radii)
        ) == plan.curve_radii
        assert route.concentric_corner_offsets_by_segment == dict(
            plan.concentric_corner_offsets_by_segment
        )
        assert route.concentric_corner_bases_by_segment == dict(
            plan.concentric_corner_bases_by_segment
        )

    blocker = next(
        plan
        for plan in observation.plan.member_geometry_plans
        if plan.edge.source == "__junction_12"
        and plan.edge.target == "s7__entry_right_9"
        and plan.edge.line_id == "lower"
    )
    assert blocker.family_id is member_geometry.RouteFamilyId.RIGHT_ENTRY_WRAP
    assert blocker.owns_complete_path
    assert blocker.gap_channels == ()


@pytest.mark.parametrize("corruption", ("radius", "map"))
def test_member_geometry_validator_rejects_owned_corner_plan_drift(
    corruption: str,
) -> None:
    path = ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"
    _graph, observation = _observe(path)
    plan = next(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge.source == "__junction_12" and item.edge.line_id == "main"
    )
    route = member_geometry.fresh_member_route(
        plan,
        Edge(plan.edge.source, plan.edge.target, plan.edge.line_id),
    )
    execution = member_geometry.MemberGeometryExecution(
        (plan,), MappingProxyType({}), MappingProxyType({plan.edge: plan})
    )
    if corruption == "radius":
        assert route.curve_radii is not None
        route.curve_radii[0] += 1.0
        bases = list(route.concentric_corner_bases_by_segment[1])
        assert bases[0] is not None
        bases[0] += 1.0
        route.concentric_corner_bases_by_segment[1] = tuple(bases)
        match = "owned corner radius changed"
    else:
        offsets = list(route.concentric_corner_offsets_by_segment[2])
        assert offsets[0] is not None
        offsets[0] += 1.0
        route.concentric_corner_offsets_by_segment[2] = tuple(offsets)
        match = "owned corner inputs changed"

    with pytest.raises(RuntimeError, match=match):
        member_geometry.validate_member_geometry_emission([route], execution)


def test_trunk_slot_settles_before_adjacent_gap_channels_freeze() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "fan_in_merge.mmd"
    )
    plan = next(
        item
        for item in observation.plan.member_geometry_plans
        if item.edge == ResolvedEdge("__junction_6", "sink__entry_left_5", "aux")
    )
    route = _route_for_plan(observation, plan)

    assert plan.owned_segment_ranks == (1, 3)
    assert plan.points[2:4] == ((216.0, 200.0), (639.0, 200.0))
    assert tuple(route.points[2:4]) == plan.points[2:4]
    _assert_channels_equal_emission(observation, plan)


def test_distinct_line_fan_traverses_bundle_before_member_freeze() -> None:
    """A fan's traverses nest in one corridor, and each slot names that gap.

    Freezing a descent hides the route from the passes keyed off an unowned
    opening descent, so a traverse nested after the freeze could never reach its
    bundle-mate's corridor.
    """
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "same_line_fan_distinct_descent.mmd"
    )
    plans = {
        item.edge.target: item
        for item in observation.plan.member_geometry_plans
        if item.edge.source == "__junction_5"
        and item.edge.target != "cont__entry_left_1"
    }

    green = plans["far__entry_top_2"]
    reds = (plans["near__entry_left_3"], plans["mid__entry_left_4"])
    for red in reds:
        assert red.points[2][1] == green.points[2][1] + OFFSET_STEP
        assert red.points[2][1] == red.points[3][1]
        assert red.points[2][0] > red.points[3][0]
        assert red.trunk_slot == green.trunk_slot
        _assert_channels_equal_emission(observation, red)


def test_one_segment_can_own_distinct_gap_row_claims() -> None:
    channels = (
        RouteMemberGapChannel(1, (20.0, 10.0), (20.0, 90.0), 0, 0, Direction.D),
        RouteMemberGapChannel(1, (20.0, 10.0), (20.0, 90.0), 0, 1, Direction.D),
    )
    plan = RouteMemberGeometryPlan(
        RouteMemberGeometryPlanId("plan"),
        RouteSystemId("system"),
        EmissionMemberId("member"),
        ResolvedEdge("source", "target", "line"),
        ("connector",),
        RouteFamilyId.BYPASS_FAMILY,
        ((0.0, 10.0), (20.0, 10.0), (20.0, 90.0)),
        None,
        OffsetRegime.BAKED,
        False,
        (),
        None,
        channels,
    )

    assert plan.gap_channels == channels
    with pytest.raises(ValueError, match="connector ownership is incomplete"):
        replace(plan, connector_ids=())
    with pytest.raises(ValueError, match="connector ownership is incomplete"):
        replace(plan, connector_ids=("connector", "connector"))
    route = member_geometry.fresh_member_route(plan, Edge("source", "target", "line"))
    assert plan.owned_segment_ranks == (1,)
    assert route.route_system_owned_segment_ranks == (1,)

    route.route_system_disposition = "planned"
    execution = member_geometry.MemberGeometryExecution(
        (plan,),
        MappingProxyType({}),
        MappingProxyType({plan.edge: plan}),
    )
    route.points[0] = (-10.0, 10.0)
    member_geometry.validate_member_geometry_emission([route], execution)
    route.points[2] = (20.0, 95.0)
    with pytest.raises(RuntimeError, match="channel geometry changed"):
        member_geometry.validate_member_geometry_emission([route], execution)

    with pytest.raises(ValueError, match="repeats a symbolic gap claim"):
        RouteMemberGeometryPlan(
            RouteMemberGeometryPlanId("duplicate"),
            RouteSystemId("system"),
            EmissionMemberId("member"),
            ResolvedEdge("source", "target", "line"),
            ("connector",),
            RouteFamilyId.BYPASS_FAMILY,
            ((0.0, 10.0), (20.0, 10.0), (20.0, 90.0)),
            None,
            OffsetRegime.BAKED,
            False,
            (),
            None,
            (channels[0], channels[0]),
        )
    with pytest.raises(ValueError, match="channel disagrees with its segment"):
        replace(
            plan,
            gap_channels=(
                replace(channels[0], start=(20.0, 12.0)),
                channels[1],
            ),
        )


def test_reservation_reroute_keeps_identity_and_reuses_settled_template() -> None:
    graph, first = _observe(ROOT / "examples" / "genomeassembly.mmd")
    routes, _moves, second_plan = _route_edges(
        graph,
        DIAGONAL_RUN,
        CURVE_RADIUS,
        compute_station_offsets(graph),
        observe_plan=True,
        reservations=first.plan,
    )
    assert second_plan is not None
    exit_axes = {
        str(axis.id): axis
        for exit_plan in second_plan.exit_turn_plans
        for axis in exit_plan.axes
    }
    for route in routes:
        if route.exit_turn_axis_id is None:
            continue
        assert route.exit_turn_segment_rank is not None
        axis = exit_axes[route.exit_turn_axis_id]
        start, end = route.points[
            route.exit_turn_segment_rank : route.exit_turn_segment_rank + 2
        ]
        assert start[axis.axis.point_index] == axis.coordinate
        assert end[axis.axis.point_index] == axis.coordinate

    first_by_id = {item.id: item for item in first.plan.member_geometry_plans}
    second_by_id = {item.id: item for item in second_plan.member_geometry_plans}
    shared = first_by_id.keys() & second_by_id.keys()

    assert shared
    corridors = build_reserved_corridors(graph, first.plan)
    for plan_id in shared:
        plan = second_by_id[plan_id]
        assert plan.consumed_reservation_ids == tuple(
            str(reservation.id)
            for reservation in first.plan.reservations
            if plan.member_id in reservation.claimant_member_ids
        )
        route = next(
            item
            for item in routes
            if ResolvedEdge(item.edge.source, item.edge.target, item.line_id)
            == plan.edge
        )
        for channel in plan.gap_channels:
            band = corridors.for_segment(
                plan.edge.source,
                plan.edge.target,
                plan.edge.line_id,
                channel.segment_rank,
            )
            if band is not None:
                assert band.lo <= channel.start[0] <= band.hi
            assert tuple(
                route.points[channel.segment_rank : channel.segment_rank + 2]
            ) == (channel.start, channel.end)


def test_failed_system_cannot_fall_back_from_member_geometry(
    monkeypatch,
) -> None:
    path = ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    original = member_geometry._route_template
    failed = False

    def fail_first(edge, family_id, ctx):
        nonlocal failed
        if not failed:
            failed = True
            raise member_geometry.MemberGeometryDeclinedError("fixture decline")
        return original(edge, family_id, ctx)

    monkeypatch.setattr(member_geometry, "_route_template", fail_first)
    with pytest.raises(
        RuntimeError, match="route-system planning declined canonical geometry"
    ):
        observe_route_edges(graph, station_offsets=compute_station_offsets(graph))

    assert failed


def _replace_member_geometry_record(route_plan, original, replacement):
    return replace(
        route_plan,
        member_geometry_plans=tuple(
            replacement if item.id == original.id else item
            for item in route_plan.member_geometry_plans
        ),
    )


def test_route_plan_query_rejects_member_geometry_connector_mismatch() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    original = observation.plan.member_geometry_plans[0]
    malformed = _replace_member_geometry_record(
        observation.plan,
        original,
        replace(original, connector_ids=(ConnectorId("wrong-connector"),)),
    )

    with pytest.raises(ValueError, match="identity disagrees with its member"):
        build_route_plan_query(malformed)


def test_route_plan_query_rejects_member_geometry_member_mismatch() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    original = observation.plan.member_geometry_plans[0]
    geometry_member_ids = {
        item.member_id for item in observation.plan.member_geometry_plans
    }
    replacement_member = next(
        member
        for member in observation.plan.members
        if member.system_id == original.system_id
        and member.id not in geometry_member_ids
    )
    malformed = _replace_member_geometry_record(
        observation.plan,
        original,
        replace(original, member_id=replacement_member.id),
    )

    with pytest.raises(ValueError, match="identity disagrees with its member"):
        build_route_plan_query(malformed)


def test_route_plan_query_rejects_duplicate_member_geometry_plan_ids() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    first, second = observation.plan.member_geometry_plans[:2]
    malformed = _replace_member_geometry_record(
        observation.plan,
        second,
        replace(second, id=first.id),
    )

    with pytest.raises(ValueError, match="duplicate member geometry plan ids"):
        build_route_plan_query(malformed)


def test_route_plan_query_rejects_member_geometry_system_index_mismatch() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    original = observation.plan.member_geometry_plans[0]
    systems = tuple(
        replace(
            system,
            member_geometry_plan_ids=tuple(
                item for item in system.member_geometry_plan_ids if item != original.id
            ),
        )
        if system.id == original.system_id
        else system
        for system in observation.plan.systems
    )

    with pytest.raises(ValueError, match="member-geometry index is inconsistent"):
        build_route_plan_query(replace(observation.plan, systems=systems))


def test_route_plan_query_rejects_duplicate_member_geometry_owner() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    original = observation.plan.member_geometry_plans[0]
    duplicate = replace(
        original,
        id=RouteMemberGeometryPlanId("duplicate-member-owner"),
    )
    systems = tuple(
        replace(
            system,
            member_geometry_plan_ids=(
                *system.member_geometry_plan_ids,
                duplicate.id,
            ),
        )
        if system.id == original.system_id
        else system
        for system in observation.plan.systems
    )
    malformed = replace(
        observation.plan,
        systems=systems,
        member_geometry_plans=(
            *observation.plan.member_geometry_plans,
            duplicate,
        ),
    )

    with pytest.raises(ValueError, match="more than one member geometry plan"):
        build_route_plan_query(malformed)


def test_route_plan_query_rejects_ownerless_planned_emitted_member() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "funcprofiler_upstream.mmd"
    )
    removed = observation.plan.member_geometry_plans[0]
    systems = tuple(
        replace(
            system,
            member_geometry_plan_ids=tuple(
                item for item in system.member_geometry_plan_ids if item != removed.id
            ),
        )
        if system.id == removed.system_id
        else system
        for system in observation.plan.systems
    )
    malformed = replace(
        observation.plan,
        systems=systems,
        member_geometry_plans=tuple(
            item
            for item in observation.plan.member_geometry_plans
            if item.id != removed.id
        ),
    )

    with pytest.raises(ValueError, match="geometry ownership is incomplete"):
        build_route_plan_query(malformed)


def test_covered_convergence_member_needs_no_emitted_geometry_owner() -> None:
    _graph, observation = _observe(
        ROOT / "examples" / "topologies" / "merge_feeders_three_columns.mmd"
    )
    covered = {
        ownership.member_id
        for plan in observation.plan.convergence_plans
        for ownership in plan.endpoint_ownership
        if ownership.role is ConvergenceEndpointRole.COVERED_CONTINUATION
    }
    bindings = {item.member_id: item for item in observation.plan.bindings}

    assert covered
    assert all(bindings[item].kind is not BindingKind.EMITTED for item in covered)
    build_route_plan_query(observation.plan)


# --- Corridor cohort aperture-requirement producer (unwired) ---


def _column_aperture_scenario(
    direction: Direction,
) -> tuple[
    MetroGraph,
    SimpleNamespace,
    member_geometry.CorridorCohortLedger,
    tuple[member_geometry.CorridorCohortTarget, ...],
    member_geometry.CorridorCohortCompilationError,
]:
    """A left/right section pair with one claim failing 20px short of clearing.

    ``s_left`` (columns 0) and ``s_right`` (column 1) sit 20px apart; a claim
    naming ``s_left`` as its target section fails by 20px against a blocker
    whose connector's source section is ``s_right``, so the aperture producer
    owes ``(120 - 100) + 20 == 40`` at column boundary 1.
    """
    graph = MetroGraph(
        sections={
            "s_left": Section(
                "s_left",
                "Left",
                grid_col=0,
                grid_col_span=1,
                grid_row=0,
                grid_row_span=1,
                bbox_x=0.0,
                bbox_w=100.0,
                bbox_y=0.0,
                bbox_h=50.0,
            ),
            "s_right": Section(
                "s_right",
                "Right",
                grid_col=1,
                grid_col_span=1,
                grid_row=0,
                grid_row_span=1,
                bbox_x=120.0,
                bbox_w=100.0,
                bbox_y=0.0,
                bbox_h=50.0,
            ),
        }
    )
    connectors = {
        "connector-a": SimpleNamespace(
            target_section="s_left", source_section="s_far_left"
        ),
        "connector-blocker": SimpleNamespace(
            target_section="s_far_right", source_section="s_right"
        ),
    }
    scaffold = SimpleNamespace(
        query=SimpleNamespace(connector=lambda connector_id: connectors[connector_id])
    )
    claim = CorridorCohortLedgerClaim(
        claim_id="claim-a",
        reservation_id="reservation-a",
        reservation_rank=0,
        claim_rank=0,
        region=ColumnGapRegion(0, 1),
        orientation=CorridorOrientation.VERTICAL,
        direction=direction,
        lane_rank=0,
        member_id="member-a",
        member_geometry_plan_id="plan:member-a",
        edge_key=("a-source", "a-target", "line"),
        family_id=RouteFamilyId.SAME_Y_STRAIGHT,
        connector_ids=("connector-a",),
        segment_rank=0,
        path_rank=0,
        endpoint_cohort_id=None,
        endpoint_network_rank=None,
        destination_boundary_carrier=False,
        destination_boundary_axis_sign=None,
        network_id=None,
        reservation_complete=True,
    )
    ledger = CorridorCohortLedger(
        claims=(claim,),
        endpoint_members=(),
        eligible_member_ids=frozenset({"member-a", "blocker"}),
        ambiguous_endpoint_cohort_ids=frozenset(),
        offset_step=10.0,
    )
    route = RoutedPath(
        Edge("blocker-src", "blocker-tgt", "line"), "line", [(0.0, 0.0), (10.0, 0.0)]
    )
    target = CorridorCohortTarget(
        "blocker",
        "plan:blocker",
        ("blocker-src", "blocker-tgt", "line"),
        RouteFamilyId.SAME_Y_STRAIGHT,
        ("connector-blocker",),
        route,
        True,
    )
    shortfall = CorridorClearanceShortfall(
        claim_ids=("claim-a",),
        blocking_obstacle_ids=("obstacle-a",),
        deficit=20.0,
        axis=0,
        required_shift_sign=1,
    )
    failure = CorridorCohortFailure(
        component_id="corridor-component|0",
        result_rank=0,
        reason=CorridorAllocationFailureReason.INFEASIBLE,
        blocking_member_ids=(),
        blocking_obstacle_ids=("obstacle-a",),
        blocking_equality_owner_ids=(),
        blocking_endpoint_owner_ids=(),
        clearance_shortfall=shortfall,
        blocking_obstacles=(
            CorridorCohortObstacleProvenance(
                "obstacle-a",
                "blocker",
                ("blocker-src", "blocker-tgt", "line"),
                0,
                ("connector-blocker",),
            ),
        ),
    )
    error = CorridorCohortCompilationError("synthetic failure", (failure,))
    return graph, scaffold, ledger, (target,), error


def test_corridor_cohort_aperture_requirements_builds_one_typed_requirement() -> None:
    graph, scaffold, ledger, targets, error = _column_aperture_scenario(Direction.R)

    (requirement,) = member_geometry._corridor_cohort_aperture_requirements(
        graph, scaffold, ledger, targets, (), error
    )

    assert requirement.axis is SettlementAxis.COLUMN
    assert requirement.boundary == 1
    assert requirement.required == pytest.approx(40.0)
    assert requirement.negative_section_ids == ("s_left",)
    assert requirement.positive_section_ids == ("s_right",)
    assert requirement.kind is BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE


@pytest.mark.parametrize("direction", [Direction.R, Direction.L])
def test_corridor_cohort_aperture_boundary_selection_ignores_connector_direction(
    direction: Direction,
) -> None:
    """Boundary side comes from the shift sign and grid position alone.

    Flipping the failing claim's running direction while holding its section
    geometry and shift sign fixed must not move which section counts as the
    negative or positive side: a boundary-selection bug that started reading
    connector source/target order would fail this for one of the two
    directions.
    """
    graph, scaffold, ledger, targets, error = _column_aperture_scenario(direction)

    (requirement,) = member_geometry._corridor_cohort_aperture_requirements(
        graph, scaffold, ledger, targets, (), error
    )

    assert requirement.negative_section_ids == ("s_left",)
    assert requirement.positive_section_ids == ("s_right",)
    assert requirement.required == pytest.approx(40.0)


def test_record_boundary_clearance_requirement_keeps_kinds_distinct() -> None:
    """A ``GENERAL`` and an ``APERTURE`` requirement on the same section pair
    and boundary number coalesce independently, each keeping its own kind.

    Before the merge-key fix, both collided on ``(boundary, owner_id,
    negative_section_ids, positive_section_ids)`` alone, so recording the
    second silently discarded the first's ``kind``/``axis`` identity.
    """
    general = BoundaryClearanceRequirement(
        SettlementAxis.COLUMN,
        3,
        "owner",
        50.0,
        ("s16",),
        ("s17",),
        "general demand",
    )
    aperture = BoundaryClearanceRequirement(
        SettlementAxis.COLUMN,
        3,
        "owner",
        80.0,
        ("s16",),
        ("s17",),
        "aperture demand",
        BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE,
    )
    requirements: dict[
        member_geometry._BoundaryRequirementKey, BoundaryClearanceRequirement
    ] = {}

    member_geometry._record_boundary_clearance_requirement(requirements, general)
    member_geometry._record_boundary_clearance_requirement(requirements, aperture)

    by_kind = {item.kind: item for item in requirements.values()}
    assert len(requirements) == 2
    assert by_kind[BoundaryClearanceRequirementKind.GENERAL].required == pytest.approx(
        50.0
    )
    assert by_kind[
        BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
    ].required == pytest.approx(80.0)
