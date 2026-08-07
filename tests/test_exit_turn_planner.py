"""Pre-routing exit-turn plans own complete source-bundle geometry."""

from __future__ import annotations

import copy
import dataclasses
import warnings
from collections.abc import Mapping
from dataclasses import replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import pytest

import nf_metro.layout.routing.exit_turns as exit_turns
import nf_metro.layout.routing.inter_section_handlers as inter_handlers
import nf_metro.layout.routing.offsets as routing_offsets
from nf_metro.api import prepare_graph, render_string, resolve_theme
from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame
from nf_metro.layout.route_plan import (
    CoordinateRegime,
    DemandAxis,
    DemandKind,
    ExitLaneOrderSource,
    ExitTurnDisposition,
    RouteFamilyId,
    RouteSystemDisposition,
    RouteSystemId,
    SharedReferenceKind,
    build_route_plan_query,
)
from nf_metro.layout.route_reservations import (
    CorridorOrientation,
    expected_exit_turn_foreign_references,
)
from nf_metro.layout.routing import (
    compute_station_offsets,
    observe_route_edges,
    route_edges,
)
from nf_metro.layout.routing.common import (
    Direction,
    OffsetRegime,
    apply_route_offsets,
)
from nf_metro.layout.routing.context import _build_routing_context
from nf_metro.layout.routing.corners import resolve_curve_radii
from nf_metro.layout.routing.exit_turns import (
    ExitTurnInvariantError,
    assert_exit_turn_snapshot,
    snapshot_exit_turn_segments,
    validate_exit_turn_plans,
)
from nf_metro.layout.routing.invariants import check_planned_fan_landing_radius
from nf_metro.layout.routing.postprocess import _build_bubble_ctx
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import LineSpread, PortSide
from nf_metro.parser.route_topology import build_route_topology_query
from nf_metro.render.plan import freeze_render_value
from nf_metro.render.svg import station_marker_box
from nf_metro.themes import NFCORE_THEME

ROOT = Path(__file__).parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
FIXTURES = ROOT / "tests" / "fixtures"
FROZEN = FIXTURES / "hash_seed_determinism"
REDUCED = (
    TOPOLOGIES / "leftward_up_exit_turn_order.mmd",
    TOPOLOGIES / "terminated_exit_lane_compaction.mmd",
)
STRICT_RENDER_REGRESSIONS = (
    TOPOLOGIES / "rail_symmetric_fork_join_spans.mmd",
    TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd",
    FIXTURES / "rail_marked_single_line.mmd",
    FIXTURES / "tb_right_exit_feeder_slots.mmd",
    FIXTURES / "ambiguous_exit_continuation.mmd",
    FIXTURES / "compact_continuation_slot_conflict.mmd",
)


def _observe(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    return graph, offsets, observation


def _build_execution(path: Path, *, offset_step: float | None = None):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph, offset_step=offset_step)
    original_offsets = dict(offsets)
    ctx = _build_routing_context(
        graph,
        DIAGONAL_RUN,
        CURVE_RADIUS,
        offsets,
        offset_step=offset_step,
    )
    execution = exit_turns.build_exit_turn_execution(graph, ctx)
    return graph, offsets, original_offsets, execution


def _assert_recursively_immutable(value: object) -> None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = type(value).__dataclass_params__
        assert params.frozen
        assert "__slots__" in vars(type(value))
        for field in dataclasses.fields(value):
            _assert_recursively_immutable(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            _assert_recursively_immutable(item)
    elif isinstance(value, Mapping):
        assert isinstance(value, MappingProxyType)
        for key, item in value.items():
            _assert_recursively_immutable(key)
            _assert_recursively_immutable(item)
    elif not isinstance(value, (str, int, float, bool, Enum, type(None))):
        pytest.fail(f"retained mutable or unsupported {type(value).__name__}")


def test_exit_turn_execution_is_recursively_immutable() -> None:
    _graph, _offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )

    _assert_recursively_immutable(execution)


def _provisional_groups(path: Path):
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    scaffold = exit_turns.build_route_semantic_scaffold(graph, ctx.topology)
    assert scaffold is not None
    groups = exit_turns._build_group_plans(
        graph,
        ctx,
        scaffold,
        exit_turns._build_planner_indexes(scaffold),
        exit_turns._plan_provenance(graph, scaffold.topology.connectors),
    )
    return graph, offsets, groups


def _plan_for_source(observation, source_id: str):
    query = build_route_plan_query(observation.plan)
    (plan,) = query.exit_turn_plans_for_source(source_id)
    return plan


def _turn_x(route, offsets) -> float:
    points = apply_route_offsets(route, offsets)
    rank = route.exit_turn_segment_rank
    assert rank is not None
    first = points[rank]
    second = points[rank + 1]
    assert first[0] == pytest.approx(second[0])
    return first[0]


def _turn_y(route, offsets) -> float:
    points = apply_route_offsets(route, offsets)
    rank = route.exit_turn_segment_rank
    assert rank is not None
    first = points[rank]
    second = points[rank + 1]
    assert first[1] == pytest.approx(second[1])
    return first[1]


def test_three_family_exit_bundle_has_one_complete_turn_plan() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")

    assert plan.disposition is ExitTurnDisposition.PLANNED
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )
    assert plan.system_member_ids == system.member_ids
    assert len(plan.system_member_ids) > len(plan.member_ids)
    assert tuple(lane.line_id for lane in plan.source_lanes) == (
        "main",
        "report",
        "sheets",
    )
    assert set(plan.member_ids) >= {
        assignment.member_id for assignment in plan.assignments
    }
    family_by_line = {
        next(
            lane.line_id
            for lane in plan.source_lanes
            if assignment.member_id in lane.member_ids
        ): assignment.planned_family_id
        for assignment in plan.assignments
    }
    assert family_by_line == {
        "main": RouteFamilyId.STANDARD_L_SHAPE,
        "report": RouteFamilyId.MERGE_BRANCH,
        "sheets": RouteFamilyId.SAME_Y_STRAIGHT,
    }
    destination_by_line = {
        next(
            lane.line_id
            for lane in plan.source_lanes
            if assignment.member_id in lane.member_ids
        ): (
            assignment.destination_column,
            assignment.destination_row,
            assignment.destination_side,
        )
        for assignment in plan.assignments
    }
    assert destination_by_line == {
        "main": (3, 1, PortSide.LEFT),
        "report": (3, 1, PortSide.LEFT),
        "sheets": (3, 0, PortSide.LEFT),
    }

    axes = {axis.line_id: axis for axis in plan.axes}
    assert axes["report"].fixed_anchor_id == "__merge_4"
    assert axes["report"].fixed_anchor_offset == pytest.approx(
        offsets[("__merge_4", "report")]
    )
    assert axes["report"].coordinate == pytest.approx(
        graph.stations["__merge_4"].x + offsets[("__merge_4", "report")]
    )
    assert axes["main"].coordinate > axes["report"].coordinate
    plan_query = build_route_plan_query(observation.plan)
    assert plan.reference_id is not None
    assert (
        plan_query.shared_reference(plan.reference_id).kind
        is SharedReferenceKind.ORDERED_TURNS
    )
    assert tuple(plan_query.demand(item).kind for item in plan.demand_ids) == (
        DemandKind.ORDERED_TURNS,
        DemandKind.RUNWAY,
    )
    routes = {
        route.line_id: route
        for route in observation.routes
        if route.edge.source == plan.source_id and route.exit_turn_axis_id is not None
    }
    assert _turn_x(routes["main"], offsets) == pytest.approx(axes["main"].coordinate)
    assert _turn_x(routes["report"], offsets) == pytest.approx(
        axes["report"].coordinate
    )
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


@pytest.mark.parametrize(
    "path",
    (
        ROOT / "examples" / "guide" / "03_fan_out.mmd",
        TOPOLOGIES / "wide_fan_out.mmd",
    ),
    ids=("guide-fan-out", "wide-fan-out"),
)
def test_planned_fan_axis_keeps_a_full_landing_curve(path: Path) -> None:
    """A shared fan opening does not donate its target-side curve radius."""
    graph, offsets, observation = _observe(path)
    plan = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.PLANNED
        and item.source_id in graph.junction_ids
        and len(item.axes) >= 2
    )
    turning = [
        route
        for route in observation.routes
        if route.exit_turn_plan_id == str(plan.id)
        and route.exit_turn_segment_rank is not None
    ]

    assert len(turning) >= 2
    source_arc_centres = []
    for route in turning:
        rank = route.exit_turn_segment_rank
        assert rank is not None
        points = apply_route_offsets(route, offsets)
        radii = resolve_curve_radii(points, route.curve_radii)
        source_radius = radii[rank - 1]
        landing_radius = radii[rank]
        source_corner = points[rank]
        source_arc_centres.append(
            (source_corner[0] - source_radius, source_corner[1] + source_radius)
        )

        assert landing_radius >= CURVE_RADIUS
        assert route.curve_radii is not None
        assert route.curve_radii[rank] >= CURVE_RADIUS
        assert abs(points[rank + 2][0] - points[rank + 1][0]) >= landing_radius

    first_centre = source_arc_centres[0]
    assert all(centre == pytest.approx(first_centre) for centre in source_arc_centres)
    assert not check_planned_fan_landing_radius(
        graph,
        observation.routes,
        offsets,
    )


def test_planned_fan_landing_radius_rejects_a_compressed_request() -> None:
    graph, offsets, observation = _observe(TOPOLOGIES / "wide_fan_out.mmd")
    route = next(
        route
        for route in observation.routes
        if route.edge.source in graph.junction_ids
        and route.exit_turn_plan_id is not None
        and route.exit_turn_segment_rank is not None
    )
    rank = route.exit_turn_segment_rank
    assert rank is not None
    assert route.curve_radii is not None
    route.curve_radii[rank] = CURVE_RADIUS / 5

    violations = check_planned_fan_landing_radius(
        graph,
        observation.routes,
        offsets,
    )

    assert {violation.line_id for violation in violations} == {route.line_id}


def test_shared_destination_entry_keeps_target_bundle_concentric() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "multi_frame_exit_lane_settlement.mmd"
    )
    routes = [
        route
        for route in observation.routes
        if route.edge.source == "side_work__exit_left_5"
        and route.edge.target == "side_report__entry_right_14"
    ]
    assignments = {
        str(assignment.member_id): assignment
        for plan in observation.plan.exit_turn_plans
        for assignment in plan.assignments
    }

    assert {route.line_id for route in routes} == {"side_a", "side_b"}
    assert (
        len({assignments[route.exit_turn_member_id].entry_group_id for route in routes})
        == 1
    )

    target_arc_centres = []
    for route in routes:
        rank = route.exit_turn_segment_rank
        assert rank is not None
        points = apply_route_offsets(route, offsets)
        radii = resolve_curve_radii(points, route.curve_radii)
        before, corner, after = points[rank : rank + 3]
        assert before[0] == pytest.approx(corner[0])
        assert after[1] == pytest.approx(corner[1])
        radius = radii[rank]
        target_arc_centres.append((corner[0] - radius, corner[1] + radius))

    assert target_arc_centres[0] == pytest.approx(target_arc_centres[1])


def test_merge_feeder_does_not_compress_a_terminal_landing_curve() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "merge_feeders_three_columns.mmd"
    )
    route = next(
        route
        for route in observation.routes
        if route.edge.source == "__junction_9"
        and route.edge.target == "e__entry_left_5"
        and route.line_id == "main"
    )
    rank = route.exit_turn_segment_rank
    assert rank is not None
    assert route.curve_radii is not None

    points = apply_route_offsets(route, offsets)
    radii = resolve_curve_radii(points, route.curve_radii)

    assert radii[rank] >= CURVE_RADIUS
    assert not check_planned_fan_landing_radius(
        graph,
        observation.routes,
        offsets,
    )


def test_leftward_upturn_preserves_source_lane_order() -> None:
    graph, offsets, observation = _observe(FROZEN / "seed_72.mmd")
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        FROZEN / "seed_72.mmd"
    )
    plan = next(item for item in execution.plans if item.source_id == "s7__exit_left_5")
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    assert system.compatibility_reasons
    assert tuple(lane.line_id for lane in plan.source_lanes) == ("l6", "l2")
    axes = {axis.line_id: axis.coordinate for axis in plan.axes}
    assert axes["l6"] < axes["l2"]
    routes = {
        route.line_id: route
        for route in observation.routes
        if route.edge.source == plan.source_id
    }
    assert all(
        route.route_system_disposition == "compatibility" for route in routes.values()
    )
    assert all(route.exit_turn_axis_id is None for route in routes.values())
    turn_x = {
        line_id: apply_route_offsets(route, offsets)[1][0]
        for line_id, route in routes.items()
    }
    assert turn_x["l6"] < turn_x["l2"]
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_compacted_leftward_source_stays_planned_as_a_straight_group() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "leftward_up_exit_turn_order.mmd"
    )
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "leftward_up_exit_turn_order.mmd"
    )
    plan = next(
        item for item in execution.plans if item.source_id == "source__exit_left_3"
    )

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert tuple(lane.line_id for lane in plan.source_lanes) == ("branch", "shared")
    assert all(
        assignment.run_direction is Direction.L
        and assignment.turn_direction is None
        and assignment.planned_family_id is RouteFamilyId.SAME_Y_STRAIGHT
        for assignment in plan.assignments
    )
    assert not plan.axes
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_linear_leftward_source_uses_one_secondary_track() -> None:
    graph = prepare_graph(
        (TOPOLOGIES / "leftward_up_exit_turn_order.mmd").read_text(),
        source_dir=str(TOPOLOGIES),
    )
    section = graph.sections["source"]
    frame = AxisFrame.for_direction(section.direction, 1.0, 1.0)

    secondary = {
        frame.secondary.get(graph.stations[station_id])
        for station_id in ("source_a", "source_b", "shared_step", "split")
    }

    assert len(secondary) == 1


def test_linear_entry_frame_requires_one_upstream_carrier() -> None:
    text = """
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
%%metro grid: upstream_a | 0,0
%%metro grid: upstream_b | 0,1
%%metro grid: target | 1,0
graph LR
    subgraph upstream_a [Upstream A]
        first_source[First]
    end
    subgraph upstream_b [Upstream B]
        second_source[Second]
    end
    subgraph target [Target]
        %%metro entry: left | first, second
        %%metro exit: right | first, second
        target_step[Target]
    end
    first_source -->|first| target_step
    second_source -->|second| target_step
"""
    graph = prepare_graph(text)
    offsets = compute_station_offsets(graph)

    ownership = routing_offsets.capture_linear_entry_frame_ownership(graph, offsets)

    assert not any(
        assignment.section_id == "target" for assignment in ownership.assignments
    )
    assert not any(
        station_id in graph._linear_entry_pill_lines_cache
        for station_id in graph.sections["target"].station_ids
    )


def test_linear_entry_frame_settlement_restores_offsets_if_owner_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = parse_metro_mermaid(
        """
%%metro line: line | Line | #3779b1
graph LR
    subgraph section [Section]
        first[First]
        second[Second]
        first -->|line| second
    end
"""
    )
    ctx = routing_offsets._build_offset_ctx(graph, 4.0)
    ctx.offsets[("first", "line")] = 0.0
    calls = 0

    def transient_frame(_ctx, section):
        nonlocal calls
        calls += 1
        if calls > 1:
            return None
        return routing_offsets._LinearEntryFrame(
            section_id=section.id,
            entry_port_id="entry",
            feeder_section_id="feeder",
            feeder_station_id="source",
            continuing=(("line", 12.0),),
            assignments=(("line", 12.0),),
            carrier_ids=("first",),
        )

    monkeypatch.setattr(routing_offsets, "_linear_entry_frame", transient_frame)

    assert routing_offsets._materialize_linear_entry_frames(ctx) == ()
    assert ctx.offsets == {("first", "line"): 0.0}


def test_exit_turn_planner_reuses_fan_semantic_scaffold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "bypass_v_tight.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    fan_execution = graph.fan_plan_execution
    assert fan_execution is not None
    assert fan_execution.scaffold is not None
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)

    def reject_rebuild(*_args, **_kwargs):
        pytest.fail("canonical route semantic scaffold was rebuilt")

    monkeypatch.setattr(exit_turns, "build_route_semantic_scaffold", reject_rebuild)

    execution = exit_turns.build_exit_turn_execution(graph, ctx)

    assert execution.scaffold is fan_execution.scaffold


def test_linear_entry_frame_requires_a_materialized_upstream_slot() -> None:
    path = TOPOLOGIES / "target_lane_transition.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    entry_port = graph.sections["source"].entry_ports[0]
    topology = build_route_topology_query(graph)
    connector_id = next(
        connector_id
        for connector_id in topology.connector_ids_for_port(entry_port)
        if topology.connector(connector_id).line_id == "third"
    )
    exit_port = topology.exit_port(topology.connector(connector_id).exit_group_id)
    offsets.pop((exit_port, "third"))

    ownership = routing_offsets.capture_linear_entry_frame_ownership(graph, offsets)

    assert not any(
        assignment.section_id == "source" for assignment in ownership.assignments
    )


def test_linear_entry_frame_excludes_far_side_entry_routes() -> None:
    path = TOPOLOGIES / "bypass_leftward_far_side_entry.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)

    ownership = routing_offsets.capture_linear_entry_frame_ownership(graph, offsets)

    assert not any(
        assignment.section_id == "tgt_sec" for assignment in ownership.assignments
    )
    assert [offsets[("tgt_sec__entry_left_1", f"l{rank}")] for rank in range(1, 8)] == [
        24.0,
        20.0,
        16.0,
        12.0,
        8.0,
        4.0,
        0.0,
    ]


def test_exit_plan_falls_back_before_changing_a_linear_entry_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = exit_turns._build_group_plans

    def conflict(*args, **kwargs):
        built = list(real_build(*args, **kwargs))
        for rank, item in enumerate(built):
            if item.plan.source_id != "__junction_7":
                continue
            lanes = tuple(
                replace(lane, planned_offset=lane.planned_offset + 4.0)
                if lane.line_id == "third"
                else lane
                for lane in item.plan.source_lanes
            )
            built[rank] = replace(item, plan=replace(item.plan, source_lanes=lanes))
        return tuple(built)

    monkeypatch.setattr(exit_turns, "_build_group_plans", conflict)
    _graph, offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "target_lane_transition.mmd"
    )
    plan = next(item for item in execution.plans if item.source_id == "__junction_7")
    affected = [item for item in execution.plans if item.system_id == plan.system_id]

    assert all(item.disposition is ExitTurnDisposition.LEGACY for item in affected)
    assert all(
        item.legacy_reason == "linear-entry-frame-ownership-conflict"
        for item in affected
    )
    assert offsets[("source__exit_right_1", "third")] == pytest.approx(8.0)


def test_exit_plan_publication_is_transactional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "target_lane_transition.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    original = dict(offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)

    def reject_publication(
        trial: dict[tuple[str, str], float],
        ownership: routing_offsets.LinearEntryFrameOwnership,
    ) -> None:
        del ownership
        assert trial is not offsets
        assert trial != offsets
        raise routing_offsets.LaneFrameInvariantError("rejected trial publication")

    monkeypatch.setattr(
        exit_turns, "validate_linear_entry_frame_ownership", reject_publication
    )

    with pytest.raises(
        routing_offsets.LaneFrameInvariantError, match="rejected trial publication"
    ):
        exit_turns.build_exit_turn_execution(graph, ctx)

    assert offsets == original


@pytest.mark.parametrize(
    ("fixture", "section_id", "line_ids"),
    (
        ("exit_lane_settlement_without_crossings.mmd", "source", ("wrap",)),
        ("exit_turn_frame_filters.mmd", "seam_target", ("seam_a", "seam_b")),
        ("external_owner_exit_lane_frame.mmd", "feeder", ("straight", "wrap")),
        (
            "external_owner_exit_lane_frame.mmd",
            "source",
            ("lower", "straight", "wrap"),
        ),
        ("multi_frame_exit_lane_settlement.mmd", "source", ("wrap",)),
        ("target_lane_transition.mmd", "source", ("first", "second", "third")),
    ),
)
def test_linear_entry_cohort_keeps_one_lane_frame(
    fixture: str,
    section_id: str,
    line_ids: tuple[str, ...],
) -> None:
    graph, offsets, observation = _observe(TOPOLOGIES / fixture)
    section = graph.sections[section_id]

    for line_id in line_ids:
        carriers = [
            station_id
            for station_id in section.station_ids
            if line_id in graph.station_lines(station_id)
        ]
        assert len(carriers) >= 3
        assert {offsets[(station_id, line_id)] for station_id in carriers} == {
            offsets[(carriers[0], line_id)]
        }
        assert all(
            transition.edge.line_id != line_id
            or graph.stations[transition.edge.source].section_id != section_id
            or graph.stations[transition.edge.target].section_id != section_id
            for plan in observation.plan.exit_turn_plans
            for transition in plan.lane_transitions
        )

    for station_id in section.station_ids:
        active = [
            offsets[(station_id, line_id)]
            for line_id in graph.station_lines(station_id)
            if (station_id, line_id) in offsets
        ]
        levels = sorted(set(active))
        if len(levels) < 2:
            continue
        assert levels == pytest.approx(
            [levels[0] + rank * 4.0 for rank in range(len(levels))]
        )


def test_adjacent_local_terminator_does_not_inflate_entry_frame_pills() -> None:
    graph, offsets, _observation = _observe(
        TOPOLOGIES / "external_owner_exit_lane_frame.mmd"
    )
    inherited = ("lower", "straight", "wrap")
    inherited_span = max(offsets["before", line_id] for line_id in inherited) - min(
        offsets["before", line_id] for line_id in inherited
    )

    for station_id in ("before", "split"):
        _cx, _cy, _width, height, _radius = station_marker_box(
            graph, NFCORE_THEME, graph.stations[station_id], offsets
        )
        assert height == pytest.approx(inherited_span + 2 * NFCORE_THEME.station_radius)


def test_station_offset_rebuild_clears_entry_pill_metadata_for_rail_mode() -> None:
    graph = prepare_graph(
        (TOPOLOGIES / "external_owner_exit_lane_frame.mmd").read_text(),
        source_dir=str(TOPOLOGIES),
    )
    compute_station_offsets(graph)
    assert graph._linear_entry_pill_lines_cache

    graph.line_spread = LineSpread.RAILS
    assert compute_station_offsets(graph) == {}
    assert graph._linear_entry_pill_lines_cache == {}


@pytest.mark.parametrize(
    ("direction", "entry_side", "exit_side", "grid"),
    (
        ("LR", "left", "right", ((0, 0), (1, 0), (2, 0))),
        ("RL", "right", "left", ((2, 0), (1, 0), (0, 0))),
        ("TB", "top", "bottom", ((0, 0), (0, 1), (0, 2))),
        ("BT", "bottom", "top", ((0, 2), (0, 1), (0, 0))),
    ),
)
def test_linear_entry_frames_are_axis_generic(
    direction: str,
    entry_side: str,
    exit_side: str,
    grid: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
) -> None:
    feeder_grid, target_grid, sink_grid = grid
    text = f"""
%%metro line: first | First | #3779b1
%%metro line: local | Local | #dde6c4
%%metro line: second | Second | #6ef362
%%metro grid: feeder | {feeder_grid[0]},{feeder_grid[1]}
%%metro grid: target | {target_grid[0]},{target_grid[1]}
%%metro grid: sink | {sink_grid[0]},{sink_grid[1]}

graph LR
    subgraph feeder [Feeder]
        %%metro direction: {direction}
        %%metro exit: {exit_side} | first, second
        feed[Feed]
    end
    subgraph target [Target]
        %%metro direction: {direction}
        %%metro entry: {entry_side} | first, second
        %%metro exit: {exit_side} | first, second
        enter[Enter]
        leave[Leave]
        enter -->|local| leave
    end
    subgraph sink [Sink]
        %%metro direction: {direction}
        %%metro entry: {entry_side} | first, second
        done[Done]
    end
    feed -->|first,second| enter
    leave -->|first,second| done
"""
    graph = prepare_graph(text)
    offsets = compute_station_offsets(graph)
    section = graph.sections["target"]

    for line_id in ("first", "second"):
        carriers = [
            station_id
            for station_id in section.station_ids
            if line_id in graph.station_lines(station_id)
        ]
        assert len({offsets[station_id, line_id] for station_id in carriers}) == 1


def test_linear_entry_frame_runtime_guard_rejects_owner_drift() -> None:
    path = TOPOLOGIES / "target_lane_transition.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    ownership = routing_offsets.capture_linear_entry_frame_ownership(graph, offsets)
    assert ownership.assignments
    offsets[("enter", "third")] += 4.0

    with pytest.raises(routing_offsets.LaneFrameInvariantError):
        routing_offsets.validate_linear_entry_frame_ownership(offsets, ownership)


def test_terminated_source_lane_does_not_leave_a_phantom_slot() -> None:
    _graph, offsets, observation = _observe(FROZEN / "seed_77.mmd")
    plan = _plan_for_source(observation, "__junction_37")

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert tuple(lane.line_id for lane in plan.source_lanes) == ("l0", "l1", "l4")
    assert tuple(lane.input_offset for lane in plan.source_lanes) == (0.0, 4.0, 12.0)
    assert tuple(lane.planned_offset for lane in plan.source_lanes) == (0.0, 4.0, 8.0)
    assert offsets[("s2__exit_right_2", "l4")] == pytest.approx(8.0)
    assert offsets[("__junction_37", "l4")] == pytest.approx(8.0)
    assert offsets[("n2_1", "l4")] == pytest.approx(8.0)
    assert all(
        transition.edge.target != "s2__exit_right_2" or transition.edge.line_id != "l4"
        for transition in plan.lane_transitions
    )
    assert all(lane.line_id != "l3" for lane in plan.source_lanes)
    route = next(
        item
        for item in observation.routes
        if item.edge.source == "s2__exit_right_2"
        and item.edge.target == "__junction_37"
        and item.line_id == "l4"
    )
    points = apply_route_offsets(route, offsets)
    assert points[0][1] == pytest.approx(points[-1][1])
    l1 = next(
        item
        for item in observation.routes
        if item.edge.source == "__junction_37" and item.line_id == "l1"
    )
    assert l1.normalize_exempt
    assert l1.exit_turn_plan_id == str(plan.id)
    assert any(
        assignment.member_id == l1.exit_turn_member_id
        for assignment in plan.assignments
    )


def test_compacted_straight_continuation_keeps_its_lane_across_the_seam() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "terminated_exit_lane_compaction.mmd"
    )
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "terminated_exit_lane_compaction.mmd"
    )
    plans = {
        plan.source_id: plan
        for plan in execution.plans
        if any(lane.line_id == "straight" for lane in plan.source_lanes)
    }

    assert set(plans) == {"__junction_8", "__junction_9"}
    assert all(
        plan.disposition is ExitTurnDisposition.PLANNED for plan in plans.values()
    )
    assert all(
        plan.lane_order_source is ExitLaneOrderSource.STATION_OFFSETS
        for plan in plans.values()
    )
    assert all(
        tuple(lane.line_id for lane in plan.source_lanes)
        == ("lower", "wrap", "straight")
        for plan in plans.values()
    )
    assert all(
        tuple(lane.planned_offset for lane in plan.source_lanes)
        == pytest.approx((0.0, 4.0, 8.0))
        for plan in plans.values()
    )
    assert all(
        transition.edge.line_id != "straight"
        for plan in plans.values()
        for transition in plan.lane_transitions
    )
    assert offsets[("before", "straight")] == pytest.approx(8.0)
    assert offsets[("split", "straight")] == pytest.approx(8.0)
    assert offsets[("before", "ends")] == pytest.approx(12.0)
    assert offsets[("split", "ends")] == pytest.approx(12.0)
    assert offsets[("prelude_b", "wrap")] == pytest.approx(offsets[("feed", "wrap")])

    route_ys = {
        round(y, 6)
        for route in observation.routes
        if route.line_id == "straight"
        for _x, y in apply_route_offsets(route, offsets)
    }
    assert route_ys == {
        round(
            graph.stations["before"].y + offsets[("before", "straight")],
            6,
        )
    }


def test_normal_planning_builds_each_exit_group_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "complex_multipath.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    original = exit_turns._build_group_plan
    calls = []

    def record(*args, **kwargs):
        exit_group = args[4]
        calls.append(exit_group.id)
        return original(*args, **kwargs)

    monkeypatch.setattr(exit_turns, "_build_group_plan", record)
    exit_turns.build_exit_turn_execution(graph, ctx)

    assert calls
    assert len(calls) == len(set(calls))


def test_right_exit_feeder_slots_preserve_the_planned_lane_order() -> None:
    graph, offsets, observation = _observe(FIXTURES / "tb_right_exit_feeder_slots.mmd")
    plan = _plan_for_source(observation, "src__exit_right_0")

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert plan.legacy_reason is None
    assert tuple(lane.line_id for lane in plan.source_lanes) == (
        "l1",
        "l2",
        "l4",
        "l3",
    )
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


@pytest.mark.parametrize(
    ("fixture", "source_id", "reason"),
    (
        (
            "ambiguous_exit_continuation.mmd",
            "__junction_4",
            "ambiguous-continuation",
        ),
        (
            "compact_continuation_slot_conflict.mmd",
            "src__exit_right_0",
            "continuation-transition-has-no-runway",
        ),
    ),
)
def test_unsupported_continuations_use_whole_group_legacy(
    fixture: str,
    source_id: str,
    reason: str,
) -> None:
    _graph, _offsets, _original_offsets, execution = _build_execution(
        FIXTURES / fixture
    )
    plan = next(item for item in execution.plans if item.source_id == source_id)

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == reason


def test_repeated_same_line_arms_share_one_lane_and_axis() -> None:
    graph = prepare_graph(
        """\
%%metro line: red | Red | #f00
graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph upper [Upper]
        b[B]
    end
    subgraph lower [Lower]
        c[C]
    end
    %%metro grid: source | 0,0
    %%metro grid: upper | 1,1
    %%metro grid: lower | 1,2
    a -->|red| b
    a -->|red| c
"""
    )
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    planned = [
        plan
        for plan in observation.plan.exit_turn_plans
        if plan.disposition is ExitTurnDisposition.PLANNED
        and len(plan.assignments) == 2
    ]

    assert len(planned) == 1
    (plan,) = planned
    assert len(plan.source_lanes) == 1
    assert len(plan.axes) == 1
    assert {item.axis_id for item in plan.assignments} == {plan.axes[0].id}


def test_two_line_direct_continuation_is_planned_without_turn_axes() -> None:
    graph = prepare_graph(
        """\
%%metro line: red | Red | #f00
%%metro line: blue | Blue | #00f
%%metro grid: source | 0,0
%%metro grid: target | 1,0
graph LR
    subgraph source [Source]
        a[A]
    end
    subgraph target [Target]
        b[B]
    end
    a -->|red,blue| b
"""
    )
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    plan = next(
        item for item in observation.plan.exit_turn_plans if len(item.assignments) == 2
    )

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert plan.axes == ()
    assert {assignment.planned_family_id for assignment in plan.assignments} == {
        RouteFamilyId.SAME_Y_STRAIGHT
    }
    assert all(assignment.axis_id is None for assignment in plan.assignments)
    assert (
        _build_bubble_ctx(observation.routes, graph).planned_geometry_stations == set()
    )
    build_route_plan_query(observation.plan)
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_straight_requirement_rejects_a_perpendicular_source_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = exit_turns.build_exit_turn_execution
    contexts = []

    def capture(graph, ctx):
        contexts.append(ctx)
        return real_build(graph, ctx)

    monkeypatch.setattr(exit_turns, "build_exit_turn_execution", capture)
    graph, _offsets, _observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    horizontal_ctx = contexts[-1]
    horizontal_edge = next(
        item
        for item in graph.edges
        if item.source == "__junction_9" and item.line_id == "sheets"
    )

    horizontal = exit_turns._source_turn_requirement(
        horizontal_edge,
        RouteFamilyId.SAME_Y_STRAIGHT,
        Direction.D,
        horizontal_ctx,
    )

    assert horizontal.legacy_reason == (
        "unsupported-subshape:vertical-source-horizontal-straight"
    )


def test_vertical_bottom_exit_owns_ordered_turn_rows() -> None:
    graph, offsets, observation = _observe(TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd")
    plan = _plan_for_source(observation, "up__exit_bottom_0")

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert plan.source_run_direction is Direction.D
    assert plan.source_axis is DemandAxis.Y
    assert tuple(axis.coordinate for axis in plan.axes) == (248.0, 244.0, 240.0, 236.0)
    routes = {
        route.line_id: route
        for route in observation.routes
        if route.exit_turn_plan_id == str(plan.id)
        and route.exit_turn_axis_id is not None
    }
    assert tuple(_turn_y(routes[f"l{rank}"], offsets) for rank in range(1, 5)) == (
        248.0,
        244.0,
        240.0,
        236.0,
    )
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_vertical_axis_overlap_range_matches_the_emitted_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = exit_turns.build_exit_turn_execution
    contexts = []

    def capture(graph, ctx):
        contexts.append(ctx)
        return real_build(graph, ctx)

    monkeypatch.setattr(exit_turns, "build_exit_turn_execution", capture)
    graph, offsets, observation = _observe(TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd")
    plan = _plan_for_source(observation, "up__exit_bottom_0")
    member_edges = {item.id: item.edge for item in observation.plan.members}
    assignments = {member_edges[item.member_id]: item for item in plan.assignments}

    for axis in plan.axes:
        assignment = next(item for item in plan.assignments if item.axis_id == axis.id)
        route = next(
            item
            for item in observation.routes
            if item.exit_turn_member_id == str(assignment.member_id)
        )
        points = apply_route_offsets(route, offsets)
        rank = route.exit_turn_segment_rank
        assert rank is not None
        expected = tuple(sorted((points[rank][0], points[rank + 1][0])))

        assert exit_turns._planned_axis_cross_range(
            graph,
            contexts[-1],
            plan,
            axis,
            assignments,
        ) == pytest.approx(expected)


@pytest.mark.parametrize(
    "path",
    (
        ROOT / "examples" / "rnaseq_sections.mmd",
        ROOT / "examples" / "rnaseq_auto.mmd",
        TOPOLOGIES / "fold_fan_across.mmd",
    ),
    ids=lambda path: path.name,
)
def test_lane_transitions_stay_within_one_section_frame(path: Path) -> None:
    graph, _offsets, observation = _observe(path)

    transitions = tuple(
        transition
        for plan in observation.plan.exit_turn_plans
        if plan.disposition is ExitTurnDisposition.PLANNED
        for transition in plan.lane_transitions
    )
    assert all(
        graph.stations[transition.edge.source].section_id
        == graph.stations[transition.edge.target].section_id
        for transition in transitions
    )
    build_route_plan_query(observation.plan)


def test_noncontiguous_source_lanes_compact_the_turning_cohort() -> None:
    _graph, offsets, observation = _observe(TOPOLOGIES / "complex_multipath.mmd")
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "complex_multipath.mmd"
    )
    plan = next(item for item in execution.plans if item.source_id == "__junction_11")
    ordered_turns = next(
        item
        for item in execution.demands
        if item.id in plan.demand_ids and item.kind is DemandKind.ORDERED_TURNS
    )

    assert {item.source_lane_rank for item in plan.assignments if item.axis_id} == {
        0,
        2,
    }
    assert ordered_turns.minimum_size == pytest.approx(plan.spacing)
    routes = {
        route.line_id: route
        for route in observation.routes
        if route.edge.source == plan.source_id
    }
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    assert all(route.exit_turn_axis_id is None for route in routes.values())
    turn_gap = abs(
        apply_route_offsets(routes["standard"], offsets)[1][0]
        - apply_route_offsets(routes["legacy"], offsets)[1][0]
    )
    assert turn_gap == pytest.approx(plan.spacing)


def test_planned_bundle_pins_consistent_same_line_attachments() -> None:
    _graph, offsets, observation = _observe(
        ROOT / "examples" / "variantbenchmarking.mmd"
    )
    target_id = "normalization__entry_left_7"
    routes = [route for route in observation.routes if route.edge.target == target_id]

    def target_axis(route) -> float:
        points = apply_route_offsets(route, offsets)
        return next(
            start[0]
            for start, end in reversed(tuple(zip(points, points[1:])))
            if start[0] == pytest.approx(end[0]) and abs(start[1] - end[1]) > 1e-6
        )

    for line_id in ("test", "truth"):
        line_routes = [route for route in routes if route.line_id == line_id]
        assert len(line_routes) == 2
        assert target_axis(line_routes[0]) == pytest.approx(target_axis(line_routes[1]))


def test_free_vertical_turn_axes_choose_origin_in_the_run_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_plan = exit_turns._plan_turn_axes
    captured = []

    def capture(*args):
        captured.append(args)
        return real_plan(*args)

    monkeypatch.setattr(exit_turns, "_plan_turn_axes", capture)
    _observe(TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd")
    assert captured
    graph, ctx, plan_id, source_id, exit_port_id, _run, lanes, seeds = captured[0]

    for run_direction, turn_direction in (
        (Direction.D, Direction.R),
        (Direction.U, Direction.L),
    ):
        synthetic = tuple(
            replace(
                seed,
                run_direction=run_direction,
                turn_direction=turn_direction,
                launch_coordinate=100.0 + seed.lane_rank * 2.0,
                minimum_runway=10.0,
                fixed_axis=None,
            )
            for seed in seeds
        )
        result = real_plan(
            graph,
            ctx,
            plan_id,
            source_id,
            exit_port_id,
            run_direction,
            lanes,
            synthetic,
        )

        assert result.legacy_reason is None
        assert all(
            (result.axis_by_member[seed.member_id].coordinate - seed.launch_coordinate)
            * run_direction.sign
            >= 10.0
            for seed in synthetic
            if seed.launch_coordinate is not None
        )


def test_straight_upward_exit_owns_no_false_turn_resources() -> None:
    graph, offsets, observation = _observe(TOPOLOGIES / "bt_exit_top_above_2line.mmd")
    plan = _plan_for_source(observation, "work__exit_top_0")

    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert plan.source_run_direction is Direction.U
    assert plan.source_axis is DemandAxis.Y
    assert plan.axes == ()
    assert plan.reference_id is None
    assert plan.demand_ids == ()
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_one_unsupported_member_keeps_the_whole_group_on_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exit_turns,
        "PLANNED_EXIT_FAMILIES",
        exit_turns.PLANNED_EXIT_FAMILIES - {RouteFamilyId.MERGE_BRANCH},
    )
    _graph, _offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = next(item for item in execution.plans if item.source_id == "__junction_9")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason is not None
    assert (
        len(
            [
                diagnostic
                for diagnostic in execution.diagnostics
                if diagnostic.code == "exit-turn-legacy"
                and diagnostic.member_id in plan.member_ids
            ]
        )
        == 1
    )


def test_route_plan_query_rejects_an_inexact_legacy_diagnostic() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    diagnostic = next(
        item for item in observation.plan.diagnostics if item.code == "exit-turn-legacy"
    )
    malformed = replace(diagnostic, blocking=True)

    with pytest.raises(ValueError, match="legacy diagnostics are inconsistent"):
        build_route_plan_query(
            replace(
                observation.plan,
                diagnostics=tuple(
                    malformed if item == diagnostic else item
                    for item in observation.plan.diagnostics
                ),
            )
        )


def test_unclassifiable_member_has_an_explicit_whole_group_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_classify = exit_turns.classify_inter_section_family

    def classify(edge, src, tgt, ctx):
        if edge.source == "__junction_9" and edge.line_id == "main":
            return None
        return real_classify(edge, src, tgt, ctx)

    monkeypatch.setattr(exit_turns, "classify_inter_section_family", classify)
    _graph, _offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = next(item for item in execution.plans if item.source_id == "__junction_9")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "missing-production-family"
    assert len(plan.unclassified_member_ids) == 1


def test_planned_turn_falls_back_before_a_compatibility_channel_collision() -> None:
    graph, offsets, observation = _observe(
        FIXTURES / "planned_compatibility_channel_collision.mmd"
    )
    plan = _plan_for_source(observation, "__junction_8")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "planned-axis-overlaps-compatibility-channel"
    assert plan.axes == ()
    assert not any(
        route.edge.source == plan.source_id and route.exit_turn_axis_id is not None
        for route in observation.routes
    )
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


@pytest.mark.parametrize("fold", [1, 2, 3])
def test_compatibility_claim_matches_the_emitted_channel_span(fold: int) -> None:
    source = (
        (TOPOLOGIES / "shared_sink_parallel.mmd")
        .read_text()
        .replace(
            "graph LR",
            f"%%metro fold_threshold: {fold}\ngraph LR",
            1,
        )
    )
    graph = prepare_graph(source, source_dir=str(TOPOLOGIES))
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    checked = 0
    for edge in graph.edges:
        source_station, target_station = graph.edge_endpoints(edge)
        family = inter_handlers.classify_inter_section_family(
            edge, source_station, target_station, ctx
        )
        if family is not RouteFamilyId.TB_BOTTOM_EXIT_AROUND_STACK:
            continue
        facts = inter_handlers._build_inter_facts(
            edge, source_station, target_station, ctx
        )
        geometry = inter_handlers._tb_bottom_exit_around_stack_geometry(facts)
        route = inter_handlers._route_tb_bottom_exit_around_stack(facts)
        assert route is not None
        channel_start, channel_end = route.points[2:4]
        assert channel_start[0] == pytest.approx(geometry.channel_x)
        assert channel_end[0] == pytest.approx(geometry.channel_x)
        assert min(channel_start[1], channel_end[1]) == pytest.approx(
            geometry.channel_y_lo
        )
        assert max(channel_start[1], channel_end[1]) == pytest.approx(
            geometry.channel_y_hi
        )
        checked += 1
    assert checked


def test_disjoint_compatibility_channel_does_not_force_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, offsets, baseline = _observe(TOPOLOGIES / "exit_run_three_drop_columns.mmd")
    baseline_plan = _plan_for_source(baseline, "__junction_9")
    axis = baseline_plan.axes[0]
    claim = exit_turns._CompatibilityChannelClaim(
        "unrelated-line",
        baseline_plan.source_axis,
        axis.coordinate,
        1_000_000.0,
        1_000_100.0,
    )
    monkeypatch.setattr(
        exit_turns,
        "_compatibility_channel_claims",
        lambda *_args, **_kwargs: (claim,),
    )

    observation = observe_route_edges(graph, station_offsets=offsets)
    plan = _plan_for_source(observation, "__junction_9")

    assert plan.disposition is ExitTurnDisposition.PLANNED


def test_missing_outbound_members_have_a_valid_legacy_lane_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_scaffold = exit_turns.build_route_semantic_scaffold

    def omit_outbound(graph, query, *, coupled_connector_groups=()):
        graph.fan_plan_execution = None
        scaffold = real_scaffold(
            graph,
            query,
            coupled_connector_groups=coupled_connector_groups,
        )
        assert scaffold is not None
        return replace(
            scaffold,
            edge_order=tuple(
                edge for edge in scaffold.edge_order if edge.source != "__junction_9"
            ),
        )

    monkeypatch.setattr(exit_turns, "build_route_semantic_scaffold", omit_outbound)
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    assert graph.fan_plan_execution is not None
    graph.fan_plan_execution = replace(graph.fan_plan_execution, scaffold=None)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    execution = exit_turns.build_exit_turn_execution(graph, ctx)
    (plan,) = tuple(
        item for item in execution.plans if item.source_id == "__junction_9"
    )

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "missing-outbound-member"
    assert plan.member_ids
    assert sorted(
        member_id for lane in plan.source_lanes for member_id in lane.member_ids
    ) == sorted(plan.member_ids)


def test_unsupported_family_after_tentative_compaction_uses_whole_group_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_classify = exit_turns.classify_inter_section_family

    def classify(edge, src, tgt, ctx):
        family = real_classify(edge, src, tgt, ctx)
        if (
            edge.source == "__junction_37"
            and edge.line_id == "l4"
            and ctx.station_offsets is not None
            and ctx.station_offsets[(edge.source, edge.line_id)] == pytest.approx(8.0)
        ):
            return RouteFamilyId.BYPASS_FAMILY
        return family

    monkeypatch.setattr(exit_turns, "classify_inter_section_family", classify)
    _graph, _offsets, observation = _observe(FROZEN / "seed_77.mmd")
    plan = _plan_for_source(observation, "__junction_37")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "unsupported-family:bypass-family"
    assert plan.axes == ()
    assert plan.lane_transitions == ()
    assert all(lane.station_ids == () for lane in plan.source_lanes)
    build_route_plan_query(observation.plan)


def test_cross_plan_station_lane_ownership_falls_back_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_ownership = exit_turns._source_lane_ownership

    def share_station(
        graph,
        offsets,
        exit_port_id,
        source_id,
        line_id,
        claimant_member_ids,
        desired,
        run_direction,
        ctx,
    ):
        stations, transitions, reason = real_ownership(
            graph,
            offsets,
            exit_port_id,
            source_id,
            line_id,
            claimant_member_ids,
            desired,
            run_direction,
            ctx,
        )
        if exit_port_id == "b__exit_right_1" and line_id == "main" and reason is None:
            stations = (*stations, "c__exit_right_2")
        return stations, transitions, reason

    monkeypatch.setattr(exit_turns, "_source_lane_ownership", share_station)
    _graph, offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    affected = [
        item
        for item in execution.plans
        if item.source_id in {"__junction_8", "__junction_9"}
    ]

    assert len(affected) == 2
    assert all(item.disposition is ExitTurnDisposition.LEGACY for item in affected)
    assert all(
        item.legacy_reason == "shared-source-ownership-conflict" for item in affected
    )
    assert offsets[("c__exit_right_2", "main")] == pytest.approx(0.0)


def test_cross_plan_station_lane_slots_are_checked_after_all_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_build = exit_turns.build_exit_turn_execution
    contexts = []

    def capture(graph, ctx):
        contexts.append(ctx)
        return real_build(graph, ctx)

    monkeypatch.setattr(exit_turns, "build_exit_turn_execution", capture)
    graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    first = _plan_for_source(observation, "__junction_8")
    second = _plan_for_source(observation, "__junction_9")
    synthetic_lane = replace(
        first.source_lanes[0],
        line_id="synthetic",
        rank=len(first.source_lanes),
        member_ids=(),
        station_ids=("c__exit_right_2",),
        planned_offset=second.source_lanes[0].planned_offset,
    )
    modified_first = replace(
        first,
        source_lanes=(*first.source_lanes, synthetic_lane),
    )
    reasons = {}

    exit_turns._add_station_lane_collision_fallbacks(
        graph,
        contexts[-1],
        (modified_first, second),
        reasons,
    )

    assert reasons == {
        modified_first.id: "shared-station-lane-collision",
        second.id: "shared-station-lane-collision",
    }


def test_mixed_disposition_member_ownership_is_rejected_before_dispatch() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    legacy = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.LEGACY
    )
    planned = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.PLANNED
    )
    member_id = planned.member_ids[0]
    malformed_legacy = replace(
        legacy,
        member_ids=(*legacy.member_ids, member_id),
    )

    with pytest.raises(ExitTurnInvariantError) as error:
        exit_turns._index_unique_member_owners((malformed_legacy, planned))

    message = str(error.value)
    assert str(legacy.system_id) in message
    assert str(planned.system_id) in message
    assert str(member_id) in message


def test_runtime_invariant_names_the_system_and_connectors() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    routes = copy.deepcopy(observation.routes)
    route = next(item for item in routes if item.exit_turn_axis_id is not None)
    assert route.exit_turn_segment_rank is not None
    rank = route.exit_turn_segment_rank
    x, y = route.points[rank]
    route.points[rank] = (x + 20.0, y)

    with pytest.raises(ExitTurnInvariantError) as error:
        validate_exit_turn_plans(graph, routes, observation.plan, offsets)

    plan = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.id == route.exit_turn_plan_id
    )
    assert str(plan.system_id) in str(error.value)
    assert all(
        str(connector_id) in str(error.value) for connector_id in plan.connector_ids
    )


def test_post_pass_snapshot_owns_family_direction_endpoints_and_radii() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    routes = copy.deepcopy(observation.routes)
    snapshot = snapshot_exit_turn_segments(
        routes,
        observation.plan.exit_turn_plans,
    )
    route = next(
        item
        for item in routes
        if item.exit_turn_axis_id is not None
        and item.exit_turn_family_id == RouteFamilyId.STANDARD_L_SHAPE.value
    )
    assert route.exit_turn_segment_rank is not None
    rank = route.exit_turn_segment_rank
    route.points[rank], route.points[rank + 1] = (
        route.points[rank + 1],
        route.points[rank],
    )
    if route.curve_radii is not None:
        route.curve_radii[rank - 1] += 1.0

    with pytest.raises(ExitTurnInvariantError) as error:
        assert_exit_turn_snapshot(routes, snapshot, "test pass")

    plan = next(
        item
        for item in observation.plan.exit_turn_plans
        if str(item.id) == route.exit_turn_plan_id
    )
    assert str(plan.system_id) in str(error.value)
    assert all(
        str(connector_id) in str(error.value) for connector_id in plan.connector_ids
    )


def test_runtime_invariant_checks_every_station_owner() -> None:
    graph, offsets, observation = _observe(FROZEN / "seed_77.mmd")
    plan = _plan_for_source(observation, "__junction_37")
    lane = plan.source_lanes[-1]
    malformed_lane = replace(lane, station_ids=(*lane.station_ids, "not-a-station"))
    malformed_plan = replace(
        plan,
        source_lanes=(*plan.source_lanes[:-1], malformed_lane),
    )

    with pytest.raises(ExitTurnInvariantError, match="unknown station or line"):
        validate_exit_turn_plans(
            graph,
            observation.routes,
            tuple(
                malformed_plan if item.id == plan.id else item
                for item in observation.plan.exit_turn_plans
            ),
            offsets,
        )


def test_runtime_invariant_rejects_a_missing_planned_offset() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    lane = plan.source_lanes[0]
    malformed_offsets = dict(offsets)
    malformed_offsets.pop((lane.station_ids[0], lane.line_id))

    with pytest.raises(ExitTurnInvariantError, match="compaction was not preserved"):
        validate_exit_turn_plans(
            graph,
            observation.routes,
            observation.plan,
            malformed_offsets,
        )


def test_runtime_invariant_rejects_a_changed_lane_transition() -> None:
    graph, offsets, observation = _observe(FROZEN / "seed_77.mmd")
    routes = copy.deepcopy(observation.routes)
    route = next(
        item for item in routes if item.exit_lane_transition_plan_id is not None
    )
    x, y = route.points[1]
    route.points[1] = (x + 1.0, y)

    with pytest.raises(ExitTurnInvariantError, match="template decision"):
        validate_exit_turn_plans(graph, routes, observation.plan, offsets)


def test_runtime_invariant_checks_rendered_turn_direction() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    routes = copy.deepcopy(observation.routes)
    route = next(
        item
        for item in routes
        if item.exit_turn_plan_id == str(plan.id)
        and item.line_id == "main"
        and item.exit_turn_segment_rank is not None
    )
    rank = route.exit_turn_segment_rank
    assert rank is not None
    assert route.points[rank + 1][1] > route.points[rank][1]
    route.offset_regime = OffsetRegime.DEFERRED
    malformed_offsets = dict(offsets)
    malformed_offsets[(route.edge.target, route.line_id)] = -300.0

    with pytest.raises(ExitTurnInvariantError, match="axis or direction"):
        validate_exit_turn_plans(
            graph,
            routes,
            observation.plan,
            malformed_offsets,
        )


def test_runtime_invariant_rejects_an_unsatisfied_runway_demand() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    assignment = next(item for item in plan.assignments if item.axis_id is not None)
    assert assignment.minimum_runway is not None
    malformed_assignment = replace(
        assignment,
        minimum_runway=assignment.minimum_runway + 10_000.0,
    )
    malformed_plan = replace(
        plan,
        assignments=tuple(
            malformed_assignment if item.member_id == assignment.member_id else item
            for item in plan.assignments
        ),
    )

    with pytest.raises(ExitTurnInvariantError, match="runway demand"):
        validate_exit_turn_plans(
            graph,
            observation.routes,
            tuple(
                malformed_plan if item.id == plan.id else item
                for item in observation.plan.exit_turn_plans
            ),
            offsets,
        )


def test_runtime_invariant_rejects_a_shifted_straight_continuation() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    assignment = next(
        item
        for item in plan.assignments
        if item.axis_id is None
        and item.planned_family_id is RouteFamilyId.SAME_Y_STRAIGHT
    )
    routes = copy.deepcopy(observation.routes)
    route = next(
        item for item in routes if item.exit_turn_member_id == str(assignment.member_id)
    )
    assert route.exit_lane_transition_plan_id is None
    x, y = route.points[-1]
    route.points[-1] = x, y + 2.0

    with pytest.raises(ExitTurnInvariantError, match="changed source lane"):
        validate_exit_turn_plans(graph, routes, observation.plan, offsets)


def test_route_plan_query_rejects_a_tampered_exit_turn_reference() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    assert plan.reference_id is not None
    reference = next(
        item
        for item in observation.plan.shared_references
        if item.id == plan.reference_id
    )
    malformed = replace(reference, coordinate_regime=CoordinateRegime.SETTLED_GRID)

    with pytest.raises(ValueError, match="exit-turn shared reference is inconsistent"):
        build_route_plan_query(
            replace(
                observation.plan,
                shared_references=tuple(
                    malformed if item.id == reference.id else item
                    for item in observation.plan.shared_references
                ),
            )
        )


def test_route_plan_query_rejects_incomplete_system_membership() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    malformed_plan = replace(
        plan,
        system_member_ids=plan.system_member_ids[:-1],
    )

    with pytest.raises(ValueError, match="complete route system"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_route_plan_query_rejects_an_omitted_exit_group_member() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.LEGACY and len(item.member_ids) > 1
    )
    omitted = plan.member_ids[-1]
    malformed_plan = replace(
        plan,
        member_ids=tuple(item for item in plan.member_ids if item != omitted),
        source_lanes=tuple(
            replace(
                lane,
                member_ids=tuple(item for item in lane.member_ids if item != omitted),
            )
            for lane in plan.source_lanes
            if any(item != omitted for item in lane.member_ids)
        ),
        assignments=tuple(
            item for item in plan.assignments if item.member_id != omitted
        ),
        unclassified_member_ids=tuple(
            item for item in plan.unclassified_member_ids if item != omitted
        ),
    )

    with pytest.raises(ValueError, match="complete exit group"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_route_plan_query_rejects_changed_direction_semantics() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    assignment = next(item for item in plan.assignments if item.axis_id is not None)
    malformed_assignment = replace(assignment, handedness=None)
    malformed_plan = replace(
        plan,
        assignments=tuple(
            malformed_assignment if item.member_id == assignment.member_id else item
            for item in plan.assignments
        ),
    )

    with pytest.raises(ValueError, match="inconsistent semantics"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_route_plan_query_rejects_changed_axis_spacing() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    axis = plan.axes[-1]
    malformed_axis = replace(axis, coordinate=axis.coordinate + 1.0)
    malformed_plan = replace(
        plan,
        axes=(*plan.axes[:-1], malformed_axis),
    )

    with pytest.raises(ValueError, match="planned lane spacing"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_exit_turn_axis_rejects_nonfinite_geometry() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")

    with pytest.raises(ValueError, match="coordinate must be finite"):
        replace(plan.axes[0], coordinate=float("nan"))


def test_route_plan_query_rejects_fallback_lane_order_for_planned_group() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    malformed_plan = replace(
        plan,
        lane_order_source=ExitLaneOrderSource.GRAPH_LINE_ORDER_FALLBACK,
    )

    with pytest.raises(ValueError, match="fallback provenance"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_implicit_line_id_uses_stable_fallback_order() -> None:
    path = TOPOLOGIES / "internal_source_equal_sibling_2fan.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    observation = observe_route_edges(graph)

    assert observation.routes
    assert any(
        plan.lane_order_source is ExitLaneOrderSource.GRAPH_LINE_ORDER_FALLBACK
        and tuple(lane.line_id for lane in plan.source_lanes) == ("run_folder",)
        for plan in observation.plan.exit_turn_plans
    )


def test_runtime_invariant_rejects_a_shifted_fixed_axis_anchor() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "peeloff_straight_drop_near_wall.mmd"
    )
    plan = _plan_for_source(observation, "__junction_7")
    axis = next(item for item in plan.axes if item.fixed_anchor_id is not None)
    assert axis.fixed_anchor_offset is not None
    malformed_axis = replace(
        axis,
        coordinate=axis.coordinate + 2.0,
        fixed_anchor_offset=axis.fixed_anchor_offset + 2.0,
    )
    malformed_plan = replace(
        plan,
        axes=tuple(
            malformed_axis if item.id == axis.id else item for item in plan.axes
        ),
    )

    with pytest.raises(ExitTurnInvariantError, match="structural anchor") as error:
        validate_exit_turn_plans(
            graph,
            observation.routes,
            tuple(
                malformed_plan if item.id == plan.id else item
                for item in observation.plan.exit_turn_plans
            ),
            offsets,
        )

    assert str(plan.system_id) in str(error.value)
    assert all(
        str(connector_id) in str(error.value) for connector_id in plan.connector_ids
    )


def test_runtime_invariant_derives_merge_anchor_offset_from_runtime_state() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    axis = next(item for item in plan.axes if item.line_id == "report")
    assert axis.fixed_anchor_offset is not None
    malformed_axis = replace(
        axis,
        coordinate=axis.coordinate + 2.0,
        fixed_anchor_offset=axis.fixed_anchor_offset + 2.0,
    )
    malformed_plan = replace(
        plan,
        axes=tuple(
            malformed_axis if item.id == axis.id else item for item in plan.axes
        ),
    )

    with pytest.raises(ExitTurnInvariantError, match="structural anchor"):
        validate_exit_turn_plans(
            graph,
            observation.routes,
            tuple(
                malformed_plan if item.id == plan.id else item
                for item in observation.plan.exit_turn_plans
            ),
            offsets,
        )


def test_route_plan_query_rejects_a_tampered_exit_lane_owner() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    first_lane = plan.source_lanes[0]
    malformed_lane = replace(first_lane, line_id="not-the-member-line")
    malformed_plan = replace(
        plan, source_lanes=(malformed_lane, *plan.source_lanes[1:])
    )

    with pytest.raises(ValueError, match="inconsistent line ownership"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_route_plan_query_rejects_a_noncanonical_foreign_conflict() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_9")
    same_system_reference = next(
        item.reference_id
        for item in observation.plan.exit_turn_plans
        if item.id != plan.id
        and item.system_id == plan.system_id
        and item.reference_id is not None
    )
    malformed_plan = replace(plan, foreign_reference_ids=(same_system_reference,))

    with pytest.raises(ValueError, match="foreign-reference index is inconsistent"):
        build_route_plan_query(
            replace(
                observation.plan,
                exit_turn_plans=tuple(
                    malformed_plan if item.id == plan.id else item
                    for item in observation.plan.exit_turn_plans
                ),
            )
        )


def test_foreign_vertical_band_conflict_is_recorded() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "bt_perp_left_entry_right_exit.mmd"
    )
    plan = next(
        item
        for item in observation.plan.exit_turn_plans
        if item.disposition is ExitTurnDisposition.PLANNED and item.axes
    )
    reservation = next(
        item
        for item in observation.plan.reservations
        if item.orientation is CorridorOrientation.VERTICAL
    )
    span = next(
        item.span for item in observation.plan.demands if item.id == plan.demand_ids[0]
    )
    axis = plan.axes[0]
    conflicting_reservation = replace(
        reservation,
        system_id=RouteSystemId("foreign-system"),
        span=span,
        claims=tuple(
            replace(claim, allocation_coordinate=axis.coordinate)
            for claim in reservation.claims
        ),
    )
    modified = replace(
        observation.plan,
        reservations=(*observation.plan.reservations, conflicting_reservation),
    )

    conflicts = expected_exit_turn_foreign_references(modified)

    assert reservation.reference_id in conflicts[plan.id]


def test_perpendicular_plan_axes_do_not_create_foreign_conflicts() -> None:
    _graph, _offsets, observation = _observe(TOPOLOGIES / "complex_multipath.mmd")
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        TOPOLOGIES / "complex_multipath.mmd"
    )
    first, second = (
        item
        for item in execution.plans
        if item.disposition is ExitTurnDisposition.PLANNED
        and item.axes
        and item.reference_id is not None
    )
    perpendicular = replace(
        second,
        source_run_direction=Direction.D,
        source_axis=DemandAxis.Y,
        axes=tuple(
            replace(
                axis,
                axis=DemandAxis.Y,
                coordinate=first.axes[0].coordinate,
            )
            for axis in second.axes
        ),
    )
    modified = replace(
        observation.plan,
        demands=execution.demands,
        exit_turn_plans=tuple(
            perpendicular if item.id == second.id else item for item in execution.plans
        ),
    )

    conflicts = expected_exit_turn_foreign_references(modified)

    assert second.reference_id not in conflicts[first.id]


def test_vertical_source_axes_conflict_with_horizontal_corridors() -> None:
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd"
    )
    plan = _plan_for_source(observation, "up__exit_bottom_0")
    reservation = next(
        item
        for item in observation.plan.reservations
        if item.orientation is CorridorOrientation.HORIZONTAL
    )
    span = next(
        item.span for item in observation.plan.demands if item.id == plan.demand_ids[0]
    )
    conflicting_reservation = replace(
        reservation,
        system_id=RouteSystemId("foreign-system"),
        span=span,
        claims=tuple(
            replace(claim, allocation_coordinate=plan.axes[0].coordinate)
            for claim in reservation.claims
        ),
    )
    modified = replace(
        observation.plan,
        reservations=(*observation.plan.reservations, conflicting_reservation),
    )

    conflicts = expected_exit_turn_foreign_references(modified)

    assert reservation.reference_id in conflicts[plan.id]


@pytest.mark.parametrize(
    "path",
    (
        TOPOLOGIES / "exit_run_three_drop_columns.mmd",
        FROZEN / "seed_72.mmd",
        FROZEN / "seed_77.mmd",
    ),
    ids=lambda path: path.name,
)
def test_exit_turn_planning_is_observer_neutral(path: Path) -> None:
    source = path.read_text()
    source_dir = str(path.parent)
    plain_graph = prepare_graph(source, source_dir=source_dir)
    plain_offsets = compute_station_offsets(plain_graph)
    plain_routes = route_edges(plain_graph, station_offsets=plain_offsets)

    observed_graph = prepare_graph(source, source_dir=source_dir)
    observed_offsets = compute_station_offsets(observed_graph)
    observation = observe_route_edges(observed_graph, station_offsets=observed_offsets)

    assert plain_offsets == observed_offsets
    assert freeze_render_value(plain_routes) == freeze_render_value(observation.routes)


def test_plain_routing_runs_the_post_emission_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    real_validate = exit_turns.validate_exit_turn_plans
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(exit_turns, "validate_exit_turn_plans", record)
    route_edges(graph, station_offsets=offsets)

    assert len(calls) == 1


def test_exit_turn_plan_is_built_once_per_routing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    real_build = exit_turns.build_exit_turn_execution
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(exit_turns, "build_exit_turn_execution", record)
    offsets = compute_station_offsets(graph)
    assert type(offsets) is dict
    assert calls == []
    observe_route_edges(graph, station_offsets=offsets)
    assert len(calls) == 1


def test_custom_spacing_is_shared_by_offsets_plan_and_routes() -> None:
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph, offset_step=10.0)
    observation = observe_route_edges(
        graph,
        station_offsets=offsets,
        offset_step=10.0,
    )
    _raw_graph, _raw_offsets, _original_offsets, execution = _build_execution(
        path, offset_step=10.0
    )
    plan = next(item for item in execution.plans if item.source_id == "__junction_9")

    assert plan.spacing == pytest.approx(10.0)
    assert all(
        abs(right.planned_offset - left.planned_offset) == pytest.approx(10.0)
        for left, right in zip(plan.source_lanes, plan.source_lanes[1:])
    )
    ordered_axes = sorted(plan.axes, key=lambda item: item.rank)
    assert all(
        abs(right.coordinate - left.coordinate)
        == pytest.approx((right.rank - left.rank) * 10.0)
        for left, right in zip(ordered_axes, ordered_axes[1:])
    )
    system = next(
        item for item in observation.plan.systems if item.id == plan.system_id
    )
    assert system.disposition is RouteSystemDisposition.COMPATIBILITY
    assert all(route.exit_turn_axis_id is None for route in observation.routes)
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_custom_spacing_is_observer_neutral() -> None:
    path = TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    source = path.read_text()
    plain_graph = prepare_graph(source, source_dir=str(path.parent))
    plain_offsets = compute_station_offsets(plain_graph, offset_step=10.0)
    plain_routes = route_edges(
        plain_graph,
        station_offsets=plain_offsets,
        offset_step=10.0,
    )

    observed_graph = prepare_graph(source, source_dir=str(path.parent))
    observed_offsets = compute_station_offsets(observed_graph, offset_step=10.0)
    observation = observe_route_edges(
        observed_graph,
        station_offsets=observed_offsets,
        offset_step=10.0,
    )

    assert plain_offsets == observed_offsets
    assert freeze_render_value(plain_routes) == freeze_render_value(observation.routes)


def test_merge_families_use_their_source_side_geometry_direction() -> None:
    for path in (
        TOPOLOGIES / "fan_in_merge.mmd",
        ROOT / "examples" / "genomeassembly_staggered.mmd",
    ):
        _graph, _offsets, observation = _observe(path)
        merge_assignments = [
            assignment
            for plan in observation.plan.exit_turn_plans
            if plan.disposition is ExitTurnDisposition.PLANNED
            for assignment in plan.assignments
            if assignment.planned_family_id
            in {RouteFamilyId.MERGE_BRANCH, RouteFamilyId.MERGE_ENTRY}
        ]

        assert merge_assignments
        assert all(
            assignment.turn_direction is Direction.D for assignment in merge_assignments
        )


def test_opposed_merge_branch_keeps_whole_exit_group_on_legacy_geometry() -> None:
    graph, _offsets, observation = _observe(
        TOPOLOGIES / "merge_feeder_shared_channel_gap.mmd"
    )
    plan = _plan_for_source(observation, "__junction_4")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "opposed-source-run"
    assert plan.axes == ()
    source_x = graph.stations[plan.source_id].x
    routes = [
        route for route in observation.routes if route.edge.source == plan.source_id
    ]
    assert routes
    assert all(route.points[1][0] > source_x for route in routes)


def test_aligned_top_entry_peeloff_keeps_its_structural_axis() -> None:
    graph, offsets, observation = _observe(
        TOPOLOGIES / "peeloff_straight_drop_near_wall.mmd"
    )
    plan = _plan_for_source(observation, "__junction_7")
    assignment = next(
        item
        for item in plan.assignments
        if item.planned_family_id is RouteFamilyId.TOP_ENTRY_L_SHAPE
    )
    axis = next(item for item in plan.axes if item.id == assignment.axis_id)
    route = next(
        item
        for item in observation.routes
        if item.exit_turn_member_id == str(assignment.member_id)
    )

    port_x = graph.stations["novel_transcripts__entry_top_5"].x
    assert plan.disposition is ExitTurnDisposition.PLANNED
    assert assignment.run_direction is Direction.R
    assert assignment.turn_direction is Direction.D
    assert axis.coordinate == pytest.approx(port_x)
    assert axis.fixed_anchor_id == "novel_transcripts__entry_top_5"
    assert plan.minimum_runway == pytest.approx(10.0)
    assert route.points[:2] == [(port_x - 10.0, 124.0), (port_x, 124.0)]
    assert route.exit_turn_segment_rank == 1
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_aligned_bottom_entry_peeloff_is_the_rotation_image() -> None:
    graph = prepare_graph(
        """\
%%metro title: Bottom perpendicular peel-off
%%metro line: branch | Branch | #e64980
%%metro line: main | Main | #2db572
%%metro grid: rise | 0,0
%%metro grid: source | 0,1
%%metro grid: straight | 1,1
graph LR
    subgraph rise [Rise]
        %%metro entry: bottom | branch
        d[Peel]
    end
    subgraph source [Source]
        %%metro exit: right | main,branch
        s[Source]
    end
    subgraph straight [Straight]
        %%metro entry: left | main
        m[Continue]
    end
    s -->|main| m
    s -->|branch| d
"""
    )
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(graph, station_offsets=offsets)
    plan = _plan_for_source(observation, "__junction_3")
    assignment = next(
        item
        for item in plan.assignments
        if item.planned_family_id is RouteFamilyId.BOTTOM_ENTRY_L_SHAPE
    )
    axis = next(item for item in plan.axes if item.id == assignment.axis_id)

    assert tuple(lane.line_id for lane in plan.source_lanes) == ("branch", "main")
    assert assignment.run_direction is Direction.R
    assert assignment.turn_direction is Direction.U
    assert axis.coordinate == pytest.approx(graph.stations["rise__entry_bottom_2"].x)
    assert axis.fixed_anchor_id == "rise__entry_bottom_2"
    validate_exit_turn_plans(graph, observation.routes, observation.plan, offsets)


def test_structural_peeloff_without_curve_runway_uses_whole_group_legacy() -> None:
    path = TOPOLOGIES / "peeloff_straight_drop_near_wall.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    observation = observe_route_edges(
        graph,
        curve_radius=20.0,
        station_offsets=offsets,
    )
    plan = _plan_for_source(observation, "__junction_7")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "insufficient-structural-runway"


def test_fixed_merge_without_curve_runway_uses_whole_group_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_requirement = exit_turns._source_turn_requirement

    def short_merge_axis(
        edge,
        family_id,
        source_run_direction,
        ctx,
        exit_port_id=None,
    ):
        if edge.source == "__junction_8" and family_id is RouteFamilyId.MERGE_BRANCH:
            requirement = real_requirement(
                edge,
                family_id,
                source_run_direction,
                ctx,
                exit_port_id,
            )
            return replace(
                requirement,
                fixed_axis=ctx.graph.stations[edge.source].x + 5.0,
            )
        return real_requirement(
            edge,
            family_id,
            source_run_direction,
            ctx,
            exit_port_id,
        )

    monkeypatch.setattr(exit_turns, "_source_turn_requirement", short_merge_axis)
    _graph, _offsets, observation = _observe(
        TOPOLOGIES / "exit_run_three_drop_columns.mmd"
    )
    plan = _plan_for_source(observation, "__junction_8")

    assert plan.disposition is ExitTurnDisposition.LEGACY
    assert plan.legacy_reason == "insufficient-fixed-runway"


@pytest.mark.parametrize("path", REDUCED, ids=lambda path: path.name)
def test_reduced_exit_turn_regressions_pass_strict_layout(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, validate=True)


@pytest.mark.parametrize("path", REDUCED, ids=lambda path: path.name)
def test_reduced_exit_turn_regressions_render_through_the_public_api(
    path: Path,
) -> None:
    svg = render_string(path.read_text())
    assert "<svg " in svg


@pytest.mark.parametrize(
    "path",
    STRICT_RENDER_REGRESSIONS,
    ids=lambda path: path.name,
)
def test_existing_exit_bundle_regressions_render_through_the_public_api(
    path: Path,
) -> None:
    svg = render_string(path.read_text())
    assert "<svg " in svg


# _adopt_prior_dispositions forces a settlement re-route's exit-turn
# disposition to match the frozen pass whenever the fresh cross-plan verdict
# on settled geometry differs from it -- the correct ownership boundary, but
# one that makes the re-route's own verdict unobservable for that plan.  This
# pins how often the corpus actually exercises that override (as opposed to
# the frozen and fresh verdicts simply agreeing) so a change that makes it
# common trips an assertion instead of passing unnoticed.  Fixture path ->
# override count; regenerate by running ``_corpus_disposition_overrides``.
EXPECTED_ADOPTED_DISPOSITION_OVERRIDES = {
    "tests/fixtures/planned_compatibility_channel_collision.mmd": 1,
}


def _corpus_disposition_overrides() -> dict[str, int]:
    """{fixture path: count} of ``exit-turn-disposition-adopted`` diagnostics."""
    from nf_metro.render.svg import build_observed_render_plan

    paths = sorted((ROOT / "examples").rglob("*.mmd"))
    paths += sorted((ROOT / "tests" / "fixtures").rglob("*.mmd"))
    paths += sorted((ROOT / "tests" / "fixtures").rglob("*.metro"))
    counts: dict[str, int] = {}
    for path in paths:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
                observed = build_observed_render_plan(graph, resolve_theme(None, graph))
        except Exception:  # noqa: BLE001 - erroring fixtures have their own tests
            continue
        plan = observed.route_plan
        if plan is None:
            continue
        n = sum(
            1
            for item in plan.diagnostics
            if item.code == "exit-turn-disposition-adopted"
        )
        if n:
            counts[str(path.relative_to(ROOT))] = n
    return counts


def test_settlement_rarely_overrides_a_fresh_exit_turn_disposition() -> None:
    assert _corpus_disposition_overrides() == EXPECTED_ADOPTED_DISPOSITION_OVERRIDES
