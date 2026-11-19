"""A junction fan seats its branches in the lanes the lane plan records there.

The fan's slots follow the record's lanes at the junction, read travel-left
first.  Three consumers of the slots read them only where a fan-mate shares the
leg: a branch running straight on takes no slot shift, a branch alone in the
fan's corridor band runs on the band centre, and a restacked trunk's corner
nests only with the trunks that turn with it, so a corner turning alone keeps
the base radius.  Merge risers and entry-frame pills read the same record.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.api import RenderConfig, prepare_graph, render_graph_result, render_string
from nf_metro.layout.constants import CURVE_RADIUS, graph_offset_step
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


def test_a_lone_wrap_run_is_not_bundled_with_an_opposite_runner() -> None:
    graph, _offsets, plan = _plan(
        TOPOLOGIES / "packed_cell_right_exit_left_entry_wrap.mmd"
    )
    step = graph_offset_step(graph)
    wrap = _route(plan, "__junction_11", "polish__entry_left_7", "short")
    run_y = wrap.points[3][1]
    assert wrap.points[3][0] < wrap.points[2][0]
    rightward = [
        a[1]
        for route in plan.routes
        if route.line_id != "short"
        for a, b in zip(route.points, route.points[1:])
        if abs(a[1] - b[1]) < 1e-6
        and b[0] > a[0]
        and min(a[0], b[0]) < wrap.points[2][0]
        and max(a[0], b[0]) > wrap.points[3][0]
    ]
    assert rightward
    assert min(abs(y - run_y) for y in rightward) > 2 * step


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


@pytest.mark.parametrize("station", ["before", "split"])
def test_an_entry_frame_pill_covers_the_sole_flow_entrys_lines(station: str) -> None:
    path = TOPOLOGIES / "external_owner_exit_lane_frame.mmd"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    offsets = compute_station_offsets(graph)
    cohort = ("lower", "straight", "wrap")
    assert set(graph.station_lines(station)) == {*cohort, "ends"}
    assert _station_bundle_offset_span(graph, station, offsets) == (
        min(offsets[station, lid] for lid in cohort),
        max(offsets[station, lid] for lid in cohort),
    )
