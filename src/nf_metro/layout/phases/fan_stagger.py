"""Flow-axis room a fanned diagonal needs for the stagger its spread group applies."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from collections.abc import Set as AbstractSet
from operator import itemgetter

from nf_metro.layout.constants import COORD_TOLERANCE_FINE, graph_offset_step
from nf_metro.layout.geometry import AxisFrame, diagonal_stagger, lanes_run_along_y
from nf_metro.layout.pass_metrics import (
    active_metrics,
    min_station_flat_length,
    min_straight_edge,
)
from nf_metro.layout.route_topology import fork_join_stations
from nf_metro.parser.model import MetroGraph, Section, Station, is_bypass_v

Pair = tuple[str, str]


def spread_groups(
    line_ids: Mapping[Pair, set[str]],
    drop: Callable[[Pair], float],
    forks: AbstractSet[str],
    joins: AbstractSet[str],
) -> list[list[Pair]]:
    """Diagonal bundles grouped as ``_spread_diagonal_bundles`` spreads them.

    Must match that pass's grouping.  A station of *forks* takes every
    diagonal lane leaving it once it has two, then one of *joins* takes every
    remaining diagonal lane entering it once it has two, each hub one
    cross-axis heading at a time.  Every other bundle spreads alone.
    """
    diagonals = [pair for pair in line_ids if abs(drop(pair)) > COORD_TOLERANCE_FINE]

    def claimed(
        pairs: list[Pair], hub: Callable[[Pair], str], members: AbstractSet[str]
    ) -> dict[Pair, str]:
        count: Counter[str] = Counter()
        for pair in pairs:
            if hub(pair) in members:
                count[hub(pair)] += len(line_ids[pair])
        return {pair: hub(pair) for pair in pairs if count[hub(pair)] >= 2}

    fork_end, join_end = itemgetter(0), itemgetter(1)
    by_fork = claimed(diagonals, fork_end, forks)
    by_join = claimed([p for p in diagonals if p not in by_fork], join_end, joins)
    groups: defaultdict[tuple[object, ...], list[Pair]] = defaultdict(list)
    for pair in diagonals:
        heading = drop(pair) > 0
        if pair in by_fork:
            groups[("fork", by_fork[pair], heading)].append(pair)
        elif pair in by_join:
            groups[("join", by_join[pair], heading)].append(pair)
        else:
            groups[pair].append(pair)
    return list(groups.values())


def snapped_drop(drop: float, y_grid: float | None) -> float:
    """The largest |drop| a Y-lattice snap of both ends can turn *drop* into.

    Stage 6.4 rounds each station Y to the nearest lattice line, so a drop that
    is not a whole number of steps can grow to the next one.
    """
    size = abs(drop)
    if not y_grid:
        return size
    steps = size / y_grid
    if abs(steps - round(steps)) * y_grid <= COORD_TOLERANCE_FINE:
        return size
    return math.ceil(steps) * y_grid


def fan_stagger_needs(
    graph: MetroGraph,
    stations: Mapping[str, Station],
    direction: str,
    *,
    y_grid: float | None = None,
) -> dict[Pair, float]:
    """Flow-axis length every fanned diagonal among *stations* needs.

    The diagonal spread pass slides each group by one shift, so every edge of a
    group needs both endpoint straights, the diagonal run and the group's whole
    stagger: ``diagonal_stagger`` over its distinct lines at its steepest drop.
    Groups follow :func:`spread_groups`, by hub when lanes run along Y and per
    bundle otherwise, a vertical flow baking its offsets into each route.  When
    lanes run along Y, *y_grid* is the lattice Stage 6.4 snaps station Ys to,
    and drops are taken as :func:`snapped_drop` makes them.  Lanes come from
    *graph*'s edges because a section subgraph also carries hidden layout-only
    edges that are never routed.
    """
    cross = AxisFrame.axes_for_direction(direction)[1]
    line_ids: defaultdict[Pair, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.source in stations and edge.target in stations:
            line_ids[(edge.source, edge.target)].add(edge.line_id)

    def line_count(group: list[Pair]) -> int:
        return len(set().union(*(line_ids[pair] for pair in group)))

    def drop(pair: Pair) -> float:
        source, target = (getattr(stations[sid], cross) for sid in pair)
        return target - source

    by_hub = lanes_run_along_y(direction)
    forks: AbstractSet[str] = frozenset()
    joins: AbstractSet[str] = frozenset()
    if by_hub:
        _targets, _sources, forks, joins = fork_join_stations(graph)
    groups = [
        group
        for group in spread_groups(line_ids, drop, forks, joins)
        if line_count(group) > 1
    ]
    if not groups:
        return {}

    lattice = y_grid if by_hub else None
    pitch = graph_offset_step(graph)
    run = active_metrics().diagonal_run
    straight = min_straight_edge()
    bypass_flat = min_station_flat_length()

    def stub(station_id: str) -> float:
        return bypass_flat if is_bypass_v(station_id) else straight

    need: dict[Pair, float] = {}
    for group in groups:
        steepest = max(snapped_drop(drop(pair), lattice) for pair in group)
        stagger = diagonal_stagger(line_count(group), pitch, run, steepest)
        for pair in group:
            need[pair] = stub(pair[0]) + stub(pair[1]) + run + stagger
    return need


def section_fan_stagger_needs(graph: MetroGraph, section: Section) -> dict[Pair, float]:
    """:func:`fan_stagger_needs` for *section*'s stations at their current drops."""
    stations = {sid: graph.stations[sid] for sid in section.station_ids}
    return fan_stagger_needs(graph, stations, section.direction or "LR")


def graph_fan_stagger_needs(graph: MetroGraph) -> dict[Pair, float]:
    """:func:`fan_stagger_needs` for every section of *graph* at its settled drops,
    or for the whole graph when it has no sections."""
    if not graph.sections:
        return fan_stagger_needs(graph, graph.stations, "LR")
    need: dict[Pair, float] = {}
    for section in graph.sections.values():
        need.update(section_fan_stagger_needs(graph, section))
    return need


def seat_within_fan_room(
    x: float,
    sign: float,
    upstream: Iterable[tuple[float, float]],
    downstream: Iterable[tuple[float, float]],
) -> float | None:
    """The flow position nearest *x* that leaves every fanned diagonal its room.

    *upstream* and *downstream* hold ``(flow position, need)`` for the station's
    neighbours on either side; *sign* is the flow sign.  ``None`` when no
    position satisfies them all.
    """
    lo = max((sign * at + room for at, room in upstream), default=-math.inf)
    hi = min((sign * at - room for at, room in downstream), default=math.inf)
    return None if lo > hi else sign * min(max(sign * x, lo), hi)
