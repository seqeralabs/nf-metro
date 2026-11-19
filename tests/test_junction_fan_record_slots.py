"""A junction fan seats its branches in the lanes the lane plan records there.

The fan's slots follow the record's lanes at the junction, read travel-left
first, after the lines that turn through the fan's corner: a line running
straight on takes the last slots and no slot shift.
A restacked trunk's corner nests only with the trunks that turn with it, so a
corner turning alone keeps the base radius.  Merge risers read the same record,
and a station's pill covers every line that stops there.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result, render_string
from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    CURVE_RADIUS,
    graph_offset_step,
)
from nf_metro.layout.geometry import point_to_polyline_distance
from nf_metro.layout.phases._common import _station_bundle_offset_span
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser.model import MetroGraph
from nf_metro.render.plan import RenderPlan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).resolve().parents[1]
REGRESSIONS = ROOT / "tests" / "fixtures" / "regressions"
REPROS = ROOT / "tests" / "fixtures" / "curve_invariant_repros"
TOPOLOGIES = ROOT / "examples" / "topologies"


def _plan(
    path: Path,
) -> tuple[MetroGraph, dict[tuple[str, str], float], RenderPlan]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        offsets = compute_station_offsets(graph)
        plan = render_graph_result(
            graph, resolve_theme(None, graph, mode=None), RenderConfig()
        ).plan
    return graph, offsets, plan


def _route(plan: RenderPlan, source: str, target: str, line_id: str):
    return next(
        route
        for route in plan.routes
        if (route.edge.source, route.edge.target, route.line_id)
        == (source, target, line_id)
    )


@pytest.mark.parametrize(
    "path",
    [
        REGRESSIONS / "approach_order_junction_gap.mmd",
        REGRESSIONS / "cohort_pinned_member_past_fixed_lane.mmd",
        REPROS / "inter_row_corridor_overflow.mmd",
        REGRESSIONS / "riboseq_row_trunk_subrun.mmd",
    ],
    ids=lambda p: p.stem,
)
def test_maps_with_record_ordered_fans_render(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(path.read_text(), source_dir=str(path.parent))


def test_fan_descents_nest_in_the_junctions_planned_lane_order() -> None:
    _graph, offsets, plan = _plan(REGRESSIONS / "approach_order_junction_gap.mmd")
    descents = {}
    for line_id in ("l1", "l3"):
        route = _route(plan, "__junction_8", "sec4__entry_left_7", line_id)
        descents[line_id] = route.points[1][0]
    # Leaving rightward and turning down, the lower lane is the inner one and
    # turns first.
    lower = max(("l1", "l3"), key=lambda lid: offsets["__junction_8", lid])
    upper = min(("l1", "l3"), key=lambda lid: offsets["__junction_8", lid])
    assert descents[lower] < descents[upper]


def test_a_straight_on_fan_branch_stays_on_its_track() -> None:
    _graph, _offsets, plan = _plan(REPROS / "bt_bottom_entry_rise_from_junction.mmd")
    route = _route(plan, "__junction_4", "z2__entry_left_3", "m1")
    riser = next(
        i
        for i, (a, b) in enumerate(zip(route.points, route.points[1:]))
        if abs(b[1] - a[1]) > abs(b[0] - a[0])
    )
    assert len({round(y, 3) for _x, y in route.points[: riser + 1]}) == 1


def test_a_lone_wrap_keeps_clear_of_the_lines_its_descent_crosses() -> None:
    graph, _offsets, plan = _plan(
        TOPOLOGIES / "packed_cell_right_exit_left_entry_wrap.mmd"
    )
    step = graph_offset_step(graph)
    wrap = _route(plan, "__junction_11", "polish__entry_left_7", "short")
    (_, top), (descent_x, run_y), (run_end_x, _) = wrap.points[1:4]
    assert wrap.curve_radii is not None
    arc_top = run_y - wrap.curve_radii[1]
    crossing, beside = [], []
    for route in plan.routes:
        if route.line_id == "short":
            continue
        for a, b in zip(route.points, route.points[1:]):
            if abs(a[1] - b[1]) > 1e-6:
                continue
            lo, hi = sorted((a[0], b[0]))
            if lo < descent_x < hi and top < a[1] < run_y:
                crossing.append(a[1])
            if lo < descent_x and hi > run_end_x:
                beside.append((a[1], b[0] > a[0]))
    assert crossing
    assert all(y < arc_top for y in crossing)
    assert all(abs(y - run_y) >= step for y, _ in beside)
    assert all(
        abs(y - run_y) >= BUNDLE_TO_BUNDLE_CLEARANCE
        for y, rightward in beside
        if rightward
    )


def test_a_restacked_corner_turning_alone_keeps_the_base_radius() -> None:
    _graph, _offsets, plan = _plan(ROOT / "examples" / "riboseq_metro.mmd")
    route = _route(
        plan,
        "novel_transcripts__exit_right_5",
        "orf_calling__entry_left_7",
        "annotation",
    )
    assert route.curve_radii is not None
    assert set(route.curve_radii) == {CURVE_RADIUS}


@pytest.mark.parametrize(
    ("path", "station"),
    [
        (TOPOLOGIES / "external_owner_exit_lane_frame.mmd", "before"),
        (TOPOLOGIES / "external_owner_exit_lane_frame.mmd", "split"),
        (TOPOLOGIES / "exit_lane_settlement_without_crossings.mmd", "before"),
        (REPROS / "bt_bottom_entry_rise_from_junction.mmd", "q1x0"),
        (TOPOLOGIES / "junction_entry_lane_rebase.mmd", "ribocode"),
    ],
    ids=[
        "local_line_starts",
        "local_line_ends",
        "settlement",
        "partly_leaving",
        "two_feeders",
    ],
)
def test_a_pill_covers_every_line_stopping_at_its_station(
    path: Path, station: str
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    served = graph.station_lines(station)
    assert _station_bundle_offset_span(graph, station, offsets) == (
        min(offsets[station, lid] for lid in served),
        max(offsets[station, lid] for lid in served),
    )


def test_a_merge_riser_steps_out_past_the_lines_turning_inside_it() -> None:
    graph, _offsets, plan = _plan(
        ROOT / "tests" / "fixtures" / "route_reservations" / "reportho.metro"
    )
    step = graph_offset_step(graph)
    report = _route(plan, "__junction_12", "__merge_5", "report")
    main = _route(plan, "__junction_12", "fetch_seqs__entry_left_6", "main")
    assert report.points[1][0] - main.points[1][0] == pytest.approx(step)
    assert report.curve_radii is not None and main.curve_radii is not None
    assert main.curve_radii[0] == pytest.approx(CURVE_RADIUS)
    assert report.curve_radii[0] == pytest.approx(CURVE_RADIUS + step)


def test_merge_feeders_land_on_the_trunk_their_line_converges_on() -> None:
    _graph, _offsets, plan = _plan(TOPOLOGIES / "exit_run_three_drop_columns.mmd")
    trunk = _route(plan, "__junction_7", "__merge_4", "report")
    for source in ("__junction_8", "__junction_9"):
        feeder = _route(plan, source, "__merge_4", "report")
        assert (
            point_to_polyline_distance(feeder.points[-1], list(trunk.points))
            <= COORD_TOLERANCE
        )
