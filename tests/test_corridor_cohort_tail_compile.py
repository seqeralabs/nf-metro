"""Corridor-cohort compile outcomes at the post-settlement aperture observation.

``_settle_render_geometry`` re-observes the routes once per call, after envelope
settlement and before ``hold_port_anchored_edges``, against the reservations the
last settlement resettle consumed (or the frozen plan when no settlement resettle
ran), with clearance requirements allowed.  That observation is the only render
pass that runs the corridor-cohort compiler with a ledger, so each fixture below
must compile to ``PLANNED`` or to one typed ``CORRIDOR_COHORT_APERTURE``
requirement.  A compiler exception propagates out of the render and fails the
test.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout import route_reservations
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.envelope_settlement import (
    EnvelopeSettlement,
    SettlementTranslation,
)
from nf_metro.layout.phases.guards import LayoutInvariantError
from nf_metro.layout.route_plan import RoutePlan, SettlementStage
from nf_metro.layout.routing import (
    corridor_cohort_integration,
    corridor_cohorts,
    member_geometry,
    planning,
)
from nf_metro.layout.routing.common import OffsetRegime
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceDemand,
    BoundaryClearanceRequirement,
    BoundaryClearanceRequirementKind,
    SettlementAxis,
)
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.render import svg

ROOT = Path(__file__).parents[1]
ORACLE = "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd"


def _is_aperture_observation(kwargs: dict) -> bool:
    """The one render observation that both holds a ledger and may publish."""
    return kwargs.get("reservations") is not None and bool(
        kwargs.get("allow_convergence_clearance_requirements")
    )


def _tail_render(
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
    layout_options: Mapping[str, object] | None = None,
) -> tuple[MetroGraph, svg.ObservedRenderPlan, tuple[RoutePlan, ...]]:
    settle_calls = 0
    tail_plans: list[RoutePlan] = []
    real_observe = svg.observe_route_edges_centred
    real_settle = svg._settle_render_geometry

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        if _is_aperture_observation(kwargs):
            tail_plans.append(observation.plan)
        return observation

    def settle(*args, **kwargs):
        nonlocal settle_calls
        settle_calls += 1
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)

    path = ROOT / relative_path
    graph = prepare_graph(
        path.read_text(), source_dir=str(path.parent), layout_options=layout_options
    )
    observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))
    assert len(tail_plans) == settle_calls >= 1
    return graph, observed, tuple(tail_plans)


def _tail_plans(
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
    layout_options: Mapping[str, object] | None = None,
) -> tuple[MetroGraph, tuple[RoutePlan, ...]]:
    graph, _observed, plans = _tail_render(relative_path, monkeypatch, layout_options)
    return graph, plans


def _aperture_requirements(plan: RoutePlan):
    return tuple(
        requirement
        for requirement in plan.boundary_clearance_requirements
        if requirement.kind is BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
    )


def _assert_planned(
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
    layout_options: Mapping[str, object] | None = None,
) -> tuple[MetroGraph, tuple[RoutePlan, ...]]:
    graph, plans = _tail_plans(relative_path, monkeypatch, layout_options)
    for plan in plans:
        assert plan.corridor_cohort_ledger is not None
        assert _aperture_requirements(plan) == ()
    return graph, plans


def test_packed_cell_oracle_compiles_to_one_aperture_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture's ``track_gap: 2`` pitches its lanes 5px apart, and the
    aperture it requires is measured at that pitch."""
    _graph, plans = _tail_plans(ORACLE, monkeypatch)
    (plan,) = plans
    (requirement,) = _aperture_requirements(plan)
    assert requirement.axis is SettlementAxis.COLUMN
    assert requirement.boundary == 2
    assert requirement.required == pytest.approx(56.0)
    assert requirement.positive_section_ids == ("qc",)


@pytest.mark.parametrize(
    ("relative_path", "layout_options"),
    (
        ("examples/variant_calling.mmd", {"track_gap": 0.0}),
        ("examples/guide/03_fan_out.mmd", {"stroke_scale": 0.5}),
        ("examples/topologies/exit_run_three_drop_columns.mmd", {"track_gap": 0.0}),
    ),
)
def test_lanes_pitched_below_the_default_step_compile_at_their_own_pitch(
    relative_path: str,
    layout_options: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lanes nest one ``offset_step`` apart, so a pitch below the default step
    is a separation the compiler must accept, between two movable lanes and
    between a movable lane and a fixed one."""
    _assert_planned(relative_path, monkeypatch, layout_options)


@pytest.mark.parametrize(
    "relative_path",
    ("examples/variantbenchmarking.mmd", "examples/variantbenchmarking_auto.mmd"),
)
def test_lanes_on_a_non_binary_pitch_compile(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stroke_scale: 0.9`` pitches lanes 3.6px apart, a step whose sums carry
    float residue; one pitch is one separation however the sum rounded."""
    _assert_planned(relative_path, monkeypatch, {"stroke_scale": 0.9})


def test_one_non_binary_pitch_reads_as_one_exact_separation() -> None:
    assert corridor_cohorts._q(698.8000000000001) - corridor_cohorts._q(
        695.2
    ) == corridor_cohorts._q(3.6)


def test_only_an_endpoint_landing_claim_is_bound_to_a_port_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``section_x_gap: 0`` a corridor-fed trunk's two-point spur shares an
    endpoint cohort with a landing member but has no lead of its own to land,
    so it binds with no port slot rather than one it cannot reach."""
    real_bind = corridor_cohort_integration._bind_claim
    unlanded_cohort_claims = 0

    def check(claim, target, landing_coordinate):
        nonlocal unlanded_cohort_claims
        bound = real_bind(claim, target, landing_coordinate)
        lands = (
            claim.endpoint_cohort_id is not None
            and target.mutable
            and claim.segment_rank == len(target.route.points) - 3
        )
        unlanded_cohort_claims += claim.endpoint_cohort_id is not None and not lands
        assert (bound.landing_coordinate is not None) == lands, claim.claim_id
        return bound

    monkeypatch.setattr(corridor_cohort_integration, "_bind_claim", check)
    _assert_planned(
        "examples/topologies/corridor_fed_trunk_output_spur.mmd",
        monkeypatch,
        {"section_x_gap": 0.0},
    )
    assert unlanded_cohort_claims


VERTICAL_FLOW_SIDE_ENTRY_FIXTURES = (
    "examples/topologies/fold_fan_across.mmd",
    "examples/topologies/fold_stacked_branch.mmd",
    "examples/topologies/reconverge_reversed_fold.mmd",
    "examples/topologies/tb_two_line_vert_seam.mmd",
    "examples/guide/04_directions.mmd",
    "tests/fixtures/tb_exit_terminal_on_carrier.mmd",
)


@pytest.mark.parametrize("relative_path", VERTICAL_FLOW_SIDE_ENTRY_FIXTURES)
def test_endpoint_lane_coordinate_lies_on_the_lead_it_labels(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target's endpoint lane names the axis its final lead holds constant and
    the coordinate that lead holds, whatever the entry section's flow."""
    real_target = member_geometry._corridor_cohort_target
    side_entries_on_vertical_flow = 0

    def check(candidate, scaffold, ctx, *, mutable):
        target = real_target(candidate, scaffold, ctx, mutable=mutable)
        axis = target.endpoint_lane_axis
        if axis is None or target.route.offset_regime is not OffsetRegime.BAKED:
            return target
        port = ctx.graph.ports[target.edge_key[1]]
        section = ctx.graph.sections[port.section_id]
        nonlocal side_entries_on_vertical_flow
        side_entries_on_vertical_flow += port.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ) and section.direction in ("TB", "BT")
        start, end = target.route.points[-2:]
        assert axis == int(port.side in (PortSide.LEFT, PortSide.RIGHT))
        assert abs(start[axis] - end[axis]) <= COORD_TOLERANCE
        assert target.endpoint_lane_coordinate == pytest.approx(end[axis])
        return target

    monkeypatch.setattr(member_geometry, "_corridor_cohort_target", check)
    _assert_planned(relative_path, monkeypatch)
    assert side_entries_on_vertical_flow


@pytest.mark.parametrize(
    "relative_path",
    (
        "examples/genomic_pipeline.mmd",
        "tests/fixtures/regressions/stacked_collector_fanin.mmd",
    ),
)
def test_leftward_bundle_compiles_in_its_travel_frame(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_planned(relative_path, monkeypatch)


def test_same_line_edges_into_one_port_share_one_endpoint_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two authored ``riboseq`` networks enter ``orf_calling`` through one port.

    A port draws one lane per line, so both members hold one endpoint network
    rank rather than two ranks tied on one slot.
    """
    _graph, plans = _tail_plans(
        "examples/topologies/junction_entry_lane_step.mmd", monkeypatch
    )
    for plan in plans:
        assert _aperture_requirements(plan) == ()
        ledger = plan.corridor_cohort_ledger
        assert ledger is not None
        ranks = {
            claim.edge_key: claim.endpoint_network_rank
            for claim in ledger.claims
            if claim.edge_key is not None
            and claim.edge_key[1] == "orf_calling__entry_left_3"
            and claim.endpoint_network_rank is not None
        }
        assert {edge_key[0] for edge_key in ranks} == {
            "preprocessing__exit_right_0",
            "__junction_6",
        }
        assert len(set(ranks.values())) == 1


def test_flow_start_entry_orders_feeders_by_descent_corridor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``s7``'s RIGHT entry is fed from above by ``lower`` and ``upper`` and from
    below by ``feeder``, which drops furthest out: the climbing feeder takes the
    bottom lane so its turn-in nests under the two descents instead of crossing
    ``upper`` at the port."""
    graph, _plans = _assert_planned(
        "examples/topologies/same_destination_vertical_convergence.mmd", monkeypatch
    )
    offsets = compute_station_offsets(graph)
    lanes = sorted(
        graph.station_lines("s7__entry_right_9"),
        key=lambda line_id: offsets[("s7__entry_right_9", line_id)],
    )
    assert lanes == ["lower", "upper", "feeder"]


@pytest.mark.parametrize(
    "relative_path",
    ("examples/variantbenchmarking.mmd", "examples/variantbenchmarking_auto.mmd"),
)
def test_forced_same_line_loop_crossing_is_not_a_forbidden_interval(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_planned(relative_path, monkeypatch)


@pytest.mark.parametrize(
    "relative_path",
    (
        "examples/riboseq_metro.mmd",
        "tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd",
    ),
)
def test_trunk_slot_materialisation_leaves_convergence_context_in_place(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A convergence leg rides member geometry only as gap-population context;
    its convergence plan emits it, so trunk-slot materialisation must not move
    the copy the cohort compiler later binds as a fixed claim."""
    context_routes: list = []
    real_context_route = member_geometry._convergence_context_route
    real_materialize = member_geometry._materialize_trunk_slots
    materialised = 0

    def record_context(ctx, key, family_id):
        route = real_context_route(ctx, key, family_id)
        if route is not None:
            context_routes.append(route)
        return route

    def check_materialize(routes, ctx, **kwargs):
        nonlocal materialised
        before = [(route, list(route.points)) for route in context_routes]
        real_materialize(routes, ctx, **kwargs)
        materialised += 1
        for route, points in before:
            assert route.points == points, route.edge

    monkeypatch.setattr(member_geometry, "_convergence_context_route", record_context)
    monkeypatch.setattr(member_geometry, "_materialize_trunk_slots", check_materialize)
    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    svg.build_observed_render_plan(graph, resolve_theme(None, graph))
    assert materialised
    assert any(
        (route.edge.source, route.edge.target, route.line_id)
        == ("__junction_12", "__merge_2", "riboseq")
        for route in context_routes
    )


RIBOSEQ_TOP_CORRIDOR_EDGES = (
    ("__junction_13", "te__entry_left_10", "rnaseq"),
    ("__junction_13", "psite_id__entry_left_9", "riboseq"),
    ("novel_transcripts__exit_right_5", "orf_calling__entry_left_7", "annotation"),
)


@pytest.mark.parametrize(
    ("relative_path", "coordinates"),
    (
        ("examples/riboseq_metro.mmd", (321.0, 317.0, 313.0)),
        (
            "tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd",
            (337.8, 333.8, 329.8),
        ),
    ),
)
def test_unowned_lane_pair_seats_on_the_side_its_end_turns_take(
    relative_path: str,
    coordinates: tuple[float, float, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three lines drop from the right into one row corridor, run left, and drop
    again.  ``rnaseq`` turns down at an end inside ``riboseq``'s run and
    ``riboseq`` turns up at an end inside ``rnaseq``'s, so ``rnaseq`` seats
    below ``riboseq`` whatever their connector ranks say.  ``annotation``'s run
    spans both lanes' ends, which turn opposite ways, so no side avoids a
    crossing and its order falls back to rank.  The compile seats every lane
    where the render draws it."""
    compiled = []
    real_compile = member_geometry.compile_corridor_cohort_plan

    def record(*args, **kwargs):
        cohort_plan = real_compile(*args, **kwargs)
        compiled.append(cohort_plan)
        return cohort_plan

    monkeypatch.setattr(member_geometry, "compile_corridor_cohort_plan", record)
    _graph, observed, plans = _tail_render(relative_path, monkeypatch)
    for plan in plans:
        assert plan.corridor_cohort_ledger is not None
        assert _aperture_requirements(plan) == ()
    expected = dict(zip(RIBOSEQ_TOP_CORRIDOR_EDGES, coordinates, strict=True))
    rendered = {
        (route.edge.source, route.edge.target, route.line_id): route.points
        for route in observed.plan.routes
    }
    for edge_key, coordinate in expected.items():
        (_run_x, run_y), (_end_x, end_y) = rendered[edge_key][2:4]
        assert run_y == pytest.approx(coordinate) == end_y
    assert compiled
    for cohort_plan in compiled:
        seated = {
            allocation.edge_key: allocation.coordinate
            for allocation in cohort_plan.allocations
            if allocation.edge_key in expected and allocation.segment_rank == 2
        }
        assert seated == pytest.approx(expected)


def test_endpoint_section_lead_is_not_filed_in_a_neighbouring_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``germline`` leg into ``annotation``'s left entry runs inside the
    ``annotation`` band, so no gap region files it."""
    real_region = route_reservations._corridor_region
    filed: list[object] = []

    def spy(graph, segment, segments, span, connector_ids, member, extents):
        corridor = real_region(
            graph, segment, segments, span, connector_ids, member, extents
        )
        if segment.after is None and (
            member.source.station_id,
            member.target.station_id,
            member.line_id,
        ) == ("__junction_9", "annotation__entry_left_6", "germline"):
            filed.append(corridor)
        return corridor

    monkeypatch.setattr(route_reservations, "_corridor_region", spy)
    _assert_planned(
        "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd", monkeypatch
    )
    assert filed
    assert set(filed) == {None}


@pytest.mark.parametrize(
    "relative_path",
    (
        "examples/differentialabundance.mmd",
        "examples/differentialabundance_default.mmd",
        "examples/topologies/bottom_exit_stacked_right_entry_fan.mmd",
        "examples/topologies/bottom_exit_stacked_right_entry_multiline_branch.mmd",
        "examples/topologies/bypass_left_entry_colspan_intervener.mmd",
        "examples/topologies/complex_multipath.mmd",
        "examples/topologies/convergence_fold_diamond.mmd",
        "examples/topologies/convergence_sink_fold.mmd",
        "examples/topologies/convergent_offrow_exit_climb.mmd",
        "examples/topologies/fan_bypass_nesting.mmd",
        "examples/topologies/fold_split_targets.mmd",
        "examples/topologies/junction_entry_lane_rebase.mmd",
        "examples/topologies/junction_entry_lane_step.mmd",
        "examples/topologies/merge_pullaway.mmd",
        "examples/topologies/merge_trunk_over_low_section.mmd",
        "examples/topologies/off_track_input_above_consumer.mmd",
        "examples/topologies/right_entry_over_top_tall_upstream.mmd",
        "tests/fixtures/tb_exit_terminal_on_carrier.mmd",
    ),
)
def test_post_settlement_observation_compiles_settled_reservations(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery-time reservations on these fixtures are narrower than their
    minimum width; observed after settlement grants that deficit, they compile."""
    _assert_planned(relative_path, monkeypatch)


@pytest.mark.parametrize(
    ("relative_path", "layout_options"),
    (
        ("examples/topologies/target_lane_transition.mmd", {"line_order": "span"}),
        (
            "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd",
            {"section_x_gap": 30.0, "y_spacing": 90.0},
        ),
    ),
)
def test_exit_turn_gap_allocation_pass_compiles_no_corridor_cohorts(
    relative_path: str,
    layout_options: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the settled allocation survives the member-geometry execution that
    allocates pending exit-turn gaps; the execution that re-plans against it
    holds the cohort ledger and compiles the corridor cohorts."""
    executions: list[tuple[bool, bool, bool]] = []
    real_execution = planning.build_member_geometry_execution

    def record(*args, **kwargs):
        executions.append(
            (
                bool(kwargs["pending_exit_turn_plan_ids"]),
                bool(kwargs["settled_exit_turn_plan_ids"]),
                kwargs["corridor_cohort_ledger"] is not None
                or bool(kwargs["corridor_targets"])
                or bool(kwargs["corridor_scalar_requests"]),
            )
        )
        return real_execution(*args, **kwargs)

    monkeypatch.setattr(planning, "build_member_geometry_execution", record)
    _assert_planned(relative_path, monkeypatch, layout_options)
    assert not any(
        compiles for allocates, _settles, compiles in executions if allocates
    )
    assert any(
        allocation[0] and settled[1] and settled[2]
        for allocation, settled in zip(executions, executions[1:], strict=False)
    )


def test_compact_convergence_seats_its_unordered_lanes_in_end_turn_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_planned(
        "examples/topologies/same_destination_vertical_convergence.mmd",
        monkeypatch,
        {"compact_offsets": True},
    )


def _flat(points) -> list[float]:
    return [coordinate for point in points for coordinate in point]


def test_granted_short_destination_cohort_is_seated_on_the_tail_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aperture observation both allows clearance requirements and holds the
    short-destination cohort's earlier grant, so it seats the cohort where the
    render draws it rather than observing the unseated approach."""
    tail_routes: list[dict[tuple[str, str, str], list[float]]] = []
    real_observe = svg.observe_route_edges_centred

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        if _is_aperture_observation(kwargs):
            tail_routes.append(
                {
                    (route.edge.source, route.edge.target, route.line_id): _flat(
                        route.points
                    )
                    for route in observation.routes
                }
            )
        return observation

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    path = ROOT / "examples/topologies/same_destination_short_overlap.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))

    drawn = {
        (route.edge.source, route.edge.target, route.line_id): _flat(route.points)
        for route in observed.plan.routes
        if route.edge.target == "target__entry_left_3"
    }
    assert len(drawn) >= 2
    (observed_routes,) = tail_routes
    for edge_key, points in drawn.items():
        assert observed_routes[edge_key] == pytest.approx(points), edge_key


RENDER_STAGES = frozenset(
    {
        SettlementStage.DISCOVERY,
        SettlementStage.GENERAL_SETTLEMENT,
        SettlementStage.COHORT_FINAL,
        SettlementStage.VALIDATION,
    }
)


def test_packed_cell_aperture_grant_widens_its_column_boundary_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``qc``'s aperture is 2.0px short at column boundary 2; one settlement of
    the observed plan pays it with a 2.0px translation, carrying ``qc`` and
    nothing on the negative side, which its own translation record shows."""
    batches: list[
        tuple[RoutePlan, tuple[BoundaryClearanceDemand, ...], EnvelopeSettlement]
    ] = []
    real_settle_envelopes = svg.settle_route_envelopes

    def settle_envelopes(graph, plan, clearance=None):
        owed = () if clearance is None else clearance(graph)
        settlement = real_settle_envelopes(graph, plan, clearance=clearance)
        if svg._corridor_cohort_aperture_requirements(plan):
            batches.append((plan, owed, settlement))
        return settlement

    monkeypatch.setattr(svg, "settle_route_envelopes", settle_envelopes)
    _graph, observed, plans = _tail_render(ORACLE, monkeypatch)

    assert len(batches) == len(plans) == 1
    ((batch_plan, owed, settlement),) = batches
    (plan,) = plans
    assert batch_plan is plan
    (requirement,) = plan.boundary_clearance_requirements
    assert requirement.kind is BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
    (demand,) = owed
    assert (demand.axis, demand.boundary) == (SettlementAxis.COLUMN, 2)
    assert demand.deficit == pytest.approx(2.0)
    (translation,) = settlement.translations
    assert (translation.axis, translation.boundary) == (SettlementAxis.COLUMN, 2)
    assert translation.amount == pytest.approx(2.0)
    assert translation.amount >= demand.deficit
    assert set(requirement.positive_section_ids) <= set(translation.section_ids)
    assert set(translation.section_ids).isdisjoint(requirement.negative_section_ids)

    published = observed.route_plan
    assert translation.message in {item.message for item in published.diagnostics}
    records = published.settlement_trace.records
    assert {record.stage for record in records} <= RENDER_STAGES


def _aperture_requirement() -> BoundaryClearanceRequirement:
    return BoundaryClearanceRequirement(
        SettlementAxis.COLUMN,
        2,
        "component|result:0",
        55.0,
        ("upstream",),
        ("downstream",),
        "corridor cohort aperture at column boundary 2",
        BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE,
    )


def _grant(amount: float, section_ids: tuple[str, ...]) -> SettlementTranslation:
    return SettlementTranslation(
        axis=SettlementAxis.COLUMN,
        boundary=2,
        coordinate=100.0,
        amount=amount,
        reservation_id=None,
        claimant_member_ids=(),
        blocker_ids=(),
        section_ids=section_ids,
        reservation_ids=(),
    )


@pytest.mark.parametrize(
    "translations",
    [
        pytest.param((), id="no-translation"),
        pytest.param((_grant(1.0, ("downstream",)),), id="grant-below-deficit"),
        pytest.param((_grant(2.0, ("elsewhere",)),), id="positive-side-left-behind"),
        pytest.param(
            (_grant(2.0, ("downstream", "upstream")),), id="negative-side-carried"
        ),
    ],
)
def test_an_aperture_grant_that_does_not_close_its_deficit_fails(
    translations: tuple[SettlementTranslation, ...],
) -> None:
    requirement = _aperture_requirement()
    demand = BoundaryClearanceDemand(
        SettlementAxis.COLUMN, 2, 55.0, 1.5, ("upstream",), requirement.description
    )
    with pytest.raises(LayoutInvariantError, match="one aperture batch"):
        svg._assert_aperture_grant_closes(
            ((requirement, demand),), EnvelopeSettlement(translations)
        )


def test_an_aperture_grant_may_exceed_its_deficit() -> None:
    requirement = _aperture_requirement()
    demand = BoundaryClearanceDemand(
        SettlementAxis.COLUMN, 2, 55.0, 1.0, ("upstream",), requirement.description
    )
    svg._assert_aperture_grant_closes(
        ((requirement, demand),),
        EnvelopeSettlement((_grant(2.0, ("downstream", "elsewhere")),)),
    )


def _layout_state(graph: MetroGraph) -> tuple[object, ...]:
    return (
        {key: (item.x, item.y) for key, item in graph.stations.items()},
        {
            key: (item.bbox_x, item.bbox_y, item.bbox_w, item.bbox_h)
            for key, item in graph.sections.items()
        },
        graph.bypass_label_obstacles,
    )


@pytest.mark.parametrize(
    "relative_path",
    (
        "examples/topologies/compact_hidden_passthrough.mmd",
        "examples/topologies/top_entry_left_neighbour.mmd",
        "examples/topologies/multirow_source_stacked_fan.mmd",
    ),
)
def test_a_discarded_aperture_observation_leaves_the_render_geometry(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observation re-anchors junctions, centres markers and refreshes the
    bypass-label obstacles on these fixtures; with no aperture requirement all
    of it is undone, the ports the label pass before it carried are still held,
    and its route observation stays on the trace."""
    observed_states: list[tuple[tuple[object, ...], ...]] = []
    carries: list[tuple[str, ...]] = []
    carried_into_observation: list[tuple[str, ...]] = []
    held: list[tuple[str, ...]] = []
    observations = 0
    tail_rank: int | None = None
    real_restoring = svg._restoring_route_observation_geometry
    real_carry = svg.carry_ports_with_section_edges
    real_hold = svg.hold_port_anchored_edges
    real_observe = svg.observe_route_edges_centred
    real_settle = svg._settle_render_geometry

    @contextmanager
    def watch(graph: MetroGraph) -> Iterator[None]:
        carried_into_observation.append(carries[-1])
        before = _layout_state(graph)
        with real_restoring(graph):
            yield
            during = _layout_state(graph)
        observed_states.append((before, during, _layout_state(graph)))

    def carry(graph, edges):
        carried = real_carry(graph, edges)
        carries.append(carried)
        return carried

    def hold(graph, edges, ports):
        held.append(ports)
        return real_hold(graph, edges, ports)

    def observe(graph, **kwargs):
        nonlocal observations, tail_rank
        if _is_aperture_observation(kwargs):
            tail_rank = observations
        observations += 1
        return real_observe(graph, **kwargs)

    def settle(*args, **kwargs):
        nonlocal observations, tail_rank
        observations, tail_rank = 0, None
        observed_states.clear()
        carries.clear()
        carried_into_observation.clear()
        held.clear()
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(svg, "_restoring_route_observation_geometry", watch)
    monkeypatch.setattr(svg, "carry_ports_with_section_edges", carry)
    monkeypatch.setattr(svg, "hold_port_anchored_edges", hold)
    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observed = svg.build_observed_render_plan(graph, resolve_theme(None, graph))

    ((before, during, after),) = observed_states
    assert during[0] != before[0]
    assert during[2] is not before[2]
    assert after[:2] == before[:2]
    assert after[2] is before[2]
    (carried,) = carried_into_observation
    assert held == ([carried] if carried else [])

    records = observed.route_plan.settlement_trace.records
    assert tail_rank == observations - 1
    (tail_record,) = (
        record for record in records if record.route_observation_rank == tail_rank
    )
    assert tail_record.stage is SettlementStage.GENERAL_SETTLEMENT
    assert (
        sum(record.stage is SettlementStage.GENERAL_SETTLEMENT for record in records)
        == observations - 1
    )
