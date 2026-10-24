"""Shared diagonal windows for feeders of one target that ride one row together.

The diagonal fallthrough routes every edge on its own, so feeders of one target
whose sources sit on one row each pick their own window.  When a line on the far
side of the drop dives first it crosses its neighbour's straight, and the
neighbour crosses it back on the target row.  A group whose windows would cross
twice instead shares the window of the member whose source sits nearest the
target: every member is already on the row there, so it never dives early.

Decided from station coordinates, per-edge floors, and the lane offsets at both
ends, before any member is emitted.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nf_metro.layout.constants import COORD_TOLERANCE, COORD_TOLERANCE_FINE
from nf_metro.layout.geometry import lanes_run_along_y

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
    """A feeder's natural window and its lane offset at each end."""

    key: _EdgeKey
    source_run: float
    window: DiagonalWindow
    source_lane: float
    target_lane: float


@dataclass(frozen=True, slots=True)
class CoRowDiagonalGroup:
    """Feeders of ``target`` leaving the row at ``row`` on one shared window."""

    target: str
    row: float
    members: tuple[_EdgeKey, ...]
    window: tuple[float, float]


@dataclass(frozen=True, slots=True)
class _Lane:
    """A drawn lane in flow coordinates: level, linear across the window, level."""

    start: float
    end: float
    source_y: float
    target_y: float

    def _between(self, u: float) -> float:
        t = (u - self.start) / (self.end - self.start)
        return self.source_y + (self.target_y - self.source_y) * t

    def before(self, u: float) -> float:
        if u <= self.start:
            return self.source_y
        if u > self.end:
            return self.target_y
        return self._between(u)

    def after(self, u: float) -> float:
        if u < self.start:
            return self.source_y
        if u >= self.end:
            return self.target_y
        return self._between(u)


def _crossings(first: _Lane, second: _Lane, tolerance: float) -> int:
    """How many times two lanes swap sides by more than *tolerance*."""
    breaks = sorted({first.start, first.end, second.start, second.end})
    gaps = [first.source_y - second.source_y]
    for u in breaks:
        gaps.append(first.before(u) - second.before(u))
        gaps.append(first.after(u) - second.after(u))
    gaps.append(first.target_y - second.target_y)
    signs = [1 if gap > 0 else -1 for gap in gaps if abs(gap) > tolerance]
    return sum(1 for a, b in itertools.pairwise(signs) if a != b)


def _lane(
    feeder: CoRowFeeder,
    window: tuple[float, float],
    flow: float,
    source_row: float,
    target_row: float,
) -> _Lane:
    return _Lane(
        flow * window[0],
        flow * window[1],
        source_row + feeder.source_lane,
        target_row + feeder.target_lane,
    )


def shared_co_row_window(
    feeders: Sequence[CoRowFeeder],
    *,
    flow: float,
    source_row: float,
    target_row: float,
    target_run: float,
    lane_step: float,
) -> tuple[float, float] | None:
    """The window *feeders* share, or ``None`` when they keep their own.

    Fires only for feeders that co-travel (every source sits before every
    natural dive) and whose natural windows cross some pair more often than one
    shared window would.  A swap counts only past half a lane step: a smaller
    one is a graze inside the drawn lane width, which the post-emission
    settling passes can also undo.  The shared window is the nearest source's,
    declined when it breaks another member's source or target straight.
    """
    tolerance = lane_step / 2
    if max(flow * f.source_run for f in feeders) >= min(
        flow * f.window.start for f in feeders
    ):
        return None
    nearest = max(feeders, key=lambda f: flow * f.source_run)
    shared = (nearest.window.start, nearest.window.end)
    if not any(
        _crossings(
            _lane(a, (a.window.start, a.window.end), flow, source_row, target_row),
            _lane(b, (b.window.start, b.window.end), flow, source_row, target_row),
            tolerance,
        )
        > _crossings(
            _lane(a, shared, flow, source_row, target_row),
            _lane(b, shared, flow, source_row, target_row),
            tolerance,
        )
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
    return shared


def _co_row_candidates(
    ctx: _RoutingCtx,
) -> Iterable[tuple[Station, float, list[tuple[Edge, Station]]]]:
    """Feeders of one target from one section's sources on one row and side.

    Only lanes stacked on Y qualify: the drawn-lane model reads the deferred
    offsets as Y shifts of an X-run diagonal.
    """
    graph = ctx.graph
    by_target: defaultdict[
        tuple[str, str | None, float], list[tuple[Edge, Station]]
    ] = defaultdict(list)
    for edge in graph.edges:
        src, tgt = graph.edge_endpoints(edge)
        section = graph.sections.get(src.section_id or "")
        if section is not None and not lanes_run_along_y(section.direction):
            continue
        if abs(tgt.y - src.y) < COORD_TOLERANCE or abs(tgt.x - src.x) < COORD_TOLERANCE:
            continue
        flow = 1.0 if tgt.x > src.x else -1.0
        by_target[(edge.target, src.section_id, flow)].append((edge, src))
    for (target_id, _section, flow), feeders in by_target.items():
        target = graph.stations[target_id]
        feeders.sort(key=lambda item: item[1].y)
        row: list[tuple[Edge, Station]] = []
        for item in [*feeders, None]:
            if item is not None and (
                not row or item[1].y - row[0][1].y < COORD_TOLERANCE
            ):
                row.append(item)
                continue
            if len({edge.source for edge, _ in row}) > 1:
                yield target, flow, row
            row = [item] if item is not None else []


def plan_co_row_diagonal_groups(
    ctx: _RoutingCtx,
    *,
    drawn_by_diagonal: Callable[[Edge], bool],
    natural_window: Callable[[Edge, Station, Station, _RoutingCtx], DiagonalWindow],
) -> dict[_EdgeKey, CoRowDiagonalGroup]:
    """Every co-row group that shares one window, keyed by member edge.

    A group is declined unless every member is drawn by the diagonal
    fallthrough: a member another handler owns keeps its own geometry, and the
    rest must not be bent toward a window it never takes.
    """
    offsets = ctx.station_offsets or {}
    groups: dict[_EdgeKey, CoRowDiagonalGroup] = {}
    for target, flow, row in _co_row_candidates(ctx):
        if not all(drawn_by_diagonal(edge) for edge, _src in row):
            continue
        feeders = [
            CoRowFeeder(
                key=(edge.source, edge.target, edge.line_id),
                source_run=src.x,
                window=natural_window(edge, src, target, ctx),
                source_lane=offsets.get((edge.source, edge.line_id), 0.0),
                target_lane=offsets.get((edge.target, edge.line_id), 0.0),
            )
            for edge, src in row
        ]
        source_row = row[0][1].y
        window = shared_co_row_window(
            feeders,
            flow=flow,
            source_row=source_row,
            target_row=target.y,
            target_run=target.x,
            lane_step=ctx.offset_step,
        )
        if window is None:
            continue
        group = CoRowDiagonalGroup(
            target=target.id,
            row=source_row,
            members=tuple(f.key for f in feeders),
            window=window,
        )
        for f in feeders:
            groups[f.key] = group
    return groups
