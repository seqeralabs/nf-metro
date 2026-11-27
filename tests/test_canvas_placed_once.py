"""The canvas is placed once, after every routing decision, as one move.

No routing or lane-plan decision reads where the canvas edge is, so a laid-out
map settles the same wherever it is laid out: the render settles it once and
translates the settled geometry onto the canvas.
"""

from __future__ import annotations

import copy
import dataclasses
import re
import warnings
from collections.abc import Iterator
from enum import Enum
from pathlib import Path

import pytest
from conftest import content_corpus

import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph
from nf_metro.convert import convert_nextflow_dag
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    assert_junctions_on_their_anchors,
)
from nf_metro.layout.port_order_plan import approach_source
from nf_metro.layout.routing.inter_section_handlers import _gap_above_band_fits
from nf_metro.layout.routing.offsets import _recorded_approach
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).parents[1]
CANVAS_DATA = Path(__file__).parent / "data" / "canvas_placement"
MOVE = (256.0, 128.0)

# Every repository map whose render grows the canvas on its left or top side.
SLIDING_MAPS = (
    "examples/diagonal_labels.mmd",
    "examples/genomic_pipeline.mmd",
    "examples/riboseq_metro.mmd",
    "examples/topologies/around_below_ep_col_gt0.mmd",
    "examples/topologies/around_section_below.mmd",
    "examples/topologies/bt_to_lr.mmd",
    "examples/topologies/bypass_left_entry_from_right.mmd",
    "examples/topologies/clear_channel_target_aware_push.mmd",
    "examples/topologies/corridor_narrow_gap_fallback.mmd",
    "examples/topologies/dogleg_exempt_distinct.mmd",
    "examples/topologies/dogleg_exempt_sameline.mmd",
    "examples/topologies/fanout_bundle_plus_spurs.mmd",
    "examples/topologies/inter_row_exempt_band_order.mmd",
    "examples/topologies/inter_row_wrap_clearance.mmd",
    "examples/topologies/junction_entry_align.mmd",
    "examples/topologies/left_entry_from_above_far.mmd",
    "examples/topologies/left_entry_up_wrap.mmd",
    "examples/topologies/left_exit_wrap_under_own_section.mmd",
    "examples/topologies/lr_perp_top_exit_perp_entry.mmd",
    "examples/topologies/lr_perp_top_exit_perp_entry_diverging.mmd",
    "examples/topologies/lr_perp_top_exit_side_entry.mmd",
    "examples/topologies/lr_perpendicular_ports_overflow.mmd",
    "examples/topologies/merge_around_below_leftmost.mmd",
    "examples/topologies/merge_leftmost_sink_branch.mmd",
    "examples/topologies/orbit_perp_exit_back_row_entry.mmd",
    "examples/topologies/orbit_perp_exit_flow_entry.mmd",
    "examples/topologies/orbit_perp_exit_perp_entry.mmd",
    "examples/topologies/packed_cell_cellmate_bypass_entry_y.mmd",
    "examples/topologies/packed_cell_cellmate_bypass_no_handoff.mmd",
    "examples/topologies/rail_inter_section.mmd",
    "examples/topologies/row_trunk_partial_through_line.mmd",
    "examples/topologies/samerow_bypass_over_top_flat_bundle.mmd",
    "examples/topologies/self_crossing_bridge.mmd",
    "examples/topologies/serpentine_grid_tall_bundle.mmd",
    "examples/topologies/serpentine_grid_wide_bundle.mmd",
    "examples/topologies/stacked_left_exit_drop.mmd",
    "examples/topologies/stacked_multiline_left_exit_drop.mmd",
    "examples/topologies/stacked_split_left_entry_drop.mmd",
    "examples/topologies/straddling_fanout_junction.mmd",
    "examples/topologies/tb_internal_diagonal.mmd",
    "examples/topologies/tb_lr_exit_left.mmd",
    "examples/topologies/tb_lr_exit_right.mmd",
    "examples/topologies/top_entry_two_corridors_split_targets.mmd",
    "examples/topologies/wrap_return_canvas_margin.mmd",
    "tests/fixtures/curve_invariant_repros/ascent_fan_blocked_turn_order.mmd",
    "tests/fixtures/curve_invariant_repros/bt_bottom_entry_bundle_rises_from_junction.mmd",
    "tests/fixtures/curve_invariant_repros/bt_bottom_entry_rise_from_junction.mmd",
    "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
    "tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd",
    "tests/fixtures/diagonal_single_trunk_off_track.mmd",
    "tests/fixtures/group_band_over_row_corridor.mmd",
    "tests/fixtures/regressions/cohort_fold_compressed.mmd",
    "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd",
    "tests/fixtures/regressions/entry_trunk_row_bow.mmd",
    "tests/fixtures/regressions/perp_entry_column_merge_feeder.mmd",
    "tests/fixtures/regressions/perp_entry_column_two_feeders.mmd",
    "tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd",
    "tests/fixtures/regressions/stacked_collector_fanin.mmd",
    "tests/fixtures/target_entry_runway_bypass.mmd",
    "tests/fixtures/topologies/novel_transcripts_repro.mmd",
)

# Stress maps on which a left-margin descent, a band above a topmost or empty
# row, or a gap channel beside an absent column would read the canvas edge.
STRESS_MAPS: tuple[tuple[str, dict[str, object]], ...] = (
    ("f8400385.mmd", {}),
    ("f8400846.mmd", {}),
    ("f8400937.mmd", {}),
    ("f8401369.mmd", {}),
    ("f8401830.mmd", {}),
    ("f8402199.mmd", {}),
    ("f8402349.mmd", {}),
    ("f8403899.mmd", {}),
    ("f8404507.mmd", {}),
    ("f8404589.mmd", {}),
    ("f8404703.mmd", {}),
    ("f8500171.mmd", {}),
    ("f8500191.mmd", {}),
    ("f8500201.mmd", {}),
    ("f8500304.mmd", {}),
    ("f8500934.mmd", {}),
    ("f8400004.mmd", {"compact_offsets": False, "stroke_scale": 2.0}),
    (
        "f8400212.mmd",
        {"section_y_gap": 80.0, "track_gap": 3.0, "compact_offsets": True},
    ),
    ("f8400369.mmd", {"line_order": "span", "center_ports": False}),
    ("f8400846.mmd", {"stroke_scale": 0.6}),
    ("bypass_split_lr_fan.mmd", {}),
    ("bypass_split_lr_single.mmd", {}),
    ("bypass_split_lr_fan_beyond.mmd", {}),
    ("bypass_split_lr_fan_target_before.mmd", {}),
    ("bypass_split_rl_fan_beyond.mmd", {}),
    ("bypass_split_rl_fan_target_before.mmd", {}),
    ("bypass_split_lr_fan_top_beyond.mmd", {}),
    ("bypass_split_lr_fan_top_under.mmd", {}),
    ("bypass_split_rl_fan_top_under.mmd", {}),
)

PACKED_CELL_OPTIONS: dict[str, object] = {
    "x_spacing": 60.0,
    "stroke_scale": 0.6,
    "track_gap": 3.0,
    "compact_offsets": False,
}


def _laid_out(path: Path, options: dict[str, object]) -> MetroGraph:
    return prepare_graph(
        path.read_text(), layout_options=options or None, source_dir=str(path.parent)
    )


def _settled(
    graph: MetroGraph,
) -> tuple[MetroGraph, svg._SettledRenderGeometry]:
    """The render's one settle of *graph*, before the canvas move."""
    settles: list[tuple[MetroGraph, svg._SettledRenderGeometry]] = []
    settle = svg._settle_render_geometry

    def keep(
        copied: MetroGraph, *args: object, **kwargs: object
    ) -> svg._SettledRenderGeometry:
        settled = settle(copied, *args, **kwargs)  # type: ignore[arg-type]
        settles.append(copy.deepcopy((copied, settled)))
        return settled

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as patch:
        warnings.simplefilter("ignore")
        patch.setattr(svg, "_settle_render_geometry", keep)
        build_render_plan(graph, resolve_theme(None, graph))
    (only,) = settles
    return only


def _settled_numbers(
    path: Path, options: dict[str, object]
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Every settled number of *path* as settled, then moved by :data:`MOVE`,
    and as settled when the laid-out map is moved first."""
    graph = _laid_out(path, options)
    moved = copy.deepcopy(graph)
    translate_graph(moved, *MOVE)
    laid_out, at_layout = _settled(graph)
    before = dict(_numbers(at_layout))
    after = dict(_numbers(svg._move_onto_the_canvas(laid_out, at_layout, MOVE)))
    return before, after, dict(_numbers(_settled(moved)[1]))


def _assert_moving_commutes_with_settling(
    path: Path, options: dict[str, object]
) -> None:
    _before, after, expected = _settled_numbers(path, options)
    assert after.keys() == expected.keys()
    assert [key for key in expected if abs(after[key] - expected[key]) > 1e-6] == []


@pytest.mark.parametrize("name", SLIDING_MAPS)
def test_a_sliding_map_settles_the_same_wherever_it_is_laid_out(
    name: str,
) -> None:
    _assert_moving_commutes_with_settling(ROOT / name, {})


@pytest.mark.parametrize(
    ("name", "options"),
    [
        *STRESS_MAPS,
        ("packed_cell_cellmate_bypass_no_handoff.mmd", PACKED_CELL_OPTIONS),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_stress_map_settles_the_same_wherever_it_is_laid_out(
    name: str, options: dict[str, object]
) -> None:
    path = CANVAS_DATA / name
    if not path.exists():
        path = ROOT / "examples" / "topologies" / name
    _assert_moving_commutes_with_settling(path, options)


def test_the_render_settles_once_and_draws_that_settle_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drawn routes are the one settle's routes, translated by one move."""
    path = ROOT / "examples/topologies/wrap_return_canvas_margin.mmd"
    graph = _laid_out(path, {})
    settles: list[list[list[tuple[float, float]]]] = []
    settle = svg._settle_render_geometry

    def spy(*args, **kwargs):
        settled = settle(*args, **kwargs)
        settles.append([list(route.points) for route in settled.routes])
        return settled

    monkeypatch.setattr(svg, "_settle_render_geometry", spy)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plan = build_render_plan(graph, resolve_theme(None, graph))

    assert len(settles) == 1
    drawn = [list(route.points) for route in plan.routes]
    dx = drawn[0][0][0] - settles[0][0][0][0]
    dy = drawn[0][0][1] - settles[0][0][0][1]
    assert dx > 0, "the map should grow its canvas on the left"
    assert all(
        abs(x - sx - dx) <= 1e-9 and abs(y - sy - dy) <= 1e-9
        for route, settled in zip(drawn, settles[0], strict=True)
        for (x, y), (sx, sy) in zip(route, settled, strict=True)
    )


def test_a_junction_off_its_anchor_is_refused_not_moved() -> None:
    graph = _laid_out(ROOT / "examples/rnaseq_sections.mmd", {})
    assert_junctions_on_their_anchors(graph)
    jid = next(iter(graph.junctions))
    junction = graph.stations[jid]
    junction.y += 7.0
    laid_out = junction.x, junction.y
    with pytest.raises(LayoutInvariantError, match=jid):
        assert_junctions_on_their_anchors(graph)
    assert (junction.x, junction.y) == laid_out
    with pytest.raises(LayoutInvariantError, match=jid):
        build_render_plan(graph, resolve_theme(None, graph))


def _numbers(value: object, where: str = "") -> Iterator[tuple[str, float]]:
    """Every number *value* holds, keyed by where it sits in it."""
    if isinstance(value, bool) or value is None or isinstance(value, str | Enum):
        return
    if isinstance(value, int | float):
        yield where, float(value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _numbers(getattr(value, item.name), f"{where}.{item.name}")
    elif isinstance(value, tuple) and hasattr(value, "_fields"):
        for name in value._fields:
            yield from _numbers(getattr(value, name), f"{where}.{name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numbers(item, f"{where}[{key!r}]")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _numbers(item, f"{where}[{index}]")
    elif isinstance(value, set | frozenset):
        for index, item in enumerate(sorted(value, key=repr)):
            yield from _numbers(item, f"{where}{{{index}}}")


# Between them these maps hold every kind of record that names a canvas
# coordinate: member geometry, accepted cohort owners and their control
# recipes, convergence trunks, landings and continuations, exit-turn axes and
# lane transitions, corridor claims, realised reservations and settlement
# translations.
RECORD_MAPS = (
    ROOT / "examples/topologies/merge_trunk_over_low_section.mmd",
    ROOT / "examples/diagonal_labels.mmd",
    CANVAS_DATA / "f8400149.mmd",
)


def test_the_canvas_move_carries_every_settled_record() -> None:
    """Moving a settled map is settling it where it is moved, in every record.

    The plan and the settlement name canvas coordinates as well as the
    drawing, and a record the move leaves behind describes the routes in
    another frame than the one they are drawn in.
    """
    moved_fields: set[str] = set()
    for path in RECORD_MAPS:
        before, after, expected = _settled_numbers(path, {})
        assert after.keys() == expected.keys()
        assert [key for key in expected if abs(after[key] - expected[key]) > 1e-6] == []
        moved_fields |= {
            re.sub(r"\[[^]]*\]", "", key)
            for key, value in expected.items()
            if value != before[key]
        }
    assert {
        ".route_plan.member_geometry_plans.points",
        ".route_plan.member_geometry_plans.gap_channels.start",
        ".route_plan.accepted_corridor_cohort_plan.owners.accepted_coordinate",
        ".route_plan.accepted_corridor_cohort_plan.owners.recipe.fixed_points.coordinate",
        ".route_plan.accepted_corridor_cohort_plan.owners.recipe.directed_runways"
        ".anchor_coordinate",
        ".route_plan.convergence_plans.trunk_axis.coordinate",
        ".route_plan.convergence_plans.landings.cross_run_start_coordinate",
        ".route_plan.convergence_plans.outgoing_continuations.start_point",
        ".route_plan.convergence_plans.endpoint_ownership.endpoint",
        ".route_plan.exit_turn_plans.axes.coordinate",
        ".route_plan.exit_turn_plans.lane_transitions.source_point",
        ".route_plan.exit_turn_plans.assignments.launch_coordinate",
        ".route_plan.reservations.claims.allocation_coordinate",
        ".route_plan.realised_reservations.coordinate",
        ".route_plan.realised_reservations.coordinate_translations.coordinate",
        ".settlement.translations.coordinate",
        ".settlement.coordinate_translations.coordinate",
    } <= moved_fields


def _render_plan(path: Path):
    graph = _laid_out(path, {})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_render_plan(graph, resolve_theme(None, graph))


def _parsed(path: Path, is_nextflow: bool) -> MetroGraph:
    text = path.read_text()
    return parse_metro_mermaid(convert_nextflow_dag(text) if is_nextflow else text)


@pytest.mark.parametrize(
    ("path", "is_nextflow"),
    [pytest.param(path, nf, id=fid) for fid, path, nf in content_corpus()],
)
def test_the_band_above_the_topmost_row_is_never_a_gap(
    path: Path, is_nextflow: bool
) -> None:
    """Above the topmost row lies canvas headroom over the whole map, never a
    band of its own: a run with no recorded side takes it only to join a run
    into its entry that the approach record sends round above."""
    graph = _parsed(path, is_nextflow)
    if not graph.sections:
        pytest.skip("a flat map has no rows")
    topmost = min(s.grid_row for s in graph.sections.values())
    assert _gap_above_band_fits(graph, topmost) is False


# Maps whose every run stays below the map top, or (map, a hop recorded below)
# where another hop goes over it.
BELOW_THE_MAP_TOP: tuple[tuple[Path, tuple[str, str] | None], ...] = (
    *(
        (path, None)
        for path in (
            ROOT / "examples/topologies/bypass_left_entry_from_right.mmd",
            ROOT / "examples/topologies/left_exit_wrap_under_own_section.mmd",
            ROOT / "examples/topologies/bypass_leftward_far_side_entry.mmd",
            CANVAS_DATA / "f8401267.mmd",
            CANVAS_DATA / "f8402283.mmd",
        )
    ),
    (CANVAS_DATA / "f8404063.mmd", ("sec3__entry_left_3", "sec2__exit_left_1")),
    (CANVAS_DATA / "f8404575.mmd", ("sec3__entry_left_4", "sec2__exit_left_1")),
)


@pytest.mark.parametrize(
    ("path", "hop"),
    BELOW_THE_MAP_TOP,
    ids=[path.stem for path, _hop in BELOW_THE_MAP_TOP],
)
def test_a_wrap_into_the_topmost_row_stays_below_the_map_top(
    path: Path, hop: tuple[str, str] | None
) -> None:
    """A hop whose approach record sends it below stays below the map top: no
    run of it is drawn above the topmost box."""
    graph = _laid_out(path, {})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plan = build_render_plan(graph, resolve_theme(None, graph))
    routes = [
        route
        for route in plan.routes
        if hop is None
        or (
            route.edge.target == hop[0]
            and approach_source(graph, route.edge.source, route.edge.line_id) == hop[1]
        )
    ]
    assert routes
    if hop is not None:
        assert {_recorded_approach(graph, route.edge) for route in routes} == {"below"}
    top = min(s.bbox_y for s in plan.graph.sections.values() if s.bbox_w > 0)
    highest = min(y for route in routes for _x, y in route.points)
    assert highest >= top, f"{path.stem} runs a line {top - highest:.1f}px over the map"


# Each loops a bundle over the top into a side entry of its topmost row and
# sends another line there: f8402603 from the loop's junction into a second
# section, the others from another source into the loop's own entry.
JOINED_OVER_TOP = (
    (CANVAS_DATA / "f8402603.mmd", "sec1__entry_right_3", ("l2", "l3")),
    (CANVAS_DATA / "f8403686.mmd", "sec2__entry_right_4", ("l0",)),
    (CANVAS_DATA / "w2_01522.mmd", "z3__entry_right_5", ("m1",)),
    (
        ROOT / "examples/topologies/fan_over_top_reach_order.mmd",
        "c__entry_right_3",
        ("l1",),
    ),
    (
        ROOT / "tests/fixtures/regressions/outward_loop_top_right_entry.mmd",
        "b__entry_right_2",
        ("m3",),
    ),
    (
        ROOT / "tests/fixtures/regressions/departure_order_perpendicular_seam.mmd",
        "z4__entry_right_7",
        ("m3",),
    ),
    (
        ROOT / "tests/fixtures/regressions/outward_loop_top_left_entry.mmd",
        "b__entry_left_2",
        ("m3",),
    ),
)


@pytest.mark.parametrize(
    ("path", "entry", "lines"),
    [pytest.param(*joined, id=joined[0].stem) for joined in JOINED_OVER_TOP],
)
def test_a_line_joins_the_over_top_loop_it_shares_an_end_with(
    path: Path, entry: str, lines: tuple[str, ...]
) -> None:
    """A line into an entry an over-the-top bundle reaches, or from that
    bundle's junction, goes over the top with it rather than leaving the
    bundle to go round below the map."""
    plan = _render_plan(path)
    top = min(s.bbox_y for s in plan.graph.sections.values() if s.bbox_w > 0)
    row = plan.graph.sections[plan.graph.ports[entry].section_id]
    for line in lines:
        (route,) = [
            route
            for route in plan.routes
            if route.edge.target == entry and route.line_id == line
        ]
        ys = [y for _x, y in route.points]
        assert min(ys) < top, f"{line} into {entry} does not go over the top"
        assert max(ys) <= max(ys[0], row.bbox_y + row.bbox_h), (
            f"{line} goes round below"
        )


def test_a_line_joins_the_bundle_below_its_entry() -> None:
    """A bypass U-turn into an entry a BOTTOM exit's loop reaches from below
    joins that bundle below the target row rather than going over the map."""
    path = ROOT / "tests/fixtures/regressions/outward_loop_bottom_right_entry.mmd"
    plan = _render_plan(path)
    graph = plan.graph
    top = min(s.bbox_y for s in graph.sections.values() if s.bbox_w > 0)
    row = graph.sections[graph.ports["b__entry_right_2"].section_id]
    (route,) = [
        route
        for route in plan.routes
        if route.edge.target == "b__entry_right_2" and route.line_id == "m3"
    ]
    ys = [y for _x, y in route.points]
    assert min(ys) >= top, "m3 goes over the map"
    assert max(ys) > row.bbox_y + row.bbox_h, "m3 does not go round below"
