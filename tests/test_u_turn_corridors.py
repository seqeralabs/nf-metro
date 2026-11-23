"""A bypass U-turn's side is decided once, from the placement the lane plan
reads, ranked among the corridors that cross no other section, and holds on
the final drawing."""

from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import cohort_shadow_oracle
import pytest

import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.layout.port_order_plan as port_order_plan_module
import nf_metro.layout.routing.exit_turns as exit_turns_module
import nf_metro.layout.routing.inter_section_handlers as handlers_module
import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph
from nf_metro.layout.constants import BYPASS_CLEARANCE, SAME_Y_TOLERANCE
from nf_metro.layout.port_order_plan import (
    _CORRIDOR_CLEAR,
    Approach,
    PortOrderBuilder,
    SideRule,
    _box,
    _Corridor,
    _CorridorHop,
    _lateral,
    _runs_opposite,
    approach_source,
    port_travel,
)
from nf_metro.layout.routing.common import (
    RoutedPath,
    right_normal_axis_sign,
    section_header_safe_cap,
)
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
    routes: tuple[RoutedPath, ...]


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
    right, left, *_extents = builder.side_corridors(*key, travel)
    return right, left


def _approach(graph: MetroGraph, key: _Key) -> Approach:
    return graph._lane_plan.port_orders.approaches[key]  # type: ignore[union-attr]


def _screen(graph: MetroGraph, key: _Key) -> str:
    sign = _approach(graph, key).side * right_normal_axis_sign(
        port_travel(graph.ports[key[0]])
    )
    return "below" if sign > 0 else "above"


def _connector_ys(laid: _Laid, key: _Key) -> list[float]:
    return [
        y
        for route in laid.routes
        if route.edge.target == key[0]
        and approach_source(laid.drawn, route.edge.source, route.edge.line_id) == key[1]
        for _x, y in route.points
    ]


# Every curated U-turn whose two clear corridors are equally long where the lane
# plan reads them, with the side the ranking gives it: the corridor in free room
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
        SideRule.TURN_ROUND,
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
TIE_KEYS = frozenset((Path(rel).stem, key) for rel, key, *_ in TIES)


@pytest.mark.parametrize(
    ("rel", "key", "rule", "screen"),
    TIES,
    ids=[f"{Path(rel).stem}-{key[1]}" for rel, key, *_ in TIES],
)
def test_a_length_tie_takes_the_side_with_room_else_the_turn_round(
    rel: str, key: _Key, rule: SideRule, screen: str
) -> None:
    laid = _laid(ROOT / rel)
    approach = _approach(laid.drawn, key)
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
        ys = _connector_ys(laid, key)
        assert ys, key
        if screen == "above":
            assert min(ys) < min(s.bbox_y for s in band), key
        else:
            assert max(ys) > max(s.bbox_y + s.bbox_h for s in band), key


def test_a_feeder_starting_beyond_a_traverse_closes_that_corridor() -> None:
    """f8404507's l2 reaches sec3's LEFT entry from the row above, beyond the
    traverse of the over-the-top corridor of the sec2 loop into the same entry,
    so where the lane plan reads them only the corridor below is clear.  The
    two corridors are not equally long, so the direct turn-round keeps its
    turn-round side: recorded and drawn below."""
    laid = _laid(CANVAS_DATA / "f8404507.mmd")
    graph = laid.drawn
    key = ("sec3__entry_left_5", "sec2__exit_left_2")
    assert _approach(graph, key).rule is SideRule.TURN_ROUND
    assert _screen(graph, key) == "below"
    right, left = _corridors(laid.sealed, key)
    assert right.feasible and not left.feasible
    assert abs(right.detour - left.detour) > SAME_Y_TOLERANCE
    target = graph.sections[graph.ports[key[0]].section_id]
    ys = [
        y
        for route in laid.routes
        if route.edge.target == key[0] and route.edge.source == key[1]
        for _x, y in route.points
    ]
    assert ys
    assert min(ys) > target.bbox_y
    assert max(ys) > target.bbox_y + target.bbox_h


# Direct turn-rounds whose two clear corridors are equally long and whose only
# section beyond the band lies on a side another line ends beyond the traverse
# of: (map, key, the contested side, the contesting edge).
CONTESTED: tuple[tuple[str, _Key, int, tuple[str, str, str]], ...] = (
    (
        "f8404976",
        ("sec1__entry_left_3", "sec0__exit_left_0"),
        -1,
        ("sec1__exit_right_1", "sec2__entry_left_4", "l0"),
    ),
    (
        "f8500517",
        ("sec2__entry_right_3", "sec1__exit_right_1"),
        -1,
        ("__junction_6", "__merge_2", "l1"),
    ),
    (
        "f8500832",
        ("sec1__entry_right_3", "sec0__exit_right_0"),
        -1,
        ("__junction_6", "sec4__entry_right_4", "l0"),
    ),
)


@pytest.mark.parametrize(
    ("stem", "key", "side", "edge"),
    CONTESTED,
    ids=[stem for stem, *_rest in CONTESTED],
)
def test_room_another_line_ends_beyond_is_not_free(
    stem: str, key: _Key, side: int, edge: tuple[str, str, str]
) -> None:
    """f8404976's target section sends l0 out round the room above the loop;
    f8500517's and f8500832's fork sends a sibling branch out round the room
    below it.  Whether that line crosses the loop depends on the lane order the
    record decides, so the room is not free and the tie takes the turn-round
    side."""
    laid = _laid(CANVAS_DATA / f"{stem}.mmd")
    approach = _approach(laid.drawn, key)
    assert (approach.side, approach.rule) == (1, SideRule.TURN_ROUND)
    graph = laid.sealed
    right, left = _corridors(graph, key)
    assert right.feasible and left.feasible
    assert abs(right.detour - left.detour) <= SAME_Y_TOLERANCE
    assert not right.room and not left.room
    builder = _builder(graph)
    travel = builder.same_axis_u_turn(*key)
    assert travel is not None
    connector = builder.connector(*key)
    assert edge in {(e.source, e.target, e.line_id) for e in graph.edges}
    assert edge not in {(e.source, e.target, e.line_id) for e in connector}
    target = graph.section_for_port(graph.ports[key[0]])
    reach = max(
        (_lateral(builder.point(node), (0.0, 0.0), travel) for node in edge[:2]),
        key=lambda lateral: side * lateral,
    )
    assert reach in builder.contested_ends(connector, target, travel)
    _a0, _a1, low, high = _box(target, travel)
    face = high if side == 1 else low
    assert side * (reach - face) > _CORRIDOR_CLEAR


@pytest.mark.parametrize("stem", [stem for stem, *_rest in CONTESTED])
def test_a_contested_turn_round_is_drawn_on_its_recorded_side(stem: str) -> None:
    """The render draws each loop on the turn-round side its record holds."""
    laid = _laid(CANVAS_DATA / f"{stem}.mmd")
    key = next(key for name, key, *_rest in CONTESTED if name == stem)
    graph = laid.drawn
    target = graph.sections[graph.ports[key[0]].section_id]
    ys = _connector_ys(laid, key)
    assert ys
    if _screen(graph, key) == "above":
        assert min(ys) < target.bbox_y
    else:
        assert max(ys) > target.bbox_y + target.bbox_h


def test_a_contested_turn_round_draws_its_plan() -> None:
    assert cohort_shadow_oracle.shadow_failure(CANVAS_DATA / "f8500832.mmd") is None


def test_a_direct_turn_round_reads_the_padding_its_target_has() -> None:
    """f8401369's sec3 carries less padding below its stations than above, so
    the corridor below is the shorter and the turn-round side holds."""
    laid = _laid(CANVAS_DATA / "f8401369.mmd")
    key = ("sec3__entry_left_3", "sec2__exit_left_0")
    approach = _approach(laid.drawn, key)
    assert (approach.side, approach.rule) == (1, SideRule.TURN_ROUND)
    assert _screen(laid.drawn, key) == "below"
    right, left = _corridors(laid.sealed, key)
    assert right.feasible and left.feasible
    assert right.detour + SAME_Y_TOLERANCE < left.detour


def _hops(laid: _Laid) -> dict[_Key, _CorridorHop]:
    """Every same-axis U-turn the record keys, ranked or direct, read where
    the lane plan read it."""
    builder = _builder(laid.sealed)
    direct = builder.turn_rounds()
    approaches = laid.drawn._lane_plan.port_orders.approaches  # type: ignore[union-attr]
    return {
        key: hop
        for key in approaches
        if (hop := builder.corridor_hop(key) or direct.get(key)) is not None
    }


def _drawn_side(laid: _Laid, key: _Key) -> str:
    """The screen side *key*'s connector is drawn round the boxes of its
    target's row that it spans."""
    graph = laid.drawn
    target = graph.sections[graph.ports[key[0]].section_id]
    points = [
        point
        for route in laid.routes
        if route.edge.target == key[0]
        and approach_source(graph, route.edge.source, route.edge.line_id) == key[1]
        for point in route.points
    ]
    assert points, key
    x0, x1 = min(x for x, _y in points), max(x for x, _y in points)
    band = [
        s
        for s in graph.sections.values()
        if s.bbox_w > 0
        and s.grid_row == target.grid_row
        and s.bbox_x <= x1
        and x0 <= s.bbox_x + s.bbox_w
    ]
    above = min(y for _x, y in points) < min(s.bbox_y for s in band)
    below = max(y for _x, y in points) > max(s.bbox_y + s.bbox_h for s in band)
    assert above != below, key
    return "above" if above else "below"


# Interleaving opposite U-turns: (map, the bypass U-turn, its direct turn-round
# partner).
PARTNERED: tuple[tuple[str, _Key, _Key], ...] = (
    (
        "f8404063",
        ("sec3__entry_left_3", "sec2__exit_left_1"),
        ("sec2__entry_right_2", "sec0__exit_right_0"),
    ),
    (
        "f8404575",
        ("sec3__entry_left_4", "sec2__exit_left_1"),
        ("sec2__entry_right_3", "sec1__exit_right_0"),
    ),
)


# Stress maps placed only under their option sets (``test_canvas_placed_once``).
OPTION_SET_ONLY = frozenset({"f8400004", "f8400369"})


@pytest.mark.parametrize(
    "path",
    [p for p in sorted(CANVAS_DATA.glob("*.mmd")) if p.stem not in OPTION_SET_ONLY],
    ids=lambda path: path.stem,
)
def test_interleaving_opposite_u_turns_take_opposite_screen_sides(path: Path) -> None:
    """Two same-axis U-turns sharing a line and running opposite ways over one
    band with interleaving legs, bypass or direct, are recorded and drawn on
    opposite screen sides: on one side their loops cross."""
    laid = _laid(path)
    hops = _hops(laid)
    linked = {
        (a.key, b.key)
        for a, b in combinations(hops.values(), 2)
        if _runs_opposite(a, b)
    }
    for a, b in linked:
        assert _screen(laid.drawn, a) != _screen(laid.drawn, b), (a, b)
        assert _drawn_side(laid, a) == _screen(laid.drawn, a), a
        assert _drawn_side(laid, b) == _screen(laid.drawn, b), b
    for stem, ranked, partner in PARTNERED:
        if stem == path.stem:
            assert {(ranked, partner), (partner, ranked)} & linked


# Opposite U-turns whose legs nest: (map, the outer, the inner).
NESTED: tuple[tuple[str, _Key, _Key], ...] = (
    (
        "f8401267",
        ("sec1__entry_right_2", "sec0__exit_right_0"),
        ("sec2__entry_left_3", "sec1__exit_left_1"),
    ),
    (
        "f8402283",
        ("sec1__entry_right_2", "sec0__exit_right_0"),
        ("sec2__entry_left_3", "sec1__exit_left_1"),
    ),
)


def _traverse_depths(laid: _Laid, key: _Key) -> tuple[list[float], tuple[float, float]]:
    """Each line's deepest drawn y on *key*'s connector, and the x extent of
    the traverses at those depths."""
    routes = [
        route
        for route in laid.routes
        if route.edge.target == key[0]
        and approach_source(laid.drawn, route.edge.source, route.edge.line_id) == key[1]
    ]
    assert routes, key
    depths = [max(y for _x, y in route.points) for route in routes]
    xs = [
        x
        for route, depth in zip(routes, depths, strict=True)
        for x, y in route.points
        if abs(y - depth) <= SAME_Y_TOLERANCE
    ]
    return depths, (min(xs), max(xs))


@pytest.mark.parametrize(
    ("stem", "outer", "inner"), NESTED, ids=[stem for stem, *_rest in NESTED]
)
def test_nested_opposite_u_turns_nest_concentrically(
    stem: str, outer: _Key, inner: _Key
) -> None:
    """Opposite U-turns whose legs nest are not linked: both are recorded below
    their band, and every line of the inner loop traverses strictly between the
    boxes it rounds and every line of the outer loop."""
    laid = _laid(CANVAS_DATA / f"{stem}.mmd")
    hops = _hops(laid)
    assert hops[outer].lines & hops[inner].lines
    assert not _runs_opposite(hops[outer], hops[inner])
    assert _screen(laid.drawn, outer) == _screen(laid.drawn, inner) == "below"
    inner_depths, (x0, x1) = _traverse_depths(laid, inner)
    outer_depths, _extent = _traverse_depths(laid, outer)
    graph = laid.drawn
    target = graph.sections[graph.ports[inner[0]].section_id]
    rounded = max(
        s.bbox_y + s.bbox_h
        for s in graph.sections.values()
        if s.bbox_w > 0
        and s.grid_row == target.grid_row
        and s.bbox_x < x1
        and x0 < s.bbox_x + s.bbox_w
    )
    assert rounded < min(inner_depths)
    assert max(inner_depths) < min(outer_depths)


@pytest.mark.parametrize(("stem", "ranked", "partner"), PARTNERED[1:], ids=["f8404575"])
def test_group_without_assignment_takes_turn_round(
    stem: str, ranked: _Key, partner: _Key
) -> None:
    """f8404575's bypass U-turn is held above by its single clear corridor,
    where its direct turn-round partner passes: no assignment separates them,
    so it takes the turn-round side, opposite its partner's, and the plan
    records it."""
    laid = _laid(CANVAS_DATA / f"{stem}.mmd")
    graph = laid.drawn
    assert _hops(laid)[ranked].held == (-1, SideRule.CORRIDOR)
    approach = _approach(graph, ranked)
    assert (approach.side, approach.rule) == (1, SideRule.TURN_ROUND)
    assert _approach(graph, partner).side == 1
    assert _screen(graph, ranked) != _screen(graph, partner)
    plan = graph._lane_plan.port_orders  # type: ignore[union-attr]
    assert plan.turn_round_fallbacks == (ranked,)


@pytest.mark.parametrize(
    ("a", "b", "interleave"),
    [
        ((0.0, 100.0), (-150.0, -50.0), True),
        ((20.0, 80.0), (-100.0, 0.0), False),
        ((0.0, 100.0), (-80.0, -20.0), False),
        ((0.0, 100.0), (-100.0, 0.0), True),
        ((0.0, 100.0), (-80.0, -0.05), True),
        ((0.05, 80.0), (-100.0, 0.0), True),
    ],
    ids=[
        "interleaved",
        "inside-the-other",
        "holding-the-other",
        "equal",
        "touching-at-one-end",
        "touching-inside",
    ],
)
def test_legs_interleave_unless_one_nests_clear_inside_the_other(
    a: tuple[float, float], b: tuple[float, float], interleave: bool
) -> None:
    legs_interleave = port_order_plan_module._legs_interleave
    assert legs_interleave(a, b) is interleave
    assert legs_interleave(b, a) is interleave


def _f8404575_builder(
    mp: pytest.MonkeyPatch, ranked: _Key, partner: _Key
) -> PortOrderBuilder:
    builder = _builder(_laid(CANVAS_DATA / "f8404575.mmd").sealed)
    real_side = builder.u_turn_side

    def u_turn_side(entry_id: str, source_id: str, below: int) -> tuple[int, SideRule]:
        if (entry_id, source_id) == partner:
            return -1, SideRule.CORRIDOR
        return real_side(entry_id, source_id, below)

    mp.setattr(builder, "u_turn_side", u_turn_side)
    return builder


def test_a_group_sharing_a_corridor_partners_side_raises() -> None:
    """A bypass U-turn held to the screen side a CORRIDOR partner takes has no
    assignment, and the turn-round fallback cannot separate it from that
    partner: the plan raises rather than knot the loops."""
    _stem, ranked, partner = PARTNERED[1]
    with pytest.MonkeyPatch.context() as mp:
        builder = _f8404575_builder(mp, ranked, partner)
        real_hop = builder.corridor_hop

        def corridor_hop(key: _Key) -> _CorridorHop | None:
            hop = real_hop(key)
            if key == ranked and hop is not None:
                return replace(hop, held=(1, SideRule.CORRIDOR))
            return hop

        mp.setattr(builder, "corridor_hop", corridor_hop)
        with pytest.raises(RuntimeError, match="interleaving legs"):
            port_order_plan_module.decide_ports(builder)
        assert builder.turn_round_fallbacks == [ranked]


def test_two_linked_turn_rounds_on_one_side_raise() -> None:
    """Two direct turn-rounds linked to each other are each decided alone: one
    taking a CORRIDOR side onto the screen side of the other raises."""
    _stem, ranked, partner = PARTNERED[1]
    with pytest.MonkeyPatch.context() as mp:
        builder = _f8404575_builder(mp, partner, ranked)
        real_scope = builder.corridor_scope

        def corridor_scope(entry_id: str, source_id: str) -> object:
            if (entry_id, source_id) == ranked:
                return None
            return real_scope(entry_id, source_id)

        mp.setattr(builder, "corridor_scope", corridor_scope)
        assert set(builder.turn_rounds()) >= {ranked, partner}
        with pytest.raises(RuntimeError, match="interleaving legs"):
            port_order_plan_module.decide_ports(builder)


# Same-row RIGHT-entry loops recorded round below: (map, connector).
SAME_ROW_UNDER: tuple[tuple[str, _Key], ...] = (
    ("f8402075", ("sec2__entry_right_4", "sec1__exit_right_1")),
    ("f8403977", ("sec2__entry_right_3", "sec1__exit_right_0")),
)


def _below_channels(path: Path, key: _Key) -> set[float]:
    """Every around-below channel routing and the exit-turn planner read for
    *key*'s connector while *path* renders."""
    seen: set[float] = set()
    real = handlers_module._right_entry_below_channel_y

    def spy(facts: handlers_module._InterFacts) -> float:
        value = real(facts)
        if facts.edge.target == key[0]:
            seen.add(value)
        return value

    with pytest.MonkeyPatch.context() as mp, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mp.setattr(handlers_module, "_right_entry_below_channel_y", spy)
        mp.setattr(exit_turns_module, "_right_entry_below_channel_y", spy)
        graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
        build_render_plan(graph, resolve_theme(None, graph))
    return seen


@pytest.mark.parametrize(
    ("stem", "key"), SAME_ROW_UNDER, ids=[stem for stem, _key in SAME_ROW_UNDER]
)
def test_same_row_right_under_run_stays_in_row_gap(stem: str, key: _Key) -> None:
    """A same-row loop into a RIGHT entry recorded round below traverses the
    gap under its own row, above every lower-row box in the hop's columns:
    clear of every box of the row it passes under and of the header of every
    lower-row box it passes over, in the one channel the exit-turn planner and
    routing both read."""
    path = CANVAS_DATA / f"{stem}.mmd"
    laid = _laid(path)
    graph = laid.drawn
    assert _screen(graph, key) == "below"
    depths, (x0, x1) = _traverse_depths(laid, key)
    source = graph.sections[graph.ports[key[1]].section_id]
    target = graph.sections[graph.ports[key[0]].section_id]
    lower_tops = [
        s.bbox_y
        for s in graph.sections.values()
        if s.bbox_w > 0
        and s.grid_row > source.grid_row
        and s.grid_col <= target.grid_col
        and source.grid_col <= s.grid_col + s.grid_col_span - 1
    ]
    assert lower_tops
    assert max(depths) < min(lower_tops)
    for section in graph.sections.values():
        if section.bbox_w <= 0 or not (
            section.bbox_x < x1 and x0 < section.bbox_x + section.bbox_w
        ):
            continue
        if section.grid_row == source.grid_row:
            bottom = section.bbox_y + section.bbox_h
            assert min(depths) >= bottom + BYPASS_CLEARANCE, section.id
        elif section.grid_row > source.grid_row:
            assert max(depths) <= section_header_safe_cap(section), section.id
    assert _below_channels(path, key) == {min(depths)}


def test_a_row_under_run_reads_every_column_a_section_spans() -> None:
    """The under-run reads a section by every column it spans: a lower-row
    box reaching into the capped columns caps the run, and a row box
    reaching into the passed columns lifts its floor."""
    under = handlers_module._row_under_run_y
    graph = copy.deepcopy(_laid(CANVAS_DATA / "f8402075.mmd").drawn)
    export, gather = graph.sections["sec2"], graph.sections["sec1"]
    report, vcf = graph.sections["sec4"], graph.sections["sec0"]
    floor = max(s.bbox_y + s.bbox_h for s in (export, gather))
    assert under(graph, 0, (1, 2), (2, 2), None) == floor + BYPASS_CLEARANCE
    report.grid_col_span = 2
    assert under(graph, 0, (1, 2), (2, 2), None) == section_header_safe_cap(report)
    report.grid_col_span = 1
    vcf.grid_col_span = 2
    vcf_floor = vcf.bbox_y + vcf.bbox_h
    assert vcf_floor > floor
    assert under(graph, 0, (1, 2), (2, 2), None) == vcf_floor + BYPASS_CLEARANCE
