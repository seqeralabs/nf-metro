"""The canvas is placed once, after every routing decision, as one move.

No routing or lane-plan decision reads where the canvas edge is, so a laid-out
map moved together with its canvas origin routes the same way and renders the
same picture: the render settles it once and translates the settled geometry
onto the canvas, rather than settling a moved copy again.
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

import nf_metro.render.svg as svg
from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.phases.canvas import translate_graph
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    assert_junctions_on_their_anchors,
)
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

# Stress maps whose routing once read the canvas edge: a left-margin descent
# capped by its room from x = 0, a band above a topmost or empty row measured
# from y = 0, a gap channel centred on a span reaching the origin.
STRESS_MAPS: tuple[tuple[str, dict[str, object]], ...] = (
    ("f8400385.mmd", {}),
    ("f8400846.mmd", {}),
    ("f8400937.mmd", {}),
    ("f8401369.mmd", {}),
    ("f8401830.mmd", {}),
    ("f8402199.mmd", {}),
    ("f8402349.mmd", {}),
    ("f8403899.mmd", {}),
    ("f8404235.mmd", {}),
    ("f8404507.mmd", {}),
    ("f8404589.mmd", {}),
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
)

PACKED_CELL_OPTIONS: dict[str, object] = {
    "x_spacing": 60.0,
    "stroke_scale": 0.6,
    "track_gap": 3.0,
    "compact_offsets": False,
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?")


def _same_picture(first: str, second: str) -> bool:
    """Whether two SVG documents draw the same picture.

    Text outside the numbers matches exactly.  The numbers are printed at full
    precision, and a coordinate routed 256px further along rounds its last
    binary digit differently, so they match to within a millionth of a pixel.
    """
    if _NUMBER.split(first) != _NUMBER.split(second):
        return False
    return all(
        a == b or abs(float(a) - float(b)) <= 1e-6
        for a, b in zip(_NUMBER.findall(first), _NUMBER.findall(second), strict=True)
    )


def _laid_out(path: Path, options: dict[str, object]) -> MetroGraph:
    return prepare_graph(
        path.read_text(), layout_options=options or None, source_dir=str(path.parent)
    )


def _render(graph: MetroGraph) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return render_graph(graph, resolve_theme(None, graph), RenderConfig())


def _moved_with_its_canvas(graph: MetroGraph) -> MetroGraph:
    moved = copy.deepcopy(graph)
    translate_graph(moved, *MOVE)
    moved.canvas_origin = MOVE
    return moved


def _assert_moved_map_renders_the_same(path: Path, options: dict[str, object]):
    graph = _laid_out(path, options)
    assert _same_picture(_render(_moved_with_its_canvas(graph)), _render(graph))


# A label beside a station on its section's vertical midpoint picks its side by
# ``station.y < y_mid`` (``labels._trial_cost``), and these midpoints tie to
# within one binary digit, which the move rounds the other way.
_LABEL_MIDPOINT_TIES = frozenset(
    {
        "tests/fixtures/curve_invariant_repros/inter_row_corridor_overflow.mmd",
        "tests/fixtures/curve_invariant_repros/riboseq_inter_row_corridor.mmd",
        "tests/fixtures/regressions/riboseq_row_trunk_subrun.mmd",
    }
)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            name,
            marks=pytest.mark.xfail(
                strict=True, reason="label side tie at the section midpoint"
            ),
        )
        if name in _LABEL_MIDPOINT_TIES
        else name
        for name in SLIDING_MAPS
    ],
)
def test_a_sliding_map_moved_with_its_canvas_renders_the_same_picture(
    name: str,
) -> None:
    _assert_moved_map_renders_the_same(ROOT / name, {})


@pytest.mark.parametrize(
    ("name", "options"),
    [
        *STRESS_MAPS,
        ("packed_cell_cellmate_bypass_no_handoff.mmd", PACKED_CELL_OPTIONS),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_stress_map_moved_with_its_canvas_renders_the_same_picture(
    name: str, options: dict[str, object]
) -> None:
    path = CANVAS_DATA / name
    if not path.exists():
        path = ROOT / "examples" / "topologies" / name
    _assert_moved_map_renders_the_same(path, options)


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
    assert plan.graph.canvas_origin == (0.0, 0.0)


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
        _render(graph)


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
        graph = _laid_out(path, {})
        theme = resolve_theme(None, graph)

        def settle(
            move: tuple[float, float],
        ) -> tuple[MetroGraph, svg._SettledRenderGeometry]:
            copied = svg._copy_graph_for_render(graph)
            translate_graph(copied, *move)
            return copied, svg._settle_render_geometry(
                copied,
                theme,
                svg.graph_offset_step(graph, theme.line_width),
                svg.SECTION_Y_GAP,
            )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            laid_out, at_layout = settle((0.0, 0.0))
            _, settled_moved = settle(MOVE)
        before = dict(_numbers(at_layout))
        expected = dict(_numbers(settled_moved))
        got = dict(_numbers(svg._move_onto_the_canvas(laid_out, at_layout, MOVE)))
        assert got.keys() == expected.keys()
        assert [key for key in expected if abs(got[key] - expected[key]) > 1e-6] == []
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


@pytest.mark.parametrize(
    "name",
    [
        "examples/topologies/bypass_left_entry_from_right.mmd",
        "examples/topologies/left_exit_wrap_under_own_section.mmd",
    ],
)
def test_a_wrap_into_the_topmost_row_stays_below_the_map_top(name: str) -> None:
    """The band above the topmost row is headroom over the whole map, a detour.

    Each of these wraps has a route that stays between or below the sections,
    so no run is drawn above the topmost section box.
    """
    graph = _laid_out(ROOT / name, {})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plan = build_render_plan(graph, resolve_theme(None, graph))
    boxes = [s for s in plan.graph.sections.values() if s.bbox_w > 0]
    top = min(s.bbox_y for s in boxes)
    highest = min(y for route in plan.routes for _x, y in route.points)
    assert highest >= top, f"{name} runs a line {top - highest:.1f}px over the map"
