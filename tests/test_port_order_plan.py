"""The port order plan: every port's line order, decided once from structure.

:mod:`nf_metro.layout.port_order_plan` is decided when a layout seals its lane
plan, before that offsets run writes any lane, so it can only be a function of
the map's structure and placement.  These tests hold the properties that make
it a plan: it covers every port, it is deterministic, and neither it nor the
lane plan module reads anything a later phase writes, beyond the fan plans
each layout installs before its first stage.  Rotation and reflection are held
for the whole record in ``test_lane_plan.py``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import warnings
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import pytest

import nf_metro.layout.fan_plans as fan_plans_module
import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.layout.port_order_plan as port_order_plan_module
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.port_order_plan import (
    OrderBasis,
    PortClass,
    PortOrderPlan,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    MetroLine,
    Port,
    PortSide,
    Section,
    Station,
)

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = ROOT / "examples" / "topologies"

COVERAGE_FIXTURES = [
    ROOT / "examples" / "rnaseq_sections.mmd",
    TOPOLOGIES / "bt_perp_left_entry_right_exit.mmd",
    TOPOLOGIES / "fan_branch_additional_outputs.mmd",
    TOPOLOGIES / "merge_adjacent_feeder.mmd",
    TOPOLOGIES / "packed_cell_right_exit_left_entry_wrap.mmd",
    TOPOLOGIES / "paired_input_fan_branch_tree.mmd",
    TOPOLOGIES / "stacked_top_entry_flanked_riser.mmd",
    TOPOLOGIES / "entry_cohort_interleaved_fixed_lane.mmd",
]

# Names whose values a later offset phase, routing or emission writes.
LATER_PHASE_NAMES = frozenset(
    {
        "offsets",
        "station_offsets",
        "routes",
        "route",
        "draft",
        "drafts",
        "fan_plan_execution",
        "_departure_lane_orders",
        "_departure_order_plan",
        "_linear_entry_pill_lines_cache",
        "compute_station_offsets",
        "route_edges",
    }
)

# Graph state each layout installs at its start (``_layout_once``), before any
# stage runs: a record module may read it only as a stated input.
LAYOUT_START_STATE = frozenset({"fan_plans", "fan_plan_query"})

STATED_INPUTS = {
    port_order_plan_module: frozenset[str](),
    lane_plan_module: frozenset({"fan_plans"}),
}

# Ways to reach any of the above, or run a phase on a copy, without naming it.
DYNAMIC_ACCESS = frozenset(
    {
        "getattr",
        "vars",
        "__dict__",
        "__import__",
        "importlib",
        "deepcopy",
        "eval",
        "exec",
    }
)

# Every (module, name) each record module imports: a new import is a new input.
ALLOWED_IMPORTS = {
    port_order_plan_module: frozenset(
        {
            ("__future__", "annotations"),
            ("collections.abc", "Callable"),
            ("collections.abc", "Iterator"),
            ("collections.abc", "Mapping"),
            ("collections.abc", "Sequence"),
            ("dataclasses", "dataclass"),
            ("enum", "Enum"),
            ("functools", "partial"),
            ("nf_metro.layout.constants", "SAME_Y_TOLERANCE"),
            ("nf_metro.layout.geometry", "flow_entry_side"),
            ("nf_metro.layout.geometry", "flow_port_sides"),
            ("nf_metro.layout.geometry", "packed_section_visual_order"),
            ("nf_metro.layout.geometry", "perpendicular_port_sides"),
            ("nf_metro.layout.routing.common", "ENTRY_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "EXIT_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "Direction"),
            ("nf_metro.layout.routing.common", "resolve_section"),
            ("nf_metro.layout.routing.common", "resolve_section_colrow"),
            ("nf_metro.layout.routing.context", "_packed_cell_mate_obstructs"),
            ("nf_metro.layout.routing.orientation", "direction_vector"),
            ("nf_metro.layout.routing.orientation", "get_point_coordinate"),
            ("nf_metro.layout.routing.orientation", "lateral_axis"),
            ("nf_metro.layout.routing.orientation", "lateral_order_sign"),
            ("nf_metro.parser.model", "MetroGraph"),
            ("nf_metro.parser.model", "Port"),
            ("nf_metro.parser.model", "PortSide"),
            ("nf_metro.parser.model", "Section"),
        }
    ),
    lane_plan_module: frozenset(
        {
            ("__future__", "annotations"),
            ("collections.abc", "Callable"),
            ("collections.abc", "Iterable"),
            ("collections.abc", "Mapping"),
            ("collections.abc", "Sequence"),
            ("dataclasses", "dataclass"),
            ("functools", "partial"),
            ("heapq", "heapify"),
            ("heapq", "heappop"),
            ("heapq", "heappush"),
            ("nf_metro.layout.constants", "SAME_Y_TOLERANCE"),
            ("nf_metro.layout.departure_order", "JUNCTIONS_POSITIONED"),
            ("nf_metro.layout.geometry", "AxisFrame"),
            ("nf_metro.layout.geometry", "flow_entry_side"),
            ("nf_metro.layout.geometry", "perpendicular_port_sides"),
            ("nf_metro.layout.port_order_plan", "ApproachKind"),
            ("nf_metro.layout.port_order_plan", "PortOrderPlan"),
            ("nf_metro.layout.port_order_plan", "PortOrderBuilder"),
            ("nf_metro.layout.port_order_plan", "decide_ports"),
            ("nf_metro.layout.port_order_plan", "port_travel"),
            ("nf_metro.layout.routing.common", "ENTRY_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "EXIT_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "Direction"),
            ("nf_metro.layout.routing.context", "_section_lane_frame"),
            ("nf_metro.layout.routing.orientation", "direction_vector"),
            ("nf_metro.layout.routing.orientation", "lateral_order_sign"),
            ("nf_metro.parser.model", "MetroGraph"),
            ("nf_metro.parser.model", "Section"),
            ("nf_metro.parser.model", "Station"),
            ("nf_metro.parser.model", "is_bypass_v"),
        }
    ),
}


@lru_cache(maxsize=None)
def _laid_out(source: str) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(source)
        compute_layout(graph)
    return graph


def _fresh_laid_out(source: str) -> MetroGraph:
    """An uncached graph a test may mutate."""
    return _laid_out.__wrapped__(source)


def _port_orders(graph: MetroGraph) -> PortOrderPlan:
    return lane_plan_module.build_lane_plan(graph).port_orders


def _plan(source: str) -> PortOrderPlan:
    return _port_orders(_laid_out(source))


@pytest.mark.parametrize("path", COVERAGE_FIXTURES, ids=lambda p: p.stem)
def test_every_port_has_an_order(path: Path) -> None:
    graph = _laid_out(path.read_text())
    plan = _port_orders(graph)

    assert set(plan.orders) == set(graph.ports)
    for order in plan:
        assert order.decided, order
        assert order.lines is not None
        assert sorted(order.lines) == sorted(graph.station_lines(order.port_id))


def test_plan_does_not_depend_on_the_hash_seed() -> None:
    fixture = COVERAGE_FIXTURES[-1]
    script = (
        "import warnings; warnings.simplefilter('ignore')\n"
        "from nf_metro.parser.mermaid import parse_metro_mermaid\n"
        "from nf_metro.layout.engine import compute_layout\n"
        "from nf_metro.layout.lane_plan import build_lane_plan\n"
        "from pathlib import Path\n"
        f"g = parse_metro_mermaid(Path({str(fixture)!r}).read_text())\n"
        "compute_layout(g)\n"
        "print(sorted(build_lane_plan(g).port_orders.orders.items()))\n"
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


def test_a_line_with_no_reference_node_is_undecided() -> None:
    graph = _fresh_laid_out((ROOT / "examples" / "rnaseq_sections.mmd").read_text())
    entry_id, line_id = next(
        (pid, lid)
        for pid, port in sorted(graph.ports.items())
        if port.is_entry
        for lid in graph.station_lines(pid)
        if len(graph.station_lines(pid)) > 1
    )
    orphaned = [
        e for e in graph.edges if not (e.target == entry_id and e.line_id == line_id)
    ]
    graph.replace_edges(orphaned)
    order = _port_orders(graph).orders[entry_id]

    assert not order.decided
    assert order.missing is not None
    assert line_id in order.missing


def test_coverage_fixtures_exercise_every_port_class() -> None:
    """The coverage fixtures exercise every :class:`PortClass`."""
    seen = {
        order.port_class
        for path in COVERAGE_FIXTURES
        for order in _plan(path.read_text())
    }
    assert seen == set(PortClass)


def _tied_entry() -> MetroGraph:
    """Two lines reach entry ``b_in`` level from two stations of the section
    before it, one ahead of the other: nothing but priority separates them."""
    graph = MetroGraph()
    for line_id in ("l2", "l1"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    for col, sid in enumerate(("a", "b")):
        graph.add_section(Section(sid, sid, direction="LR", grid_col=col, grid_row=0))
    for node_id, x in (("s1", 0.0), ("s2", 10.0)):
        graph.register_station(Station(node_id, node_id, section_id="a"))
        graph.stations[node_id].x = x
    graph.add_port(Port("b_in", "b", PortSide.LEFT, is_entry=True))
    graph.stations["b_in"].x = 100.0
    graph.register_station(Station("t", "t", section_id="b"))
    graph.stations["t"].x = 150.0
    graph.add_edge(Edge("s1", "b_in", "l1"))
    graph.add_edge(Edge("s2", "b_in", "l2"))
    for line_id in ("l1", "l2"):
        graph.add_edge(Edge("b_in", "t", line_id))
    return graph


def test_lines_tied_at_an_entry_fall_to_priority() -> None:
    order = _port_orders(_tied_entry()).orders["b_in"]

    assert order.lines == ("l2", "l1")
    assert order.basis is OrderBasis.PRIORITY


def test_order_bases_cover_every_kind() -> None:
    """The coverage fixtures and the tied entry exercise every
    :class:`OrderBasis`."""
    seen = {
        order.basis for path in COVERAGE_FIXTURES for order in _plan(path.read_text())
    }
    seen |= {order.basis for order in _port_orders(_tied_entry())}
    assert seen == set(OrderBasis)


def _two_section_loop() -> MetroGraph:
    """Sections ``a`` and ``b`` feed each other, two lines through each port.

    Every port's lines share one reference node at one lateral, so each order
    rests on the rank of a reference port that depends on it round the loop.
    """
    graph = MetroGraph()
    for line_id in ("l1", "l2"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    for col, sid in enumerate(("a", "b")):
        graph.add_section(Section(sid, sid, direction="LR", grid_col=col, grid_row=0))
        for side, is_entry, x in (
            (PortSide.LEFT, True, 0.0),
            (PortSide.RIGHT, False, 50.0),
        ):
            port_id = f"{sid}_{'in' if is_entry else 'out'}"
            graph.add_port(Port(port_id, sid, side, is_entry=is_entry))
            graph.stations[port_id].x = col * 100.0 + x
    for line_id in ("l1", "l2"):
        for src, tgt in (
            ("a_in", "a_out"),
            ("a_out", "b_in"),
            ("b_in", "b_out"),
            ("b_out", "a_in"),
        ):
            graph.add_edge(Edge(src, tgt, line_id))
    return graph


def _self_feeding_section() -> MetroGraph:
    """Section ``a`` feeds its own entry, two lines through one station.

    The section's lanes need the entry's order, which rests on the exit's,
    which is read from the section's lanes, round the loop.
    """
    graph = MetroGraph()
    for line_id in ("l1", "l2"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    graph.add_section(Section("a", "a", direction="LR", grid_col=0, grid_row=0))
    graph.register_station(Station("s", "s", section_id="a"))
    graph.add_port(Port("a_0", "a", PortSide.RIGHT, is_entry=False))
    graph.add_port(Port("a_1", "a", PortSide.LEFT, is_entry=True))
    graph.stations["a_0"].x = 50.0
    for line_id in ("l1", "l2"):
        for src, tgt in (("a_1", "s"), ("s", "a_0"), ("a_0", "a_1")):
            graph.add_edge(Edge(src, tgt, line_id))
    return graph


def test_a_port_on_a_section_cycle_is_undecided() -> None:
    plan = lane_plan_module.build_lane_plan(_self_feeding_section())
    orders = plan.port_orders.orders

    assert plan.tracks.cycle_sections == ("a",)
    assert not orders["a_0"].decided
    assert "section cycle" in (orders["a_0"].missing or "")
    assert not orders["a_1"].decided
    assert "'a_0'" in (orders["a_1"].missing or "")


def test_lines_from_one_station_take_lanes_by_priority() -> None:
    """Two lines leaving through one exit from one in-section station arrive
    from nowhere off their run, so their section slots, in declaration order,
    decide their lanes and the exit reads them."""
    graph = MetroGraph()
    for line_id in ("l2", "l1"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    graph.add_section(Section("a", "a", direction="LR", grid_col=0, grid_row=0))
    graph.register_station(Station("s", "s", section_id="a"))
    graph.add_port(Port("a_out", "a", PortSide.RIGHT, is_entry=False))
    graph.stations["a_out"].x = 50.0
    for line_id in ("l1", "l2"):
        graph.add_edge(Edge("s", "a_out", line_id))

    plan = lane_plan_module.build_lane_plan(graph)
    order = plan.port_orders.orders["a_out"]

    assert order.lines == ("l2", "l1")
    assert order.basis is OrderBasis.LANES
    assert plan.tracks.lanes[("a_out", "l2")] == 0
    assert plan.tracks.lanes[("a_out", "l1")] == 1


def _quarter_turn(point: tuple[float, float], turns: int) -> tuple[float, float]:
    x, y = point
    for _ in range(turns):
        x, y = -y, x
    return (x, y)


@pytest.mark.parametrize(
    ("turns", "direction", "side"),
    [
        (0, "LR", PortSide.LEFT),
        (1, "TB", PortSide.TOP),
        (2, "RL", PortSide.RIGHT),
        (3, "BT", PortSide.BOTTOM),
    ],
    ids=["LR", "TB", "RL", "BT"],
)
def test_an_exit_against_the_flow_orders_across_its_own_travel(
    turns: int, direction: str, side: PortSide
) -> None:
    """Lines leaving against the flow run straight from their feeders into the
    port, so the feeder on the traveller's left comes first: for an LR
    section's LEFT exit, the lower feeder."""
    graph = MetroGraph()
    for line_id in ("upper", "lower"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    graph.add_section(Section("a", "a", direction=direction, grid_col=0, grid_row=0))
    placed = {"upper": (50.0, 0.0), "lower": (50.0, 40.0), "a_out": (0.0, 20.0)}
    for node_id, point in placed.items():
        if node_id == "a_out":
            graph.add_port(Port(node_id, "a", side, is_entry=False))
        else:
            graph.register_station(Station(node_id, node_id, section_id="a"))
        station = graph.stations[node_id]
        station.x, station.y = _quarter_turn(point, turns)
    for line_id in ("upper", "lower"):
        graph.add_edge(Edge(line_id, "a_out", line_id))

    order = _port_orders(graph).orders["a_out"]

    assert order.lines == ("lower", "upper")
    assert order.basis is OrderBasis.LANES


def test_an_entry_without_grid_cells_is_undecided() -> None:
    graph = _two_section_loop()
    graph.sections["a"].grid_col = -1

    order = _port_orders(graph).orders["b_in"]

    assert not order.decided
    assert order.missing is not None
    assert order.missing.startswith("grid cells")


def _names(tree: ast.Module) -> set[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    return names | {alias.name for n in froms for alias in n.names}


def _later_phase_reads(
    source: str,
    allowed: frozenset[tuple[str, str]],
    stated: frozenset[str] = frozenset(),
) -> list[str]:
    """What in *source* could read later-phase state, read layout-start state
    beyond *stated*, or run a phase on a copy."""
    tree = ast.parse(source)
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names = _names(tree)
    found = sorted(
        names & (LATER_PHASE_NAMES | DYNAMIC_ACCESS | (LAYOUT_START_STATE - stated))
    )
    if any(isinstance(n, ast.Import) for n in ast.walk(tree)):
        found.append("plain import")
    if any(n.level != 0 for n in froms):
        found.append("relative import")
    imported = {(n.module, alias.name) for n in froms for alias in n.names}
    found.extend(f"{m}.{n}" for m, n in sorted(imported - allowed, key=str))
    return found


@pytest.mark.parametrize(
    "module", list(ALLOWED_IMPORTS), ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_the_record_modules_read_no_later_phase_state(module: ModuleType) -> None:
    """Their inputs are structure, placement and the state each module states
    of what its layout installs at its start: no lane offset, route, draft,
    departure record or offsets/routing entry point appears in either module,
    and each imports nothing beyond its stated inputs."""
    source = Path(str(module.__file__)).read_text()
    assert (
        _later_phase_reads(source, ALLOWED_IMPORTS[module], STATED_INPUTS[module]) == []
    )


@pytest.mark.parametrize(
    "module", list(STATED_INPUTS), ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_each_stated_input_is_read(module: ModuleType) -> None:
    source = Path(str(module.__file__)).read_text()
    assert STATED_INPUTS[module] <= LAYOUT_START_STATE
    assert STATED_INPUTS[module] <= _names(ast.parse(source))


def test_the_lane_plan_reads_the_fan_plans_its_layout_installed() -> None:
    """Each record is built from the fan plans installed at the start of its
    own layout, and no layout installs fan plans after its record seals."""
    events: list[tuple[str, object]] = []
    real_install = fan_plans_module.install_fan_plan_execution
    real_start = lane_plan_module.start_layout
    real_build = lane_plan_module.build_lane_plan

    def install(graph: MetroGraph, execution: object) -> None:
        events.append(("install", execution))
        real_install(graph, execution)  # type: ignore[arg-type]

    def start(graph: MetroGraph) -> None:
        events.append(("start", None))
        real_start(graph)

    def build(graph: MetroGraph) -> lane_plan_module.LanePlan:
        events.append(("build", graph.fan_plan_execution))
        return real_build(graph)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fan_plans_module, "install_fan_plan_execution", install)
        mp.setattr(lane_plan_module, "start_layout", start)
        mp.setattr(lane_plan_module, "build_lane_plan", build)
        graph = _fresh_laid_out((TOPOLOGIES / "wide_fan_out.mmd").read_text())

    assert any(plan.owns_geometry and plan.offset_carriers for plan in graph.fan_plans)
    layouts: list[list[tuple[str, object]]] = []
    for kind, value in events:
        if kind == "start":
            layouts.append([])
        else:
            layouts[-1].append((kind, value))
    assert any(kind == "build" for layout in layouts for kind, _ in layout)
    for layout in layouts:
        kinds = [kind for kind, _ in layout]
        if "build" not in kinds:
            continue
        assert kinds == ["install", "build"]
        assert layout[1][1] is layout[0][1]


@pytest.mark.parametrize(
    "source",
    [
        "import copy\ndef f(g): return copy.deepcopy(g)",
        "import nf_metro.layout.phases.spacing as sp\ndef f(g): return sp.x(g)",
        "from . import engine\ndef f(g): engine.compute_layout(g)",
        "def f(g): return getattr(g, 'station' + '_offsets')",
        "from nf_metro.layout.routing.common import compute_routes_somehow",
        "def f(g): return vars(g)['station' + '_offsets']",
        "def f(g): return g.__dict__['station' + '_offsets']",
        "def f(g): return g.fan_plans",
    ],
    ids=[
        "deepcopy",
        "plain-import",
        "relative-import",
        "getattr",
        "new-name",
        "vars",
        "dunder-dict",
        "unstated-layout-start-state",
    ],
)
def test_the_module_check_catches_indirect_reads(source: str) -> None:
    assert _later_phase_reads(source, ALLOWED_IMPORTS[port_order_plan_module])
