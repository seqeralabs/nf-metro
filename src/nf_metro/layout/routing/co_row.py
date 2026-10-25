"""Shared diagonal windows for feeders of one target that ride one row together.

The diagonal handlers route every edge on its own, so feeders of one target
whose sources sit on one row (one column in a vertical-flow section) each pick
their own window.  When a line on the far side of the drop dives first it
crosses its neighbour's straight, and the neighbour crosses it back on the
target's row.  A group whose windows would cross twice instead shares the
window of the member whose source sits nearest the target: every member is
already on the row there, so it never dives early.

Decided from station coordinates, per-edge floors, and each line's lane at both
ends, before any member is emitted.  Coordinates run along the section's flow
axis (the *run*) and across it (the *lateral*), so one model serves both
diagonal handlers.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nf_metro.layout.constants import COORD_TOLERANCE, COORD_TOLERANCE_FINE
from nf_metro.layout.geometry import AxisFrame, axis_split

if TYPE_CHECKING:
    from nf_metro.layout.routing.context import _EdgeKey, _RoutingCtx
    from nf_metro.parser.model import Edge, Station


@dataclass(frozen=True, slots=True)
class DiagonalWindow:
    """One edge's diagonal on the run axis, with the straights it must keep."""

    start: float
    end: float
    source_floor: float
    target_floor: float


@dataclass(frozen=True, slots=True)
class CoRowFeeder:
    """A feeder's natural window and the lateral its lane is drawn at each end."""

    key: _EdgeKey
    source_run: float
    window: DiagonalWindow
    source_lateral: float
    target_lateral: float


@dataclass(frozen=True, slots=True)
class CoRowDiagonalGroup:
    """Feeders of ``target`` leaving the lateral ``row`` on one shared window."""

    target: str
    row: float
    members: tuple[_EdgeKey, ...]
    window: tuple[float, float]


@dataclass(frozen=True, slots=True)
class _Lane:
    """A drawn lane in flow coordinates: level, linear across the window, level."""

    start: float
    end: float
    source: float
    target: float

    def _between(self, u: float) -> float:
        t = (u - self.start) / (self.end - self.start)
        return self.source + (self.target - self.source) * t

    def before(self, u: float) -> float:
        if u <= self.start:
            return self.source
        if u > self.end:
            return self.target
        return self._between(u)

    def after(self, u: float) -> float:
        if u < self.start:
            return self.source
        if u >= self.end:
            return self.target
        return self._between(u)


def _crossings(first: _Lane, second: _Lane, tolerance: float) -> int:
    """How many times two lanes swap sides by more than *tolerance*."""
    breaks = sorted({first.start, first.end, second.start, second.end})
    gaps = [first.source - second.source]
    for u in breaks:
        gaps.append(first.before(u) - second.before(u))
        gaps.append(first.after(u) - second.after(u))
    gaps.append(first.target - second.target)
    signs = [1 if gap > 0 else -1 for gap in gaps if abs(gap) > tolerance]
    return sum(1 for a, b in itertools.pairwise(signs) if a != b)


def _lane(feeder: CoRowFeeder, window: tuple[float, float], flow: float) -> _Lane:
    return _Lane(
        flow * window[0],
        flow * window[1],
        feeder.source_lateral,
        feeder.target_lateral,
    )


def _own_window(feeder: CoRowFeeder) -> tuple[float, float]:
    return feeder.window.start, feeder.window.end


def shared_co_row_window(
    feeders: Sequence[CoRowFeeder],
    *,
    flow: float,
    target_run: float,
    lane_step: float,
) -> tuple[float, float] | None:
    """The window *feeders* share, or ``None`` when they keep their own.

    Fires only for feeders that co-travel (every source sits before every
    natural dive) and whose natural windows cross some pair more often than one
    shared window would.  A swap counts only past half a lane step: a smaller
    one is a graze inside the drawn lane width, which the post-emission
    settling passes can also undo.  The shared window is the nearest source's,
    declined when it breaks another member's source or target straight, or
    when it runs two lines from different sources closer than half a lane
    step apart across the diagonal.
    """
    tolerance = lane_step / 2
    if max(flow * f.source_run for f in feeders) >= min(
        flow * f.window.start for f in feeders
    ):
        return None
    nearest = max(feeders, key=lambda f: flow * f.source_run)
    shared = _own_window(nearest)
    if not any(
        _crossings(
            _lane(a, _own_window(a), flow), _lane(b, _own_window(b), flow), tolerance
        )
        > _crossings(_lane(a, shared, flow), _lane(b, shared, flow), tolerance)
        for a, b in itertools.combinations(feeders, 2)
        if a.key[0] != b.key[0]
    ):
        return None
    for f in feeders:
        source_straight = flow * (shared[0] - f.source_run)
        target_straight = flow * (target_run - shared[1])
        if (
            source_straight < f.window.source_floor - COORD_TOLERANCE_FINE
            or target_straight < f.window.target_floor - COORD_TOLERANCE_FINE
        ):
            return None
    if any(
        _diagonal_separation(a, b, shared) < tolerance
        for a, b in itertools.combinations(feeders, 2)
        if a.key[0] != b.key[0] and a.key[2] != b.key[2]
    ):
        return None
    return shared


def _diagonal_separation(
    first: CoRowFeeder, second: CoRowFeeder, window: tuple[float, float]
) -> float:
    """Closest perpendicular gap of two lanes drawn across one shared *window*.

    The diagonal spread pass slides a group's lanes along the run until their
    target-end lateral gap is a full perpendicular gap, and the source end keeps
    its own lateral gap's share plus that slide.  Lanes that swap sides cross
    rather than run together, so they have no gap to keep.
    """
    source_gap = first.source_lateral - second.source_lateral
    target_gap = first.target_lateral - second.target_lateral
    if source_gap * target_gap < 0:
        return math.inf
    run = abs(window[1] - window[0])
    lateral = max(
        abs(first.target_lateral - first.source_lateral),
        abs(second.target_lateral - second.source_lateral),
    )
    hypot = math.hypot(run, lateral)
    source_end = (abs(source_gap) * run + abs(target_gap) * (hypot - run)) / hypot
    return min(abs(target_gap), source_end)


def run_and_lateral(station: Station, direction: str) -> tuple[float, float]:
    """*station*'s coordinates along and across a *direction* flow."""
    return axis_split(
        AxisFrame.axes_for_direction(direction)[0], (station.x, station.y)
    )


def co_row_feeder(
    edge: Edge,
    src: Station,
    direction: str,
    window: DiagonalWindow,
    lanes: tuple[float, float],
) -> CoRowFeeder:
    """*edge* drawn across *window* in a *direction* flow.

    *lanes* are the laterals its lane is drawn at, at its source and target.
    """
    return CoRowFeeder(
        key=(edge.source, edge.target, edge.line_id),
        source_run=run_and_lateral(src, direction)[0],
        window=window,
        source_lateral=lanes[0],
        target_lateral=lanes[1],
    )


def _co_row_candidates(
    ctx: _RoutingCtx,
) -> Iterable[tuple[Station, float, float, list[tuple[Edge, float]]]]:
    """Feeders of one target from one section's sources on one row and side.

    Yields ``(target, flow, target_run, [(edge, source_lateral), ...])`` with
    run and lateral read along the source section's flow axis.
    """
    graph = ctx.graph
    by_target: defaultdict[tuple[str, str | None, float], list[tuple[Edge, float]]] = (
        defaultdict(list)
    )
    target_runs: dict[tuple[str, str | None, float], float] = {}
    for edge in graph.edges:
        src, tgt = graph.edge_endpoints(edge)
        section = graph.sections.get(src.section_id or "")
        direction = section.direction if section is not None else "LR"
        src_run, src_lateral = run_and_lateral(src, direction)
        tgt_run, tgt_lateral = run_and_lateral(tgt, direction)
        if (
            abs(tgt_lateral - src_lateral) < COORD_TOLERANCE
            or abs(tgt_run - src_run) < COORD_TOLERANCE
        ):
            continue
        flow = 1.0 if tgt_run > src_run else -1.0
        key = (edge.target, src.section_id, flow)
        by_target[key].append((edge, src_lateral))
        target_runs[key] = tgt_run
    for key, feeders in by_target.items():
        target_id, _section, flow = key
        target = graph.stations[target_id]
        feeders.sort(key=lambda item: item[1])
        row: list[tuple[Edge, float]] = []
        for item in [*feeders, None]:
            if item is not None and (not row or item[1] - row[0][1] < COORD_TOLERANCE):
                row.append(item)
                continue
            if len({edge.source for edge, _ in row}) > 1:
                yield target, flow, target_runs[key], row
            row = [item] if item is not None else []


def plan_co_row_diagonal_groups(
    ctx: _RoutingCtx,
    *,
    feeder_for: Callable[[Edge], CoRowFeeder | None],
) -> dict[_EdgeKey, CoRowDiagonalGroup]:
    """Every co-row group that shares one window, keyed by member edge.

    *feeder_for* returns ``None`` for an edge no diagonal handler draws.  A
    group is declined unless every member is drawn by one: a member another
    handler owns keeps its own geometry, and the rest must not be bent toward a
    window it never takes.
    """
    groups: dict[_EdgeKey, CoRowDiagonalGroup] = {}
    for target, flow, target_run, row in _co_row_candidates(ctx):
        feeders: list[CoRowFeeder] = []
        for edge, _lateral in row:
            feeder = feeder_for(edge)
            if feeder is None:
                break
            feeders.append(feeder)
        else:
            window = shared_co_row_window(
                feeders,
                flow=flow,
                target_run=target_run,
                lane_step=ctx.offset_step,
            )
            if window is None:
                continue
            group = CoRowDiagonalGroup(
                target=target.id,
                row=row[0][1],
                members=tuple(f.key for f in feeders),
                window=window,
            )
            for f in feeders:
                groups[f.key] = group
    return groups
