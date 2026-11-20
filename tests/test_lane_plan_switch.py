"""The lane plan drives the drawing: every lane the render draws is the one
the sealed record plans.

The offsets every routing pass reads are the record's lanes read through
their draw accessors (:func:`nf_metro.layout.lane_plan.planned_offsets`),
and the routing consumers take their decisions from the record: approach
sides, far sides, perpendicular-entry crossings, exit-turn source lanes and
the trunk-slot nest order.  Validation observes a layout and never seals its
record, so a ``validate=True`` render draws the production render.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import sys
import textwrap
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest

import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.render.svg as svg
from nf_metro.api import prepare_graph, render_string, resolve_theme
from nf_metro.layout import engine as engine_module
from nf_metro.layout.constants import graph_offset_step
from nf_metro.layout.geometry import (
    perpendicular_port_sides,
    port_free_axis,
    station_lane_coord,
)
from nf_metro.layout.lane_plan import JUNCTIONS_POSITIONED, LanePlan
from nf_metro.layout.port_order_plan import ApproachKind, SideRule, port_travel
from nf_metro.layout.route_plan import (
    ExitLaneTransitionPlacement,
    ExitTurnDisposition,
    ExitTurnPlan,
)
from nf_metro.layout.routing import corridor_cohort_integration
from nf_metro.layout.routing import exit_turns as exit_turns_module
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.context import _section_lane_frame
from nf_metro.layout.routing.orientation import (
    DemandAxis,
    lateral_axis,
    lateral_order_sign,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import LineSpread, MetroGraph

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
FIXTURES = ROOT / "tests" / "fixtures"

CURATED = sorted(
    p
    for p in [*(ROOT / "examples").rglob("*.mmd"), *FIXTURES.rglob("*.mmd")]
    if "invalid" not in p.parts
)

DRAWN_TOLERANCE = 0.5
"""Pixels a drawn lane may sit off its planned coordinate."""
APPROACH_BEYOND_TAPER = 12.0
"""Pixels off the port a route must lie before its side counts: inside that,
the lane taper into the port."""


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _curated(stem: str) -> Path:
    return next(p for p in CURATED if p.stem == stem)


@dataclass
class _Drawn:
    graph: MetroGraph | None
    plan: LanePlan | None = None
    offsets: dict[tuple[str, str], float] = field(default_factory=dict)
    routes: list[object] = field(default_factory=list)
    polylines: list[list[tuple[float, float]]] = field(default_factory=list)
    step: float = 0.0
    exit_turns: tuple[ExitTurnPlan, ...] = ()


@lru_cache(maxsize=None)
def _drawn(path: Path) -> _Drawn:
    """Lay out and render *path*, keeping the settled geometry the render
    draws and the graph copy it settled, whose rows and columns the render
    translates; ``graph`` is ``None`` when the map aborts."""
    settled: list[tuple[MetroGraph, svg._SettledRenderGeometry]] = []
    real = svg._settle_render_geometry

    def settle(
        graph: MetroGraph, *args: object, **kwargs: object
    ) -> svg._SettledRenderGeometry:
        result = real(graph, *args, **kwargs)  # type: ignore[arg-type]
        settled.append((graph, result))
        return result

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(svg, "_settle_render_geometry", settle)
        try:
            source = prepare_graph(path.read_text(), source_dir=str(path.parent))
            observed = svg.build_observed_render_plan(
                source, resolve_theme(None, source)
            )
        except Exception:
            return _Drawn(None)
    if not settled:
        return _Drawn(None)
    graph, last = settled[-1]
    return _Drawn(
        graph,
        source._lane_plan,
        dict(last.station_offsets),
        list(last.routes),
        [list(p) for p in last.route_polylines],
        graph_offset_step(graph),
        observed.route_plan.exit_turn_plans,
    )


def _perpendicular(graph: MetroGraph, node: str) -> bool:
    port = graph.ports.get(node)
    if port is None:
        return False
    section = graph.sections.get(port.section_id)
    return section is not None and port.side in perpendicular_port_sides(
        section.direction
    )


def _end_lateral(
    graph: MetroGraph, points: list[tuple[float, float]], at_start: bool, node: str
) -> tuple[str, float] | None:
    """The axis across a route's end segment and the route's coordinate on
    it there, or ``None`` for a diagonal or degenerate end or one that stops
    short of *node* along that segment: a feeder into a convergence's merge
    junction ends where it lands on the trunk."""
    pts = points if at_start else points[::-1]
    end = pts[0]
    other = next((p for p in pts[1:] if p != end), None)
    if other is None:
        return None
    station = graph.stations[node]
    dx, dy = other[0] - end[0], other[1] - end[1]
    if abs(dy) <= 1e-6 < abs(dx) and abs(end[0] - station.x) <= DRAWN_TOLERANCE:
        return "y", end[1]
    if abs(dx) <= 1e-6 < abs(dy) and abs(end[1] - station.y) <= DRAWN_TOLERANCE:
        return "x", end[0]
    return None


def _planned_lateral(drawn: _Drawn, node: str, line_id: str, axis: str) -> float | None:
    """Where the record puts *line_id* at *node* on screen *axis*, or ``None``
    where the record states nothing on that axis."""
    graph, plan = drawn.graph, drawn.plan
    assert graph is not None and plan is not None
    station = graph.stations[node]
    if _perpendicular(graph, node):
        port = graph.ports[node]
        value = plan.tracks.perpendicular.get(node, {}).get(line_id)
        if value is None or port_free_axis(port.side) != axis:
            return None
        return (
            getattr(station, axis)
            + lateral_order_sign(port_travel(port)) * value * drawn.step
        )
    section = graph.sections.get(station.section_id or "")
    if (
        section is None
        or (node, line_id) not in drawn.offsets
        or _feeds_a_perpendicular_entry(graph, node)
    ):
        return None
    frame = _section_lane_frame(graph, section)
    if frame.secondary.name != axis:
        return None
    return station_lane_coord(frame, station, drawn.offsets[(node, line_id)])


def _feeds_a_perpendicular_entry(graph: MetroGraph, node: str) -> bool:
    """Whether *node* is a junction whose lines all go on into one TOP or
    BOTTOM entry: they converge across that port's travel, not on its
    section's lanes."""
    if node not in graph.junctions:
        return False
    targets = {edge.target for edge in graph.edges_from(node)}
    return len(targets) == 1 and _perpendicular(graph, next(iter(targets)))


def _on_a_rail(graph: MetroGraph, node: str) -> bool:
    """Whether *node* is a station inside a rail section, which draws each of
    its lines on that line's own rail, in the rail layout's slot order at the
    rail pitch, rather than at a lane offset; the section's ports take the
    record's lanes."""
    return graph.station_is_rail(node) and not graph.stations[node].is_port


def _drawn_mismatches(drawn: _Drawn) -> list[tuple[str, str, float, float]]:
    """Every route end off its planned lane.  A perpendicular port's riser
    anchors its bundle by its own convention (reflected about the widest
    lane, or on the lanes of an entry it drops straight into), so there the
    record fixes the lanes' order and pitch: every end at the port sits one
    shared distance from its planned lane."""
    graph = drawn.graph
    assert graph is not None
    out = []
    shifts: dict[str, list[tuple[str, float, float]]] = {}
    for route, polyline in zip(drawn.routes, drawn.polylines, strict=True):
        edge = route.edge  # type: ignore[attr-defined]
        for node, at_start in ((edge.source, True), (edge.target, False)):
            if _on_a_rail(graph, node):
                continue
            got = _end_lateral(graph, polyline, at_start, node)
            if got is None:
                continue
            axis, lateral = got
            want = _planned_lateral(drawn, node, edge.line_id, axis)
            if want is None:
                continue
            if _perpendicular(graph, node):
                shifts.setdefault(node, []).append((edge.line_id, want, lateral))
            elif abs(want - lateral) > DRAWN_TOLERANCE:
                out.append((node, edge.line_id, want, lateral))
    for node, ends in shifts.items():
        anchor = ends[0][2] - ends[0][1]
        out.extend(
            (node, lid, want + anchor, lateral)
            for lid, want, lateral in ends
            if abs(lateral - want - anchor) > DRAWN_TOLERANCE
        )
    return out


# ---- 1. planned == drawn


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_the_offsets_are_the_planned_offsets(path: Path) -> None:
    drawn = _drawn(path)
    graph = drawn.graph
    if graph is None or graph.line_spread is LineSpread.RAILS:
        return
    assert drawn.plan is not None
    planned = lane_plan_module.planned_offsets(graph, drawn.plan, drawn.step)
    assert drawn.offsets == planned


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_every_route_ends_on_its_planned_lane(path: Path) -> None:
    """At both ends of every drawn route the line sits on the lane the record
    plans for it there, read across the route's end segment."""
    drawn = _drawn(path)
    if drawn.graph is None or drawn.plan is None:
        return
    assert _drawn_mismatches(drawn) == []


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_every_station_draws_its_lines_in_planned_order(path: Path) -> None:
    """Across every flow-side station the drawn route ends lie in the order of
    the record's lanes."""
    drawn = _drawn(path)
    graph, plan = drawn.graph, drawn.plan
    if graph is None or plan is None:
        return
    ends: dict[str, dict[str, float]] = {}
    for route, polyline in zip(drawn.routes, drawn.polylines, strict=True):
        edge = route.edge  # type: ignore[attr-defined]
        for node, at_start in ((edge.source, True), (edge.target, False)):
            got = _end_lateral(graph, polyline, at_start, node)
            station = graph.stations[node]
            section = graph.sections.get(station.section_id or "")
            if (
                got is None
                or section is None
                or _perpendicular(graph, node)
                or _on_a_rail(graph, node)
            ):
                continue
            frame = _section_lane_frame(graph, section)
            if frame.secondary.name == got[0]:
                ends.setdefault(node, {})[edge.line_id] = got[1]
    wrong = []
    for node, laterals in ends.items():
        lanes = {
            lid: plan.tracks.lanes[(node, lid)]
            for lid in laterals
            if (node, lid) in plan.tracks.lanes
        }
        section = graph.sections[graph.stations[node].section_id or ""]
        sign = _section_lane_frame(graph, section).secondary_sign
        for a in lanes:
            for b in lanes:
                if lanes[a] < lanes[b] and (
                    sign * (laterals[b] - laterals[a]) <= DRAWN_TOLERANCE
                ):
                    wrong.append((node, a, b))
    assert wrong == []


# ---- 2. seams


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_a_seam_draws_the_order_it_joins(path: Path) -> None:
    """Lines pinned across one straight seam leave its exit and reach its
    entry in one order, unless the record names their crossing there."""
    drawn = _drawn(path)
    graph, plan = drawn.graph, drawn.plan
    if graph is None or plan is None:
        return
    forced = {
        (f.port, frozenset(f.lines)) for f in plan.tracks.forced if f.port is not None
    }
    by_seam: dict[tuple[str, str], list[str]] = {}
    for (entry_id, lid), exit_id in plan.seams.pins.items():
        by_seam.setdefault((exit_id, entry_id), []).append(lid)
    wrong = []
    for (exit_id, entry_id), lines in by_seam.items():
        ex = graph.stations[exit_id]
        en = graph.stations[entry_id]
        for a in lines:
            for b in lines:
                if a >= b:
                    continue
                pa = (
                    _planned_lateral(drawn, exit_id, a, "y"),
                    _planned_lateral(drawn, exit_id, b, "y"),
                )
                pb = (
                    _planned_lateral(drawn, entry_id, a, "y"),
                    _planned_lateral(drawn, entry_id, b, "y"),
                )
                if None in pa or None in pb or abs(ex.y - en.y) > 1e-6:
                    continue
                kept = (pa[0] - pa[1]) * (pb[0] - pb[1]) > 0  # type: ignore[operator]
                if not kept and not any(
                    port == entry_id and {a, b} <= lines_ for port, lines_ in forced
                ):
                    wrong.append((exit_id, entry_id, a, b))
    assert wrong == []


# ---- 3. approach sides


def _origin(graph: MetroGraph, node: str, line_id: str) -> str:
    return lane_plan_module.approach_source(graph, node, line_id)


def _screen_side(graph: MetroGraph, entry_id: str, side: int) -> int:
    """Screen sign, on the entry's lateral axis, of the travel-frame *side*."""
    return side * lateral_order_sign(port_travel(graph.ports[entry_id]))


# Main's TOP/BOTTOM-entry L-shape drops straight in on the port column past a
# section on the band, where the turned LR hop bypasses it (#1058, #1521); the
# record states the turned side.
L_SHAPE_DROPS_PAST_A_BAND_SECTION = frozenset(
    {"peeloff_straight_drop_near_wall", "tb_bottom_exit_fork_diamond"}
)
APPROACH_CASES = [
    pytest.param(
        path,
        marks=pytest.mark.xfail(
            strict=True,
            reason="main's orientation-asymmetric TOP/BOTTOM-entry L-shape dispatch "
            "(#1058/#1521); renders == main; rotating it is a #2094 item",
        ),
    )
    if path.stem in L_SHAPE_DROPS_PAST_A_BAND_SECTION
    else path
    for path in CURATED
]


@pytest.mark.parametrize("path", APPROACH_CASES, ids=_rel)
def test_every_approach_arrives_on_its_planned_side(path: Path) -> None:
    """A connector the approach record sends round one side of its entry's
    band reaches the entry from that side in the drawn route."""
    drawn = _drawn(path)
    graph, plan = drawn.graph, drawn.plan
    if graph is None or plan is None:
        return
    wrong = []
    for (entry_id, source_id), approach in plan.port_orders.approaches.items():
        if approach.kind is ApproachKind.LEVEL or approach.side == 0:
            continue
        axis = int(lateral_axis(port_travel(graph.ports[entry_id])) is DemandAxis.Y)
        port_c = (graph.stations[entry_id].x, graph.stations[entry_id].y)[axis]
        for route, polyline in zip(drawn.routes, drawn.polylines, strict=True):
            edge = route.edge  # type: ignore[attr-defined]
            if edge.target != entry_id:
                continue
            if _origin(graph, edge.source, edge.line_id) != source_id:
                continue
            last = next(
                (
                    p[axis]
                    for p in reversed(polyline)
                    if abs(p[axis] - port_c) > APPROACH_BEYOND_TAPER
                ),
                None,
            )
            got = 0 if last is None else (1 if last > port_c else -1)
            want = _screen_side(graph, entry_id, approach.side)
            if got != want:
                wrong.append((entry_id, source_id, edge.line_id, want, got))
            break
    assert wrong == []


# ---- 4. validation observes


@contextmanager
def _seal_points(
    seen: list[tuple[LanePlan, frozenset[str], str, MetroGraph]],
) -> Iterator[None]:
    real = lane_plan_module.build_lane_plan

    def build(graph: MetroGraph) -> LanePlan:
        plan = real(graph)
        caller = next(
            frame.function
            for frame in inspect.stack()[1:]
            if not frame.filename.endswith(
                ("lane_plan.py", "routing/offsets.py", "routing/__init__.py")
            )
        )
        seen.append((plan, frozenset(graph._stages_completed), caller, graph))
        return plan

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lane_plan_module, "build_lane_plan", build)
        yield


@contextmanager
def _validating(validate: bool) -> Iterator[None]:
    import nf_metro.api as api_module

    real = engine_module.compute_layout

    def layout(graph: MetroGraph, *args: object, **kwargs: object) -> object:
        kwargs["validate"] = validate
        return real(graph, *args, **kwargs)  # type: ignore[arg-type]

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(api_module, "compute_layout", layout)
        yield


def _seal_point(path: Path, validate: bool) -> tuple[frozenset[str], str] | None:
    """The stages done and the caller where the layout's record was built,
    read even when the layout aborts after sealing it."""
    seen: list[tuple[LanePlan, frozenset[str], str, MetroGraph]] = []
    with _validating(validate), _seal_points(seen), contextlib.suppress(Exception):
        prepare_graph(path.read_text(), source_dir=str(path.parent))
    return next(
        (
            (stages, caller)
            for plan, stages, caller, graph in seen
            if plan is graph._lane_plan
        ),
        None,
    )


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_validation_seals_where_production_does(path: Path) -> None:
    """A validated layout that reaches the seal seals where production does;
    one a guard aborts before the seal has no seal to compare."""
    production = _seal_point(path, validate=False)
    validated = _seal_point(path, validate=True)
    if production is not None and validated is not None:
        assert validated == production


VALIDATE_DIVERGED = [
    "packed_cell_right_exit_left_entry_wrap",
    "out_of_section_retag_fan",
    "same_destination_short_overlap",
    "row_trunk_partial_through_line",
    "entry_trunk_row_bow",
    "riboseq_inter_row_corridor",
    "variantbenchmarking_auto",
]


def _md5(path: Path, validate: bool) -> str:
    with _validating(validate):
        out = render_string(path.read_text(), source_dir=str(path.parent))
    return hashlib.md5(out.encode()).hexdigest()


@pytest.mark.parametrize("stem", VALIDATE_DIVERGED)
def test_a_validated_render_draws_the_production_render(stem: str) -> None:
    path = _curated(stem)
    assert _md5(path, validate=True) == _md5(path, validate=False)


def test_validation_scope_is_not_a_module_global() -> None:
    """The observing scope lives in a context variable and is reset when
    the guard dispatch leaves, raised or not."""
    from contextvars import ContextVar

    assert isinstance(lane_plan_module._OBSERVED, ContextVar)
    graph = parse_metro_mermaid((ROOT / "examples" / "rnaseq_sections.mmd").read_text())
    with pytest.raises(RuntimeError), lane_plan_module.observing():
        assert lane_plan_module._OBSERVED.get() is not None
        raise RuntimeError
    assert lane_plan_module._OBSERVED.get() is None
    del graph


# ---- 5. provisional records


def test_a_run_before_the_seal_writes_a_provisional_record() -> None:
    """An offsets run before the junctions are positioned writes the record
    its placement plans and stores nothing."""
    seen: list[tuple[bool, bool]] = []
    real = offsets_module.planned_offsets

    def spy(graph: MetroGraph, plan: LanePlan, step: float) -> dict:
        got = real(graph, plan, step)
        if JUNCTIONS_POSITIONED not in graph._stages_completed:
            want = real(graph, lane_plan_module.build_lane_plan(graph), step)
            seen.append((got == want, graph._lane_plan is None))
        return got

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(offsets_module, "planned_offsets", spy)
        for stem in ("rnaseq_sections", "wide_fan_out", "fold_bypass_creep_tight"):
            path = _curated(stem)
            prepare_graph(path.read_text(), source_dir=str(path.parent))
    assert seen
    assert all(equal and unsealed for equal, unsealed in seen)


# ---- 6. convergence landing on a trunk corner


def test_a_feeder_meeting_a_trunk_corner_lands_on_the_leg_past_it() -> None:
    from nf_metro.layout.routing.convergences import _trunk_landing_point

    trunk = [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0)]
    feeder = [(100.0, -60.0), (100.0, 0.0)]
    radius = 10.0
    assert _trunk_landing_point(feeder, trunk, radius) == (100.0, 2 * radius)
    short = [(0.0, 0.0), (100.0, 0.0), (100.0, 12.0)]
    assert _trunk_landing_point(feeder, short, radius) == (100.0, 12.0)
    across = [(40.0, -60.0), (40.0, 0.0)]
    assert _trunk_landing_point(across, trunk, radius) == (40.0, 0.0)
    against = [(100.0, 60.0), (100.0, 0.0)]
    assert _trunk_landing_point(against, trunk, radius) == (100.0, 0.0)


# ---- 7. exit turns plan from the record


def test_exit_turn_execution_never_writes_the_offsets() -> None:
    source = textwrap.dedent(
        inspect.getsource(exit_turns_module.build_exit_turn_execution)
    )
    writes = []
    for node in ast.walk(ast.parse(source)):
        target = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("clear", "update", "pop", "setdefault"):
                target = node.func.value
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            for t in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(t, ast.Subscript):
                    target = t.value
        if isinstance(target, ast.Attribute) and target.attr == "station_offsets":
            writes.append(ast.unparse(node))
    assert writes == []
    assert not hasattr(exit_turns_module, "_fan_stated_offsets")


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_every_planned_exit_turn_lane_is_the_record(path: Path) -> None:
    """Every station a PLANNED exit-turn plan claims for a line holds the
    line on the plan's lane in the drawn offsets."""
    drawn = _drawn(path)
    if drawn.graph is None:
        return
    wrong = []
    for plan in drawn.exit_turns:
        if plan.disposition is not ExitTurnDisposition.PLANNED:
            continue
        for lane in plan.source_lanes:
            for station_id in lane.station_ids:
                held = drawn.offsets.get((station_id, lane.line_id), 0.0)
                if abs(held - lane.input_offset) > 1e-6:
                    wrong.append((station_id, lane.line_id))
    assert wrong == []


C9_WITNESSES = sorted((ROOT / "tests" / "data" / "lane_plan_c9").glob("*.mmd"))
"""Generated maps whose records once parted an aligned in-section source from
its exit port: a bundle entering a later part crossing the earlier part's
bundle, a bypass-V helper standing for a nesting line's source, and a
vertical-flow station whose lanes lie along a horizontal run to its exit.
Each aborts later on a defect it shares with the pre-plan layout."""


@pytest.mark.parametrize("path", C9_WITNESSES, ids=lambda p: p.stem)
def test_no_planned_exit_turn_steps_a_line_between_its_source_and_its_exit(
    path: Path,
) -> None:
    """A PLANNED exit-turn plan draws each line from an aligned in-section
    source on the lane its exit port holds: the record gives the two one lane,
    so no source-lane step lies on the straight between them."""
    steps: list[tuple[str, str, str]] = []
    real = exit_turns_module.build_exit_turn_execution

    def build(graph: MetroGraph, ctx: object, **kwargs: object) -> object:
        execution = real(graph, ctx, **kwargs)  # type: ignore[arg-type]
        steps.extend(
            (t.edge.source, t.edge.target, t.edge.line_id)
            for plan in execution.plans
            if plan.disposition is ExitTurnDisposition.PLANNED
            for t in plan.lane_transitions
            if t.placement is ExitLaneTransitionPlacement.TARGET
        )
        return execution

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        for module in list(sys.modules.values()):
            if getattr(module, "build_exit_turn_execution", None) is real:
                mp.setattr(module, "build_exit_turn_execution", build)
        with contextlib.suppress(Exception):
            render_string(path.read_text(), source_dir=str(path.parent))
    assert steps == []


# ---- 8. nest order


@pytest.mark.parametrize(
    "stem", ["fan_over_top_reach_order", "fan_over_top_flow_feeder"]
)
def test_a_planned_exit_turn_publishes_its_nest_order(stem: str) -> None:
    drawn = _drawn(_curated(stem))
    assert drawn.graph is not None
    published = [
        plan
        for plan in drawn.exit_turns
        if plan.disposition is ExitTurnDisposition.PLANNED and plan.nest_order
    ]
    assert published
    for plan in published:
        assert set(plan.nest_order) == {axis.line_id for axis in plan.axes}


# ---- 9. invariants consume the plan


VALIDATED = [
    "rnaseq_auto",
    "cohort_fold_compressed",
    "bypass_fan_in_outer_slot",
    "trunk_lane_taken_by_turning_line",
    "fold_bypass_creep_tight",
    "side_branch_ascent_label_strike",
    "out_of_section_retag_fan",
]


@pytest.mark.parametrize("stem", VALIDATED)
def test_the_validated_layout_passes(stem: str) -> None:
    path = _curated(stem)
    with _validating(True):
        prepare_graph(path.read_text(), source_dir=str(path.parent))


STRADDLED_FAN_PORTS = [
    ("side_branch_ascent_label_strike", "sec__entry_left_2"),
    ("symmetric_deadend_fanout", "s1__entry_left_1"),
]


@pytest.mark.parametrize(("stem", "port_id"), STRADDLED_FAN_PORTS)
def test_a_port_branches_land_on_from_both_sides_continues_the_centreline(
    stem: str, port_id: str
) -> None:
    """A fan with no branch on its centreline whose branches land on one port
    from both sides carries the whole fan through that port, straddling the
    centreline: the plan freezes the port on it."""
    drawn = _drawn(_curated(stem))
    assert drawn.graph is not None
    plans = [
        plan
        for plan in drawn.graph.fan_plans
        if port_id in {pid for b in plan.branches for pid in b.landing_port_ids}
    ]
    assert plans
    assert all(port_id in plan.centreline_port_ids for plan in plans)


def test_the_shallow_feeder_check_reads_the_approach_record() -> None:
    from nf_metro.layout.routing.offsets import same_row_bypass_entry

    source = inspect.getsource(same_row_bypass_entry)
    assert "_same_row_bypass_approaches" not in source
    assert not hasattr(offsets_module, "_same_row_bypass_approaches")


# ---- 11. consumers


def test_no_dealt_perpendicular_entry_columns() -> None:
    from nf_metro.layout.routing import context, perp_columns

    assert not hasattr(perp_columns, "PerpEntryColumn")
    assert "perp_entry_columns" not in context._RoutingCtx.__dataclass_fields__


def test_the_far_left_entry_u_turn_runs_over_the_top() -> None:
    drawn = _drawn(_curated("samerow_left_exit_far_left_entry"))
    graph, plan = drawn.graph, drawn.plan
    assert graph is not None and plan is not None
    over = [
        key
        for key, a in plan.port_orders.approaches.items()
        if a.rule is SideRule.OVER_TOP
    ]
    assert over
    for entry_id, source_id in over:
        top = graph.sections[graph.ports[entry_id].section_id].bbox_y
        for route, polyline in zip(drawn.routes, drawn.polylines, strict=True):
            edge = route.edge  # type: ignore[attr-defined]
            if (
                edge.target == entry_id
                and _origin(graph, edge.source, edge.line_id) == source_id
            ):
                assert min(y for _x, y in polyline) < top


def test_a_rail_mode_map_reads_no_lanes() -> None:
    path = ROOT / "examples" / "rail_mode.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    assert graph.line_spread is LineSpread.RAILS
    assert offsets_module.compute_station_offsets(graph) == {}


@pytest.mark.parametrize(
    "stem",
    [
        "rail_boundary_bundle_fan",
        "rail_symmetric_fork_join_spans",
        "rail_marked_single_line",
    ],
)
def test_a_rail_section_port_straddles_its_bundle(stem: str) -> None:
    drawn = _drawn(_curated(stem))
    graph, plan = drawn.graph, drawn.plan
    assert graph is not None and plan is not None
    rail_ports = [
        pid
        for pid, port in graph.ports.items()
        if graph.is_rail_section(port.section_id)
    ]
    assert rail_ports
    assert set(plan.tracks.rail_origins) <= set(rail_ports)
    for pid in rail_ports:
        values = [
            drawn.offsets.get((pid, lid), 0.0) for lid in graph.station_lines(pid)
        ]
        if values:
            assert abs(min(values) + max(values)) <= 1e-6, pid


# ---- 12. overlaid endpoint slots


def test_no_overlaid_endpoint_slot_compatibility() -> None:
    reasons = corridor_cohort_integration.CorridorCompatibilityReason
    assert "OVERLAID_ENDPOINT_SLOTS" not in reasons.__members__
