"""Corridor-cohort compile outcomes at the post-settlement aperture observation.

``_simulated_tail_plans`` re-observes the routes once per
``_settle_render_geometry`` call, after envelope settlement and before
``hold_port_anchored_edges``, against the reservations the last settlement
resettle consumed (or the frozen plan when no settlement resettle ran), with
clearance requirements allowed.  That observation is the first to run the
corridor-cohort compiler with a ledger, so each fixture below must compile to
``PLANNED`` or to one typed ``CORRIDOR_COHORT_APERTURE`` requirement.  A
compiler exception propagates out of the render and fails the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.phases._common import _restoring_layout_geometry
from nf_metro.layout.route_plan import RoutePlan
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.common import OffsetRegime
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceRequirementKind,
    SettlementAxis,
)
from nf_metro.parser.model import MetroGraph, PortSide
from nf_metro.render import svg

ROOT = Path(__file__).parents[1]


def _simulated_tail_plans(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[MetroGraph, tuple[RoutePlan, ...]]:
    observations: list[tuple[MetroGraph, dict, RoutePlan]] = []
    tail_plans: list[RoutePlan] = []
    pending = [False]
    real_observe = svg.observe_route_edges_centred
    real_settle = svg._settle_render_geometry
    real_hold = svg.hold_port_anchored_edges
    real_attach = svg.attach_settlement_diagnostics

    def observe(graph, **kwargs):
        observation = real_observe(graph, **kwargs)
        observations.append((graph, kwargs, observation.plan))
        return observation

    def settle(*args, **kwargs):
        observations.clear()
        pending[0] = True
        return real_settle(*args, **kwargs)

    def observe_tail() -> None:
        if not pending[0]:
            return
        pending[0] = False
        consumed = [kw for _g, kw, _p in observations if kw.get("reservations")]
        graph, last_kwargs, last_plan = observations[-1]
        reservations = consumed[-1]["reservations"] if consumed else last_plan
        translations = (
            consumed[-1].get("reservation_translations", ()) if consumed else ()
        )
        with _restoring_layout_geometry(graph):
            tail_plans.append(
                real_observe(
                    graph,
                    station_offsets=last_kwargs["station_offsets"],
                    offset_step=last_kwargs["offset_step"],
                    reservations=reservations,
                    reservation_translations=translations,
                    allow_convergence_clearance_requirements=True,
                ).plan
            )

    def hold(*args, **kwargs):
        observe_tail()
        return real_hold(*args, **kwargs)

    def attach(*args, **kwargs):
        observe_tail()
        return real_attach(*args, **kwargs)

    monkeypatch.setattr(svg, "observe_route_edges_centred", observe)
    monkeypatch.setattr(svg, "_settle_render_geometry", settle)
    monkeypatch.setattr(svg, "hold_port_anchored_edges", hold)
    monkeypatch.setattr(svg, "attach_settlement_diagnostics", attach)

    path = ROOT / relative_path
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    svg.build_observed_render_plan(graph, resolve_theme(None, graph))
    assert tail_plans, "no settlement pass reached the aperture observation"
    return graph, tuple(tail_plans)


def _aperture_requirements(plan: RoutePlan):
    return tuple(
        requirement
        for requirement in plan.boundary_clearance_requirements
        if requirement.kind is BoundaryClearanceRequirementKind.CORRIDOR_COHORT_APERTURE
    )


def _assert_planned(
    relative_path: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[MetroGraph, tuple[RoutePlan, ...]]:
    graph, plans = _simulated_tail_plans(relative_path, monkeypatch)
    for plan in plans:
        assert plan.corridor_cohort_ledger is not None
        assert _aperture_requirements(plan) == ()
    return graph, plans


def test_packed_cell_oracle_compiles_to_one_aperture_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _graph, plans = _simulated_tail_plans(
        "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd", monkeypatch
    )
    (plan,) = plans
    (requirement,) = _aperture_requirements(plan)
    assert requirement.axis is SettlementAxis.COLUMN
    assert requirement.boundary == 2
    assert requirement.positive_section_ids == ("qc",)


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
    _graph, plans = _simulated_tail_plans(
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
