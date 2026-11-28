"""A junction fan seats its branches in the lanes the lane plan records there.

The fan's slots follow the record's lanes at the junction, read travel-left
first; a line running straight on takes no slot shift.  A fan's lone turning
line descends in a gap slot seated with the gap's other channels, and a
descent seated alone where a counter-running channel stands takes its side of
the pair the gap holds.  A wrap of a fan line from another source running the
fan's inter-row channel takes the lane the fan gives that line.
A fan corner is sized by the lines that turn through it, read in the frame of
each branch's first turn, and a restacked trunk's corner nests only with the
trunks that turn with it, so a corner turning alone keeps the base radius.
Merge risers read the same record, and a station's pill covers every line that
stops there.
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
from nf_metro.layout.route_topology import divergence_junction_sources
from nf_metro.layout.routing.context import (
    _classify_merge_edges,
    _compute_junction_fan_info,
)
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.layout.routing.reserved_bands import ReservedCorridors
from nf_metro.parser.model import MetroGraph
from nf_metro.parser.route_topology import build_route_topology_query
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
            graph, resolve_theme(graph, mode=None), RenderConfig()
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


def _record_fan_info(
    graph: MetroGraph, offsets: dict[tuple[str, str], float]
) -> dict[tuple[str, str, str], tuple[int, int]]:
    topology = build_route_topology_query(graph)
    merge = _classify_merge_edges(
        graph, graph.junction_ids, topology, ReservedCorridors()
    )
    return _compute_junction_fan_info(
        graph,
        divergence_junction_sources(graph, topology),
        {lid: i for i, lid in enumerate(graph.lines)},
        skip_edges=merge.skip_edges | merge.index_exclude,
        topology=topology,
        station_offsets=offsets,
    )


def test_fan_slots_follow_the_record_with_lines_running_straight_on() -> None:
    graph, offsets, _ = _plan(REGRESSIONS / "approach_order_interleaved_feeders.mmd")
    fan = _record_fan_info(graph, offsets)
    slots = {
        line_id: fan["__junction_9", target, line_id][0]
        for target, line_id in (
            ("z2__entry_left_6", "m2"),
            ("z2__entry_left_6", "m3"),
            ("z3__entry_left_5", "m1"),
        )
    }
    # Leaving rightward, travel-left is screen-up: slots run in the record's
    # lane order whichever of the lines turns through the corner.
    assert sorted(slots, key=slots.__getitem__) == sorted(
        slots, key=lambda lid: offsets["__junction_9", lid]
    )


def _vertical_leg(route, down: bool) -> float:
    return next(
        a[0]
        for a, b in zip(route.points, route.points[1:])
        if abs(a[0] - b[0]) <= COORD_TOLERANCE
        and abs(b[1] - a[1]) > COORD_TOLERANCE
        and (b[1] > a[1]) is down
    )


@pytest.mark.parametrize(
    ("path", "descent", "riser"),
    [
        (
            TOPOLOGIES / "member_channel_runway_candidate_outside_band.mmd",
            ("__junction_8", "sec4__entry_left_4", "l0"),
            ("__junction_9", "__merge_2", "l1"),
        ),
        (
            REGRESSIONS / "approach_order_interleaved_feeders.mmd",
            ("__junction_9", "z3__entry_left_5", "m1"),
            ("__junction_8", "z2__entry_left_6", "m0"),
        ),
    ],
    ids=["descent-beside-convergence-riser", "lone-turner-beside-riser"],
)
def test_a_lone_descent_and_the_riser_against_it_share_their_gap(
    path: Path, descent: tuple[str, str, str], riser: tuple[str, str, str]
) -> None:
    _graph, _offsets, plan = _plan(path)
    down = _vertical_leg(_route(plan, *descent), down=True)
    up = _vertical_leg(_route(plan, *riser), down=False)
    assert abs(down - up) >= BUNDLE_TO_BUNDLE_CLEARANCE - COORD_TOLERANCE


def _gap_run(route) -> float:
    a, _b = max(
        (
            (a, b)
            for a, b in zip(route.points, route.points[1:])
            if abs(a[1] - b[1]) <= COORD_TOLERANCE
        ),
        key=lambda leg: abs(leg[1][0] - leg[0][0]),
    )
    return a[1]


@pytest.mark.parametrize(
    ("path", "fan_target", "fan_lines", "other", "other_line"),
    [
        (
            REGRESSIONS / "riboseq_row_trunk_subrun.mmd",
            "orf_calling__entry_left_8",
            ("riboseq", "tiseq"),
            ("novel_transcripts__exit_right_2", "te__entry_left_9"),
            "rnaseq",
        ),
        (
            REPROS / "inter_row_corridor_overflow.mmd",
            "calling__entry_left_8",
            ("la", "lc"),
            ("discovery__exit_right_2", "ratio__entry_left_9"),
            "lb",
        ),
    ],
    ids=["riboseq_row", "inter_row"],
)
def test_a_line_runs_its_fan_lane_in_a_gap_the_fan_shares(
    path: Path,
    fan_target: str,
    fan_lines: tuple[str, ...],
    other: tuple[str, str],
    other_line: str,
) -> None:
    """Every line in the gap holds the lane the fan records for it, packed:
    a straight-on fan line's lane carries that line's own wrap, not a hole."""
    graph, offsets, plan = _plan(path)
    step = graph_offset_step(graph)
    runs = {
        lid: _gap_run(_route(plan, "__junction_12", fan_target, lid))
        for lid in fan_lines
    }
    runs[other_line] = _gap_run(_route(plan, *other, other_line))
    fan = _record_fan_info(graph, offsets)
    slot = {
        lid: pos for (jid, _t, lid), (pos, _n) in fan.items() if jid == "__junction_12"
    }
    ys = [runs[lid] for lid in sorted(runs, key=slot.__getitem__)]
    steps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(abs(abs(d) - step) <= COORD_TOLERANCE for d in steps), ys
    assert len({d > 0 for d in steps}) == 1, ys


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
    ("path", "source", "turners"),
    [
        (
            REPROS / "inter_row_corridor_overflow.mmd",
            "__junction_12",
            (("calling__entry_left_8", "la"), ("calling__entry_left_8", "lc")),
        ),
        (
            TOPOLOGIES / "seed72_cross_family_fan.mmd",
            "__junction_4",
            (("exempt_target__entry_right_3", "exempt"),),
        ),
    ],
    ids=["two_turners", "lone_turner"],
)
def test_a_fan_corner_is_sized_by_the_lines_turning_through_it(
    path: Path, source: str, turners: tuple[tuple[str, str], ...]
) -> None:
    _graph, _offsets, plan = _plan(path)
    radii = []
    for target, line_id in turners:
        route = _route(plan, source, target, line_id)
        assert route.curve_radii is not None
        radii.append(route.curve_radii)
    step = graph_offset_step(_graph)
    assert sorted(r[0] for r in radii) == [
        CURVE_RADIUS + k * step for k in range(len(radii))
    ]
    if len(radii) == 1:
        assert radii[0][-1] == CURVE_RADIUS


def test_an_around_below_branch_reads_its_slot_in_its_descents_frame() -> None:
    _graph, _offsets, plan = _plan(TOPOLOGIES / "bypass_left_entry_from_right.mmd")
    wrap = _route(plan, "__junction_3", "target__entry_left_1", "main")
    mate = _route(plan, "__junction_3", "sibling__entry_left_2", "side")
    step = graph_offset_step(_graph)
    assert wrap.curve_radii is not None and mate.curve_radii is not None
    assert wrap.curve_radii[:2] == (CURVE_RADIUS + step,) * 2
    assert mate.curve_radii[0] == CURVE_RADIUS
    assert wrap.curve_radii[2:] == (CURVE_RADIUS, CURVE_RADIUS)


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
