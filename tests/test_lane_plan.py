"""The lane plan record: sealed once per layout, total, deterministic, unread.

:mod:`nf_metro.layout.lane_plan` decides every port's line order, every
same-band entry approach, each port's order travel and lane sense, each
perpendicular entry's consumer turns and the far-side approaches at the first
offsets run of a layout at or after stage 5.1.  These tests hold the record's
contract: one record per layout, replayed for the rest of it; total on every
curated map; independent of the hash seed; read by nothing in the package; a
rotated or reflected placement plans the rotated or reflected record; and its
approach sides are the ones dispatch draws today.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pytest

import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.layout.port_order_plan as port_order_plan_module
from nf_metro.layout import engine as engine_module
from nf_metro.layout.lane_plan import LanePlan
from nf_metro.layout.port_order_plan import (
    ApproachKind,
    OrderBasis,
    SideRule,
    _lateral,
    _port_travel,
)
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.offsets import (
    compute_station_offsets,
    same_row_bypass_entry,
)
from nf_metro.layout.routing.orientation import (
    direction_vector,
    lateral_order_sign,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, PortSide

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"
FIXTURES = ROOT / "tests" / "fixtures"

CURATED = sorted(
    p
    for p in [*(ROOT / "examples").rglob("*.mmd"), *FIXTURES.rglob("*.mmd")]
    if "invalid" not in p.parts
)

# Fixtures whose laid-out placement the rotation tests turn exactly; together
# they exercise every record rule (see ``test_rotation_fixtures_cover_every_rule``).
ROTATION_FIXTURES = [
    TOPOLOGIES / "bottom_entry_two_feeders_own_lanes.mmd",
    TOPOLOGIES / "bt_infer_ports.mmd",
    TOPOLOGIES / "disconnected_components_fold.mmd",
    TOPOLOGIES / "entry_cohort_interleaved_fixed_lane.mmd",
    TOPOLOGIES / "junction_entry_lane_step.mmd",
    TOPOLOGIES / "lr_perp_top_exit_perp_entry_diverging.mmd",
    TOPOLOGIES / "right_entry_wrap_bundle.mmd",
    TOPOLOGIES / "tb_right_entry_stack.mmd",
    FIXTURES / "regressions" / "stacked_collector_fanin.mmd",
]

# Same-row bypasses whose drawn approach the record does not state yet: the
# U-turn loop from a same-row LEFT exit into a LEFT entry it lies right of is
# drawn over the top while the turn-round rule records the traveller's right.
APPROACH_SIDE_EXCEPTIONS = frozenset(
    {("samerow_left_exit_far_left_entry", "te__entry_left_9", "psite_id__exit_left_4")}
)


@dataclass
class _Run:
    graph: MetroGraph | None
    plans: list[LanePlan] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@contextmanager
def _recording_builds(run: _Run) -> Iterator[None]:
    real = lane_plan_module.build_lane_plan

    def spy(graph: MetroGraph) -> LanePlan:
        try:
            plan = real(graph)
        except Exception as exc:
            run.failures.append(f"{type(exc).__name__}: {exc}")
            raise
        run.plans.append(plan)
        return plan

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lane_plan_module, "build_lane_plan", spy)
        yield


@lru_cache(maxsize=None)
def _laid_out(path: Path) -> _Run:
    """Lay *path* out, keeping every record built; ``graph`` is ``None`` when
    the map does not parse or its layout aborts."""
    run = _Run(None)
    with warnings.catch_warnings(), _recording_builds(run):
        warnings.simplefilter("ignore")
        try:
            graph = parse_metro_mermaid(path.read_text())
        except Exception:
            return run
        try:
            engine_module.compute_layout(graph)
        except Exception:
            return run
    run.graph = graph
    return run


def _plan(graph: MetroGraph) -> LanePlan:
    return lane_plan_module.build_lane_plan(graph)


# ---- totality, lifetime and determinism


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_sealed_record_is_total(path: Path) -> None:
    run = _laid_out(path)

    assert run.failures == []
    if run.graph is None:
        return
    plan = run.graph._lane_plan
    if plan is None:
        assert not run.plans
        return
    graph = run.graph
    assert plan is run.plans[-1]
    assert set(plan.port_orders.orders) == set(graph.ports)
    assert set(plan.order_travels) == set(graph.ports) == set(plan.lane_senses)
    for order in plan.port_orders:
        if order.decided:
            assert order.lines is not None
            assert sorted(order.lines) == graph.station_lines(order.port_id)
        else:
            assert order.missing
    for (entry_id, source_id), approach in plan.port_orders.approaches.items():
        assert graph.ports[entry_id].is_entry
        assert (approach.kind is ApproachKind.LEVEL) == (approach.side == 0)
        assert (approach.rule is SideRule.LEVEL) == (approach.side == 0)
        assert source_id in {
            node
            for lid in graph.station_lines(entry_id)
            for node, _via in port_order_plan_module._Builder(graph).upstream_sources(
                entry_id, lid
            )
        }
    for port_id, turns in plan.perpendicular_turns.items():
        assert graph.ports[port_id].is_entry
        assert {lid for turn in turns for lid in turn.lines} <= set(
            graph.station_lines(port_id)
        )


def _origin(graph: MetroGraph, node: str, line_id: str) -> str:
    seen: set[str] = set()
    while node in graph.junction_ids and node not in seen:
        seen.add(node)
        ups = [e.source for e in graph.edges_to(node) if e.line_id == line_id]
        if len(ups) != 1:
            return node
        node = ups[0]
    return node


def _screen_side(graph: MetroGraph, entry_id: str, side: int) -> str:
    travel = _port_travel(graph.ports[entry_id])
    if side == 0:
        return "flat"
    return "below" if side * lateral_order_sign(travel) > 0 else "above"


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_approach_sides_match_the_drawn_same_row_bypasses(path: Path) -> None:
    """Every same-row bypass dispatch draws round its row today arrives on the
    side the approach record states."""
    run = _laid_out(path)
    graph = run.graph
    if graph is None or graph._lane_plan is None:
        return
    approaches = graph._lane_plan.port_orders.approaches
    offsets = compute_station_offsets(graph)
    drawn: list[tuple[Edge, str]] = []
    real = offsets_module._same_row_bypass_approaches

    def spy(
        g: MetroGraph,
        edges: Sequence[Edge],
        station_offsets: dict[tuple[str, str], float] | None,
    ) -> list[Literal["above", "below", "flat"]]:
        got = real(g, edges, station_offsets)
        drawn.extend(zip(edges, got))
        return got

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(offsets_module, "_same_row_bypass_approaches", spy)
        for port_id in sorted(graph.ports):
            if graph.ports[port_id].is_entry:
                same_row_bypass_entry(graph, port_id, offsets)
    for edge, side in drawn:
        origin = _origin(graph, edge.source, edge.line_id)
        if (path.stem, edge.target, origin) in APPROACH_SIDE_EXCEPTIONS:
            continue
        record = approaches.get((edge.target, origin))
        assert record is not None, (edge.target, origin)
        assert _screen_side(graph, edge.target, record.side) == side, (
            edge.target,
            origin,
        )


def test_approach_side_exceptions_are_live() -> None:
    for stem, entry_id, source_id in APPROACH_SIDE_EXCEPTIONS:
        graph = _laid_out(TOPOLOGIES / f"{stem}.mmd").graph
        assert graph is not None and graph._lane_plan is not None
        record = graph._lane_plan.port_orders.approaches[(entry_id, source_id)]
        assert record.rule is SideRule.TURN_ROUND


@contextmanager
def _layouts(kinds: list[str], builds: list[int]) -> Iterator[None]:
    """Record each layout's kind and, per record built, its layout's index."""
    real_layout = engine_module._layout_once
    real_build = lane_plan_module.build_lane_plan
    real_sinks = engine_module._line_crossed_file_icon_sinks
    pending_off_track = [False]

    def layout_once(graph: MetroGraph, **kwargs: object) -> None:
        names = {frame.function for frame in inspect.stack()}
        if not kinds:
            kinds.append("first")
        elif "apply_geometric_bypass" in names:
            kinds.append("bypass")
        elif "_apply_label_strike_clearance" in names:
            kinds.append("strike")
        elif pending_off_track[0]:
            kinds.append("off-track")
        else:
            kinds.append("other")
        pending_off_track[0] = False
        real_layout(graph, **kwargs)  # type: ignore[arg-type]

    def sinks(graph: MetroGraph) -> object:
        found = real_sinks(graph)
        pending_off_track[0] = bool(found)
        return found

    def build(graph: MetroGraph) -> LanePlan:
        builds.append(len(kinds) - 1)
        return real_build(graph)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_module, "_layout_once", layout_once)
        mp.setattr(engine_module, "_line_crossed_file_icon_sinks", sinks)
        mp.setattr(lane_plan_module, "build_lane_plan", build)
        yield


@pytest.mark.parametrize(
    ("path", "relayout"),
    [
        (ROOT / "examples" / "inactive_lines.mmd", "bypass"),
        (TOPOLOGIES / "out_of_section_retag_fan.mmd", "off-track"),
        (TOPOLOGIES / "offtrack_output_peel_before_successor.mmd", "strike"),
    ],
    ids=["bypass", "off-track", "strike"],
)
def test_each_layout_seals_one_record(path: Path, relayout: str) -> None:
    kinds: list[str] = []
    builds: list[int] = []
    with warnings.catch_warnings(), _layouts(kinds, builds):
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(path.read_text())
        engine_module.compute_layout(graph)

    assert relayout in kinds
    assert len(builds) == len(set(builds)) == len(kinds)
    assert builds == sorted(builds)


def test_a_layout_replays_its_record() -> None:
    graph = _laid_out(ROOT / "examples" / "rnaseq_sections.mmd").graph
    assert graph is not None
    sealed = graph._lane_plan
    builds: list[int] = []
    with _layouts([], builds):
        compute_station_offsets(graph)

    assert builds == []
    assert graph._lane_plan is sealed


def test_the_record_is_sealed_before_the_first_lane_after_5_1() -> None:
    seen: list[tuple[bool, LanePlan | None, int]] = []
    base = offsets_module._compute_base_offsets

    def spy(ctx: offsets_module._OffsetCtx) -> None:
        stages = ctx.graph._stages_completed
        seen.append(("5.1" in stages, ctx.graph._lane_plan, len(ctx.offsets)))
        base(ctx)

    with warnings.catch_warnings(), pytest.MonkeyPatch.context() as mp:
        warnings.simplefilter("ignore")
        mp.setattr(offsets_module, "_compute_base_offsets", spy)
        graph = parse_metro_mermaid(
            (ROOT / "examples" / "rnaseq_sections.mmd").read_text()
        )
        engine_module.compute_layout(graph)

    assert any(after for after, _plan, _n in seen)
    for after, plan, written in seen:
        assert written == 0
        assert (plan is not None) == after


@pytest.mark.parametrize("path", ROTATION_FIXTURES[:4], ids=lambda p: p.stem)
def test_the_record_does_not_depend_on_the_hash_seed(path: Path) -> None:
    script = (
        "import warnings; warnings.simplefilter('ignore')\n"
        "from nf_metro.parser.mermaid import parse_metro_mermaid\n"
        "from nf_metro.layout.engine import compute_layout\n"
        "from pathlib import Path\n"
        f"g = parse_metro_mermaid(Path({str(path)!r}).read_text())\n"
        "compute_layout(g)\n"
        "print(repr(g._lane_plan))\n"
    )
    outputs = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.add(result.stdout)
    assert len(outputs) == 1


# ---- read by nothing


def _src_modules() -> Iterator[tuple[str, ast.Module]]:
    src = ROOT / "src" / "nf_metro"
    for path in sorted(src.rglob("*.py")):
        yield str(path.relative_to(src)), ast.parse(path.read_text())


# The record's lifetime hooks and the graph field's type are its only uses.
RECORD_USES = {
    "layout/engine.py": {"start_layout"},
    "layout/routing/offsets.py": {"seal_lane_plan"},
    "parser/model.py": {"LanePlan"},
}
RECORD_MODULES = {"layout/lane_plan.py", "layout/port_order_plan.py"}


def test_nothing_reads_the_record() -> None:
    found: list[str] = []
    for name, tree in _src_modules():
        if name in RECORD_MODULES:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_lane_plan":
                found.append(f"{name}:{node.lineno} ._lane_plan")
            if isinstance(node, ast.ImportFrom) and node.module in (
                "nf_metro.layout.lane_plan",
                "nf_metro.layout.port_order_plan",
            ):
                names = {alias.name for alias in node.names}
                if names - RECORD_USES.get(name, set()):
                    found.append(f"{name}:{node.lineno} imports {sorted(names)}")
            if isinstance(node, ast.Import) and any(
                alias.name.endswith(("lane_plan", "port_order_plan"))
                for alias in node.names
            ):
                found.append(f"{name}:{node.lineno} imports the record module")
    assert found == []


def test_model_declares_the_record_field_only() -> None:
    model = dict(_src_modules())["parser/model.py"]
    reads = [
        node
        for node in ast.walk(model)
        if isinstance(node, ast.Attribute) and node.attr == "_lane_plan"
    ]
    assert reads == []


# ---- rotation and reflection

_VECTOR_OF_DIRECTION = {"LR": (1, 0), "RL": (-1, 0), "TB": (0, 1), "BT": (0, -1)}
_VECTOR_OF_SIDE = {
    PortSide.LEFT: (-1, 0),
    PortSide.RIGHT: (1, 0),
    PortSide.TOP: (0, -1),
    PortSide.BOTTOM: (0, 1),
}
_VECTOR_OF_TRAVEL = {d: direction_vector(d) for d in Direction}


@dataclass(frozen=True)
class _Turn:
    turns: int
    reflect: bool

    @property
    def name(self) -> str:
        return f"{'m' if self.reflect else 'r'}{90 * self.turns}"

    def vec(self, x: float, y: float) -> tuple[float, float]:
        if self.reflect:
            x = -x
        for _ in range(self.turns):
            x, y = -y, x
        return x, y

    def rect(self, x: float, y: float, w: float, h: float) -> tuple[float, ...]:
        corners = [self.vec(x, y), self.vec(x + w, y + h)]
        xs, ys = sorted(c[0] for c in corners), sorted(c[1] for c in corners)
        return xs[0], ys[0], xs[1] - xs[0], ys[1] - ys[0]

    def direction(self, flow: str) -> str:
        image = self.vec(*_VECTOR_OF_DIRECTION[flow])
        return next(k for k, v in _VECTOR_OF_DIRECTION.items() if v == image)

    def side(self, side: PortSide) -> PortSide:
        image = self.vec(*_VECTOR_OF_SIDE[side])
        return next(k for k, v in _VECTOR_OF_SIDE.items() if v == image)

    def travel(self, travel: Direction | None) -> Direction | None:
        if travel is None:
            return None
        image = self.vec(*_VECTOR_OF_TRAVEL[travel])
        return next(k for k, v in _VECTOR_OF_TRAVEL.items() if v == image)


TURNS = [_Turn(t, False) for t in (1, 2, 3)] + [_Turn(t, True) for t in range(4)]


def _turned(graph: MetroGraph, turn: _Turn) -> MetroGraph:
    """*graph* with its placement, grid, directions and port sides turned."""
    image = deepcopy(graph)
    for station in image.stations.values():
        station.x, station.y = turn.vec(station.x, station.y)
    cells = {}
    for section in image.sections.values():
        section.bbox_x, section.bbox_y, section.bbox_w, section.bbox_h = turn.rect(
            section.bbox_x, section.bbox_y, section.bbox_w, section.bbox_h
        )
        section.direction = turn.direction(section.direction)
        if section.grid_col >= 0 and section.grid_row >= 0:
            cells[section.id] = turn.rect(
                section.grid_col,
                section.grid_row,
                section.grid_col_span,
                section.grid_row_span,
            )
    col0 = min((c[0] for c in cells.values()), default=0)
    row0 = min((c[1] for c in cells.values()), default=0)
    for sid, (cx, cy, cw, ch) in cells.items():
        section = image.sections[sid]
        section.grid_col, section.grid_row = round(cx - col0), round(cy - row0)
        section.grid_col_span, section.grid_row_span = round(cw), round(ch)
    for port in image.ports.values():
        port.side = turn.side(port.side)
    return image


def _convention_side(entry_travel: Direction) -> int:
    below = _lateral((0.0, 1.0), (0.0, 0.0), entry_travel)
    return -1 if below < 0 else 1


_CONVENTIONS = {
    SideRule.SCREEN_BELOW: _convention_side,
    SideRule.TURN_ROUND: lambda _travel: 1,
}


@lru_cache(maxsize=None)
def _rotation_case(path: Path) -> tuple[MetroGraph, LanePlan]:
    graph = _laid_out(path).graph
    assert graph is not None and not any(len(m) >= 2 for m in graph.cell_packs.values())
    return graph, _plan(graph)


@pytest.mark.parametrize(
    ("path", "turn"),
    [(p, t) for p in ROTATION_FIXTURES for t in TURNS],
    ids=lambda v: v.stem if isinstance(v, Path) else v.name,
)
def test_a_turned_placement_plans_the_turned_record(path: Path, turn: _Turn) -> None:
    """Every rule reads its inputs in a travel frame, so turning the placement
    turns the record: a rotation keeps each order and side, a reflection
    reverses each lateral-decided order and flips each side.  The two side
    conventions dispatch draws are the exception: a turn-round U-turn loops on
    the traveller's right in every frame, and the screen-below side is the
    traveller's right except across leftward travel.  Where a turn changes a
    convention's side, the orders and far sides resting on it are not
    compared."""
    graph, reference = _rotation_case(path)
    image_graph = _turned(graph, turn)
    image = _plan(image_graph)
    flip = -1 if turn.reflect else 1

    convention_kept = True
    for key, ref in reference.port_orders.approaches.items():
        got = image.port_orders.approaches[key]
        assert (got.kind, got.rule, got.reach) == (ref.kind, ref.rule, ref.reach), key
        if ref.rule in _CONVENTIONS:
            travel = _port_travel(image_graph.ports[key[0]])
            assert got.side == _CONVENTIONS[ref.rule](travel), key
            convention_kept &= got.side == flip * ref.side
        else:
            assert got.side == flip * ref.side, key

    for port_id, travel in reference.order_travels.items():
        assert image.order_travels[port_id] is turn.travel(travel), port_id
    for port_id, turns in reference.perpendicular_turns.items():
        assert [(t.consumer_id, t.lines, turn.travel(t.turn)) for t in turns] == [
            (t.consumer_id, t.lines, t.turn) for t in image.perpendicular_turns[port_id]
        ]
    if not convention_kept:
        return
    assert image.far_sides == {k: flip * v for k, v in reference.far_sides.items()}
    for port_id, ref_order in reference.port_orders.orders.items():
        image_order = image.port_orders.orders[port_id]
        assert image_order.port_class is ref_order.port_class, port_id
        if not turn.reflect:
            assert image_order.lines == ref_order.lines, port_id
        elif ref_order.basis is OrderBasis.LATERAL:
            assert ref_order.lines is not None
            assert image_order.lines == tuple(reversed(ref_order.lines)), port_id


def test_rotation_fixtures_cover_every_rule() -> None:
    """The fixtures above exercise every rule the record states."""
    fired: set[str] = set()
    builder_cls = port_order_plan_module._Builder
    real = {
        name: getattr(builder_cls, name)
        for name in ("joining_side", "cell_lateral", "own_band", "_round_own_side")
    }
    real_merge = port_order_plan_module._merge_orders

    def joining_side(self, ref, lateral, here, travel, kind):  # type: ignore[no-untyped-def]
        value = real["joining_side"](self, ref, lateral, here, travel, kind)
        if value:
            fired.add("nest" if kind is ApproachKind.U_TURN else "joining side")
        return value

    def cell_lateral(self, node_id, ref_id, travel):  # type: ignore[no-untyped-def]
        value = real["cell_lateral"](self, node_id, ref_id, travel)
        a, b = self.cell_centre(node_id), self.cell_centre(ref_id)
        if value == 0.0 and abs(_lateral(a, b, travel)) > 0:
            fired.add("shared band")
        return value

    def own_band(self, node_id):  # type: ignore[no-untyped-def]
        value = real["own_band"](self, node_id)
        if value is not None:
            fired.add("own band")
        return value

    def round_own_side(self, refs, laterals, kinds, here, travel):  # type: ignore[no-untyped-def]
        before = dict(laterals)
        real["_round_own_side"](self, refs, laterals, kinds, here, travel)
        if before != laterals:
            fired.add("round own side")

    def merge(planned, chains, lines):  # type: ignore[no-untyped-def]
        value = real_merge(planned, chains, lines)
        if value != list(planned):
            fired.add("bundle merge")
        return value

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builder_cls, "joining_side", joining_side)
        mp.setattr(builder_cls, "cell_lateral", cell_lateral)
        mp.setattr(builder_cls, "own_band", own_band)
        mp.setattr(builder_cls, "_round_own_side", round_own_side)
        mp.setattr(port_order_plan_module, "_merge_orders", merge)
        for path in ROTATION_FIXTURES:
            graph, _ = _rotation_case(path)
            plan = _plan(graph)
            for port_id, turns in plan.perpendicular_turns.items():
                if sum(t.turn is not None for t in turns) >= 2:
                    fired.add("multi-consumer turns")
                default = plan.order_travels[port_id] is not _port_travel(
                    graph.ports[port_id]
                )
                if len(turns) == 1 and turns[0].turn is not None and default:
                    fired.add("single-consumer turn")
            for approach in plan.port_orders.approaches.values():
                fired.add(approach.rule.value)
            if plan.far_sides:
                fired.add("far side")

    assert fired >= {
        "joining side",
        "nest",
        "shared band",
        "own band",
        "round own side",
        "bundle merge",
        "multi-consumer turns",
        "single-consumer turn",
        SideRule.TURN_ROUND.value,
        SideRule.SCREEN_BELOW.value,
        "far side",
    }


# ---- screen conventions the record keeps


def test_the_row_top_corridor_is_dispatch_rule_21_scope() -> None:
    """Only a LEFT entry of an LR section takes the row-top corridor: it is the
    packed-cell same-row bypass handler's own scope."""
    seen = 0
    for path in CURATED:
        graph = _laid_out(path).graph
        if graph is None or graph._lane_plan is None:
            continue
        for (entry_id, _), approach in graph._lane_plan.port_orders.approaches.items():
            if approach.rule is SideRule.ROW_TOP:
                port = graph.ports[entry_id]
                assert port.side is PortSide.LEFT
                assert graph.section_for_port(port).direction == "LR"
                assert approach.side == -1
                seen += 1
    assert seen > 0


def test_a_packed_cell_member_sits_at_its_own_slot() -> None:
    graph = _laid_out(TOPOLOGIES / "packed_cell_cellmate_bypass.mmd").graph
    assert graph is not None
    builder = port_order_plan_module._Builder(graph)
    packs = [m for m in graph.cell_packs.values() if len(m) >= 2]
    assert packs
    for members in packs:
        sections = [graph.sections[m] for m in members]
        by_box = sorted(sections, key=lambda s: s.bbox_x)
        centres = [builder.section_centre(s)[0] for s in by_box]
        assert centres == sorted(centres)
        assert len(set(centres)) == len(centres)


def test_screen_below_convention_by_travel() -> None:
    assert _convention_side(Direction.R) == 1
    assert _convention_side(Direction.D) == 1
    assert _convention_side(Direction.U) == 1
    assert _convention_side(Direction.L) == -1
