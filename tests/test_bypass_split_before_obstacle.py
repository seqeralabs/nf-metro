"""A line leaving its bundle to go round a section splits at the last gap
before that section, not beside its source.

A bundle stays together as long as it can: the line that must bypass a section
in its row runs on with its bundle-mates and drops beside that section's near
face.  A junction fan, or a gap channel shared by several bypasses, turns
together, so one member whose own target lies before the obstacle holds the
whole group back to its target.  All geometry is read from the render plan.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

import nf_metro.layout.routing.inter_section_handlers as ish
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import EDGE_TO_BUNDLE_CLEARANCE
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.render import build_render_plan
from nf_metro.render.plan import RenderPlan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
CANVAS_DATA = Path(__file__).parent / "data" / "canvas_placement"
TOL = 0.5

RL_OPTION_SETS: dict[str, dict[str, dict[str, object]]] = {
    "manual_rl_row_nonconsumer_bypass": {
        "o0": {"track_gap": 0.5, "x_spacing": 120.0},
        "o1": {
            "y_spacing": 25.0,
            "track_gap": 1.5,
            "center_ports": True,
            "section_x_gap": 20.0,
        },
        "o2": {
            "row_align": "top",
            "fold_threshold": 8,
            "track_gap": 0.0,
            "compact_offsets": False,
        },
    },
    "packed_cell_cellmate_bypass": {
        "o0": {
            "line_order": "span",
            "compact_offsets": True,
            "row_align": "content",
            "track_gap": 0.5,
        },
        "o1": {"diamond_style": "straight", "track_gap": 0.5},
        "o2": {"row_align": "top", "fold_threshold": 4, "x_spacing": 60.0},
    },
}

# (map, options, the bypassing line, its target port, the section it goes round)
SPLITS = [
    *(
        pytest.param(
            TOPOLOGIES / f"{stem}.mmd",
            options,
            "rna",
            "reporting__entry_right_11",
            "realign",
            id=f"{stem}-{set_id}",
        )
        for stem, sets in RL_OPTION_SETS.items()
        for set_id, options in {"default": {}, **sets}.items()
    ),
    *(
        pytest.param(CANVAS_DATA / f"{stem}.mmd", {}, "rna", target, "realign", id=stem)
        for stem, target in (
            ("bypass_split_lr_fan", "reporting__entry_left_11"),
            ("bypass_split_lr_single", "reporting__entry_left_10"),
            ("bypass_split_lr_fan_beyond", "reporting__entry_left_10"),
            ("bypass_split_rl_fan_beyond", "reporting__entry_right_10"),
            ("bypass_split_lr_fan_top_beyond", "reporting__entry_left_10"),
            ("bypass_split_lr_fan_top_under", "reporting__entry_left_10"),
            ("bypass_split_rl_fan_top_under", "reporting__entry_right_10"),
        )
    ),
]

FANS = [
    CANVAS_DATA / "bypass_split_lr_fan_target_before.mmd",
    CANVAS_DATA / "bypass_split_lr_fan_beyond.mmd",
    CANVAS_DATA / "bypass_split_rl_fan_target_before.mmd",
    CANVAS_DATA / "bypass_split_rl_fan_beyond.mmd",
    CANVAS_DATA / "bypass_split_lr_fan_top_beyond.mmd",
    CANVAS_DATA / "bypass_split_lr_fan_top_under.mmd",
    CANVAS_DATA / "bypass_split_rl_fan_top_under.mmd",
]

# The fan every fan witness turns, and the section of its branch whose
# cross-row target lies before the obstacle.
JUNCTION = "__junction_12"
EARLY_TARGET = "far"
TARGET_BEFORE_OBSTACLE = [
    CANVAS_DATA / "bypass_split_lr_fan_target_before.mmd",
    CANVAS_DATA / "bypass_split_rl_fan_target_before.mmd",
]


def _plan(path: Path, options: dict[str, object]) -> RenderPlan:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = prepare_graph(
            path.read_text(),
            layout_options=options or None,
            source_dir=str(path.parent),
        )
        return build_render_plan(graph, resolve_theme(None, graph))


def _drawn(plan: RenderPlan, route: Any) -> list[tuple[float, float]]:
    return apply_route_offsets(route, plan.station_offsets)


def _first_riser(points: list[tuple[float, float]]) -> float | None:
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if abs(x1 - x0) < 1e-6 and abs(y1 - y0) > 1.0:
            return x0
    return None


def _route(plan: RenderPlan, target: str, line: str) -> Any:
    (route,) = [r for r in plan.routes if r.edge.target == target and r.line_id == line]
    return route


def _bundle_width(plan: RenderPlan, source: str) -> float:
    lines = {r.line_id for r in plan.routes if r.edge.source == source}
    lanes = [plan.station_offsets[(source, line)] for line in lines]
    return max(lanes) - min(lanes)


def _id(value: object) -> str:
    return value.stem if isinstance(value, Path) else ""


@pytest.mark.parametrize(("path", "options", "line", "target", "obstacle"), SPLITS)
def test_the_bypass_splits_beside_the_section_it_goes_round(
    path: Path, options: dict[str, object], line: str, target: str, obstacle: str
) -> None:
    plan = _plan(path, options)
    route = _route(plan, target, line)
    points = _drawn(plan, route)
    riser = _first_riser(points)
    assert riser is not None
    box = plan.graph.sections[obstacle]
    leftward = points[-1][0] < points[0][0]
    face = box.bbox_x + box.bbox_w if leftward else box.bbox_x
    clear_of_face = riser - face if leftward else face - riser
    reach = EDGE_TO_BUNDLE_CLEARANCE + _bundle_width(plan, route.edge.source)
    assert EDGE_TO_BUNDLE_CLEARANCE - TOL <= clear_of_face <= reach + TOL, (
        f"{line} drops at x={riser:.1f}, {clear_of_face:.1f} off {obstacle}'s "
        f"near face {face:.1f}"
    )


@pytest.mark.parametrize("path", FANS, ids=_id)
def test_every_turning_branch_of_a_fan_turns_through_one_column(path: Path) -> None:
    plan = _plan(path, {})
    risers = [
        riser
        for r in plan.routes
        if r.edge.source == JUNCTION
        for riser in [_first_riser(_drawn(plan, r))]
        if riser is not None
    ]
    assert len(risers) >= 2
    assert max(risers) - min(risers) <= _bundle_width(plan, JUNCTION) + TOL, risers


@pytest.mark.parametrize("path", TARGET_BEFORE_OBSTACLE, ids=_id)
def test_a_fan_turns_before_a_branch_target_that_lies_before_the_obstacle(
    path: Path,
) -> None:
    plan = _plan(path, {})
    box = plan.graph.sections[EARLY_TARGET]
    for r in plan.routes:
        if r.edge.source != JUNCTION:
            continue
        points = _drawn(plan, r)
        riser = _first_riser(points)
        if riser is None:
            continue
        leftward = box.bbox_x < points[0][0]
        near_face = box.bbox_x + box.bbox_w if leftward else box.bbox_x
        past = near_face - riser if leftward else riser - near_face
        assert past < 0, (
            f"{r.line_id} turns at x={riser:.1f}, past {EARLY_TARGET}'s near "
            f"face {near_face:.1f}"
        )


@pytest.mark.parametrize(
    ("path", "options"),
    [pytest.param(*split.values[:2], id=split.id) for split in SPLITS],
)
def test_the_split_needs_no_clearance_push(
    path: Path, options: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    pushes: list[tuple[float, float]] = []
    clear = ish._clear_channel_x_in_band

    def record(graph: Any, x: float, *args: Any, **kwargs: Any) -> float:
        cleared = clear(graph, x, *args, **kwargs)
        if abs(cleared - x) > 1e-9:
            pushes.append((x, cleared))
        return cleared

    monkeypatch.setattr(ish, "_clear_channel_x_in_band", record)
    _plan(path, options)
    assert pushes == []
