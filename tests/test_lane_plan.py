"""The lane plan record: sealed once per layout, total, deterministic, unread.

:mod:`nf_metro.layout.lane_plan` decides every port's line order, every
same-band entry approach, each port's order travel and lane sense, each
perpendicular entry's consumer turns, the far-side approaches, every
in-section lane and the lanes either side of every straight seam at the first
offsets run of a layout at or after stage 5.1.  These tests hold the record's
contract: one record per layout, replayed for the rest of it; total on every
curated map; independent of the hash seed; pinned per curated map; read by
nothing in the package; a rotated or reflected placement plans the rotated or
reflected record; its approach sides are the ones dispatch draws; no seam
changes a bundle's order, outside the listed seams where a straight-seam
entry reverses one, or steps a line that crosses it straight; the seam joint's
lanes are the track plan's, outside a listed entry; and a line born on an
exit's level run leaves the exit on its consumers' side.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
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
from nf_metro.layout.constants import SAME_Y_TOLERANCE, graph_offset_step
from nf_metro.layout.geometry import perpendicular_port_sides
from nf_metro.layout.lane_plan import LanePlan
from nf_metro.layout.port_order_plan import (
    Approach,
    ApproachKind,
    OrderBasis,
    SideRule,
    _lateral,
    port_travel,
)
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.common import EXIT_SIDE_DIRECTION, Direction
from nf_metro.layout.routing.context import _section_lane_frame
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
# they exercise every record rule (see the ``test_rotation_fixtures_cover_*``
# tests).
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
    TOPOLOGIES / "wide_fan_out.mmd",
    ROOT / "examples" / "live" / "pipeline.mmd",
    TOPOLOGIES / "bypass_fan_in_outer_slot.mmd",
    TOPOLOGIES / "merge_right_entry.mmd",
    FIXTURES / "regressions" / "departure_order_carry_replay.mmd",
    TOPOLOGIES / "terminated_exit_lane_compaction.mmd",
    FIXTURES / "regressions" / "departure_order_level_exit_feeder.mmd",
    FIXTURES / "regressions" / "entry_slot_overlay_from_exit_forward.mmd",
    FIXTURES / "curve_invariant_repros" / "trunk_lane_taken_by_turning_line.mmd",
    FIXTURES / "curve_invariant_repros" / "backward_right_entry_lane_handover.mmd",
    FIXTURES / "regressions" / "approach_order_multi_line_feeder.mmd",
    FIXTURES / "regressions" / "approach_order_carry_sandwich.mmd",
]

# Same-row bypasses whose drawn approach differs from the record.
APPROACH_SIDE_EXCEPTIONS: frozenset[tuple[str, str, str]] = frozenset()


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


def _no_exits(_port_id: str) -> None:
    return None


def _no_seams(_port_id: str, _order: tuple[str, ...] | None) -> None:
    return None


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
            for node, _via in port_order_plan_module.PortOrderBuilder(
                graph, _no_exits, _no_seams
            ).upstream_sources(entry_id, lid)
        }
    for port_id, turns in plan.perpendicular_turns.items():
        assert graph.ports[port_id].is_entry
        assert {lid for turn in turns for lid in turn.lines} <= set(
            graph.station_lines(port_id)
        )
    in_sections = {
        (node, lid)
        for node, station in graph.stations.items()
        if station.section_id in graph.sections
        for lid in graph.station_lines(node)
    }
    fed_junctions = {
        (junction, edge.line_id)
        for junction in graph.junction_ids
        for edge in graph.edges_to(junction)
        if (edge.source, edge.line_id) in in_sections
    }
    perpendicular = {
        (node, lid)
        for node, lid in in_sections
        if node in graph.ports and _perpendicular(graph, node)
    }
    assert set(plan.tracks.lanes) == (in_sections - perpendicular) | fed_junctions
    assert {
        (port_id, lid)
        for port_id, offsets in plan.tracks.perpendicular.items()
        for lid in offsets
    } == perpendicular
    for port_id, offsets in plan.tracks.perpendicular.items():
        assert len(set(offsets.values())) == len(offsets)
    seams = plan.seams
    for entry_id, lanes in seams.entries.items():
        assert not _perpendicular(graph, entry_id) and graph.ports[entry_id].is_entry
        assert sorted(lanes) == graph.station_lines(entry_id)
        assert len(set(lanes.values())) == len(lanes)
    for (entry_id, lid), exit_id in seams.pins.items():
        assert lid in seams.entries[entry_id]
        assert lid in graph.station_lines(exit_id)
        assert not graph.ports[exit_id].is_entry
    for port_id, offsets in seams.perpendicular.items():
        assert _perpendicular(graph, port_id)
        assert offsets == plan.tracks.perpendicular[port_id]


def _perpendicular(graph: MetroGraph, port_id: str) -> bool:
    port = graph.ports[port_id]
    return port.side in perpendicular_port_sides(graph.section_for_port(port).direction)


def _level_edges(
    graph: MetroGraph, section_id: str
) -> list[tuple[Edge, float, float, float]]:
    """In-section edges whose ends share the lane coordinate, each with its
    flow span and lane coordinate."""
    frame = _section_lane_frame(graph, graph.sections[section_id])
    out = []
    for edge in graph.edges:
        a, b = graph.stations[edge.source], graph.stations[edge.target]
        if a.section_id != section_id or b.section_id != section_id:
            continue
        lateral = frame.secondary.get(a)
        if abs(frame.secondary.get(b) - lateral) > SAME_Y_TOLERANCE:
            continue
        lo, hi = sorted((frame.primary.get(a), frame.primary.get(b)))
        out.append((edge, lo, hi, lateral))
    return out


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_two_coexisting_lines_share_a_lane(path: Path) -> None:
    """Two lines running side by side along one level stretch of a section
    hold different lanes there."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    lanes = graph._lane_plan.tracks.lanes
    clashes = []
    for section_id in graph.sections:
        edges = _level_edges(graph, section_id)
        for i, (e, lo, hi, lateral) in enumerate(edges):
            for f, f_lo, f_hi, f_lateral in edges[i + 1 :]:
                if (
                    e.line_id == f.line_id
                    or abs(lateral - f_lateral) > SAME_Y_TOLERANCE
                    or min(hi, f_hi) - max(lo, f_lo) <= SAME_Y_TOLERANCE
                ):
                    continue
                for node_e, node_f in ((e.source, f.source), (e.target, f.target)):
                    if (node_e, e.line_id) not in lanes or (
                        node_f,
                        f.line_id,
                    ) not in lanes:
                        continue
                    if lanes[(node_e, e.line_id)] == lanes[(node_f, f.line_id)]:
                        clashes.append((node_e, e.line_id, node_f, f.line_id))
    assert clashes == []


# ---- seams and born lines


def _reached(graph: MetroGraph, node: str, line_id: str) -> set[str]:
    """The non-junction nodes *line_id* reaches from *node* through junctions."""
    found: set[str] = set()
    seen: set[str] = set()
    frontier = [e.target for e in graph.edges_from(node) if e.line_id == line_id]
    while frontier:
        target = frontier.pop()
        if target in seen:
            continue
        seen.add(target)
        if target in graph.junction_ids:
            frontier.extend(
                e.target for e in graph.edges_from(target) if e.line_id == line_id
            )
        else:
            found.add(target)
    return found


def _seam_bundles(graph: MetroGraph) -> dict[tuple[str, str], list[str]]:
    """Per (exit, entry) seam, the lines crossing it."""
    bundles: dict[tuple[str, str], list[str]] = {}
    for exit_id in sorted(graph.ports):
        if graph.ports[exit_id].is_entry:
            continue
        for lid in graph.station_lines(exit_id):
            for node in sorted(_reached(graph, exit_id, lid)):
                if node in graph.ports and graph.ports[node].is_entry:
                    bundles.setdefault((exit_id, node), []).append(lid)
    return bundles


def _reordered_bundles(
    graph: MetroGraph,
) -> dict[tuple[str, str], tuple[list[str], list[str]]]:
    """Per (exit, entry) seam whose bundle the entry orders otherwise than
    the exit: the bundle's lines in the exit's order, then the entry's."""
    assert graph._lane_plan is not None
    orders = graph._lane_plan.port_orders.orders
    out = {}
    for (exit_id, entry_id), carried in _seam_bundles(graph).items():
        left, right = orders[exit_id].lines, orders[entry_id].lines
        if len(carried) < 2 or left is None or right is None:
            continue
        leaving = [lid for lid in left if lid in carried]
        arriving = [lid for lid in right if lid in carried]
        if leaving != arriving:
            out[(exit_id, entry_id)] = (leaving, arriving)
    return out


def _recorded_conflicts(graph: MetroGraph) -> dict[str, set[str]]:
    """Per entry, the lines its feeding bundles order oppositely, as the
    record states them."""
    plan = graph._lane_plan
    assert plan is not None
    out: dict[str, set[str]] = {}
    for forced in plan.tracks.forced:
        if forced.port is not None:
            out.setdefault(forced.port, set()).update(forced.lines)
    return out


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_seam_changes_a_bundle_order(path: Path) -> None:
    """Every line an exit carries into an entry crosses that seam in the
    order it leaves the exit: travel-frame orders keep each line's side
    through every turn, so the entry's order of a bundle is its exit's,
    except between lines another bundle into the entry orders oppositely,
    which the record states as a forced crossing at the entry."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    conflicts = _recorded_conflicts(graph)
    changed = []
    for (exit_id, entry_id), (leaving, arriving) in _reordered_bundles(graph).items():
        swapped = {lid for lid, other in zip(leaving, arriving) if lid != other}
        if not swapped <= conflicts.get(entry_id, set()):
            changed.append((exit_id, entry_id))
    assert changed == []


@pytest.mark.parametrize(
    ("rel", "entry_id"),
    [
        ("tests/fixtures/hash_seed_determinism/seed_15.mmd", "s10__entry_right_19"),
        (
            "tests/fixtures/regressions/approach_order_carry_sandwich.mmd",
            "sec3__entry_left_4",
        ),
    ],
    ids=str,
)
def test_opposite_bundles_into_an_entry_are_a_forced_crossing(
    rel: str, entry_id: str
) -> None:
    """Two bundles into one entry carrying the same lines in opposite orders
    cross whatever the entry's order: the record names the entry and the
    lines."""
    graph = _laid_out(ROOT / rel).graph
    assert graph is not None and graph._lane_plan is not None
    orders = graph._lane_plan.port_orders.orders
    feeding = [
        [lid for lid in orders[exit_id].lines or () if lid in carried]
        for (exit_id, entry), carried in _seam_bundles(graph).items()
        if entry == entry_id
    ]
    stated = _recorded_conflicts(graph)[entry_id]
    assert any(
        [lid for lid in a if lid in stated] == [lid for lid in b if lid in stated][::-1]
        for i, a in enumerate(feeding)
        for b in feeding[i + 1 :]
    )


def test_a_free_line_between_adjacent_pins_keeps_its_bundle() -> None:
    """A free line ordered between two pins with no lane free between them
    passes the pin it shares no bundle with."""
    fill = lane_plan_module._fill_round_pins
    assert fill(["a", "f", "b"], {"a": 0, "b": 1}, [["f", "b"]]) == {
        "a": 0,
        "b": 1,
        "f": -1,
    }
    assert fill(["a", "f", "b"], {"a": 0, "b": 1}, [["a", "f"]]) == {
        "a": 0,
        "b": 1,
        "f": 2,
    }
    assert fill(["a", "c", "f", "b"], {"a": 0, "c": 2, "b": 3}, [["a", "f", "b"]]) == {
        "a": 0,
        "c": 2,
        "b": 3,
        "f": 1,
    }
    assert fill(["a", "f", "b"], {"a": 0, "b": 1}, [["a", "f"], ["f", "b"]]) == {
        "a": 0,
        "b": 1,
        "f": 2,
    }


def test_an_undecided_seam_entry_fills_in_declaration_order() -> None:
    """A seam-pinned entry whose order is undecided fills its lines round
    the pins in their declaration order, not their ids'."""
    graph = _laid_out(
        FIXTURES / "regressions" / "approach_order_multi_line_feeder.mmd"
    ).graph
    assert graph is not None and graph._lane_plan is not None
    entry_id = "z2__entry_left_2"
    tracks = lane_plan_module._Tracks(
        graph, lane_plan_module._Frames(graph), graph._lane_plan.lane_senses
    )
    for section_id in sorted(graph.sections):
        tracks.section(section_id)
    filled: list[list[str]] = []
    real = lane_plan_module._fill_round_pins

    def spy(order, pins, bundles=()):  # type: ignore[no-untyped-def]
        filled.append(list(order))
        return real(order, pins, bundles)

    declared = ["m2", "m1", "m0"]
    tracks.builder.priority = {lid: i for i, lid in enumerate(declared)}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lane_plan_module, "_fill_round_pins", spy)
        tracks.seam_order(entry_id, None)
    ascending = (
        declared if graph._lane_plan.lane_senses[entry_id] > 0 else declared[::-1]
    )
    assert filled == [ascending]


def _planned_lateral(
    graph: MetroGraph, plan: LanePlan, port_id: str, line_id: str
) -> float:
    """*line_id*'s planned coordinate across its port's section lanes."""
    frame = _section_lane_frame(graph, graph.section_for_port(graph.ports[port_id]))
    lane = plan.tracks.lanes[(port_id, line_id)]
    return frame.secondary.get(graph.stations[port_id]) + (
        frame.secondary_sign * lane * graph_offset_step(graph)
    )


def _boxes_between(graph: MetroGraph, exit_id: str, entry_id: str) -> bool:
    """Whether a section box stands across the straight run between two ports."""
    a, b = graph.stations[exit_id], graph.stations[entry_id]
    horizontal = abs(a.y - b.y) <= SAME_Y_TOLERANCE
    lo, hi = sorted((a.x, b.x) if horizontal else (a.y, b.y))
    across = a.y if horizontal else a.x
    for section in graph.sections.values():
        if section.bbox_w <= 0 or section.bbox_h <= 0:
            continue
        s0, s1 = (
            (section.bbox_x, section.bbox_x + section.bbox_w)
            if horizontal
            else (section.bbox_y, section.bbox_y + section.bbox_h)
        )
        c0, c1 = (
            (section.bbox_y, section.bbox_y + section.bbox_h)
            if horizontal
            else (section.bbox_x, section.bbox_x + section.bbox_w)
        )
        if c0 < across < c1 and min(s1, hi) - max(s0, lo) > SAME_Y_TOLERANCE:
            return True
    return False


def _straight_seams(graph: MetroGraph) -> list[tuple[str, str, str]]:
    """``(exit, entry, line)`` for every line joining two facing flow-side
    ports on one lane coordinate, through junctions only, past no section."""
    out = []
    for (exit_id, entry_id), lines in sorted(_seam_bundles(graph).items()):
        if _perpendicular(graph, exit_id) or _perpendicular(graph, entry_id):
            continue
        exit_port, entry_port = graph.ports[exit_id], graph.ports[entry_id]
        if EXIT_SIDE_DIRECTION[exit_port.side] is not port_travel(entry_port):
            continue
        a, b = graph.stations[exit_id], graph.stations[entry_id]
        frame = _section_lane_frame(graph, graph.section_for_port(entry_port))
        if abs(frame.secondary.get(a) - frame.secondary.get(b)) > SAME_Y_TOLERANCE:
            continue
        if _boxes_between(graph, exit_id, entry_id):
            continue
        out.extend((exit_id, entry_id, lid) for lid in lines)
    return out


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_straight_seam_keeps_its_lines_level(path: Path) -> None:
    """A line crossing a straight seam leaves its exit and reaches the entry
    on one coordinate, unless the exit's lane falls between the entry's lanes
    or another exit also feeds the line straight in: no out-and-back step."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    plan = graph._lane_plan
    step = graph_offset_step(graph)
    seams = _straight_seams(graph)
    sources: dict[tuple[str, str], set[str]] = {}
    for exit_id, entry_id, lid in seams:
        sources.setdefault((entry_id, lid), set()).add(exit_id)
    stepped = []
    for exit_id, entry_id, lid in seams:
        here = _planned_lateral(graph, plan, exit_id, lid)
        there = _planned_lateral(graph, plan, entry_id, lid)
        on_grid = (here - there) / step
        if abs(on_grid - round(on_grid)) > 0.05 or len(sources[(entry_id, lid)]) > 1:
            continue
        if abs(here - there) > SAME_Y_TOLERANCE:
            stepped.append((exit_id, entry_id, lid))
    assert stepped == []


def _seam_lane_disagreements(graph: MetroGraph) -> list[str]:
    """Entries whose seam lanes are not their track lanes, or whose seam
    order is not read from their track lanes."""
    plan = graph._lane_plan
    assert plan is not None
    out = []
    for entry_id, lanes in sorted(plan.seams.entries.items()):
        tracked = {lid: plan.tracks.lanes[(entry_id, lid)] for lid in lanes}
        by_lane = sorted(lanes, key=lambda lid: (tracked[lid], lid))
        if plan.lane_senses[entry_id] < 0:
            by_lane.reverse()
        order = plan.port_orders.orders[entry_id]
        if tracked != lanes or (
            order.basis is OrderBasis.SEAM and order.lines != tuple(by_lane)
        ):
            out.append(entry_id)
    return out


# Seam-pinned entries whose seam lanes are not their track lanes.
SEAM_LANE_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_seam_lanes_are_the_track_lanes(path: Path) -> None:
    """Every lane the seam joint records at an entry is the track plan's lane
    there, and an entry its seam lanes order reads that order across its
    track lanes."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    disagree = [
        entry_id
        for entry_id in _seam_lane_disagreements(graph)
        if (_rel(path), entry_id) not in SEAM_LANE_EXCEPTIONS
    ]
    assert disagree == []


@pytest.mark.parametrize(("rel", "entry_id"), sorted(SEAM_LANE_EXCEPTIONS), ids=str)
def test_seam_lane_exceptions_are_live(rel: str, entry_id: str) -> None:
    graph = _laid_out(ROOT / rel).graph
    assert graph is not None and graph._lane_plan is not None
    plan = graph._lane_plan
    seam = plan.seams.entries[entry_id]
    by_seam = sorted(seam, key=lambda lid: (seam[lid], lid))
    if plan.lane_senses[entry_id] < 0:
        by_seam.reverse()
    assert entry_id in _seam_lane_disagreements(graph)
    assert plan.port_orders.orders[entry_id].basis is OrderBasis.SEAM
    assert plan.port_orders.orders[entry_id].lines == tuple(by_seam)
    assert any(plan.tracks.lanes[(entry_id, lid)] != lane for lid, lane in seam.items())


def _cell_lateral(graph: MetroGraph, node: str, ref: str, travel: Direction) -> float:
    def centre(node_id: str) -> tuple[float, float]:
        section = graph.sections[graph.stations[node_id].section_id or ""]
        return (
            section.grid_col + section.grid_col_span / 2,
            section.grid_row + section.grid_row_span / 2,
        )

    return _lateral(centre(node), centre(ref), travel)


def _consumer_sides(
    graph: MetroGraph, plan: LanePlan, exit_id: str, line_id: str
) -> set[float] | None:
    """Where *line_id* heads for past *exit_id*, per consumer, across the
    exit's travel: for an entry on the exit's band (one the record holds an
    approach from the exit to), that approach's lateral read along the
    exit's travel; for any other consumer, its grid cell's lateral, reversed
    for a far-side approach.  ``None`` when an on-band entry is perpendicular
    or travels across the exit's travel, or an off-band consumer's cell is
    level with the exit's."""
    travel = EXIT_SIDE_DIRECTION[graph.ports[exit_id].side]
    tx, ty = direction_vector(travel)
    sides = set()
    for node in _reached(graph, exit_id, line_id):
        section = graph.sections.get(graph.stations[node].section_id or "")
        if section is None or section.grid_col < 0:
            continue
        approach = plan.port_orders.approaches.get((node, exit_id))
        if approach is None:
            lateral = _cell_lateral(graph, node, exit_id, travel)
            if abs(lateral) <= SAME_Y_TOLERANCE:
                return None
            far = (node, exit_id, line_id) in plan.far_sides
            sides.add(-lateral if far else lateral)
            continue
        if _perpendicular(graph, node):
            return None
        if approach.kind is ApproachKind.LEVEL:
            sides.add(0.0)
            continue
        ex, ey = direction_vector(port_travel(graph.ports[node]))
        parallel = ex * tx + ey * ty
        if abs(parallel) < 0.5:
            return None
        sides.add(approach.lateral * parallel)
    return sides


def _level_run(graph: MetroGraph, section_id: str, node: str) -> set[str]:
    """The nodes of *section_id* joined to *node* by level edges."""
    joined: dict[str, set[str]] = {}
    for edge, *_span in _level_edges(graph, section_id):
        joined.setdefault(edge.source, set()).add(edge.target)
        joined.setdefault(edge.target, set()).add(edge.source)
    run, frontier = {node}, [node]
    while frontier:
        for other in joined.get(frontier.pop(), ()):
            if other not in run:
                run.add(other)
                frontier.append(other)
    return run


def _born_on(graph: MetroGraph, run: set[str], line_id: str) -> bool:
    """Whether *line_id* starts in its section at a node of *run*."""
    return any(
        line_id in graph.station_lines(node)
        and not any(
            e.line_id == line_id
            and graph.stations[e.source].section_id == graph.stations[node].section_id
            for e in graph.edges_to(node)
        )
        for node in run
    )


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_born_line_leaves_on_its_consumers_side(path: Path) -> None:
    """A line born on an exit's level run, beside lines that entered the
    section, leaves the exit on the side, across its travel, where every one
    of its consumers lies from every one of theirs: off the exit's band by
    grid cell, on it by the side its approach record states.  A run holding
    a seam-pinned entry places its born lines by the seam-pinned run's rule
    instead."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    plan = graph._lane_plan
    orders = plan.port_orders.orders
    wrong = []
    for section_id, section in sorted(graph.sections.items()):
        if section.grid_col < 0 or section.grid_row < 0:
            continue
        entered = {
            lid
            for port_id in section.entry_ports
            if port_id in graph.ports
            for lid in graph.station_lines(port_id)
        }
        for exit_id in section.exit_ports:
            if exit_id not in graph.ports or _perpendicular(graph, exit_id):
                continue
            lines = orders[exit_id].lines
            run = _level_run(graph, section_id, exit_id)
            if lines is None or any(n in plan.seams.entries for n in run):
                continue
            sides = {lid: _consumer_sides(graph, plan, exit_id, lid) for lid in lines}
            for born in lines:
                mine = sides[born]
                if born in entered or not mine or not _born_on(graph, run, born):
                    continue
                for kept in lines:
                    theirs = sides[kept]
                    if kept not in entered or not theirs:
                        continue
                    if min(mine) > max(theirs) + SAME_Y_TOLERANCE:
                        right = True
                    elif max(mine) < min(theirs) - SAME_Y_TOLERANCE:
                        right = False
                    else:
                        continue
                    if (lines.index(born) > lines.index(kept)) != right:
                        wrong.append((exit_id, born, kept))
    assert wrong == []


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
    travel = port_travel(graph.ports[entry_id])
    if side == 0:
        return "flat"
    return "below" if side * lateral_order_sign(travel) > 0 else "above"


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_approach_sides_match_the_drawn_same_row_bypasses(path: Path) -> None:
    """Every same-row bypass dispatch draws round its row arrives on the
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


@pytest.mark.parametrize(
    "path",
    [*ROTATION_FIXTURES[:4], *ROTATION_FIXTURES[-5:-1]],
    ids=lambda p: p.stem,
)
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


# ---- the pinned record

RECORD_DIGESTS = ROOT / "tests" / "data" / "lane_plan_digests.json"


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _record_payload(plan: LanePlan) -> list[object]:
    tracks = plan.tracks
    return [
        sorted([node, lid, lane] for (node, lid), lane in tracks.lanes.items()),
        [
            [port_id, None if order.lines is None else list(order.lines)]
            for port_id, order in sorted(plan.port_orders.orders.items())
        ],
        [[f.section_id, f.run, f.port, list(f.lines)] for f in tracks.forced],
        list(tracks.cycle_sections),
        sorted(
            [entry_id, lid, lane]
            for entry_id, lanes in plan.seams.entries.items()
            for lid, lane in lanes.items()
        ),
        sorted(
            [entry_id, lid, exit_id]
            for (entry_id, lid), exit_id in plan.seams.pins.items()
        ),
        sorted(
            [port_id, lid, offset]
            for port_id, offsets in plan.seams.perpendicular.items()
            for lid, offset in offsets.items()
        ),
        sorted(
            [port_id, lid, offset]
            for port_id, offsets in tracks.perpendicular.items()
            for lid, offset in offsets.items()
        ),
        sorted(
            [t.node_id, t.leaving, t.travel.value, list(t.lines), list(t.through)]
            for t in tracks.turn_offs.values()
        ),
    ]


def _map_digest(run: _Run) -> str | None:
    """Digest of every record *run*'s layouts sealed, in order: lanes, port
    orders, forced crossings, cycle sections and seam lanes.  ``None`` when
    the map's layout aborts or seals no record."""
    if run.graph is None or run.graph._lane_plan is None:
        return None
    payload = json.dumps([_record_payload(p) for p in run.plans], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _pinned_digests() -> dict[str, str | None]:
    return dict(json.loads(RECORD_DIGESTS.read_text()))


def _write_record_digests() -> None:
    digests = {_rel(p): _map_digest(_laid_out(p)) for p in CURATED}
    RECORD_DIGESTS.write_text(json.dumps(digests, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("path", CURATED, ids=_rel)
def test_the_record_matches_its_pinned_digest(path: Path) -> None:
    """Each curated map seals the records pinned in ``RECORD_DIGESTS``, so a
    change to any track or port-order rule shows as the maps it moves.

    Regenerate the pin, only when that change is intended, with
    ``python tests/test_lane_plan.py``; the digests do not depend on the hash
    seed."""
    assert _map_digest(_laid_out(path)) == _pinned_digests().get(_rel(path), "absent")


def test_the_pinned_digests_name_every_curated_map() -> None:
    assert set(_pinned_digests()) == {_rel(p) for p in CURATED}


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


def _lane_direction(graph: MetroGraph, section_id: str) -> tuple[float, float]:
    frame = _section_lane_frame(graph, graph.sections[section_id])
    if frame.secondary.name == "x":
        return (frame.secondary_sign, 0.0)
    return (0.0, frame.secondary_sign)


def _keeps_lane_frames(graph: MetroGraph, image: MetroGraph, turn: _Turn) -> bool:
    """Whether *turn* carries every section's lane direction onto the image
    section's."""
    return all(
        turn.vec(*_lane_direction(graph, sid)) == _lane_direction(image, sid)
        for sid in graph.sections
    )


@lru_cache(maxsize=None)
def _rotation_case(path: Path) -> tuple[MetroGraph, LanePlan]:
    graph = _laid_out(path).graph
    assert graph is not None and not any(len(m) >= 2 for m in graph.cell_packs.values())
    return graph, _plan(graph)


# Rotations that mirror some section's lane frame (see
# ``test_a_turned_placement_plans_the_turned_record``): the exit orders read
# from that section's lanes reverse, and with them the entry orders they feed.
ORDER_TURN_EXCEPTIONS = frozenset(
    {
        ("approach_order_multi_line_feeder", "r270"),
        ("bottom_entry_two_feeders_own_lanes", "r180"),
        ("departure_order_carry_replay", "r90"),
        ("lr_perp_top_exit_perp_entry_diverging", "r180"),
        ("right_entry_wrap_bundle", "r180"),
        ("stacked_collector_fanin", "r180"),
        ("tb_right_entry_stack", "r180"),
        ("wide_fan_out", "r180"),
    }
)


# Quarter turns under which a born line takes its side from an approach whose
# side is a screen convention the turn does not carry (``_CONVENTIONS``): an
# exit's approach to a consumer on its band.  The record's lanes there are not
# the turned lanes, and neither are the orders read from them.
LANE_TURN_EXCEPTIONS = frozenset(
    {
        ("bottom_entry_two_feeders_own_lanes", "r90"),
        ("bottom_entry_two_feeders_own_lanes", "r270"),
    }
)


@contextmanager
def _approaches_read(found: dict[tuple[str, str], Approach]) -> Iterator[None]:
    """Record every approach the record's builder decides."""
    real = port_order_plan_module.PortOrderBuilder.approach

    def approach(self, entry_id, source_id):  # type: ignore[no-untyped-def]
        value = real(self, entry_id, source_id)
        found[(entry_id, source_id)] = value
        return value

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(port_order_plan_module.PortOrderBuilder, "approach", approach)
        yield


def _conventions_kept(reference: LanePlan, image: LanePlan, turn: _Turn) -> bool:
    """Whether *turn* carries every side-convention approach's side as it
    carries every other side."""
    flip = -1 if turn.reflect else 1
    return all(
        image.port_orders.approaches[key].side == flip * ref.side
        for key, ref in reference.port_orders.approaches.items()
        if ref.rule in _CONVENTIONS
    )


@pytest.mark.parametrize(
    ("path", "turn"),
    [(p, t) for p in ROTATION_FIXTURES for t in TURNS],
    ids=lambda v: v.stem if isinstance(v, Path) else v.name,
)
def test_a_turned_placement_plans_the_turned_record(path: Path, turn: _Turn) -> None:
    """Every rule reads its inputs in a travel frame, so turning the placement
    turns the record.

    Every turn keeps each approach's kind, rule and reach and each port's
    class, and turns each port's order travel and each perpendicular turn.  It
    flips each approach side under a reflection, except the two side
    conventions dispatch draws: a turn-round U-turn loops on the traveller's
    right in every frame, and the screen-below side is the traveller's right
    except across leftward travel.

    Lanes are decided in each section's lane frame, which is the drawing's
    convention rather than one frame turned: an RL section's lanes run as an
    LR section's reflected, TB and BT sections' as an LR section's turned a
    quarter, except that a TB section entered from the right, receiving a
    reversed bundle or joined to no other section runs its lanes toward +x.  A
    turn that carries every section's lane direction onto the image
    section's plans the same lanes, the same straight-seam pins and entry
    lanes, and each perpendicular seam's offsets, which are read across the
    seam's travel, flipped under a reflection; except the turns in
    ``LANE_TURN_EXCEPTIONS``, where a born line's side comes from a side
    convention the turn does not carry.

    Where the turn carries every convention side as well, it flips each far
    side under a reflection and turns each order: a rotation keeps it; a
    reflection reverses a lateral-decided order, and, where the turn carries
    every lane direction, an order read from the lanes or the seam pins.  The
    turns in ``ORDER_TURN_EXCEPTIONS`` mirror some section's lane frame, and
    those in ``LANE_TURN_EXCEPTIONS`` turn some lanes differently, so neither
    is held to the orders."""
    graph, reference = _rotation_case(path)
    image_graph = _turned(graph, turn)
    image = _plan(image_graph)
    flip = -1 if turn.reflect else 1
    keeps_frames = _keeps_lane_frames(graph, image_graph, turn)

    for key, ref in reference.port_orders.approaches.items():
        got = image.port_orders.approaches[key]
        assert (got.kind, got.rule, got.reach) == (ref.kind, ref.rule, ref.reach), key
        if ref.rule in _CONVENTIONS:
            travel = port_travel(image_graph.ports[key[0]])
            assert got.side == _CONVENTIONS[ref.rule](travel), key
        else:
            assert got.side == flip * ref.side, key

    for port_id, travel in reference.order_travels.items():
        assert image.order_travels[port_id] is turn.travel(travel), port_id
    for port_id, turns in reference.perpendicular_turns.items():
        assert [(t.consumer_id, t.lines, turn.travel(t.turn)) for t in turns] == [
            (t.consumer_id, t.lines, t.turn) for t in image.perpendicular_turns[port_id]
        ]
    for port_id, ref_order in reference.port_orders.orders.items():
        image_order = image.port_orders.orders[port_id]
        assert image_order.port_class is ref_order.port_class, port_id
    if keeps_frames and (path.stem, turn.name) not in LANE_TURN_EXCEPTIONS:
        assert image.tracks.lanes == reference.tracks.lanes
        assert image.seams.entries == reference.seams.entries
        assert image.seams.pins == reference.seams.pins
        assert image.seams.perpendicular == {
            port_id: {lid: flip * v for lid, v in offsets.items()}
            for port_id, offsets in reference.seams.perpendicular.items()
        }
        for port_id, offsets in reference.tracks.perpendicular.items():
            turned = image.tracks.perpendicular[port_id]
            assert len({turned[lid] - flip * v for lid, v in offsets.items()}) == 1
        for key, ref_turn in reference.tracks.turn_offs.items():
            got_turn = image.tracks.turn_offs[key]
            assert got_turn.travel is turn.travel(ref_turn.travel), key
            assert got_turn.through == ref_turn.through, key
            ref_lines = ref_turn.lines[::-1] if turn.reflect else ref_turn.lines
            assert got_turn.lines == ref_lines, key

    if not _conventions_kept(reference, image, turn):
        return
    assert image.far_sides == {k: flip * v for k, v in reference.far_sides.items()}
    if (path.stem, turn.name) in ORDER_TURN_EXCEPTIONS | LANE_TURN_EXCEPTIONS:
        return
    for port_id, ref_order in reference.port_orders.orders.items():
        image_order = image.port_orders.orders[port_id]
        if not turn.reflect:
            assert image_order.lines == ref_order.lines, port_id
        elif ref_order.basis is OrderBasis.LATERAL or (
            keeps_frames and ref_order.basis in (OrderBasis.LANES, OrderBasis.SEAM)
        ):
            assert ref_order.lines is not None
            assert image_order.lines == tuple(reversed(ref_order.lines)), port_id


@pytest.mark.parametrize(
    ("stem", "turn_name"), sorted(ORDER_TURN_EXCEPTIONS), ids=lambda v: v
)
def test_order_turn_exceptions_are_live(stem: str, turn_name: str) -> None:
    path = next(p for p in ROTATION_FIXTURES if p.stem == stem)
    turn = next(t for t in TURNS if t.name == turn_name)
    graph, reference = _rotation_case(path)
    image_graph = _turned(graph, turn)
    image = _plan(image_graph)

    assert not turn.reflect
    assert _conventions_kept(reference, image, turn)
    assert not _keeps_lane_frames(graph, image_graph, turn)
    assert any(
        image.port_orders.orders[port_id].lines != ref_order.lines
        for port_id, ref_order in reference.port_orders.orders.items()
    )


@pytest.mark.parametrize(
    ("stem", "turn_name"), sorted(LANE_TURN_EXCEPTIONS), ids=lambda v: v
)
def test_lane_turn_exceptions_are_live(stem: str, turn_name: str) -> None:
    path = next(p for p in ROTATION_FIXTURES if p.stem == stem)
    turn = next(t for t in TURNS if t.name == turn_name)
    graph, _ = _rotation_case(path)
    image_graph = _turned(graph, turn)
    read: dict[tuple[str, str], Approach] = {}
    turned: dict[tuple[str, str], Approach] = {}
    with _approaches_read(read):
        reference = _plan(graph)
    with _approaches_read(turned):
        image = _plan(image_graph)

    flip = -1 if turn.reflect else 1
    assert _keeps_lane_frames(graph, image_graph, turn)
    assert image.tracks.lanes != reference.tracks.lanes
    assert any(
        not image_graph.ports[key[0]].is_entry
        and approach.rule in _CONVENTIONS
        and turned[key].side != flip * approach.side
        for key, approach in read.items()
    )


def test_rotation_fixtures_cover_every_rule() -> None:
    """The fixtures above exercise every rule the record states."""
    fired: set[str] = set()
    builder_cls = port_order_plan_module.PortOrderBuilder
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
                default = plan.order_travels[port_id] is not port_travel(
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


def _track_rule_spies(fired: set[str]) -> list[tuple[object, str, object]]:
    """Spies on the track plan's rules, each naming the rule it saw change a
    decision into *fired*."""
    tracks_cls = lane_plan_module._Tracks
    real_tracks = {
        name: getattr(tracks_cls, name)
        for name in (
            "_runs",
            "_part_pins",
            "_carried_orders",
            "_fill_bundled",
            "_spans",
            "_coexist",
            "_partner_lane",
            "_arrival",
            "_keep_bundles",
        )
    }
    real_nest = lane_plan_module._nest_by_source
    real_travel = lane_plan_module._travel_order

    def runs(self, section_id):  # type: ignore[no-untyped-def]
        value = real_tracks["_runs"](self, section_id)
        if value.later_parts:
            fired.add("run split")
        return value

    def part_pins(self, run, fixed):  # type: ignore[no-untyped-def]
        value = real_tracks["_part_pins"](self, run, fixed)
        if fixed:
            fired.add("fan carriers")
        if value:
            fired.add("part pins")
        return value

    def carried_orders(self, section_id):  # type: ignore[no-untyped-def]
        value = real_tracks["_carried_orders"](self, section_id)
        if value:
            fired.add("carried order")
        return value

    def fill_bundled(self, *args):  # type: ignore[no-untyped-def]
        fired.add("bundled fill")
        return real_tracks["_fill_bundled"](self, *args)

    def spans(self, section_id, nodes, lines):  # type: ignore[no-untyped-def]
        value = real_tracks["_spans"](self, section_id, nodes, lines)
        if any(len(pieces) > 1 for pieces in value.values()):
            fired.add("pieces")
        return value

    def coexist(self, lid, span, other, other_span, section_id, nodes):  # type: ignore[no-untyped-def]
        value = real_tracks["_coexist"](
            self, lid, span, other, other_span, section_id, nodes
        )
        touch = min(span[1], other_span[1]) - max(span[0], other_span[0])
        if not value and abs(touch) <= SAME_Y_TOLERANCE:
            fired.add("hand-over")
        return value

    def partner_lane(self, *args):  # type: ignore[no-untyped-def]
        value = real_tracks["_partner_lane"](self, *args)
        if value is not None:
            fired.add("partner lane")
        return value

    def arrival(self, run, lid):  # type: ignore[no-untyped-def]
        found = real_tracks["_arrival"](self, run, lid)
        if found.key != round(found.key, 1):
            turned = abs(found.key) < lane_plan_module._SIDED
            fired.add("turned in" if turned else "parting")
        return found

    def keep_bundles(self, run, order, chains):  # type: ignore[no-untyped-def]
        value = real_tracks["_keep_bundles"](self, run, order, chains)
        if value != list(order):
            fired.add("bundle kept into part")
        return value

    def nest_by_source(order, *args):  # type: ignore[no-untyped-def]
        value = real_nest(order, *args)
        if value != list(order):
            fired.add("nested by source")
        return value

    def travel_order(line_id, carriers, ins):  # type: ignore[no-untyped-def]
        value = real_travel(line_id, carriers, ins)
        if value != carriers:
            fired.add("travel order")
        return value

    return [
        *(
            (tracks_cls, name, spy)
            for name, spy in (
                ("_runs", runs),
                ("_part_pins", part_pins),
                ("_carried_orders", carried_orders),
                ("_fill_bundled", fill_bundled),
                ("_spans", spans),
                ("_coexist", coexist),
                ("_partner_lane", partner_lane),
                ("_arrival", arrival),
                ("_keep_bundles", keep_bundles),
            )
        ),
        (lane_plan_module, "_nest_by_source", nest_by_source),
        (lane_plan_module, "_travel_order", travel_order),
    ]


def test_rotation_fixtures_cover_every_track_rule() -> None:
    """The fixtures above exercise every rule the track plan states."""
    fired: set[str] = set()
    with pytest.MonkeyPatch.context() as mp:
        for owner, name, spy in _track_rule_spies(fired):
            mp.setattr(owner, name, spy)
        for path in ROTATION_FIXTURES:
            _plan(_rotation_case(path)[0])

    assert fired >= {
        "run split",
        "part pins",
        "fan carriers",
        "carried order",
        "bundled fill",
        "pieces",
        "hand-over",
        "partner lane",
        "turned in",
        "parting",
        "nested by source",
        "travel order",
        "bundle kept into part",
    }


def _lanes_run_along_x(graph: MetroGraph, section_id: str) -> bool:
    frame = _section_lane_frame(graph, graph.sections[section_id])
    return frame.secondary.name == "x"


_TRACKS = lane_plan_module._Tracks

# (owner, name, rule, when): *rule* fired when ``when(value, *args)`` holds of
# a call to ``owner.name``.
BORN_AND_SEAM_SPIES = [
    (_TRACKS, "_born_order", "born placed", lambda v, _s, _sec, o, _b: v != list(o)),
    (_TRACKS, "_joined_side", "joined side", lambda v, *_a: v != 0),
    (_TRACKS, "_peel_chains", "peel chain", lambda v, *_a: bool(v)),
    (
        _TRACKS,
        "_peel_chains",
        "turned peel",
        lambda v, s, sid, _b: bool(v) and _lanes_run_along_x(s.graph, sid),
    ),
    (_TRACKS, "_forward_chains", "forward chain", lambda v, *_a: bool(v)),
    (_TRACKS, "_pins", "seam pin", lambda v, *_a: bool(v)),
    (
        _TRACKS,
        "_pins",
        "negative pin",
        lambda v, *_a: any(k < 0 for k, _ in v.values()),
    ),
    (_TRACKS, "_pin_fits_bundles", "pin kept beside a bundle", lambda v, *_a: v),
    (
        lane_plan_module,
        "_fill_round_pins",
        "filled below a pin",
        lambda v, _order, pins, *_b: min(v.values()) < min(pins.values()),
    ),
    (
        lane_plan_module,
        "_fill_round_pins",
        "free line kept beside its bundle",
        lambda v, order, pins, *_b: (
            [lid for lid in sorted(v, key=v.__getitem__)] != list(order)
        ),
    ),
    (lane_plan_module, "_opposed", "opposed bundles", lambda v, *_a: bool(v)),
    (_TRACKS, "_pinned_floor", "floor below zero", lambda v, *_a: v < 0),
    (_TRACKS, "_born_side", "born side", lambda v, *_a: v[0] != 0),
    (_TRACKS, "_fork_side", "fork side", lambda v, *_a: v is not None and v[0] != 0),
    (_TRACKS, "_heading", "level heading", lambda v, *_a: v == 0.0),
    (_TRACKS, "_arrival_mates", "arrival side", lambda v, *_a: v[0] != 0),
    (_TRACKS, "_born_below", "born order kept below", lambda v, *_a: v > 0),
    (_TRACKS, "_born_lane", "born fill", lambda _v, *_a: True),
    (_TRACKS, "_breezes", "breeze", lambda v, *_a: v),
    (_TRACKS, "perpendicular_seams", "perpendicular seam", lambda v, *_a: bool(v)),
    (_TRACKS, "_carry", "perpendicular carry", lambda v, *_a: len(v) > 1),
    (_TRACKS, "_turn_merge", "turn merge", lambda v, *_a: len(v) > 1),
    (_TRACKS, "_through", "through line", lambda v, *_a: v),
    (
        port_order_plan_module.PortOrderBuilder,
        "_depart",
        "bypass parts outermost",
        lambda v, _b, _e, ordered, *_a: v != ordered,
    ),
]


def _firing(real: object, rule: str, when: object, fired: set[str]) -> object:
    def wrapped(*args):  # type: ignore[no-untyped-def]
        value = real(*args)  # type: ignore[operator]
        if when(value, *args):  # type: ignore[operator]
            fired.add(rule)
        return value

    return wrapped


def test_rotation_fixtures_cover_every_born_line_and_seam_rule() -> None:
    """The fixtures above, and their quarter turns, exercise every born-line
    and seam rule."""
    fired: set[str] = set()
    with pytest.MonkeyPatch.context() as mp:
        for owner, name, rule, when in BORN_AND_SEAM_SPIES:
            mp.setattr(owner, name, _firing(getattr(owner, name), rule, when, fired))
        for path in ROTATION_FIXTURES:
            graph = _rotation_case(path)[0]
            _plan(graph)
            _plan(_turned(graph, TURNS[0]))

    assert fired >= {rule for _owner, _name, rule, _when in BORN_AND_SEAM_SPIES}


# ---- perpendicular ports, entry departures, turn-offs


def _column(graph: MetroGraph, port_id: str) -> tuple[str, dict[str, int]] | None:
    """The one in-section node every line of perpendicular *port_id* turns
    into (an entry) or out of (an exit), with each line's lane there."""
    plan = graph._lane_plan
    assert plan is not None
    port = graph.ports[port_id]
    ends = {
        (e.target if port.is_entry else e.source, e.line_id)
        for e in (
            graph.edges_from(port_id) if port.is_entry else graph.edges_to(port_id)
        )
        if graph.stations[e.target if port.is_entry else e.source].section_id
        == port.section_id
    }
    nodes = {node for node, _lid in ends}
    lines = graph.station_lines(port_id)
    if len(nodes) != 1 or {lid for _node, lid in ends} != set(lines):
        return None
    node = nodes.pop()
    if any((node, lid) not in plan.tracks.lanes for lid in lines):
        return None
    return node, {lid: plan.tracks.lanes[(node, lid)] for lid in lines}


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_perpendicular_port_carries_its_column(path: Path) -> None:
    """A perpendicular port whose lines all turn into, or out of, one column
    holds that column's lanes across its travel, gaps kept, so the turn is
    concentric; an entry's innermost line lies on the port's coordinate, or
    the column's lane zero when the column holds none below its lines."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    plan = graph._lane_plan
    bad = []
    for port_id, offsets in sorted(plan.tracks.perpendicular.items()):
        column = _column(graph, port_id)
        consumers = [t for t in plan.perpendicular_turns.get(port_id, ())]
        if column is None or len(consumers) > 1:
            continue
        _node, lanes = column
        if len(set(lanes.values())) != len(lanes):
            continue
        sense = plan.lane_senses[port_id]
        shifts = {offsets[lid] - sense * lane for lid, lane in lanes.items()}
        if len(shifts) != 1:
            bad.append((port_id, "not concentric"))
            continue
        if graph.ports[port_id].is_entry and port_id not in plan.seams.perpendicular:
            anchor = 0 if min(lanes.values()) > 0 else None
            if anchor is None and 0 not in offsets.values():
                bad.append((port_id, "innermost off the port"))
            if anchor == 0 and shifts != {0}:
                bad.append((port_id, "lane zero off the port"))
    assert bad == []


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_multi_consumer_perpendicular_entry_keeps_each_turn_side(path: Path) -> None:
    """A perpendicular entry feeding several in-section consumers lays its
    lines side by side, each consumer's lines in the order they hold after
    turning into it, read across that turn."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    plan = graph._lane_plan
    bad = []
    for port_id, turns in sorted(plan.perpendicular_turns.items()):
        if len(turns) < 2 or port_id not in plan.tracks.perpendicular:
            continue
        offsets = plan.tracks.perpendicular[port_id]
        values = sorted(offsets.values())
        if values != list(range(values[0], values[0] + len(values))):
            bad.append((port_id, "gap"))
        section = graph.section_for_port(graph.ports[port_id])
        sign = _section_lane_frame(graph, section).secondary_sign
        for turn in turns:
            if turn.turn is None or len(turn.lines) < 2:
                continue
            lanes = {
                lid: plan.tracks.lanes[(turn.consumer_id, lid)] for lid in turn.lines
            }
            after = sorted(
                turn.lines,
                key=lambda lid: lateral_order_sign(turn.turn) * sign * lanes[lid],
            )
            if sorted(turn.lines, key=offsets.__getitem__) != after:
                bad.append((port_id, turn.consumer_id))
    assert bad == []


def _parting_side(graph: MetroGraph, entry_id: str, line_id: str) -> int | None:
    """``0`` when *line_id* runs from flow-side *entry_id* only to consumers
    level with it, ``+1`` / ``-1`` when only to consumers on the traveller's
    right / left, ``None`` otherwise."""
    port = graph.ports[entry_id]
    travel = port_travel(port)
    here = graph.stations[entry_id]
    laterals = [
        _lateral(
            (graph.stations[e.target].x, graph.stations[e.target].y),
            (here.x, here.y),
            travel,
        )
        for e in graph.edges_from(entry_id)
        if e.line_id == line_id
        and graph.stations[e.target].section_id == port.section_id
    ]
    sides = {0 if abs(v) <= 0.1 else (1 if v > 0 else -1) for v in laterals}
    return sides.pop() if len(sides) == 1 else None


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_bypass_parting_at_its_entry_lies_outermost(path: Path) -> None:
    """Where an entry's lines part at the port, some running on level and
    others leaving at once, a line arriving by a same-band bypass on the side
    it leaves to, in no feeding bundle with another of the entry's lines,
    lies outside every line that runs on level."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    plan = graph._lane_plan
    bad = []
    for entry_id, order in sorted(plan.port_orders.orders.items()):
        if not order.is_entry or order.lines is None or len(order.lines) < 2:
            continue
        if _perpendicular(graph, entry_id):
            continue
        sides = {lid: _parting_side(graph, entry_id, lid) for lid in order.lines}
        if (
            None in sides.values()
            or 0 not in sides.values()
            or set(sides.values()) == {0}
        ):
            continue
        builder = port_order_plan_module.PortOrderBuilder(graph, _no_exits, _no_seams)
        feeding: dict[str, set[str]] = {}
        for lid in order.lines:
            for node, _via in builder.upstream_sources(entry_id, lid):
                feeding.setdefault(node, set()).add(lid)
        bundled = {
            lid
            for node, lines in feeding.items()
            if node in graph.ports and len(lines) >= 2
            for lid in lines
        }
        level = [order.lines.index(lid) for lid, side in sides.items() if side == 0]
        for lid, side in sides.items():
            if side == 0 or lid in bundled:
                continue
            approaches = [
                a
                for (entry, source), a in plan.port_orders.approaches.items()
                if entry == entry_id and lid in feeding.get(source, ())
            ]
            if not any(
                a.kind is ApproachKind.BYPASS and a.side == side for a in approaches
            ):
                continue
            at = order.lines.index(lid)
            if (side > 0 and at < max(level)) or (side < 0 and at > min(level)):
                bad.append((entry_id, lid))
    assert bad == []


def test_the_over_top_u_turn_is_the_wrap_handler_scope() -> None:
    """Only a single-line U-turn from a LEFT exit into a far LEFT entry of an
    LR section loops over the top: the far-side wrap handler's own scope."""
    seen = 0
    for path in CURATED:
        graph = _laid_out(path).graph
        if graph is None or graph._lane_plan is None:
            continue
        for (
            entry_id,
            source_id,
        ), approach in graph._lane_plan.port_orders.approaches.items():
            if approach.rule is not SideRule.OVER_TOP:
                continue
            entry, source = graph.ports[entry_id], graph.ports[source_id]
            assert entry.side is PortSide.LEFT and source.side is PortSide.LEFT
            assert graph.section_for_port(entry).direction == "LR"
            assert approach.kind is ApproachKind.U_TURN and approach.side == -1
            assert len({e.line_id for e in graph.edges_to(entry_id)}) == 1
            seen += 1
    assert seen > 0


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_fan_carrier_keeps_its_slot(path: Path) -> None:
    """Every section station a fan plan carries holds the carrier's slot."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    lanes = graph._lane_plan.tracks.lanes
    moved = [
        (carrier.station_id, slot.line_id)
        for plan in graph.fan_plans
        if plan.owns_geometry
        for carrier in plan.offset_carriers
        if graph.stations[carrier.station_id].section_id in graph.sections
        and carrier.station_id not in graph.junction_ids
        for slot in carrier.assignments
        if (carrier.station_id, slot.line_id) in lanes
        and lanes[(carrier.station_id, slot.line_id)] != slot.slot
    ]
    assert moved == []


@pytest.mark.parametrize("path", CURATED, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_through_line_turns_off_beyond_the_lines_ending(path: Path) -> None:
    """Lines leaving a node's run into a leg (or joining it from one) meet
    the leg in the order of their side of travel, the side the leg heads for
    first, except that a line whose run carries on level through the node
    turns off beyond (joins before) every line whose run ends there."""
    graph = _laid_out(path).graph
    if graph is None or graph._lane_plan is None:
        return
    bad = []
    for key, turn_off in sorted(graph._lane_plan.tracks.turn_offs.items()):
        assert sorted(turn_off.lines) == sorted(graph.station_lines(turn_off.node_id))
        assert set(turn_off.through) <= set(turn_off.lines)
        for heads_right in (False, True):
            along = turn_off.along(heads_right)
            by_side = turn_off.lines[::-1] if heads_right else turn_off.lines
            ending = [lid for lid in by_side if lid not in turn_off.through]
            through = [lid for lid in by_side if lid in turn_off.through]
            expect = ending + through if turn_off.leaving else through + ending
            if list(along) != expect:
                bad.append(key)
    assert bad == []


def test_a_line_carrying_on_past_its_branch_turns_off_beyond() -> None:
    """At the junction after riboseq_metro's alignment exit, rnaseq both runs
    on level to Novel transcripts and branches down past it: its branch turns
    off beyond riboseq, whose run ends there, whichever side the legs head."""
    graph = _laid_out(ROOT / "examples" / "riboseq_metro.mmd").graph
    assert graph is not None and graph._lane_plan is not None
    turn_off = graph._lane_plan.tracks.turn_offs[("__junction_13", True)]
    assert turn_off.through == ("rnaseq",)
    assert turn_off.along(True)[-1] == turn_off.along(False)[-1] == "rnaseq"


def test_the_record_is_immutable_and_survives_deepcopy() -> None:
    graph = _laid_out(TOPOLOGIES / "return_trunk_over_member_run.mmd").graph
    assert graph is not None and graph._lane_plan is not None
    plan = graph._lane_plan
    some_lane = next(iter(plan.tracks.lanes))
    some_entry = next(iter(plan.seams.entries))
    with pytest.raises(TypeError):
        plan.tracks.lanes[some_lane] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.seams.entries[some_entry]["x"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.port_orders.orders["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.lane_senses["x"] = 1  # type: ignore[index]
    copied = deepcopy(graph)
    assert copied._lane_plan is plan
    assert deepcopy(plan) is plan


def test_the_record_and_dispatch_share_the_same_row_bypass_predicate() -> None:
    """The record's packed-cell same-row bypass eligibility and dispatch's are
    one structural predicate."""
    name = "packed_cell_same_row_bypass"
    callers = set()
    for module, tree in _src_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if called == name:
                    callers.add(module)
    assert {
        "layout/port_order_plan.py",
        "layout/routing/inter_section_handlers.py",
    } <= callers


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


def test_a_turned_peel_ranks_a_packed_cell_member_in_its_own_cell() -> None:
    """Read in a turned flow frame, a packed section not yet boxed keeps its
    rank inside its own grid cell's pack."""
    from nf_metro.layout import fan_ordering
    from nf_metro.layout.geometry import packed_section_visual_rank
    from nf_metro.layout.routing.common import resolve_section_colrow

    graph = _laid_out(TOPOLOGIES / "packed_cell_cellmate_bypass.mmd").graph
    assert graph is not None
    image = deepcopy(graph)
    members = next(m for m in image.cell_packs.values() if len(m) >= 2)
    ranked = []
    for section_id in members:
        section = image.sections[section_id]
        section.bbox_w = 0
        station = next(
            st for st in image.stations.values() if st.section_id == section_id
        )
        col, row = resolve_section_colrow(image, station)
        assert col is not None and row is not None
        rank = packed_section_visual_rank(image, section, col, row)
        ranked.append(rank)
        assert fan_ordering._section_order_coordinate(image, station, 1) == (
            row * (len(image.sections) + 1) + rank
        )
    assert max(ranked) > 0


def test_a_packed_cell_member_sits_at_its_own_slot() -> None:
    graph = _laid_out(TOPOLOGIES / "packed_cell_cellmate_bypass.mmd").graph
    assert graph is not None
    builder = port_order_plan_module.PortOrderBuilder(graph, _no_exits, _no_seams)
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


if __name__ == "__main__":
    _write_record_digests()
