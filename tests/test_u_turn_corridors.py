"""A bypass U-turn's side is decided once, from the placement the lane plan
reads, ranked among the corridors that cross no other section, and holds on
the final drawing."""

from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import SAME_Y_TOLERANCE
from nf_metro.layout.port_order_plan import (
    PortOrderBuilder,
    SideRule,
    _Corridor,
    _CorridorHop,
    approach_source,
    port_travel,
)
from nf_metro.layout.routing.common import right_normal_axis_sign
from nf_metro.parser.model import MetroGraph
from nf_metro.render.svg import build_render_plan
from nf_metro.themes import resolve_theme

ROOT = Path(__file__).parents[1]
CANVAS_DATA = Path(__file__).parent / "data" / "canvas_placement"

_Key = tuple[str, str]


@dataclass(frozen=True)
class _Laid:
    sealed: MetroGraph
    """The graph as the lane plan read it when it was decided."""
    drawn: MetroGraph
    routes: tuple[object, ...]


@lru_cache(maxsize=None)
def _laid(path: Path) -> _Laid:
    seals: list[tuple[MetroGraph, object]] = []
    real = lane_plan_module.build_lane_plan

    def build(graph: MetroGraph) -> lane_plan_module.LanePlan:
        snapshot = copy.deepcopy(graph)
        plan = real(graph)
        seals.append((snapshot, plan))
        return plan

    settled: list[tuple[MetroGraph, svg._SettledRenderGeometry]] = []
    settle = svg._settle_once_on_the_canvas

    def keep(
        *args: object, **kwargs: object
    ) -> tuple[MetroGraph, svg._SettledRenderGeometry]:
        result = settle(*args, **kwargs)  # type: ignore[arg-type]
        settled.append(result)
        return result

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(lane_plan_module, "build_lane_plan", build)
        mp.setattr(svg, "_settle_once_on_the_canvas", keep)
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        build_render_plan(graph, resolve_theme(None, graph))
    sealed = next(snap for snap, built in seals if built is graph._lane_plan)
    drawn, geometry = settled[-1]
    return _Laid(sealed, drawn, tuple(geometry.routes))


def _builder(graph: MetroGraph) -> PortOrderBuilder:
    return PortOrderBuilder(graph, lambda _port: None, lambda _port, _order: None)


def _hop(graph: MetroGraph, key: _Key) -> _CorridorHop | None:
    return _builder(graph).corridor_hop(key)


def _corridors(graph: MetroGraph, key: _Key) -> tuple[_Corridor, _Corridor]:
    builder = _builder(graph)
    travel = builder.same_axis_u_turn(*key)
    assert travel is not None, key
    right, left, _span, _band = builder.side_corridors(*key, travel)
    return right, left


def _screen(graph: MetroGraph, key: _Key) -> str:
    approach = graph._lane_plan.port_orders.approaches[key]  # type: ignore[union-attr]
    sign = approach.side * right_normal_axis_sign(port_travel(graph.ports[key[0]]))
    return "below" if sign > 0 else "above"


# Every curated U-turn whose two clear corridors are equally long where the lane
# plan reads them, with the side the ranking gives it: the corridor in room
# the layout already has, else the turn-round side.
TIES: tuple[tuple[str, _Key, SideRule, str], ...] = (
    (
        "examples/topologies/samerow_left_exit_far_left_entry.mmd",
        ("te__entry_left_9", "psite_id__exit_left_4"),
        SideRule.CORRIDOR,
        "above",
    ),
    (
        "examples/topologies/bypass_left_entry_from_right.mmd",
        ("target__entry_left_1", "source__exit_left_0"),
        SideRule.CORRIDOR,
        "below",
    ),
    (
        "examples/topologies/bypass_leftward_far_side_entry.mmd",
        ("tgt_sec__entry_left_1", "src_sec__exit_left_0"),
        SideRule.TURN_ROUND,
        "below",
    ),
    (
        "examples/topologies/fan_over_top_reach_order.mmd",
        ("c__entry_right_3", "a__exit_right_0"),
        SideRule.TURN_ROUND,
        "above",
    ),
    (
        "examples/topologies/fan_over_top_reach_order.mmd",
        ("c__entry_right_3", "b__exit_right_1"),
        SideRule.TURN_ROUND,
        "above",
    ),
    (
        "tests/fixtures/curve_invariant_repros/fan_over_top_flow_feeder.mmd",
        ("sec3__entry_right_5", "sec1__exit_right_1"),
        SideRule.TURN_ROUND,
        "above",
    ),
    (
        "tests/fixtures/curve_invariant_repros/fan_over_top_flow_feeder.mmd",
        ("sec3__entry_right_5", "sec2__exit_right_2"),
        SideRule.TURN_ROUND,
        "above",
    ),
)
TIE_KEYS = frozenset((Path(rel).stem, key) for rel, key, _rule, _screen in TIES)


@pytest.mark.parametrize(
    ("rel", "key", "rule", "screen"),
    TIES,
    ids=[f"{Path(rel).stem}-{key[1]}" for rel, key, _rule, _screen in TIES],
)
def test_a_length_tie_takes_the_side_with_room_else_the_turn_round(
    rel: str, key: _Key, rule: SideRule, screen: str
) -> None:
    laid = _laid(ROOT / rel)
    approach = laid.drawn._lane_plan.port_orders.approaches[key]  # type: ignore[union-attr]
    assert approach.rule is rule
    assert _screen(laid.drawn, key) == screen
    right, left = _corridors(laid.sealed, key)
    assert right.feasible and left.feasible
    assert abs(right.detour - left.detour) <= SAME_Y_TOLERANCE
    assert (right.room != left.room) is (rule is SideRule.CORRIDOR)


@pytest.mark.parametrize(
    "path",
    [
        *(
            CANVAS_DATA / f"{stem}.mmd"
            for stem in ("f8401267", "f8401678", "f8402283", "f8402603", "f8401577")
        ),
        *(
            ROOT / rel
            for rel in (
                "examples/topologies/bypass_left_entry_from_right.mmd",
                "examples/topologies/bypass_leftward_far_side_entry.mmd",
                "examples/topologies/fan_over_top_reach_order.mmd",
                "tests/fixtures/curve_invariant_repros/fan_over_top_flow_feeder.mmd",
            )
        ),
    ],
    ids=lambda path: path.stem,
)
def test_a_corridor_ranking_holds_on_the_final_drawing(path: Path) -> None:
    """Every bypass U-turn's corridors are as clear on the drawn graph as
    where the lane plan read them, and the shorter stays the shorter, so the
    recorded side is the one the ranking gives the drawing; a length tie is
    accepted only where :data:`TIES` names it."""
    laid = _laid(path)
    approaches = laid.drawn._lane_plan.port_orders.approaches  # type: ignore[union-attr]
    ranked = [key for key in approaches if _hop(laid.sealed, key) is not None]
    assert ranked
    for key in ranked:
        sealed, drawn = _hop(laid.sealed, key), _hop(laid.drawn, key)
        assert sealed is not None and drawn is not None, key
        assert (sealed.right.feasible, sealed.left.feasible) == (
            drawn.right.feasible,
            drawn.left.feasible,
        ), key
        margin = sealed.left.detour - sealed.right.detour
        if abs(margin) <= SAME_Y_TOLERANCE:
            assert (path.stem, key) in TIE_KEYS, key
            continue
        assert (drawn.left.detour - drawn.right.detour) * margin > 0, key


def test_opposite_runs_on_one_band_take_opposite_sides() -> None:
    """f8401678's leftward and rightward bypass U-turns share a line over one
    band: they are recorded on opposite screen sides, and drawn one over every
    box on the band and the other under every box on it."""
    laid = _laid(CANVAS_DATA / "f8401678.mmd")
    graph = laid.drawn
    keys = (
        ("sec1__entry_left_3", "sec0__exit_left_0"),
        ("sec2__entry_right_4", "sec1__exit_right_1"),
    )
    screens = {key: _screen(graph, key) for key in keys}
    assert set(screens.values()) == {"above", "below"}
    for key, screen in screens.items():
        target = graph.sections[graph.ports[key[0]].section_id]
        rows = range(target.grid_row, target.grid_row + target.grid_row_span)
        band = [
            s
            for s in graph.sections.values()
            if s.bbox_w > 0 and s.grid_row in rows and s.grid_col >= 0
        ]
        ys = [
            y
            for route in laid.routes
            if route.edge.target == key[0]  # type: ignore[attr-defined]
            and approach_source(graph, route.edge.source, route.edge.line_id)  # type: ignore[attr-defined]
            == key[1]
            for _x, y in route.points  # type: ignore[attr-defined]
        ]
        assert ys, key
        if screen == "above":
            assert min(ys) < min(s.bbox_y for s in band), key
        else:
            assert max(ys) > max(s.bbox_y + s.bbox_h for s in band), key
