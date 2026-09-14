"""Real topology witnesses for the routing gates reconciled in issue 1746."""

from __future__ import annotations

import copy
import re
import warnings
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from layout_validator import check_route_segment_crossings, check_station_as_elbow

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.layout import engine as layout_engine
from nf_metro.layout.constants import (
    COORD_TOLERANCE,
    CURVE_RADIUS,
    graph_offset_step,
)
from nf_metro.layout.envelope_settlement import (
    measure_boundary_clearance_requirements,
    quantised_allocation,
    settle_route_envelopes,
)
from nf_metro.layout.phases.guards import (
    SettledRouteValidationError,
    _guard_no_diagonal_strikes_horizontal_label,
)
from nf_metro.layout.route_plan import RouteSystemId, build_route_plan_query
from nf_metro.layout.routing import common as routing_common
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing import core as routing_core
from nf_metro.layout.routing import member_geometry as routing_member_geometry
from nf_metro.layout.routing import normalize as routing_normalize
from nf_metro.layout.routing.common import (
    port_peeloff_tail,
)
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.invariants import (
    check_same_destination_approach_bundle,
)
from nf_metro.layout.routing.reserved_bands import ReservedCorridors
from nf_metro.layout.settlement_demand import (
    BoundaryClearanceRequirement,
    SettlementAxis,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, PortSide
from nf_metro.render import svg as render_svg
from nf_metro.render.animate import _build_line_motion_paths
from nf_metro.render.svg import _plan_decision_fingerprint, build_observed_render_plan

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ROOT / "examples" / "topologies"


def _planned_and_routed(stem: str):
    """One fixture's graph, both of its offset frames, and its routes.

    ``route_edges`` rewrites the mapping it is handed, so the frame
    :func:`compute_station_offsets` decided survives only as a copy taken
    before that call.  A caller asking what the offset phases planned needs
    that copy; a caller asking what the renderer draws with needs the
    rewritten one.
    """
    path = TOPOLOGIES / f"{stem}.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        offsets = compute_station_offsets(graph)
        planned = dict(offsets)
        routes = route_edges(graph, station_offsets=offsets)
    return graph, planned, offsets, routes


def _routed(stem: str):
    graph, _planned, offsets, routes = _planned_and_routed(stem)
    return graph, offsets, routes


def _normalization_context(graph, offsets):
    return SimpleNamespace(
        graph=graph,
        merge=SimpleNamespace(entry_port_for={}),
        offset_step=graph_offset_step(graph),
        curve_radius=CURVE_RADIUS,
        station_offsets=offsets,
        reversed_sections=set(),
        reserved_bands=ReservedCorridors(),
    )


def _destination_routes(graph, routes):
    port_id = graph.sections["target"].entry_ports[0]
    return {route.line_id: route for route in routes if route.edge.target == port_id}


def _displaced_approach_bundle(graph, routed, *, reverse: bool = False):
    """A converging approach bundle whose slots demand a real tail move.

    Nudging one member's riser out by one offset step leaves that member on the
    bundle's inner slot boundary and pushes every other member's slot off the
    riser it already draws.  So the settlement has something to move, and the
    proposal machinery runs past the zero-delta exit that a bundle already
    seated on its slots returns at.
    """
    step = graph_offset_step(graph)
    routes = copy.deepcopy(routed)
    if reverse:
        routes.reverse()
    drawn = next(
        routing_common.iter_same_destination_approach_bundles(routes, graph, step)
    )
    nudged = next(route for route, _tail in drawn.entries if route.line_id == "lower")
    riser_rank = len(nudged.points) - 3
    for rank in (riser_rank, riser_rank + 1):
        nudged.points[rank] = (nudged.points[rank][0] + step, nudged.points[rank][1])
    bundle = next(
        routing_common.iter_same_destination_approach_bundles(routes, graph, step)
    )
    slots = routing_common.same_destination_approach_slots(bundle, graph, step)
    return routes, bundle, slots


def _recorded_clearance_measurements(monkeypatch: pytest.MonkeyPatch) -> list:
    """Each ``(frozen requirements, measured demands)`` pair the render asks for.

    A boundary carrying several requirements is widened once, by the largest,
    so the translation alone cannot say how many requirements were measured or
    what each of them was owed.  Recording the measurement is what makes those
    answerable.
    """
    recorded: list = []
    real = render_svg.measure_boundary_clearance_requirements

    def record(graph, requirements):
        demands = real(graph, requirements)
        recorded.append((tuple(requirements), demands))
        return demands

    monkeypatch.setattr(render_svg, "measure_boundary_clearance_requirements", record)
    return recorded


def _release_plan_ownership(bundle) -> None:
    """Clear every frozen decision that would hold a bundle's tails in place."""
    for route, _tail in bundle.entries:
        route.route_system_owned_segment_ranks = ()
        route.convergence_owned_segment_ranks = ()
        route.route_reservation_ids = ()
        route.exit_turn_axis_id = None
        route.exit_turn_segment_rank = None
        route.fan_route_emitter = None


def test_recompacted_fanout_exit_uses_one_contiguous_offset_frame() -> None:
    graph, planned, _offsets, routes = _planned_and_routed("recompacted_fanout_exit")
    exit_id = graph.sections["source"].exit_ports[0]
    junction_id = next(
        edge.target
        for edge in graph.edges_from(exit_id)
        if edge.target in graph.junction_ids
    )
    expected = {"upper": 0.0, "middle": 4.0, "lower": 8.0}

    assert {line_id: planned[exit_id, line_id] for line_id in expected} == expected
    assert {line_id: planned[junction_id, line_id] for line_id in expected} == expected
    assert {
        route.line_id: route.points
        for route in routes
        if route.edge.source == exit_id and route.edge.target == junction_id
    } == {
        "upper": [(250.0, 120.0), (260.0, 120.0)],
        "middle": [(250.0, 124.0), (260.0, 124.0)],
        "lower": [(250.0, 128.0), (260.0, 128.0)],
    }
    assert {
        station_id: graph.stations[station_id].y
        for station_id in ("paired_in", "paired_step", "paired_out")
    } == {
        "paired_in": 120.0,
        "paired_step": 120.0,
        "paired_out": 120.0,
    }
    assert next(
        route
        for route in routes
        if route.edge.source == "paired_step" and route.edge.target == "paired_out"
    ).points == [
        (graph.stations["paired_step"].x, 120.0),
        (graph.stations["paired_out"].x, 120.0),
    ]
    trunk = next(
        route
        for route in routes
        if route.edge.source == "paired_out"
        and route.edge.target == "paired__exit_right_1"
    )
    assert len(trunk.points) == 2
    assert trunk.points[0][1] == trunk.points[1][1] == 120.0


def test_flow_exit_fed_junction_freezes_its_cross_member_source_turn() -> None:
    graph, _offsets, routes = _routed("same_destination_short_overlap")
    junction = graph.stations["__junction_4"]
    port = graph.ports["scaffolding__entry_top_2"]

    assert port.x == junction.x
    assert check_station_as_elbow(graph) == []

    upstream = {
        route.line_id: route
        for route in routes
        if route.edge.source == "source__exit_right_0"
        and route.edge.target == "__junction_4"
    }
    turns = {
        route.line_id: route
        for route in routes
        if route.edge.source == "__junction_4"
        and route.edge.target == "scaffolding__entry_top_2"
    }
    assert set(upstream) == set(turns) == {"assembly", "reads"}
    assert all(
        upstream[line_id].points[-1] == turns[line_id].points[0] for line_id in turns
    )
    assert all(len(route.points) == 2 for route in turns.values())
    assert turns["reads"].source_turnout == routing_common.SourceTurnout(
        "source__exit_right_0",
        None,
        routing_common.Direction.R,
        routing_common.Direction.D,
        10.0,
    )
    assert turns["assembly"].source_turnout == routing_common.SourceTurnout(
        "source__exit_right_0",
        "target__entry_left_3",
        routing_common.Direction.R,
        routing_common.Direction.D,
        14.0,
    )


def test_source_turnout_animation_uses_the_same_concentric_radii() -> None:
    graph, offsets, routes = _routed("same_destination_short_overlap")
    paths = _build_line_motion_paths(graph, routes, offsets)

    assert any(
        line_id == "reads" and "L 192.00 124.00 Q 202.00 124.00 202.00 134.00" in path
        for line_id, path in paths
    )
    assert any(
        line_id == "assembly"
        and "L 192.00 120.00 Q 206.00 120.00 206.00 134.00" in path
        for line_id, path in paths
    )


def test_render_plan_reuses_one_materialized_turnout_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_materialize = render_svg.materialize_source_turnout_paths

    def count_materialization(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(
        render_svg,
        "materialize_source_turnout_paths",
        count_materialization,
    )
    path = TOPOLOGIES / "same_destination_short_overlap.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observed = build_observed_render_plan(graph, resolve_theme(None, graph))
    captured = {}
    real_render_edges = render_svg._render_edges

    def capture_edges(
        drawing, graph, routes, polylines, radii, breaks, theme, *args, **kwargs
    ):
        captured["polylines"] = tuple(tuple(points) for points in polylines)
        captured["radii"] = tuple(
            None if route_radii is None else tuple(route_radii) for route_radii in radii
        )
        return real_render_edges(
            drawing,
            graph,
            routes,
            polylines,
            radii,
            breaks,
            theme,
            *args,
            **kwargs,
        )

    monkeypatch.setattr(render_svg, "_render_edges", capture_edges)
    render_svg.emit_render_plan(observed.plan)
    indices = observed.plan.edge_route_indices

    assert calls == 1
    assert captured["polylines"] == tuple(
        observed.plan.route_polylines[index] for index in indices
    )
    assert captured["radii"] == tuple(
        observed.plan.route_curve_radii[index] for index in indices
    )


def test_invalid_drawable_turnout_shape_raises_project_invariant() -> None:
    route = routing_common.RoutedPath(
        Edge("junction", "target", "line"),
        "line",
        [(100.0, 0.0), (100.0, 100.0)],
        source_turnout=routing_common.SourceTurnout(
            "missing",
            None,
            routing_common.Direction.R,
            routing_common.Direction.D,
            10.0,
        ),
    )

    with pytest.raises(SettledRouteValidationError, match="one incoming member"):
        render_svg.materialize_source_turnout_paths(
            [route], [route.points], default_radius=10.0
        )


def test_short_same_destination_overlap_is_refused_atomically() -> None:
    graph, offsets, routes = _routed("same_destination_short_overlap")
    destination = _destination_routes(graph, routes)
    tails = {
        line_id: port_peeloff_tail(route) for line_id, route in destination.items()
    }

    assert all(tail is not None for tail in tails.values())
    assert {
        (tail.trunk_sign, tail.vertical_sign, tail.port_lead_sign)
        for tail in tails.values()
    } == {(1, 1, 1)}
    overlap = min(tail.port_y for tail in tails.values()) - max(
        tail.trunk_y for tail in tails.values()
    )
    suffix_lo = max(
        min(tail.peel_x, destination[line_id].points[-1][0])
        for line_id, tail in tails.items()
    )
    suffix_hi = min(
        max(tail.peel_x, destination[line_id].points[-1][0])
        for line_id, tail in tails.items()
    )
    suffix_overlap = suffix_hi - suffix_lo
    threshold = 2 * CURVE_RADIUS - COORD_TOLERANCE

    assert overlap == pytest.approx(18.0)
    assert suffix_overlap >= threshold
    assert overlap < threshold
    assert all(
        min(resolve_curve_radii(route.points, route.curve_radii)) == CURVE_RADIUS
        for route in destination.values()
    )

    before = copy.deepcopy(routes)
    routing_normalize._stagger_convergent_distinct_lines(
        routes, _normalization_context(graph, offsets)
    )

    assert routes == before
    assert check_same_destination_approach_bundle(graph, routes) == []


def test_short_same_destination_overlap_settles_as_one_planned_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    measured = _recorded_clearance_measurements(monkeypatch)
    path = TOPOLOGIES / "same_destination_short_overlap.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        baseline_target_y = graph.sections["target"].bbox_y
        source = graph.sections["source"]
        source_bottom = source.bbox_y + source.bbox_h
        first = build_observed_render_plan(graph, resolve_theme(None, graph))
        second = build_observed_render_plan(graph, resolve_theme(None, graph))

    (requirement,), (demand,) = measured[0]

    assert (requirement.negative_section_ids, requirement.positive_section_ids) == (
        ("source",),
        ("target",),
    )
    assert requirement.required == pytest.approx(221.591)
    assert baseline_target_y - source_bottom == pytest.approx(219.591)
    assert demand.deficit == pytest.approx(2.0)
    # Every deficit at or under two quanta settles to the same width, so the
    # coordinate below is the ledger's floor and not a measurement.  The
    # deficit assertion above is what a 1px clearance error has to get past.
    assert quantised_allocation(demand.deficit) == pytest.approx(2.0)
    assert first.plan.graph.sections["target"].bbox_y == pytest.approx(
        baseline_target_y + 2.0
    )
    assert first.route_plan.boundary_clearance_requirements == ()
    assert len(first.route_plan.boundary_clearance_owner_ids) == 1
    assert _plan_decision_fingerprint(first.route_plan) == _plan_decision_fingerprint(
        second.route_plan
    )

    target_id = first.plan.graph.sections["target"].entry_ports[0]
    target_routes = {
        route.line_id: route
        for route in first.plan.routes
        if route.edge.target == target_id
    }
    assert {line_id: route.points for line_id, route in target_routes.items()} == {
        "assembly": (
            (206.0, 120.0),
            (663.0, 120.0),
            (663.0, 567.182),
            (696.0, 567.182),
        ),
        "audit": (
            (630.0, 547.182),
            (659.0, 547.182),
            (659.0, 571.182),
            (696.0, 571.182),
        ),
    }
    assert {
        line_id: resolve_curve_radii(route.points, route.curve_radii)
        for line_id, route in target_routes.items()
    } == {"assembly": [14.0, 10.0], "audit": [10.0, 14.0]}
    assert target_routes["assembly"].points[1][0] - target_routes["audit"].points[1][
        0
    ] == graph_offset_step(graph)

    source_turns = {
        route.line_id: route.source_turnout
        for route in first.plan.routes
        if route.edge.source == "__junction_4"
        and route.edge.target == "scaffolding__entry_top_2"
    }
    assert source_turns["reads"].incoming_source_id == "source__exit_right_0"
    assert source_turns["reads"].continuing_target_id is None
    assert source_turns["reads"].radius == 10.0
    assert source_turns["assembly"].incoming_source_id == "source__exit_right_0"
    assert source_turns["assembly"].continuing_target_id == "target__entry_left_3"
    assert source_turns["assembly"].incoming_direction is routing_common.Direction.R
    assert source_turns["assembly"].outgoing_direction is routing_common.Direction.D
    assert source_turns["assembly"].radius == 14.0
    planned_points = {
        (plan.edge.source, plan.edge.target, plan.edge.line_id): plan.points
        for plan in first.route_plan.member_geometry_plans
    }
    assert all(
        planned_points[(route.edge.source, route.edge.target, route.edge.line_id)]
        == route.points
        for route in target_routes.values()
    )
    planned_turnout = next(
        plan.source_turnout
        for plan in first.route_plan.member_geometry_plans
        if plan.edge.source == "__junction_4"
        and plan.edge.target == "scaffolding__entry_top_2"
        and plan.edge.line_id == "assembly"
    )
    assert (
        planned_turnout.incoming_source_id,
        planned_turnout.continuing_target_id,
        planned_turnout.incoming_direction,
        planned_turnout.outgoing_direction,
        planned_turnout.radius,
    ) == (
        source_turns["assembly"].incoming_source_id,
        source_turns["assembly"].continuing_target_id,
        source_turns["assembly"].incoming_direction,
        source_turns["assembly"].outgoing_direction,
        source_turns["assembly"].radius,
    )


def test_same_row_short_overlap_settles_every_route_system_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-row convergences each publish a requirement, and one move pays both.

    Doubling the fixture gives one row boundary two route systems, each owing
    its own target box a corner runway.  Both requirements have to be measured
    on the live boxes, and the single translation the boundary allows has to be
    the larger of what they ask -- here the same amount, since the copies are
    congruent.
    """
    measured = _recorded_clearance_measurements(monkeypatch)
    # The relocation puts both target boxes a chosen distance short of the
    # runway their corner needs above the source row, so the deficit is a
    # multiple of the settlement quantum rather than the ledger's 2px floor,
    # where any smaller measurement would settle to the same coordinate.
    expected_deficit = 10.0
    relocated_y = 355.591
    path = TOPOLOGIES / "same_destination_short_overlap.mmd"
    header, body = path.read_text().split("graph LR", maxsplit=1)
    replacements = {
        "source_reads": "second_source_reads",
        "source_build": "second_source_build",
        "align_primary": "second_align_primary",
        "align_secondary": "second_align_secondary",
        "target_primary": "second_target_primary",
        "target_secondary": "second_target_secondary",
        "target_summary": "second_target_summary",
        "target_in": "second_target_in",
        "contact_view": "second_contact_view",
        "summary_view": "second_summary_view",
        "map_view": "second_map_view",
        "scaffold": "second_scaffold",
        "source": "second_source",
        "scaffolding": "second_scaffolding",
        "target": "second_target",
        "assembly": "second_assembly",
        "reads": "second_reads",
        "audit": "second_audit",
    }
    second_body = body
    for original, replacement in replacements.items():
        second_body = re.sub(rf"\b{re.escape(original)}\b", replacement, second_body)
    doubled = (
        header
        + "%%metro line: second_assembly | Second assembly | #24B064\n"
        + "%%metro line: second_reads | Second read support | #FA6863\n"
        + "%%metro line: second_audit | Second audit | #8453d7\n"
        + "%%metro grid: second_source | 10,0,2\n"
        + "%%metro grid: second_scaffolding | 10,2,4\n"
        + "%%metro grid: second_target | 14,4,4\n\n"
        + "graph LR"
        + body
        + second_body
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(doubled, source_dir=str(path.parent))
        graph.replace_edges(
            [
                edge
                for edge in graph.edges
                if not (
                    edge.source in graph.ports
                    and edge.target in {"align_secondary", "second_align_secondary"}
                )
            ]
        )
        source_bottom = max(
            graph.sections[section_id].bbox_y + graph.sections[section_id].bbox_h
            for section_id in ("source", "second_source")
        )
        for section_id in ("target", "second_target"):
            section = graph.sections[section_id]
            delta = relocated_y - section.bbox_y
            section.bbox_y += delta
            for station_id in section.station_ids:
                graph.stations[station_id].y += delta
        baseline = {
            section_id: graph.sections[section_id].bbox_y
            for section_id in ("target", "second_target")
        }
        observed = build_observed_render_plan(graph, resolve_theme(None, graph))

    requirements, demands = measured[0]

    assert len({requirement.owner_id for requirement in requirements}) == 2
    assert sorted(requirement.positive_section_ids for requirement in requirements) == [
        ("second_target",),
        ("target",),
    ]
    assert {requirement.negative_section_ids for requirement in requirements} == {
        ("second_source", "source")
    }
    assert relocated_y == pytest.approx(
        source_bottom + requirements[0].required - expected_deficit
    )
    assert len({requirement.required for requirement in requirements}) == 1
    assert [demand.deficit for demand in demands] == [
        pytest.approx(expected_deficit),
        pytest.approx(expected_deficit),
    ]
    assert observed.route_plan.boundary_clearance_requirements == ()
    assert len(observed.route_plan.boundary_clearance_owner_ids) == 2
    assert {
        section_id: observed.plan.graph.sections[section_id].bbox_y
        for section_id in baseline
    } == pytest.approx(
        {section_id: y + expected_deficit for section_id, y in baseline.items()}
    )
    for section_id, line_ids in (
        ("target", ("assembly", "audit")),
        ("second_target", ("second_assembly", "second_audit")),
    ):
        target_id = observed.plan.graph.sections[section_id].entry_ports[0]
        destination = {
            route.line_id: route
            for route in observed.plan.routes
            if route.edge.target == target_id
        }
        assert destination[line_ids[0]].points[1][0] == pytest.approx(
            destination[line_ids[1]].points[1][0] + 4.0
        )
    observed_routes = list(observed.plan.routes)
    assert (
        check_route_segment_crossings(
            observed.plan.graph,
            (dict(observed.plan.station_offsets), observed_routes),
        )
        == []
    )


def test_same_owner_clearance_cohorts_keep_independent_live_gaps() -> None:
    graph = prepare_graph(
        """%%metro grid: near_negative | 0,0
%%metro grid: far_negative | 1,0
%%metro grid: positive_a | 0,1
%%metro grid: positive_b | 1,1
graph LR
    subgraph near_negative [Near negative]
        near_node[Near]
    end
    subgraph far_negative [Far negative]
        far_node[Far]
    end
    subgraph positive_a [Positive A]
        positive_a_node[A]
    end
    subgraph positive_b [Positive B]
        positive_b_node[B]
    end
"""
    )
    graph.sections["near_negative"].bbox_y = 0.0
    graph.sections["near_negative"].bbox_h = 80.0
    graph.sections["far_negative"].bbox_y = -20.0
    graph.sections["far_negative"].bbox_h = 20.0
    graph.sections["positive_a"].bbox_y = 100.0
    graph.sections["positive_b"].bbox_y = 100.0

    requirements = {}
    for requirement in (
        BoundaryClearanceRequirement(
            SettlementAxis.ROW,
            1,
            "shared-owner",
            22.0,
            ("near_negative",),
            ("positive_a",),
            "near cohort",
        ),
        BoundaryClearanceRequirement(
            SettlementAxis.ROW,
            1,
            "shared-owner",
            106.0,
            ("far_negative",),
            ("positive_b",),
            "far cohort",
        ),
    ):
        routing_member_geometry._record_boundary_clearance_requirement(
            requirements, requirement
        )
    recorded = tuple(requirements[key] for key in sorted(requirements))

    assert len(recorded) == 2
    # Each cohort faces its own pair of boxes, so the two gaps are short by
    # different amounts.  The boundary they share can only be widened once, by
    # the larger, which is what makes the smaller one observable here at all.
    assert [
        (demand.description, demand.deficit)
        for demand in measure_boundary_clearance_requirements(graph, recorded)
    ] == [("far cohort", pytest.approx(6.0)), ("near cohort", pytest.approx(2.0))]
    plan = routing_core.observe_route_edges(graph).plan
    settlement = settle_route_envelopes(
        graph,
        replace(plan, reservations=()),
        clearance=partial(
            measure_boundary_clearance_requirements,
            requirements=recorded,
        ),
    )

    assert len(settlement.translations) == 1
    assert settlement.translations[0].amount == pytest.approx(6.0)
    assert settlement.translations[0].clearance.description == "far cohort"
    assert graph.sections["positive_a"].bbox_y == pytest.approx(106.0)
    assert graph.sections["positive_b"].bbox_y == pytest.approx(106.0)


def test_published_clearance_owners_drop_superseded_prior_owner() -> None:
    requirement = BoundaryClearanceRequirement(
        SettlementAxis.ROW,
        1,
        "new-owner",
        20.0,
        ("negative",),
        ("positive",),
        "new cohort",
    )

    assert routing_core._published_boundary_clearance_owner_ids(
        frozenset({"surviving-owner", "superseded-owner"}),
        frozenset(
            {
                RouteSystemId("surviving-owner"),
                RouteSystemId("new-owner"),
            }
        ),
        (requirement,),
    ) == frozenset({"surviving-owner", "new-owner"})


def test_final_checkpoint_observes_the_returned_source_column_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "same_destination_short_overlap.mmd"
    observed_geometry = []
    original = layout_engine._run_after_final_checkpoint

    def spy(graph, section_y_gap, section_y_padding):
        observed_geometry.append(
            (
                graph.ports["scaffolding__entry_top_2"].x,
                graph.stations["__junction_4"].x,
            )
        )
        original(graph, section_y_gap, section_y_padding)

    monkeypatch.setattr(layout_engine, "_run_after_final_checkpoint", spy)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
        layout_engine.compute_layout(graph, validate=True)
    returned_geometry = (
        graph.ports["scaffolding__entry_top_2"].x,
        graph.stations["__junction_4"].x,
    )

    assert observed_geometry
    assert all(geometry == returned_geometry for geometry in observed_geometry)
    assert returned_geometry[0] == returned_geometry[1]
    assert check_station_as_elbow(graph) == []


@pytest.mark.parametrize(
    "ineligible",
    (
        "visible_source",
        "station_source",
        "opposite_tangent",
        "short_incoming_runway",
    ),
)
def test_source_turnout_requires_a_lifecycle_safe_hidden_fork(
    ineligible: str,
) -> None:
    graph, _offsets, routed = _routed("same_destination_short_overlap")
    routes = copy.deepcopy(routed)
    for route in routes:
        route.source_turnout = None
    turn = next(
        route
        for route in routes
        if route.edge.source == "__junction_4"
        and route.edge.target == "scaffolding__entry_top_2"
        and route.line_id == "assembly"
    )
    incoming = next(
        route
        for route in routes
        if route.edge.source == "source__exit_right_0"
        and route.edge.target == "__junction_4"
        and route.line_id == "assembly"
    )
    continuing = next(
        route
        for route in routes
        if route.edge.source == "__junction_4"
        and route.edge.target == "target__entry_left_3"
        and route.line_id == "assembly"
    )

    if ineligible == "visible_source":
        graph.stations["__junction_4"].label = "Visible"
    elif ineligible == "station_source":
        graph.stations["__junction_4"].is_port = False
    elif ineligible == "opposite_tangent":
        continuing.points[1] = (continuing.points[0][0] - 20.0, continuing.points[0][1])
    else:
        incoming.points[-2] = (incoming.points[-1][0] - 5.0, incoming.points[-1][1])

    routing_member_geometry._plan_source_turnouts(routes, graph, CURVE_RADIUS)

    assert turn.source_turnout is None


def test_blocked_riser_seats_both_approach_tails_on_their_slots() -> None:
    """The blocked riser's two approaches reach their port already on their slots.

    Both tails leave one exit fan, which opens its descents one nesting step
    apart, so each approach arrives on the slot its port lane names and the
    exempt, wholly plan-owned riser it shares the gap with stands three offset
    steps clear.  There is nothing for the approach settlement to move, so it
    is a no-op however often it runs.  The refusal path, where a slot move
    would be held by a frozen decision, needs a bundle that moves and is
    witnessed in ``test_route_system_owned_approach_tail_refuses_the_slot_move``.
    """
    graph, offsets, routed = _routed("same_destination_vertical_convergence")
    routes = copy.deepcopy(routed)
    step = graph_offset_step(graph)
    blocker = next(
        route
        for route in routes
        if route.edge.source == "__junction_12"
        and route.edge.target == "s7__entry_right_9"
        and route.line_id == "lower"
    )
    bundle = next(
        routing_common.iter_same_destination_approach_bundles(routes, graph, step)
    )
    slots = routing_common.same_destination_approach_slots(bundle, graph, step)

    assert bundle.per_line["upper"].peel_x == slots["upper"].peel_x == 649.0
    assert bundle.per_line["lower"].peel_x == slots["lower"].peel_x == 653.0
    assert blocker.normalize_exempt
    assert blocker.route_system_owned_segment_ranks == (0, 1, 2, 3, 4)
    assert slots["upper"].peel_x - blocker.points[3][0] == 3 * step

    before = copy.deepcopy(routes)
    context = _normalization_context(graph, offsets)
    routing_normalize._settle_same_destination_approach_bundles(routes, context)
    assert routes == before
    routing_normalize._settle_same_destination_approach_bundles(routes, context)
    assert routes == before
    assert check_same_destination_approach_bundle(graph, routes) == []

    drawn_crossings = check_route_segment_crossings(graph, (dict(offsets), routed))
    settled_crossings = check_route_segment_crossings(graph, (dict(offsets), routes))

    assert {violation.message for violation in settled_crossings} == {
        violation.message for violation in drawn_crossings
    }
    assert all(
        "__junction_12->target__entry_left_8" not in violation.message
        for violation in settled_crossings
    )


def test_route_system_owned_approach_tail_refuses_the_slot_move() -> None:
    """A frozen riser refuses the whole bundle's slot move rather than half of it.

    ``upper`` owns every segment of its approach, so once the bundle asks it to
    change channel the move is held and no member is repositioned at all.
    Releasing that ownership on the same bundle seats both members on the slots
    their port lanes name, which is what makes the refusal attributable to the
    ownership and not to the geometry the move would land on.
    """
    graph, _offsets, routed = _routed("same_destination_vertical_convergence")
    routes, bundle, slots = _displaced_approach_bundle(graph, routed)
    held = next(route for route, _tail in bundle.entries if route.line_id == "upper")

    assert len(held.points) - 3 in held.route_system_owned_segment_ranks
    assert bundle.per_line["upper"].peel_x != slots["upper"].peel_x
    assert (
        routing_common.feasible_same_destination_approach_proposals(
            graph, routes, bundle, slots
        )
        is None
    )

    released, released_bundle, released_slots = _displaced_approach_bundle(
        graph, routed
    )
    _release_plan_ownership(released_bundle)
    proposals = routing_common.feasible_same_destination_approach_proposals(
        graph, released, released_bundle, released_slots
    )

    assert proposals is not None
    assert len(proposals) == 2
    assert {
        proposal.route.line_id: proposal.points[-3][0] for proposal in proposals
    } == {line_id: slot.peel_x for line_id, slot in released_slots.items()}


def test_settled_destination_axis_lands_in_realised_reservation_band() -> None:
    path = TOPOLOGIES / "same_destination_short_overlap.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    observed = build_observed_render_plan(graph, resolve_theme(None, graph))
    target_id = observed.plan.graph.sections["target"].entry_ports[0]
    route_rank, route = next(
        (rank, route)
        for rank, route in enumerate(observed.plan.routes)
        if route.edge.target == target_id and route.line_id == "audit"
    )
    segment_rank = len(route.points) - 3
    reservation = next(
        reservation
        for reservation in observed.route_plan.reservations
        if any(
            claim.path_rank == route_rank
            and claim.segment_rank <= segment_rank <= claim.segment_end_rank
            for claim in reservation.claims
        )
    )
    realised = build_route_plan_query(observed.route_plan).realised_reservation(
        reservation.id
    )

    assert realised is not None
    coordinate = observed.plan.route_polylines[route_rank][segment_rank][0]
    assert (
        realised.region_start + realised.negative_side_clearance
        <= coordinate
        <= realised.region_end - realised.positive_side_clearance
    )


# (fixture stem, section, the strike-clearance gaps that section buys).  Both
# fixtures draw an outgoing diagonal over a horizontal label and settle it by
# growing the gap after the struck station's own layer.
_OUTGOING_DIAGONAL_LABEL_CASES = [
    ("same_destination_short_overlap", "scaffolding", "scaffold", {2: 1}),
    ("multirow_source_stacked_fan", "align_sec", "star", {3: 1}),
]


@pytest.mark.parametrize(
    "stem,section_id,struck_id,strike_gaps", _OUTGOING_DIAGONAL_LABEL_CASES
)
def test_same_destination_witness_clears_outgoing_diagonal_label(
    stem: str, section_id: str, struck_id: str, strike_gaps: dict[int, int]
) -> None:
    graph, _offsets, _routes = _routed(stem)

    assert graph.sections[section_id].label_strike_layer_gaps == strike_gaps
    assert layout_engine._strike_growable_target(graph, struck_id) == (
        graph.sections[section_id],
        graph.stations[struck_id].layer,
    )
    _guard_no_diagonal_strikes_horizontal_label(graph, "test")


def test_following_gap_lever_is_reached_for_only_on_the_strike_retry() -> None:
    """The first attempt offers a station's own gap; only the retry adds the next.

    Where growing the struck station's own gap clears the strike, the following
    column is never offered and never grown.  On a fixture whose first attempt
    fails to reduce the issue count, the retry widens the offer to the gap after
    the struck layer, and that is the column the settled section ends up buying
    -- a column the first attempt's lever set provably does not name.
    """
    clear = parse_metro_mermaid(
        """%%metro line: line | Line | #0570b0
graph LR
subgraph lane [Lane]
    source[Source] -->|line| target[Target]
    target -->|line| sink[Sink]
end
"""
    )
    layout_engine.compute_layout(clear)
    target_layer = clear.stations["target"].layer

    assert layout_engine._strike_growable_target(clear, "target") == (
        clear.sections["lane"],
        target_layer,
    )
    assert ("gap", "lane", target_layer) in layout_engine._label_strike_levers(
        clear, "target", set()
    )
    assert ("gap", "lane", target_layer + 1) not in layout_engine._label_strike_levers(
        clear, "target", set()
    )
    assert clear.sections["lane"].label_strike_layer_gaps == {}

    retried, _offsets, _routes = _routed("same_destination_short_overlap")
    struck_layer = retried.stations["scaffold"].layer
    first_attempt = layout_engine._label_strike_levers(retried, "scaffold", set())

    assert ("gap", "scaffolding", struck_layer) in first_attempt
    assert ("gap", "scaffolding", struck_layer + 1) not in first_attempt
    assert retried.sections["scaffolding"].label_strike_layer_gaps == {
        struck_layer + 1: 1
    }


def test_mixed_side_partition_is_independent_of_route_order() -> None:
    """A moving bundle proposes the same geometry whichever way its routes iterate.

    The conflict and clearance scans read the whole population, whose entry
    ports alternate sides, so reversing it reverses what those walks see first.
    Both orders are given a bundle that must change channel, which is what puts
    the scans in play at all.
    """
    graph, _offsets, routed = _routed("same_destination_vertical_convergence")
    side_runs: list[PortSide] = []
    for route in routed:
        port = graph.ports.get(route.edge.target)
        if port is None or not route.is_inter_section:
            continue
        if not side_runs or side_runs[-1] is not port.side:
            side_runs.append(port.side)
    assert any(
        side_runs[index : index + 3] == [PortSide.LEFT, PortSide.RIGHT, PortSide.LEFT]
        for index in range(len(side_runs) - 2)
    )

    partitions: list[frozenset[tuple[str, tuple[tuple[float, float], ...]]]] = []
    for reverse in (False, True):
        population, bundle, slots = _displaced_approach_bundle(
            graph, routed, reverse=reverse
        )
        _release_plan_ownership(bundle)
        proposals = routing_common.feasible_same_destination_approach_proposals(
            graph, population, bundle, slots
        )
        assert proposals is not None
        assert any(
            proposal.points != tuple(proposal.route.points) for proposal in proposals
        )
        partitions.append(
            frozenset(
                (proposal.route.line_id, tuple(proposal.points))
                for proposal in proposals
            )
        )

    assert len(partitions[0]) == 2
    assert partitions[0] == partitions[1]


def test_zero_delta_approach_bundle_skips_the_conflict_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle whose tails already sit on their slots costs no conflict scan.

    Every proposal reproduces its own route's points, so the co-travelling
    clearance walk would read geometry already drawn and the conflict pair would
    difference that geometry against itself. Both are skipped. Displacing one
    tail off the drawn point puts the proposals back in scanning territory, so
    the skip is keyed to the zero delta and not to this bundle.
    """
    graph, _offsets, routed = _routed("same_destination_vertical_convergence")
    routes = copy.deepcopy(routed)
    step = graph_offset_step(graph)
    bundle = next(
        routing_common.iter_same_destination_approach_bundles(routes, graph, step)
    )
    slots = routing_common.same_destination_approach_slots(bundle, graph, step)
    scans = 0
    real_conflicts = routing_common._same_destination_conflicts

    def counted_conflicts(*args, **kwargs):
        nonlocal scans
        scans += 1
        return real_conflicts(*args, **kwargs)

    monkeypatch.setattr(
        routing_common, "_same_destination_conflicts", counted_conflicts
    )

    proposals = routing_common.feasible_same_destination_approach_proposals(
        graph, routes, bundle, slots
    )

    assert proposals is not None
    assert all(
        proposal.points == tuple(proposal.route.points) for proposal in proposals
    )
    assert scans == 0

    displaced = proposals[0].route
    segment_rank = len(displaced.points) - 3
    displaced.points[segment_rank] = (
        displaced.points[segment_rank][0] + step,
        displaced.points[segment_rank][1],
    )
    routing_common.feasible_same_destination_approach_proposals(
        graph, routes, bundle, slots
    )

    assert scans == 2
