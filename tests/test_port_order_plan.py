"""The port order plan: every port's line order, decided once from structure.

:mod:`nf_metro.layout.port_order_plan` is decided when a layout seals its lane
plan, before that offsets run writes any lane, so it can only be a function of
the map's structure and placement.  These tests hold the properties that make
it a plan: it covers every port, it is deterministic, and neither it nor the
lane plan module reads anything a later phase writes.  Rotation and reflection
are held for the whole record in ``test_lane_plan.py``.
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

import nf_metro.layout.lane_plan as lane_plan_module
import nf_metro.layout.port_order_plan as port_order_plan_module
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.port_order_plan import (
    OrderBasis,
    PortClass,
    PortOrderPlan,
    build_port_order_plan,
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
            ("dataclasses", "dataclass"),
            ("nf_metro.layout.constants", "SAME_Y_TOLERANCE"),
            ("nf_metro.layout.departure_order", "JUNCTIONS_POSITIONED"),
            ("nf_metro.layout.geometry", "AxisFrame"),
            ("nf_metro.layout.geometry", "flow_entry_side"),
            ("nf_metro.layout.geometry", "perpendicular_port_sides"),
            ("nf_metro.layout.port_order_plan", "ApproachKind"),
            ("nf_metro.layout.port_order_plan", "PortOrderPlan"),
            ("nf_metro.layout.port_order_plan", "PortOrderBuilder"),
            ("nf_metro.layout.port_order_plan", "port_travel"),
            ("nf_metro.layout.port_order_plan", "plan_builder"),
            ("nf_metro.layout.routing.common", "ENTRY_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "EXIT_SIDE_DIRECTION"),
            ("nf_metro.layout.routing.common", "Direction"),
            ("nf_metro.layout.routing.context", "_section_lane_frame"),
            ("nf_metro.layout.routing.orientation", "direction_vector"),
            ("nf_metro.layout.routing.orientation", "lateral_order_sign"),
            ("nf_metro.parser.model", "MetroGraph"),
            ("nf_metro.parser.model", "Section"),
            ("nf_metro.parser.model", "Station"),
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


def _plan(source: str) -> PortOrderPlan:
    return build_port_order_plan(_laid_out(source))


@pytest.mark.parametrize("path", COVERAGE_FIXTURES, ids=lambda p: p.stem)
def test_every_port_has_an_order(path: Path) -> None:
    graph = _laid_out(path.read_text())
    plan = build_port_order_plan(graph)

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
        "from nf_metro.layout.port_order_plan import build_port_order_plan\n"
        "from pathlib import Path\n"
        f"g = parse_metro_mermaid(Path({str(fixture)!r}).read_text())\n"
        "compute_layout(g)\n"
        "print(sorted(build_port_order_plan(g).orders.items()))\n"
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
    order = build_port_order_plan(graph).orders[entry_id]

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


def test_order_bases_cover_every_kind() -> None:
    """The coverage fixtures exercise every :class:`OrderBasis`."""
    seen = {
        order.basis for path in COVERAGE_FIXTURES for order in _plan(path.read_text())
    }
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

    The exit (``a_0``) is planned first; its tied lines carry the entry's
    rank, and the entry's order rests on the exit's, round the loop.
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
    plan = build_port_order_plan(_self_feeding_section())

    assert not plan.orders["a_1"].decided
    assert "section cycle" in (plan.orders["a_1"].missing or "")
    assert plan.orders["a_0"].decided


def test_lines_from_one_station_fall_to_priority() -> None:
    """Two lines leaving through one exit from one in-section station tie on
    lateral and have no port rank, so declaration order decides."""
    graph = MetroGraph()
    for line_id in ("l2", "l1"):
        graph.lines[line_id] = MetroLine(line_id, line_id, "#000000")
    graph.add_section(Section("a", "a", direction="LR", grid_col=0, grid_row=0))
    graph.register_station(Station("s", "s", section_id="a"))
    graph.add_port(Port("a_out", "a", PortSide.RIGHT, is_entry=False))
    graph.stations["a_out"].x = 50.0
    for line_id in ("l1", "l2"):
        graph.add_edge(Edge("s", "a_out", line_id))

    order = build_port_order_plan(graph).orders["a_out"]

    assert order.lines == ("l2", "l1")
    assert order.basis is OrderBasis.PRIORITY


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

    order = build_port_order_plan(graph).orders["a_out"]

    assert order.lines == ("lower", "upper")
    assert order.basis is OrderBasis.LATERAL


def test_an_entry_without_grid_cells_is_undecided() -> None:
    graph = _two_section_loop()
    graph.sections["a"].grid_col = -1

    order = build_port_order_plan(graph).orders["b_in"]

    assert not order.decided
    assert order.missing is not None
    assert order.missing.startswith("grid cells")


def _later_phase_reads(source: str, allowed: frozenset[tuple[str, str]]) -> list[str]:
    """What in *source* could read later-phase state or run a phase on a copy."""
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    froms = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names |= {alias.name for n in froms for alias in n.names}
    found = sorted(names & (LATER_PHASE_NAMES | DYNAMIC_ACCESS))
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
    """Their inputs are structure and placement: no lane offset, route, draft,
    departure record or offsets/routing entry point appears in either module,
    and each imports nothing beyond its stated inputs."""
    source = Path(str(module.__file__)).read_text()
    assert _later_phase_reads(source, ALLOWED_IMPORTS[module]) == []


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
    ],
    ids=[
        "deepcopy",
        "plain-import",
        "relative-import",
        "getattr",
        "new-name",
        "vars",
        "dunder-dict",
    ],
)
def test_the_module_check_catches_indirect_reads(source: str) -> None:
    assert _later_phase_reads(source, ALLOWED_IMPORTS[port_order_plan_module])
