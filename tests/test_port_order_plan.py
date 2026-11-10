"""The port order plan: every port's line order, decided once from structure.

:mod:`nf_metro.layout.port_order_plan` is decided at the start of each offsets
run, before any lane offset exists, so it can only be a function of the map's
structure and placement.  These tests hold the properties that make it a plan:
it covers every port, it is deterministic, a rotated map plans the rotated
orders, and its module reads nothing a later phase writes.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import warnings
from functools import lru_cache
from pathlib import Path

import pytest
from orientation_signature import port_key
from orientation_transform import Orientation, transform_source

import nf_metro.layout.port_order_plan as port_order_plan_module
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.port_order_plan import (
    OrderBasis,
    PortClass,
    PortOrderPlan,
    build_port_order_plan,
)
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.offsets import _run_offset_phases
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

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
]

# Fully authored (direction and grid on every section), so the orientation
# transform carries their whole geometry.
ROTATION_FIXTURES = [
    TOPOLOGIES / "bt_perp_left_entry_right_exit.mmd",
    TOPOLOGIES / "bt_to_tb.mmd",
    TOPOLOGIES / "lr_top_entry_cross_column_two_line.mmd",
    TOPOLOGIES / "orbit_perp_exit_back_row_entry.mmd",
    TOPOLOGIES / "orbit_perp_exit_perp_entry.mmd",
    TOPOLOGIES / "samerow_bypass_joins_flat_bundle_rl.mmd",
    TOPOLOGIES / "tb_bottom_exit_bundle_jog.mmd",
]

QUARTER_TURNS = [Orientation(turns, False) for turns in (1, 2, 3)]
REFLECTIONS = [Orientation(turns, True) for turns in range(4)]

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


@lru_cache(maxsize=None)
def _laid_out(source: str) -> MetroGraph:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        graph = parse_metro_mermaid(source)
        compute_layout(graph)
    return graph


def _plan(source: str) -> PortOrderPlan:
    return build_port_order_plan(_laid_out(source))


@pytest.mark.parametrize("path", COVERAGE_FIXTURES, ids=lambda p: p.stem)
def test_every_port_has_an_order(path: Path) -> None:
    graph = _laid_out(path.read_text())
    plan = build_port_order_plan(graph)

    assert set(plan.orders) == set(graph.ports)
    for order in plan:
        assert order.decided, order
        assert order.missing is None
        assert order.lines is not None
        assert sorted(order.lines) == sorted(graph.station_lines(order.port_id))


def test_the_offsets_run_records_the_plan_before_any_offset() -> None:
    """The plan exists before phase 1 writes the first lane."""
    graph = _laid_out((ROOT / "examples" / "rnaseq_sections.mmd").read_text())
    seen: list[tuple[PortOrderPlan | None, int]] = []
    base = offsets_module._compute_base_offsets

    def spy(ctx: offsets_module._OffsetCtx) -> None:
        seen.append((ctx.port_order_plan, len(ctx.offsets)))
        base(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(offsets_module, "_compute_base_offsets", spy)
        ctx = _run_offset_phases(graph, 4.0)

    ((plan_at_phase_1, written),) = seen
    assert written == 0
    assert plan_at_phase_1 is not None
    assert ctx.port_order_plan == plan_at_phase_1
    assert set(plan_at_phase_1.orders) == set(graph.ports)


@pytest.mark.parametrize("path", COVERAGE_FIXTURES[:3], ids=lambda p: p.stem)
def test_plan_is_deterministic(path: Path) -> None:
    graph = _laid_out(path.read_text())
    assert build_port_order_plan(graph) == build_port_order_plan(graph)


def test_plan_does_not_depend_on_the_hash_seed() -> None:
    fixture = COVERAGE_FIXTURES[-1]
    script = (
        "import warnings; warnings.simplefilter('ignore')\n"
        "from nf_metro.parser.mermaid import parse_metro_mermaid\n"
        "from nf_metro.layout.engine import compute_layout\n"
        "from nf_metro.layout.port_order_plan import build_port_order_plan\n"
        f"g = parse_metro_mermaid(open({str(fixture)!r}).read())\n"
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


@pytest.mark.parametrize(
    ("path", "orientation"),
    [(p, o) for p in ROTATION_FIXTURES for o in QUARTER_TURNS],
    ids=lambda v: v.stem if isinstance(v, Path) else v.name,
)
def test_a_rotated_map_plans_the_same_orders(
    path: Path, orientation: Orientation
) -> None:
    source = path.read_text()
    reference = {port_key(k): v for k, v in _plan(source).orders.items()}
    image = {
        port_key(k): v
        for k, v in _plan(transform_source(source, orientation)).orders.items()
    }

    assert set(image) == set(reference)
    for key, order in reference.items():
        assert image[key].lines == order.lines, key
        assert image[key].port_class is order.port_class, key


@pytest.mark.parametrize(
    ("path", "orientation"),
    [(p, o) for p in ROTATION_FIXTURES for o in REFLECTIONS],
    ids=lambda v: v.stem if isinstance(v, Path) else v.name,
)
def test_a_reflected_map_reverses_each_geometric_order(
    path: Path, orientation: Orientation
) -> None:
    """Reflection flips travel-left and travel-right, so an order decided by
    where the lines come from reverses; a priority tie-break does not."""
    source = path.read_text()
    reference = {port_key(k): v for k, v in _plan(source).orders.items()}
    image = {
        port_key(k): v
        for k, v in _plan(transform_source(source, orientation)).orders.items()
    }

    for key, order in reference.items():
        if order.basis is OrderBasis.LATERAL:
            assert order.lines is not None
            assert image[key].lines == tuple(reversed(order.lines)), key


def test_a_line_with_no_reference_node_is_undecided() -> None:
    graph = _laid_out((ROOT / "examples" / "rnaseq_sections.mmd").read_text())
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
    original = list(graph.edges)
    graph.replace_edges(orphaned)
    try:
        order = build_port_order_plan(graph).orders[entry_id]
    finally:
        graph.replace_edges(original)

    assert not order.decided
    assert order.missing is not None
    assert line_id in order.missing


def test_port_classes_cover_the_census_kinds() -> None:
    """The coverage fixtures exercise every port class the census reports."""
    seen = {
        order.port_class
        for path in COVERAGE_FIXTURES
        for order in _plan(path.read_text())
    }
    assert seen == set(PortClass)


def test_the_plan_module_reads_no_later_phase_state() -> None:
    """Its inputs are structure and placement: no lane offset, route, draft,
    departure record or offsets/routing entry point appears in the module."""
    tree = ast.parse(Path(port_order_plan_module.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {
        alias.name.rsplit(".", 1)[-1]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    }
    modules = {
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module is not None
    }

    assert not names & LATER_PHASE_NAMES
    assert modules <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        "types",
        "nf_metro.layout.constants",
        "nf_metro.layout.geometry",
        "nf_metro.layout.routing.common",
        "nf_metro.layout.routing.orientation",
        "nf_metro.parser.model",
    }
