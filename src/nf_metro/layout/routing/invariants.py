"""Runtime invariants on the output of :func:`route_edges`.

:func:`check_bundle_order_preserved` asserts that for any pair of
routes sharing ``(edge.source, edge.target)``, the lines' relative
side (left vs right of travel) is CONSTANT across the parallel
waypoint walk.  A flip is a visible line crossing.

Why pairwise-index walk?  Bundled routes share a waypoint count and
the same sequence of cardinal tangents, so segment k of A and
segment k of B are "the same segment, parallel-offset".  Corner-xy
clustering fails: per-line offsets put each line's corners at
slightly different xy, so tight tolerance misses real bugs while
loose tolerance flags every concentric corner.

Returns a list of :class:`BundleOrderViolation`; the caller decides
whether to log, raise, or ignore.  Tests in
``tests/test_bundle_order_invariant.py`` exercise it against every
gallery example and topology fixture.
"""

from __future__ import annotations

import math
import os
import warnings
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from nf_metro.layout.constants import (
    BUNDLE_TO_BUNDLE_CLEARANCE,
    COORD_TOLERANCE,
    COORD_TOLERANCE_FINE,
    CURVE_RADIUS,
    EDGE_TO_BUNDLE_CLEARANCE,
    FLOW_ALIGNED_PORT_ADVICE,
    MIN_CORRIDOR_Y_OVERLAP,
    OFFSET_STEP,
    SAME_Y_TOLERANCE,
)
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    gap_lo_for_x,
    horizontal_direction,
    initial_fanout_descent_span,
    iter_horizontal_trunks,
    iter_port_peeloff_bundles,
    iter_vertical_segments,
    peeloff_target_slots,
    resolve_section,
    tail_on_slot,
    trunk_segments_cross,
    vertical_direction,
)
from nf_metro.parser.model import Edge, MetroGraph, PortSide, Station

# Segments shorter than this are sub-pixel artefacts of per-line
# offsets and carry no meaningful direction of travel.
_MIN_SEGMENT_LENGTH = 1.0


class _HasMessage(Protocol):
    def message(self) -> str: ...


class Side(Enum):
    """Side of a line relative to its bundle mate's trajectory."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    COINCIDENT = "COINCIDENT"


@dataclass(frozen=True)
class BundleOrderViolation:
    """One bundle-order violation.  ``corner_xy`` = waypoint where the
    flip was first observed on line A; ``in_tangent`` / ``out_tangent``
    = travel directions before / on the offending segment;
    ``segment_index`` = the offending segment's index in line A's
    points list (``points[k]`` -> ``points[k+1]``).
    """

    edge_source: str
    edge_target: str
    line_a: str
    line_b: str
    corner_xy: tuple[float, float]
    in_tangent: Direction
    out_tangent: Direction
    before: Side
    after: Side
    segment_index: int = -1

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        cx, cy = self.corner_xy
        return (
            f"bundle {self.edge_source!r}->{self.edge_target!r} "
            f"corner ({cx:.1f},{cy:.1f}) "
            f"in={self.in_tangent.value} out={self.out_tangent.value} "
            f"segment={self.segment_index}: "
            f"expected line {self.line_a!r} on {self.before.value} of "
            f"line {self.line_b!r} (matching incoming run); "
            f"observed {self.line_a!r} on {self.after.value} of "
            f"line {self.line_b!r} on outgoing run"
        )


def _segment_unit_perp(
    p1: tuple[float, float], p2: tuple[float, float]
) -> tuple[float, float] | None:
    """Unit perpendicular ``(-dy, dx)/|seg|``; ``None`` for sub-pixel segments."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < _MIN_SEGMENT_LENGTH:
        return None
    return (-dy / length, dx / length)


def _side_sign(
    a_p1: tuple[float, float],
    b_p1: tuple[float, float],
    perp: tuple[float, float],
) -> int:
    """Sign of ``(A - B) . perp``: +1 LEFT, -1 RIGHT, 0 COINCIDENT."""
    dxp = a_p1[0] - b_p1[0]
    dyp = a_p1[1] - b_p1[1]
    proj = dxp * perp[0] + dyp * perp[1]
    if abs(proj) <= COORD_TOLERANCE_FINE:
        return 0
    return 1 if proj > 0 else -1


def _segment_cardinal(
    p1: tuple[float, float], p2: tuple[float, float]
) -> Direction | None:
    """Cardinal direction with GENEROUS off-axis tolerance; ``None`` if degenerate."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    if abs(dx) < _MIN_SEGMENT_LENGTH and abs(dy) < _MIN_SEGMENT_LENGTH:
        return None
    if abs(dx) >= abs(dy):
        return horizontal_direction(dx)
    return vertical_direction(dy)


def check_bundle_order_preserved(
    routes: list[RoutedPath],
) -> list[BundleOrderViolation]:
    """Return one :class:`BundleOrderViolation` per bundled pair whose
    relative side flips along the parallel waypoint walk.

    Routes are grouped by ``(edge.source, edge.target)``.  For each
    pair ``(A, B)`` in a bundle with matching waypoint counts, side
    sign of A relative to B is sampled at each segment's midpoint;
    the invariant is that the sign is CONSTANT across all
    non-coincident segments.  Skipped: single-line bundles, pairs
    with mismatched waypoint counts, sub-pixel / coincident segments.
    """
    violations: list[BundleOrderViolation] = []

    bundles: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
    for r in routes:
        bundles[(r.edge.source, r.edge.target)].append(r)

    for (src_id, tgt_id), bundle in bundles.items():
        if len(bundle) < 2:
            continue
        for ai in range(len(bundle)):
            for bi in range(ai + 1, len(bundle)):
                v = _check_pair(src_id, tgt_id, bundle[ai], bundle[bi])
                if v is not None:
                    violations.append(v)

    return violations


def _check_pair(
    src_id: str,
    tgt_id: str,
    a_route: RoutedPath,
    b_route: RoutedPath,
) -> BundleOrderViolation | None:
    """Walk two bundled routes in parallel; return the first sign flip.

    The bundle's travel direction per segment is the routes' midpoint
    tangent (parallel by construction; averaged against per-line
    nudges).  The side sign is sampled at each segment's MIDPOINT to
    average out per-line corner displacement at L-shape endpoints; at
    a true crossing the midpoint sign disagrees with its neighbour's.
    """
    if len(a_route.points) != len(b_route.points) or len(a_route.points) < 2:
        return None

    last_sign = 0
    last_dir: Direction | None = None
    for k in range(len(a_route.points) - 1):
        a_p1, a_p2 = a_route.points[k], a_route.points[k + 1]
        b_p1, b_p2 = b_route.points[k], b_route.points[k + 1]
        mid_p1 = ((a_p1[0] + b_p1[0]) * 0.5, (a_p1[1] + b_p1[1]) * 0.5)
        mid_p2 = ((a_p2[0] + b_p2[0]) * 0.5, (a_p2[1] + b_p2[1]) * 0.5)
        perp = _segment_unit_perp(mid_p1, mid_p2)
        if perp is None:
            continue
        a_mid = ((a_p1[0] + a_p2[0]) * 0.5, (a_p1[1] + a_p2[1]) * 0.5)
        b_mid = ((b_p1[0] + b_p2[0]) * 0.5, (b_p1[1] + b_p2[1]) * 0.5)
        sign = _side_sign(a_mid, b_mid, perp)
        if sign == 0:
            continue
        cur_dir = _segment_cardinal(mid_p1, mid_p2)
        if last_sign != 0 and sign != last_sign:
            # ``last_dir`` and ``cur_dir`` are non-None whenever we reach
            # here: both were set on iterations that already passed the
            # ``perp is None`` / ``sign == 0`` gates, which require the
            # same >= 1px segment length ``_segment_cardinal`` does.
            assert last_dir is not None and cur_dir is not None
            return BundleOrderViolation(
                edge_source=src_id,
                edge_target=tgt_id,
                line_a=a_route.line_id,
                line_b=b_route.line_id,
                corner_xy=a_p1,
                in_tangent=last_dir,
                out_tangent=cur_dir,
                before=Side.LEFT if last_sign > 0 else Side.RIGHT,
                after=Side.LEFT if sign > 0 else Side.RIGHT,
                segment_index=k,
            )
        last_sign = sign
        last_dir = cur_dir

    return None


# ---------------------------------------------------------------------------
# Fan-out junction tail join
# ---------------------------------------------------------------------------

# A gap ALONG the upstream travel direction reads as a visible "bite"
# at the corner apex (the line stops short of its own bend); anything
# larger than this is the seam / notch the fix closes.  A PERPENDICULAR
# gap up to a stroke width is hidden under the line and tolerated.
_TAIL_JOIN_TANGENT_TOLERANCE = 1.0


@dataclass(frozen=True)
class FanoutTailGap:
    """One upstream/downstream handoff mismatch at a fan-out junction.

    ``junction_id`` is the fan-out junction; ``line_id`` the metro line
    whose ``port -> junction`` route ends at ``upstream_end`` while its
    paired ``junction -> target`` route begins at ``downstream_start``.
    ``tangent_gap`` is the component of the offset ALONG the upstream
    travel direction -- the visible along-line "bite" at the apex.
    """

    junction_id: str
    line_id: str
    upstream_source: str
    downstream_target: str
    upstream_end: tuple[float, float]
    downstream_start: tuple[float, float]
    tangent_gap: float

    def message(self) -> str:
        ux, uy = self.upstream_end
        dx, dy = self.downstream_start
        return (
            f"fan-out junction {self.junction_id!r} line {self.line_id!r}: "
            f"upstream {self.upstream_source!r}->{self.junction_id!r} ends at "
            f"({ux:.1f},{uy:.1f}) but downstream {self.junction_id!r}->"
            f"{self.downstream_target!r} starts at ({dx:.1f},{dy:.1f}); "
            f"along-travel gap {self.tangent_gap:.1f}px > "
            f"{_TAIL_JOIN_TANGENT_TOLERANCE:.1f}px (visible apex notch)"
        )


def fanout_junctions(graph) -> dict[str, str]:  # noqa: ANN001 - MetroGraph (avoid cycle)
    """Map each *fan-out* junction id to its single upstream source id.

    A fan-out junction is a junction station fed by edges from exactly
    ONE distinct upstream source (a single exit port or upstream
    junction) and fanning out to one or more inter-section targets.
    Merge junctions (>1 distinct upstream source) are excluded: their
    trunk routing intentionally lands branches on a shared bypass Y and
    must not be snapped together at the junction.
    """
    junction_ids = graph.junction_ids
    result: dict[str, str] = {}
    for jid in junction_ids:
        sources = {e.source for e in graph.edges_to(jid)}
        if len(sources) != 1:
            continue
        if not any(True for _ in graph.edges_from(jid)):
            continue
        result[jid] = next(iter(sources))
    return result


def _fanout_route_maps(
    routes: list[RoutedPath],
    fanouts: dict[str, str],
) -> tuple[dict[tuple[str, str], RoutedPath], dict[tuple[str, str], RoutedPath]]:
    """Index fan-out-incident routes by ``(junction_id, line_id)``.

    Returns ``(upstream, downstream)``: ``upstream`` holds each
    ``port -> junction`` route, ``downstream`` the first
    ``junction -> target`` route for that line.  Both the apex-gap check
    and the routing pass that closes it consume these maps, so the keying
    is defined once.
    """
    upstream: dict[tuple[str, str], RoutedPath] = {}
    downstream: dict[tuple[str, str], RoutedPath] = {}
    for r in routes:
        if not r.points:
            continue
        if r.edge.target in fanouts:
            upstream[(r.edge.target, r.line_id)] = r
        if r.edge.source in fanouts:
            downstream.setdefault((r.edge.source, r.line_id), r)
    return upstream, downstream


def check_fanout_tail_join(
    routes: list[RoutedPath],
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
) -> list[FanoutTailGap]:
    """Return gaps where a fan-out junction's upstream tail does not meet
    its paired downstream route.

    For every fan-out junction (see :func:`fanout_junctions`), the
    component of the offset between an incoming ``port -> junction``
    route's end and the SAME-line outgoing ``junction -> target`` route's
    start, measured ALONG the upstream travel direction, must be within
    ``_TAIL_JOIN_TANGENT_TOLERANCE``.  A larger along-travel gap is the
    visible apex notch (the line stops short of its own bend) that this
    invariant guards against.  A purely perpendicular offset (the inner
    bundle member's concentric approach Y) is hidden under the stroke and
    not flagged.
    """
    fanouts = fanout_junctions(graph)
    if not fanouts:
        return []

    upstream, downstream = _fanout_route_maps(routes, fanouts)

    gaps: list[FanoutTailGap] = []
    for (jid, line_id), up in upstream.items():
        down = downstream.get((jid, line_id))
        if down is None or len(up.points) < 2:
            continue
        ux, uy = up.points[-1]
        dx, dy = down.points[0]
        # Travel direction of the upstream tail (its last segment).
        p_prev = up.points[-2]
        tx, ty = ux - p_prev[0], uy - p_prev[1]
        seg_len = (tx * tx + ty * ty) ** 0.5
        if seg_len < _MIN_SEGMENT_LENGTH:
            continue
        # Project the (downstream_start - upstream_end) offset onto the
        # unit travel direction: the along-line component.
        tangent_gap = abs(((dx - ux) * tx + (dy - uy) * ty) / seg_len)
        if tangent_gap > _TAIL_JOIN_TANGENT_TOLERANCE:
            gaps.append(
                FanoutTailGap(
                    junction_id=jid,
                    line_id=line_id,
                    upstream_source=up.edge.source,
                    downstream_target=down.edge.target,
                    upstream_end=(ux, uy),
                    downstream_start=(dx, dy),
                    tangent_gap=tangent_gap,
                )
            )
    return gaps


# ---------------------------------------------------------------------------
# Merge-port approach-side slot allocation
# ---------------------------------------------------------------------------

# Feeders sharing a port's row land within a few px of its base Y once
# per-line offsets are applied; a perpendicular feeder arrives from a
# different section row, a whole section-height away.  10px cleanly
# separates the two without tripping on multi-line bundle spread.
_MERGE_APPROACH_Y_TOL = 10.0


@dataclass(frozen=True)
class MergePortApproachViolation:
    """A line slotted on the wrong side of a multi-feeder merge port.

    At an LR/RL entry port fed by more than one exit port, a line that
    arrives perpendicular to the bundle (rising from a section below or
    descending from one above, with no horizontal co-travel in the
    port's row) must take the bundle slot nearest its approach side: the
    bottom slot when rising from below, the top slot when descending
    from above.  Otherwise its perpendicular riser crosses over the
    lines that arrive horizontally.  ``offset`` is the offending line's
    Y offset at the port; ``bound`` is the horizontal-co-traveller
    offset it violates.
    """

    port_id: str
    line_id: str
    approach: str  # "below" or "above"
    offset: float
    bound: float

    def message(self) -> str:
        slot = "bottom" if self.approach == "below" else "top"
        return (
            f"merge port {self.port_id!r} line {self.line_id!r}: arrives "
            f"from {self.approach} but sits at offset {self.offset:.1f}, on the wrong "
            f"side of horizontal co-travellers (bound {self.bound:.1f}); "
            f"a perpendicular re-joining line must take the {slot} slot to "
            f"avoid crossing the bundle"
        )


def _immediate_feeder(graph, port_id: str, line_id: str):  # noqa: ANN001, ANN202
    """Return ``(source_station, is_junction)`` feeding ``line_id`` in.

    Uses the IMMEDIATE edge source - it does not walk back through a
    junction.  Whether the source is a fan/merge junction matters: only
    a line fed by a direct exit port at a different row is a clean
    inter-section edge the router L-shapes into the port with a riser at
    the boundary (a true perpendicular join).  A junction-fed line may
    drop to the port's row far upstream and co-travel horizontally, so
    its junction's Y does not describe how it approaches the port.
    """
    for edge in graph.edges_to(port_id):
        if edge.line_id != line_id:
            continue
        src = graph.stations.get(edge.source)
        if src is None:
            return None, False
        return src, edge.source in graph.junctions
    return None, False


def bypass_horizontal_targets(
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
    port_id: str,
) -> dict[str, Station]:
    """Return mapping from line_id to first internal target for bypass lines.

    A bypass line co-travels horizontally to an entry port (its feeder is a
    junction at the port's own Y) but its first downstream station inside the
    section sits at a different Y.
    """
    port_st = graph.stations.get(port_id)
    if port_st is None:
        return {}
    outgoing = {e.line_id: e for e in graph.edges_from(port_id)}
    result: dict[str, Station] = {}
    for lid in graph.station_lines(port_id):
        src, is_junction = _immediate_feeder(graph, port_id, lid)
        if src is None or not is_junction:
            continue
        if abs(src.y - port_st.y) > _MERGE_APPROACH_Y_TOL:
            continue
        edge = outgoing.get(lid)
        if edge is None:
            continue
        tgt = graph.stations.get(edge.target)
        if tgt is None or tgt.is_port:
            continue
        if abs(tgt.y - port_st.y) > COORD_TOLERANCE_FINE:
            result[lid] = tgt
    return result


def classify_merge_port_feeders(
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
    port_id: str,
) -> tuple[list[str], list[str], list[str]] | None:
    """Classify a merge port's feeder lines by approach side.

    Returns ``(horizontal, below, above)`` line-id lists for an LR/RL
    entry port fed by at least two distinct feeders, with at least one
    horizontal co-traveller and at least one perpendicular feeder.  A
    line is horizontal when its immediate feeder sits in the port's own
    row; it is ``below`` / ``above`` only when fed by a direct exit port
    (not a junction) a row below / above the port - the clean
    inter-section edge that arrives perpendicular at the boundary.  A
    line dropped into the row by an upstream fan/merge junction
    co-travels horizontally and is not a perpendicular joiner, so it is
    left unclassified.  Bypass horizontal lines (junction-derived,
    same-Y feeder, but heading to a deeper station) are excluded from
    the horizontal list so they do not inflate ``max_horiz`` and push
    perpendicular lines into unnecessary outer slots.  Returns ``None``
    when the port is not such a reconvergence merge - i.e. when there
    is no approach-side decision to make.
    """
    port_obj = graph.ports.get(port_id)
    if port_obj is None or not port_obj.is_entry:
        return None
    if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
        return None
    port_st = graph.stations.get(port_id)
    if port_st is None:
        return None

    bypass_lids = set(bypass_horizontal_targets(graph, port_id))
    distinct_sources: set[int] = set()
    horizontal: list[str] = []
    below: list[str] = []
    above: list[str] = []
    for lid in graph.station_lines(port_id):
        src, is_junction = _immediate_feeder(graph, port_id, lid)
        if src is None:
            continue
        distinct_sources.add(id(src))
        if lid in bypass_lids:
            continue
        dy = src.y - port_st.y
        if abs(dy) <= _MERGE_APPROACH_Y_TOL:
            horizontal.append(lid)
        elif is_junction:
            continue
        elif dy > 0:
            below.append(lid)
        else:
            above.append(lid)
    if len(distinct_sources) < 2:
        return None
    if not horizontal or not (below or above):
        return None
    return horizontal, below, above


def check_merge_port_approach_side(
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
    offsets: dict[tuple[str, str], float],
) -> list[MergePortApproachViolation]:
    """Return merge ports where a perpendicular feeder is mis-slotted.

    For every reconvergence merge port (see
    :func:`classify_merge_port_feeders`), a line rising from below must
    sit at or below every horizontal co-traveller, and a line descending
    from above must sit at or above every horizontal co-traveller.
    """
    violations: list[MergePortApproachViolation] = []
    for port_id in graph.ports:
        classified = classify_merge_port_feeders(graph, port_id)
        if classified is None:
            continue
        horizontal, below, above = classified
        horiz_offs = [offsets.get((port_id, lid), 0.0) for lid in horizontal]
        max_horiz = max(horiz_offs)
        min_horiz = min(horiz_offs)
        for lid in below:
            off = offsets.get((port_id, lid), 0.0)
            if off < max_horiz - COORD_TOLERANCE_FINE:
                violations.append(
                    MergePortApproachViolation(
                        port_id=port_id,
                        line_id=lid,
                        approach="below",
                        offset=off,
                        bound=max_horiz,
                    )
                )
        for lid in above:
            off = offsets.get((port_id, lid), 0.0)
            if off > min_horiz + COORD_TOLERANCE_FINE:
                violations.append(
                    MergePortApproachViolation(
                        port_id=port_id,
                        line_id=lid,
                        approach="above",
                        offset=off,
                        bound=min_horiz,
                    )
                )
    return violations


@dataclass(frozen=True)
class MergePortOutgoingFlip:
    """A perpendicular line re-joined at a merge port that flips its slot on
    the outgoing run, crossing the trunk between the merge and its consumer.

    ``port_offset`` is the slot the line takes at the merge port; it flips to
    ``flipped_offset`` at ``at_station`` further along the merge row.
    """

    port_id: str
    line_id: str
    port_offset: float
    flipped_offset: float
    at_station: str

    def message(self) -> str:
        return (
            f"merge port {self.port_id!r} line {self.line_id!r}: takes slot "
            f"{self.port_offset:.1f} at the merge but flips to "
            f"{self.flipped_offset:.1f} at {self.at_station!r} on the same "
            "row, crossing the trunk on the outgoing run; a re-joined line "
            "must keep its slot to its consumer"
        )


def check_merge_port_outgoing_side_preserved(
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
    offsets: dict[tuple[str, str], float],
) -> list[MergePortOutgoingFlip]:
    """Return merge ports whose re-joined line flips slot on the outgoing run.

    A line re-slotted at a merge port (see
    :func:`classify_merge_port_feeders`) keeps that slot along the merge
    row down to its consumer; any same-row downstream station carrying it at
    a different offset is a crossover of the trunk.
    """
    violations: list[MergePortOutgoingFlip] = []
    for port_id in graph.ports:
        classified = classify_merge_port_feeders(graph, port_id)
        if classified is None:
            continue
        _horizontal, below, above = classified
        perp = list(below) + list(above)
        if not perp:
            continue
        row_y = graph.stations[port_id].y
        port_offs = {lid: offsets.get((port_id, lid), 0.0) for lid in perp}
        visited = {port_id}
        queue = deque([port_id])
        while queue:
            cur = queue.popleft()
            for edge in graph.edges_from(cur):
                tgt_id = edge.target
                if tgt_id in visited:
                    continue
                tgt = graph.stations[tgt_id]
                if abs(tgt.y - row_y) > SAME_Y_TOLERANCE:
                    continue
                visited.add(tgt_id)
                tgt_lines = graph.station_lines(tgt_id)
                for lid in perp:
                    if lid not in tgt_lines:
                        continue
                    off = offsets.get((tgt_id, lid), 0.0)
                    if abs(off - port_offs[lid]) > COORD_TOLERANCE_FINE:
                        violations.append(
                            MergePortOutgoingFlip(
                                port_id=port_id,
                                line_id=lid,
                                port_offset=port_offs[lid],
                                flipped_offset=off,
                                at_station=tgt_id,
                            )
                        )
                queue.append(tgt_id)
    return violations


# ---------------------------------------------------------------------------
# Exit port preserves the single entry bundle's order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitBundleOrderViolation:
    """An LR/RL exit port that re-orders a bundle relative to the single
    entry bundle feeding the section, kinking a straight-through line.

    ``entry_order`` / ``exit_order`` are the shared lines sorted by their
    per-line offset at each port.
    """

    section_id: str
    entry_port: str
    exit_port: str
    entry_order: tuple[str, ...]
    exit_order: tuple[str, ...]

    def message(self) -> str:
        return (
            f"section {self.section_id!r}: exit port {self.exit_port!r} "
            f"re-orders the bundle from its single entry {self.entry_port!r} "
            f"(entry order {self.entry_order} != exit order {self.exit_order}); "
            "a straight-through line is pushed off its incoming slot"
        )


def check_exit_inherits_entry_bundle_order(
    graph,  # noqa: ANN001 - MetroGraph (avoid import cycle)
    offsets: dict[tuple[str, str], float],
) -> list[ExitBundleOrderViolation]:
    """Return LR/RL exit ports that re-order a single incoming bundle.

    When a left/right section has exactly one entry port whose lines are a
    superset of an exit port's lines, that entry bundle establishes the
    order; the exit port must keep the shared lines in the same relative
    vertical order, so a line travelling straight through keeps its slot.
    TB sections are exempt: their exit reverses offsets for concentric arcs.
    """

    def _order(port_id: str, lines: set[str]) -> tuple[str, ...]:
        return tuple(
            sorted(lines, key=lambda lid: (offsets.get((port_id, lid), 0.0), lid))
        )

    violations: list[ExitBundleOrderViolation] = []
    for port_id, port in graph.ports.items():
        if port.is_entry or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        section = graph.sections.get(port.section_id)
        if section is None or section.direction not in ("LR", "RL"):
            continue
        entry_ports = list(section.entry_ports)
        if len(entry_ports) != 1:
            continue
        entry_id = entry_ports[0]
        exit_lines = set(graph.station_lines(port_id))
        if len(exit_lines) < 2 or not exit_lines.issubset(
            graph.station_lines(entry_id)
        ):
            continue
        entry_order = _order(entry_id, exit_lines)
        exit_order = _order(port_id, exit_lines)
        if entry_order != exit_order:
            violations.append(
                ExitBundleOrderViolation(
                    section_id=section.id,
                    entry_port=entry_id,
                    exit_port=port_id,
                    entry_order=entry_order,
                    exit_order=exit_order,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Partial-line fan-branch offset gaps
# ---------------------------------------------------------------------------


def distinct_offset_levels(values: Iterable[float]) -> list[float]:
    """Return ascending offset levels, merging values within tolerance.

    Coincident lines (offsets within ``COORD_TOLERANCE_FINE``) collapse
    to a single level so they are treated as one occupied slot.
    """
    levels: list[float] = []
    for v in sorted(values):
        if not levels or v - levels[-1] > COORD_TOLERANCE_FINE:
            levels.append(v)
    return levels


def is_independent_fan_branch(graph: MetroGraph, station_id: str) -> bool:
    """Return whether a station is an independent fan branch.

    Such a station carries two or more lines that all arrive from a
    fan-out and leave to a fan-in at other rows: it has no edge to a
    same-section neighbour on its own base Y, so no straight horizontal
    through-track runs through its marker.  Re-compacting its present
    lines onto consecutive offset slots therefore cannot bend a shared
    track - the only connections are the fan-out / fan-in curves, which
    bend regardless.

    A station with a same-Y same-section neighbour (a port, a hidden
    pass-through, or another station) is excluded: its lines genuinely
    run straight across that neighbour and must keep their bundle slots.
    """
    st = graph.stations.get(station_id)
    if st is None or st.is_port or st.is_hidden or st.section_id is None:
        return False
    if station_id in graph.junctions:
        return False
    if len(graph.station_lines(station_id)) < 2:
        return False
    neighbours = [e.target for e in graph.edges_from(station_id)]
    neighbours += [e.source for e in graph.edges_to(station_id)]
    for other_id in neighbours:
        other = graph.stations.get(other_id)
        if other is None or other.section_id != st.section_id:
            continue
        if abs(other.y - st.y) <= SAME_Y_TOLERANCE:
            return False
    return True


@dataclass(frozen=True)
class PartialBranchGapViolation:
    """An independent fan branch whose present lines reserve an absent
    line's offset slot, leaving a gap in its marker.

    ``offsets`` is the sorted ``(line_id, offset)`` tuple of the lines
    the station actually carries; the gap is the missing slot between
    two of them.
    """

    station_id: str
    offsets: tuple[tuple[str, float], ...]

    def message(self) -> str:
        slots = ", ".join(f"{lid}={off:.1f}" for lid, off in self.offsets)
        return (
            f"station {self.station_id!r} is an independent fan branch but "
            f"its lines reserve an absent-line slot ({slots}); present lines "
            f"must occupy consecutive offset slots with no gap"
        )


@dataclass(frozen=True)
class CollinearOverlapViolation:
    """Two distinct-line inter-section segments drawn on top of each other.

    ``axis`` is ``"V"`` or ``"H"``; ``coord`` is the shared channel
    position (X for vertical, Y for horizontal); ``span`` is the
    overlapping length along the axis.
    """

    line_a: str
    line_b: str
    edge_a: tuple[str, str]
    edge_b: tuple[str, str]
    axis: str
    coord: float
    span: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"collinear overlay: line {self.line_a!r} "
            f"({self.edge_a[0]}->{self.edge_a[1]}) and line {self.line_b!r} "
            f"({self.edge_b[0]}->{self.edge_b[1]}) coincide on the {self.axis} "
            f"channel at {self.coord:.1f} over {self.span:.1f}px"
        )


@dataclass(frozen=True)
class DiagonalOverlapViolation:
    """Two distinct-line diagonal segments running on top of each other.

    The axis-aligned :class:`CollinearOverlapViolation` cannot see this: a
    fixed-axis (Y for LR, baked-X for TB) per-line offset gives a perpendicular
    separation of only ``OFFSET_STEP * sin(theta)`` on a diagonal, which
    collapses toward zero as the diagonal nears the offset axis.  ``perp_sep`` is
    the measured perpendicular gap between the two co-running diagonals;
    ``span`` is the overlapping length along the shared direction.
    """

    line_a: str
    line_b: str
    edge_a: tuple[str, str]
    edge_b: tuple[str, str]
    perp_sep: float
    span: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"diagonal overlay: line {self.line_a!r} "
            f"({self.edge_a[0]}->{self.edge_a[1]}) and line {self.line_b!r} "
            f"({self.edge_b[0]}->{self.edge_b[1]}) run {self.perp_sep:.2f}px apart "
            f"on a shared diagonal over {self.span:.1f}px "
            f"(need >= {_DIAGONAL_MIN_PERP_SEP:.1f}px)"
        )


_COLLINEAR_LATERAL_TOL = 1.0
_COLLINEAR_MIN_SPAN = 40.0

# A diagonal bundle's lines must keep a true perpendicular separation, not a
# fixed-axis one whose perpendicular component degrades to zero as the diagonal
# nears the offset axis.  Flag any distinct-line pair that runs closer than this
# along a shared diagonal: half an OFFSET_STEP is the point at which ~3-4px
# strokes visibly fuse into one fat line.
_DIAGONAL_MIN_PERP_SEP = OFFSET_STEP * 0.5
_DIAGONAL_SLOPE_TOL = 0.12  # radians (~7 degrees); near-parallel diagonals


def _axis_aligned(
    p1: tuple[float, float], p2: tuple[float, float]
) -> tuple[str | None, float]:
    """Classify a segment as vertical/horizontal and return its channel coord."""
    (x1, y1), (x2, y2) = p1, p2
    if abs(x1 - x2) < COORD_TOLERANCE_FINE and abs(y1 - y2) > _COLLINEAR_LATERAL_TOL:
        return "V", (x1 + x2) * 0.5
    if abs(y1 - y2) < COORD_TOLERANCE_FINE and abs(x1 - x2) > _COLLINEAR_LATERAL_TOL:
        return "H", (y1 + y2) * 0.5
    return None, 0.0


def _route_render_points(
    rp: RoutedPath, offsets: dict[tuple[str, str], float]
) -> list[tuple[float, float]]:
    """Final render geometry for a route (mirrors render.apply_route_offsets)."""
    if rp.offsets_applied:
        return list(rp.points)
    src_off = offsets.get((rp.edge.source, rp.line_id), 0.0)
    tgt_off = offsets.get((rp.edge.target, rp.line_id), 0.0)
    orig_sy = rp.points[0][1]
    orig_ty = rp.points[-1][1]
    out: list[tuple[float, float]] = []
    last = len(rp.points) - 1
    for k, (x, y) in enumerate(rp.points):
        if k == 0:
            out.append((x, y + src_off))
        elif k == last:
            out.append((x, y + tgt_off))
        elif abs(y - orig_sy) <= abs(y - orig_ty):
            out.append((x, y + src_off))
        else:
            out.append((x, y + tgt_off))
    return out


def _collinear_overlay_violations(
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
    endpoint_xy: dict[str, tuple[float, float]],
    *,
    inter_section: bool,
) -> list[CollinearOverlapViolation]:
    """Pairs of distinct-line axis-aligned segments drawn exactly on top.

    Shared core of the inter- and intra-section collinear-overlay checks.
    Scans the ``is_inter_section == inter_section`` routes, builds their
    final offset-applied axis-aligned segments, and flags any
    different-line pair whose channel coordinate coincides within
    ``_COLLINEAR_LATERAL_TOL`` and whose overlap along the axis exceeds
    ``_COLLINEAR_MIN_SPAN``.  Overlaps that merely converge onto a shared
    endpoint in ``endpoint_xy`` (a port for inter-section routes, any
    station for intra-section routes) are excused.
    """
    segs: list[tuple[str, tuple[str, str], str, float, float, float]] = []
    for rp in routes:
        if rp.is_inter_section != inter_section:
            continue
        pts = _route_render_points(rp, offsets)
        edge = (rp.edge.source, rp.edge.target)
        for p1, p2 in zip(pts, pts[1:]):
            axis, coord = _axis_aligned(p1, p2)
            if axis is None:
                continue
            if axis == "V":
                lo, hi = sorted((p1[1], p2[1]))
            else:
                lo, hi = sorted((p1[0], p2[0]))
            segs.append((rp.line_id, edge, axis, coord, lo, hi))

    violations: list[CollinearOverlapViolation] = []
    for i in range(len(segs)):
        la, ea, ax_a, c_a, lo_a, hi_a = segs[i]
        for j in range(i + 1, len(segs)):
            lb, eb, ax_b, c_b, lo_b, hi_b = segs[j]
            if la == lb or ax_a != ax_b:
                continue
            if abs(c_a - c_b) > _COLLINEAR_LATERAL_TOL:
                continue
            olo, ohi = max(lo_a, lo_b), min(hi_a, hi_b)
            span = ohi - olo
            if span <= _COLLINEAR_MIN_SPAN:
                continue
            if _converges_at_shared_port(endpoint_xy, ea, eb, ax_a, c_a, olo, ohi):
                continue
            violations.append(
                CollinearOverlapViolation(
                    line_a=la,
                    line_b=lb,
                    edge_a=ea,
                    edge_b=eb,
                    axis=ax_a,
                    coord=c_a,
                    span=span,
                )
            )
    return violations


def check_no_collinear_distinct_lines(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[CollinearOverlapViolation]:
    """Return distinct-line inter-section segments drawn exactly on top.

    Two co-travelling lines that share a bundle must occupy distinct
    parallel slots (at least ``OFFSET_STEP`` apart laterally).  When a
    bundling/offset defect collapses them to the same channel they
    render as one stroke obscuring the other.  This check looks at the
    final, offset-applied geometry: it flags pairs of DIFFERENT-line
    axis-aligned inter-section segments whose channel coordinate
    coincides within ``_COLLINEAR_LATERAL_TOL`` and whose overlap along
    the axis exceeds ``_COLLINEAR_MIN_SPAN``.

    Legitimate cases are excluded: lines converging onto a shared
    endpoint port (an unavoidable single approach), tight parallel
    bundles (>= one ``OFFSET_STEP`` apart never coincide), and short
    trunk-level lead-ins below the min span.
    """
    port_xy = {
        pid: (graph.stations[pid].x, graph.stations[pid].y)
        for pid in graph.ports
        if pid in graph.stations
    }
    return _collinear_overlay_violations(routes, offsets, port_xy, inter_section=True)


def check_intra_section_collinear_distinct_lines(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[CollinearOverlapViolation]:
    """Return distinct-line *intra-section* segments drawn exactly on top.

    The intra-section counterpart to
    :func:`check_no_collinear_distinct_lines` (which only scans
    ``is_inter_section`` routes).  Two distinct lines running inside one
    section must keep their parallel slots there too; a collapse hides one
    line behind another within the section body.  Convergence onto a
    shared endpoint station (a real merge/fork node, not a boundary port)
    is excused, so genuine reconvergences are not flagged.
    """
    station_xy = {sid: (st.x, st.y) for sid, st in graph.stations.items()}
    # Edges internal to a rail-mode section legitimately run several
    # co-travelling lines on parallel rails; the rail router (not this
    # normal-pass geometry) renders them, so they are not an overlay defect.
    # Drop them before the overlay scan.
    if getattr(graph, "has_rail_sections", False):
        routes = [r for r in routes if not _edge_in_rail_section(graph, r.edge)]
    return _collinear_overlay_violations(
        routes, offsets, station_xy, inter_section=False
    )


def _edge_in_rail_section(graph: MetroGraph, edge: Edge) -> bool:
    """True when both endpoints of *edge* are real stations of one rail section."""
    src = graph.stations.get(edge.source)
    tgt = graph.stations.get(edge.target)
    if src is None or tgt is None or src.is_port or tgt.is_port:
        return False
    return (
        src.section_id is not None
        and src.section_id == tgt.section_id
        and graph.is_rail_section(src.section_id)
    )


def _converges_at_shared_port(
    port_xy: dict[str, tuple[float, float]],
    edge_a: tuple[str, str],
    edge_b: tuple[str, str],
    axis: str,
    coord: float,
    olo: float,
    ohi: float,
) -> bool:
    """True when the overlap is a brief convergence onto a shared port.

    Distinct lines may touch at a shared endpoint port (an unavoidable
    single point), but a long co-run alongside the port is a real overlay:
    the convergence must collapse to the port, not run parallel-on-top for
    a meaningful length.  Excuse the overlap only when it reaches the port
    marker AND extends no further than ``_COLLINEAR_MIN_SPAN`` from it.
    """
    tol = COORD_TOLERANCE + _COLLINEAR_LATERAL_TOL
    for pid in (set(edge_a) & set(edge_b)) & port_xy.keys():
        px, py = port_xy[pid]
        if axis == "V":
            anchor = py
            on_channel = abs(px - coord) <= tol
        else:
            anchor = px
            on_channel = abs(py - coord) <= tol
        if not on_channel or not (olo - tol <= anchor <= ohi + tol):
            continue
        # Distance the overlap reaches away from the port along the axis.
        reach = max(anchor - olo, ohi - anchor)
        if reach <= _COLLINEAR_MIN_SPAN:
            return True
    return False


_Seg = tuple[tuple[float, float], tuple[float, float]]


def _diagonal_segments(
    rp: RoutedPath, offsets: dict[tuple[str, str], float]
) -> list[_Seg]:
    """Final-render diagonal segments of *rp* (both axes change appreciably)."""
    pts = _route_render_points(rp, offsets)
    out: list[_Seg] = []
    for p1, p2 in zip(pts, pts[1:]):
        if (
            abs(p1[0] - p2[0]) > _COLLINEAR_LATERAL_TOL
            and abs(p1[1] - p2[1]) > _COLLINEAR_LATERAL_TOL
        ):
            out.append((p1, p2))
    return out


def _diagonal_coincidence(seg_a: _Seg, seg_b: _Seg) -> tuple[float, float] | None:
    """Perpendicular gap and overlap span of two near-parallel diagonals.

    Returns ``None`` when the segments are not near-parallel, do not overlap
    when projected onto the shared direction, or cross (opposite-sign
    perpendicular offsets at the overlap ends) -- a crossing is a normal metro
    junction, not a parallel overlay.  Otherwise returns ``(perp_sep, span)``
    where ``perp_sep`` is the *widest* perpendicular gap across the overlap and
    ``span`` its projected length.

    Keying on the widest gap is what separates a true co-running bundle (close
    at both ends) from a fan diverging out of a shared hub (close at the hub end
    only): the diverging pair is wide at the far end, so it is not flagged even
    though it is near-parallel and shares an endpoint.
    """
    (ax1, ay1), (ax2, ay2) = seg_a
    (bx1, by1), (bx2, by2) = seg_b
    ang_a = math.atan2(ay2 - ay1, ax2 - ax1)
    ang_b = math.atan2(by2 - by1, bx2 - bx1)
    # Compare as undirected lines: a half-turn between the two travel
    # directions is the same orientation.
    d = abs(ang_a - ang_b) % math.pi
    d = min(d, math.pi - d)
    if d > _DIAGONAL_SLOPE_TOL:
        return None

    ux, uy = ax2 - ax1, ay2 - ay1
    length = math.hypot(ux, uy)
    if length < COORD_TOLERANCE:
        return None
    ux, uy = ux / length, uy / length
    nx, ny = -uy, ux  # unit normal of seg_a

    def project(px: float, py: float) -> float:
        return (px - ax1) * ux + (py - ay1) * uy

    def perp(px: float, py: float) -> float:
        return (px - ax1) * nx + (py - ay1) * ny

    tb_lo, tb_hi = project(bx1, by1), project(bx2, by2)
    pb_lo, pb_hi = perp(bx1, by1), perp(bx2, by2)
    olo, ohi = max(0.0, min(tb_lo, tb_hi)), min(length, max(tb_lo, tb_hi))
    span = ohi - olo
    if span <= _COLLINEAR_MIN_SPAN:
        return None
    if abs(tb_hi - tb_lo) < COORD_TOLERANCE:
        return None

    # Perpendicular gap is linear in the projection parameter, so evaluate it at
    # the two ends of the overlap interval.  A sign flip between them is a
    # crossing (not a parallel overlay); otherwise the widest of the two is how
    # far the lines ever separate across the shared run.

    def perp_at(t: float) -> float:
        frac = (t - tb_lo) / (tb_hi - tb_lo)
        return pb_lo + frac * (pb_hi - pb_lo)

    g_lo, g_hi = perp_at(olo), perp_at(ohi)
    if g_lo * g_hi < -COORD_TOLERANCE_FINE:
        return None
    return max(abs(g_lo), abs(g_hi)), span


def check_no_collinear_distinct_diagonals(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[DiagonalOverlapViolation]:
    """Return distinct-line diagonal segments collapsed on top of each other.

    The diagonal counterpart to :func:`check_no_collinear_distinct_lines` and
    :func:`check_intra_section_collinear_distinct_lines`, which only inspect
    axis-aligned segments.  A bundle's per-line offset is applied along a fixed
    axis, so on a diagonal the perpendicular separation shrinks to
    ``OFFSET_STEP * sin(theta)`` and the lines fuse into one stroke.  This flags
    any different-line pair whose diagonals run near-parallel, overlap by more
    than ``_COLLINEAR_MIN_SPAN``, and stay closer than ``_DIAGONAL_MIN_PERP_SEP``
    apart without crossing.  Rail-section internal edges (rendered on explicit
    parallel rails) are excluded, matching the axis-aligned intra check.
    """
    skip_rail = getattr(graph, "has_rail_sections", False)
    diags: list[tuple[str, tuple[str, str], _Seg]] = []
    for rp in routes:
        if skip_rail and _edge_in_rail_section(graph, rp.edge):
            continue
        for seg in _diagonal_segments(rp, offsets):
            diags.append((rp.line_id, (rp.edge.source, rp.edge.target), seg))

    violations: list[DiagonalOverlapViolation] = []
    for i in range(len(diags)):
        la, ea, sa = diags[i]
        for j in range(i + 1, len(diags)):
            lb, eb, sb = diags[j]
            if la == lb:
                continue
            result = _diagonal_coincidence(sa, sb)
            if result is None:
                continue
            perp_sep, span = result
            if perp_sep >= _DIAGONAL_MIN_PERP_SEP:
                continue
            violations.append(
                DiagonalOverlapViolation(
                    line_a=la,
                    line_b=lb,
                    edge_a=ea,
                    edge_b=eb,
                    perp_sep=perp_sep,
                    span=span,
                )
            )
    return violations


def check_partial_branch_offset_gaps(
    graph: MetroGraph,
    offsets: dict[tuple[str, str], float],
    *,
    offset_step: float = OFFSET_STEP,
) -> list[PartialBranchGapViolation]:
    """Return independent fan branches whose marker reserves an absent slot.

    Only meaningful under ``compact_offsets``: that mode promises to
    tighten bundle spacing, so a partial-line branch should sit on
    consecutive slots rather than reserve a gap for lines it does not
    carry.  In non-compact mode lines keep globally reserved slots by
    design, so the check is a no-op there.
    """
    if not graph.compact_offsets:
        return []
    violations: list[PartialBranchGapViolation] = []
    for sid in graph.stations:
        if not is_independent_fan_branch(graph, sid):
            continue
        sorted_offs = sorted(
            (offsets.get((sid, lid), 0.0), lid) for lid in graph.station_lines(sid)
        )
        levels = distinct_offset_levels(off for off, _ in sorted_offs)
        # A reserved absent-line slot is an interior gap between two
        # distinct occupied levels wider than one step.  Lines that share
        # a level (coincident) collapse to one level and never trip this.
        has_gap = any(
            levels[i + 1] - levels[i] > offset_step + COORD_TOLERANCE_FINE
            for i in range(len(levels) - 1)
        )
        if has_gap:
            violations.append(
                PartialBranchGapViolation(
                    station_id=sid,
                    offsets=tuple((lid, off) for off, lid in sorted_offs),
                )
            )
    return violations


@dataclass(frozen=True)
class SameLineParallelRun:
    """Two same-line segments running parallel a few pixels apart over a span.

    The two segments share an endpoint - one source fans out to several
    targets, or several feeds converge on one port - yet travel their common
    stretch in adjacent channels instead of one merged trunk, so the single
    line renders as two parallel same-colour tracks.
    """

    line_id: str
    edge_a: tuple[str, str]
    edge_b: tuple[str, str]
    axis: str
    sep: float
    span: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"same-line parallel run: line {self.line_id!r} "
            f"({self.edge_a[0]}->{self.edge_a[1]}) and "
            f"({self.edge_b[0]}->{self.edge_b[1]}) run parallel on the "
            f"{self.axis} axis {self.sep:.1f}px apart over {self.span:.1f}px"
        )


def check_no_same_line_parallel_descents(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[SameLineParallelRun]:
    """Return same-line vertical descents that share an endpoint yet run parallel.

    A line that fans out from one source to several targets (or converges
    on one port from several feeds) must descend as a SINGLE trunk over the
    span its branches travel together, splitting only where each branch
    turns off.  When the branches instead occupy adjacent offset slots they
    render as two parallel same-colour tracks that read as two routes.

    Flags pairs of same-line vertical inter-section segments that share a
    source or target endpoint, sit more than ``_COLLINEAR_LATERAL_TOL`` but
    no more than ``EDGE_TO_BUNDLE_CLEARANCE`` apart (a merged trunk falls
    below the floor; genuinely-distant corridors above the ceiling), and
    overlap by more than ``_COLLINEAR_MIN_SPAN``.
    """
    segs: list[tuple[str, tuple[str, str], float, float, float]] = []
    for rp in routes:
        if not rp.is_inter_section:
            continue
        pts = _route_render_points(rp, offsets)
        edge = (rp.edge.source, rp.edge.target)
        for p1, p2 in zip(pts, pts[1:]):
            axis, coord = _axis_aligned(p1, p2)
            if axis != "V":
                continue
            lo, hi = sorted((p1[1], p2[1]))
            segs.append((rp.line_id, edge, coord, lo, hi))

    violations: list[SameLineParallelRun] = []
    for i in range(len(segs)):
        la, ea, xa, lo_a, hi_a = segs[i]
        for j in range(i + 1, len(segs)):
            lb, eb, xb, lo_b, hi_b = segs[j]
            if la != lb:
                continue
            if ea[0] != eb[0] and ea[1] != eb[1]:
                continue
            sep = abs(xa - xb)
            if sep <= _COLLINEAR_LATERAL_TOL or sep > EDGE_TO_BUNDLE_CLEARANCE:
                continue
            olo, ohi = max(lo_a, lo_b), min(hi_a, hi_b)
            span = ohi - olo
            if span <= _COLLINEAR_MIN_SPAN:
                continue
            violations.append(
                SameLineParallelRun(
                    line_id=la,
                    edge_a=ea,
                    edge_b=eb,
                    axis="V",
                    sep=sep,
                    span=span,
                )
            )
    return violations


@dataclass
class SplitFanoutDescent:
    """Two same-line fan-out descents that overlap in Y at distinct Xs.

    Both branches leave one source horizontal-then-vertical and descend
    through a common Y band, yet open in separate channels instead of one
    fused trunk.  A split that begins before either branch turns off puts the
    farther-reaching branch on the inside of the nearer one, so its onward
    horizontal run crosses the nearer branch's descent.
    """

    line_id: str
    source: str
    x_a: float
    x_b: float
    sep: float
    overlap: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"split same-line fan-out: line {self.line_id!r} leaves "
            f"{self.source} as two descents {self.sep:.1f}px apart "
            f"(x={self.x_a:.1f}, {self.x_b:.1f}) overlapping {self.overlap:.1f}px "
            f"in Y instead of one fused trunk"
        )


def check_no_split_same_line_fanout_descents(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[SplitFanoutDescent]:
    """Return same-line fan-out descents that overlap in Y at distinct Xs.

    Several inter-section edges of one line fanning out from a single source
    must descend as ONE trunk over the span their branches travel together,
    splitting only where each branch turns off.  When two such descents
    overlap in their Y span yet sit at distinct Xs, the split has begun before
    either branch diverges; the farther branch then crosses the nearer one's
    descent (issue #702).  Coincident descents (a fused trunk) are the wanted
    state and never flag.
    """
    by_source: dict[tuple[str, str, bool], list[tuple[float, float, float]]] = (
        defaultdict(list)
    )
    for rp in routes:
        if not rp.is_inter_section:
            continue
        span = initial_fanout_descent_span(rp)
        if span is None:
            continue
        x, y_lo, y_hi, down = span
        by_source[(rp.edge.source, rp.line_id, down)].append((x, y_lo, y_hi))

    violations: list[SplitFanoutDescent] = []
    for (source, line_id, _down), descents in by_source.items():
        if len(descents) < 2:
            continue
        for i in range(len(descents)):
            xa, lo_a, hi_a = descents[i]
            for j in range(i + 1, len(descents)):
                xb, lo_b, hi_b = descents[j]
                overlap = min(hi_a, hi_b) - max(lo_a, lo_b)
                if overlap <= COORD_TOLERANCE:
                    continue
                sep = abs(xa - xb)
                if sep <= COORD_TOLERANCE:
                    continue
                violations.append(
                    SplitFanoutDescent(
                        line_id=line_id,
                        source=source,
                        x_a=xa,
                        x_b=xb,
                        sep=sep,
                        overlap=overlap,
                    )
                )
    return violations


@dataclass(frozen=True)
class DoglegCrossesExemptTrunk:
    """A non-exempt trunk doglegged off an exempt run that crosses it.

    An ``normalize_exempt`` horizontal run (wrap / around-section loop) and a
    different line's bypass trunk share an inter-row channel within a bundle
    gap.  Cleared to one side they read as a tight parallel bundle; cleared to
    the wrong side the movable trunk's riser pierces the exempt run (and the
    exempt riser pierces the movable run), so the two colours cross twice
    instead of running parallel (issue #702).
    """

    line_id: str
    exempt_line: str
    edge: tuple[str, str]
    exempt_edge: tuple[str, str]
    x: float
    y: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"dogleg crossing: line {self.line_id!r} "
            f"({self.edge[0]}->{self.edge[1]}) crosses exempt trunk "
            f"{self.exempt_line!r} ({self.exempt_edge[0]}->"
            f"{self.exempt_edge[1]}) at ({self.x:.1f},{self.y:.1f}) instead of "
            f"running parallel above or below it"
        )


def check_no_dogleg_crosses_exempt_trunk(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[DoglegCrossesExemptTrunk]:
    """Return non-exempt trunks that cross an exempt trunk they bundle with.

    A movable bypass trunk nudged off an ``normalize_exempt`` run of a
    different line must clear to the side that keeps the two parallel; landing
    on the side whose riser pierces the exempt run trades one fused stroke for
    a double crossing (issue #702).  Only pairs sharing an inter-row channel
    (within ``2 * OFFSET_STEP`` in Y and overlapping in X) are considered, so a
    legitimate bundle a full gap apart never flags.
    """
    exempt = [
        (rp, seg)
        for rp in routes
        if rp.is_inter_section and rp.normalize_exempt
        for _k, seg in iter_horizontal_trunks(rp)
    ]
    if not exempt:
        return []
    violations: list[DoglegCrossesExemptTrunk] = []
    for rp in routes:
        if not rp.is_inter_section or rp.normalize_exempt:
            continue
        for _k, seg in iter_horizontal_trunks(rp):
            for erp, eseg in exempt:
                if erp.line_id == rp.line_id:
                    continue
                if abs(seg.y - eseg.y) >= 2 * OFFSET_STEP:
                    continue
                if seg.x_lo >= eseg.x_hi or eseg.x_lo >= seg.x_hi:
                    continue
                pt = trunk_segments_cross(seg, eseg)
                if pt is None:
                    continue
                violations.append(
                    DoglegCrossesExemptTrunk(
                        line_id=rp.line_id,
                        exempt_line=erp.line_id,
                        edge=(rp.edge.source, rp.edge.target),
                        exempt_edge=(erp.edge.source, erp.edge.target),
                        x=pt[0],
                        y=pt[1],
                    )
                )
    return violations


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping / touching ``(lo, hi)`` intervals into maximal runs."""
    out: list[tuple[float, float]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + COORD_TOLERANCE:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


@dataclass(frozen=True)
class StackedElbowGraze:
    """Two opposing elbows in one inter-section gap whose corners graze.

    Two different lines descend the same gap in vertical risers that are
    *stacked* (their spans meet at one elbow band rather than running
    parallel), yet sit within ``BUNDLE_TO_BUNDLE_CLEARANCE`` of each other in
    X.  Their turning corners then overlap, so the elbow of one line touches
    the riser/elbow of the other instead of the two being distributed across
    the gap width.
    """

    line_a: str
    line_b: str
    edge_a: tuple[str, str]
    edge_b: tuple[str, str]
    x_a: float
    x_b: float
    y_overlap: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"stacked-elbow graze: risers of {self.line_a!r} "
            f"({self.edge_a[0]}->{self.edge_a[1]}) at x={self.x_a:.1f} and "
            f"{self.line_b!r} ({self.edge_b[0]}->{self.edge_b[1]}) at "
            f"x={self.x_b:.1f} overlap only {self.y_overlap:.1f}px in Y yet sit "
            f"{abs(self.x_a - self.x_b):.1f}px apart (< {BUNDLE_TO_BUNDLE_CLEARANCE}); "
            f"their opposing elbows graze instead of distributing across the gap"
        )


def check_stacked_elbow_clearance(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[StackedElbowGraze]:
    """Return stacked, non-parallel inter-section risers packed too tightly.

    Two vertical inter-section risers of different lines that merely *meet*
    at one elbow band - their Y spans overlap by less than
    ``MIN_CORRIDOR_Y_OVERLAP`` - are not a parallel bundle: one is a deep
    descent landing on a lane the other then leaves.  When such a pair sits
    within ``BUNDLE_TO_BUNDLE_CLEARANCE`` in X, the corners off the two risers
    overlap and graze.  They must instead be distributed across the gap as
    separate corridors, which puts at least ``BUNDLE_TO_BUNDLE_CLEARANCE``
    between them.

    Risers that genuinely overlap in Y (a real parallel bundle) are exempt:
    their concentric corners are expected to nest at ``OFFSET_STEP``.  Risers
    sharing a source or target endpoint are exempt too: those are one fan-out
    or fan-in, whose branches legitimately stack at one elbow as they diverge
    from / converge on the shared node.

    Each line's vertical inter-section segments are first merged into maximal
    runs per X column, so a bundle whose riser is split into segments by
    intermediate corners is compared as one tall run (and so reads as a long
    parallel overlap, exempt) rather than as short segment fragments that
    could each show a spurious tiny overlap.
    """
    by_line_x: dict[tuple[str, float], list[tuple[float, float]]] = defaultdict(list)
    edge_of: dict[tuple[str, float], tuple[str, str]] = {}
    for rp in routes:
        if not rp.is_inter_section:
            continue
        pts = _route_render_points(rp, offsets)
        edge = (rp.edge.source, rp.edge.target)
        for p1, p2 in zip(pts, pts[1:]):
            axis, coord = _axis_aligned(p1, p2)
            if axis != "V":
                continue
            xkey = round(coord / COORD_TOLERANCE) * COORD_TOLERANCE
            lo, hi = sorted((p1[1], p2[1]))
            by_line_x[(rp.line_id, xkey)].append((lo, hi))
            edge_of[(rp.line_id, xkey)] = edge

    risers: list[tuple[str, tuple[str, str], float, float, float]] = []
    for (line_id, xkey), spans in by_line_x.items():
        for lo, hi in _merge_spans(spans):
            risers.append((line_id, edge_of[(line_id, xkey)], xkey, lo, hi))

    violations: list[StackedElbowGraze] = []
    for i in range(len(risers)):
        la, ea, xa, lo_a, hi_a = risers[i]
        for j in range(i + 1, len(risers)):
            lb, eb, xb, lo_b, hi_b = risers[j]
            if la == lb:
                continue
            if ea[0] == eb[0] or ea[1] == eb[1]:
                continue
            if abs(xa - xb) >= BUNDLE_TO_BUNDLE_CLEARANCE - COORD_TOLERANCE:
                continue
            overlap = min(hi_a, hi_b) - max(lo_a, lo_b)
            # Lower bound: the two risers must genuinely meet (a non-meeting
            # pair leaves a Y gap and so cannot graze); upper bound is the
            # parallel-run floor.
            if not (COORD_TOLERANCE < overlap < MIN_CORRIDOR_Y_OVERLAP):
                continue
            violations.append(
                StackedElbowGraze(
                    line_a=la,
                    line_b=lb,
                    edge_a=ea,
                    edge_b=eb,
                    x_a=xa,
                    x_b=xb,
                    y_overlap=overlap,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Bundle-corner concentricity
# ---------------------------------------------------------------------------

# Arc-centre spread above this reads as a visible pinch/gap through the bend.
_CONCENTRIC_CENTRE_TOLERANCE = 1.0
# A corner counts as wholesale-translated only when both flanking legs are
# offset from the bundle-mate by the same amount; a difference above this means
# one leg is pinned (a transition corner), where non-concentric is intended.
_WHOLESALE_LEG_TOLERANCE = 1.0


@dataclass(frozen=True)
class NonConcentricCornerViolation:
    """A wholesale-translated bundle corner whose arcs don't share a centre.

    At a corner where the whole bend translates per line (both flanking legs
    offset by the same perpendicular distance), the bundle-mates' arcs must
    share a common centre so the inter-line gap stays constant through the
    bend.  ``centre_spread`` is the distance between the two arc centres;
    anything above tolerance pinches or gaps the bundle mid-curve.
    """

    edge_source: str
    edge_target: str
    line_a: str
    line_b: str
    corner_index: int
    corner_xy: tuple[float, float]
    centre_spread: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        cx, cy = self.corner_xy
        return (
            f"non-concentric bundle corner {self.edge_source!r}->"
            f"{self.edge_target!r} at ({cx:.1f},{cy:.1f}) "
            f"(corner #{self.corner_index}): lines {self.line_a!r} and "
            f"{self.line_b!r} translate the whole corner together yet their "
            f"arc centres are {self.centre_spread:.1f}px apart "
            f"(> {_CONCENTRIC_CENTRE_TOLERANCE:.1f}px) - the bundle pinches "
            f"through the bend.  Size the corner via "
            f"concentric_corner_radius(_at), not a hand-signed radius"
        )


def _arc_centre(
    corner: tuple[float, float],
    radius: float,
    turn_in: tuple[float, float],
    turn_out: tuple[float, float],
) -> tuple[float, float]:
    """Centre of the rounded-corner arc: ``corner + radius * (turn_out - turn_in)``."""
    ux = turn_out[0] - turn_in[0]
    uy = turn_out[1] - turn_in[1]
    return (corner[0] + radius * ux, corner[1] + radius * uy)


def _resolved_corner_radii(
    rp: RoutedPath, pts: list[tuple[float, float]]
) -> list[float]:
    """Effective (segment-clamped) radius at each corner of *pts*."""
    from nf_metro.layout.routing.corners import resolve_curve_radii

    return resolve_curve_radii(pts, rp.curve_radii)


def check_concentric_bundle_corners(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[NonConcentricCornerViolation]:
    """Return wholesale-translated bundle corners whose arcs aren't concentric.

    The correctness check the corner-radius *source* ratchet
    (``tests/test_corner_radius_ratchet.py``) explicitly cannot perform: a
    radius traceable to an approved helper can nest non-concentrically when the
    caller hand-picks the wrong sign.

    Routes are grouped by ``(edge.source, edge.target)``.  For each bundled
    pair sharing a waypoint count, every interior corner is classified from the
    *final* offset-applied geometry: a corner is **wholesale-translated** (must
    be concentric) when the two flanking legs are each offset from the mate by
    the same perpendicular distance, and a **transition** corner (skipped) when
    one leg is pinned.  At a wholesale corner the two arc centres
    (``corner + radius * (turn_out - turn_in)``, using the segment-clamped
    radius) must coincide within tolerance; a larger spread is a visible pinch.
    """
    bundles: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
    for r in routes:
        bundles[(r.edge.source, r.edge.target)].append(r)

    violations: list[NonConcentricCornerViolation] = []
    for (src_id, tgt_id), bundle in bundles.items():
        if len(bundle) < 2:
            continue
        rendered = [(r, _route_render_points(r, offsets)) for r in bundle]
        radii = {id(r): _resolved_corner_radii(r, pts) for r, pts in rendered}
        for ai in range(len(rendered)):
            ra, pa = rendered[ai]
            for bi in range(ai + 1, len(rendered)):
                rb, pb = rendered[bi]
                if len(pa) != len(pb) or len(pa) < 3:
                    continue
                v = _pair_corner_violation(
                    src_id, tgt_id, ra, pa, radii[id(ra)], rb, pb, radii[id(rb)]
                )
                if v is not None:
                    violations.append(v)
    return violations


def _pair_corner_violation(
    src_id: str,
    tgt_id: str,
    ra: RoutedPath,
    pa: list[tuple[float, float]],
    radii_a: list[float],
    rb: RoutedPath,
    pb: list[tuple[float, float]],
    radii_b: list[float],
) -> NonConcentricCornerViolation | None:
    """First non-concentric wholesale corner between two bundled routes."""
    for k in range(1, len(pa) - 1):
        turn_in = _segment_unit(pa[k - 1], pa[k])
        turn_out = _segment_unit(pa[k], pa[k + 1])
        if turn_in is None or turn_out is None:
            continue
        # Real 90-degree turn only: a straight pass-through has turn_out == turn_in
        # so (turn_out - turn_in) is zero and the centre test is degenerate.
        if (
            abs(turn_in[0] * turn_out[0] + turn_in[1] * turn_out[1])
            > COORD_TOLERANCE_FINE
        ):
            continue
        # Corner displacement of B from A, split into the offset of the
        # incoming leg (component along turn_out) and the outgoing leg
        # (component along turn_in).
        vx = pb[k][0] - pa[k][0]
        vy = pb[k][1] - pa[k][1]
        d_in_leg = abs(vx * turn_out[0] + vy * turn_out[1])
        d_out_leg = abs(vx * turn_in[0] + vy * turn_in[1])
        if max(d_in_leg, d_out_leg) <= _WHOLESALE_LEG_TOLERANCE:
            continue  # coincident corner: no bundle offset to nest
        if abs(d_in_leg - d_out_leg) > _WHOLESALE_LEG_TOLERANCE:
            continue  # transition corner: one leg pinned, non-concentric by design
        if k - 1 >= len(radii_a) or k - 1 >= len(radii_b):
            continue
        ca = _arc_centre(pa[k], radii_a[k - 1], turn_in, turn_out)
        cb = _arc_centre(pb[k], radii_b[k - 1], turn_in, turn_out)
        spread = ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5
        if spread > _CONCENTRIC_CENTRE_TOLERANCE:
            return NonConcentricCornerViolation(
                edge_source=src_id,
                edge_target=tgt_id,
                line_a=ra.line_id,
                line_b=rb.line_id,
                corner_index=k,
                corner_xy=pa[k],
                centre_spread=spread,
            )
    return None


@dataclass(frozen=True)
class PerpEntryBoundaryViolation:
    """A line that reverses lateral direction crossing a perpendicular port.

    At a shared TOP/BOTTOM entry port, the inter-section approach lands at
    ``approach_x`` while the same line's intra-section drop departs at
    ``departure_x``.  When these disagree the line jogs onto the port marker
    and back off it - an S-cusp on the section boundary.  ``port_y`` is the
    boundary the two crossings straddle.
    """

    port_id: str
    line_id: str
    approach_x: float
    departure_x: float
    port_y: float

    def message(self) -> str:
        return (
            f"perp entry port {self.port_id} line {self.line_id}: approach "
            f"crosses boundary y={self.port_y:.1f} at x={self.approach_x:.1f} "
            f"but departure leaves at x={self.departure_x:.1f} "
            f"(lateral reversal at boundary)"
        )


def _boundary_crossing_x(
    points: list[tuple[float, float]], port_y: float, *, approach: bool
) -> float | None:
    """X of the vertical leg by which a route touches a perpendicular port.

    A route into the port (``approach``) reaches ``port_y`` with its last
    vertical segment; a route out of it (departure) leaves ``port_y`` with its
    first vertical segment.  Either way the line crosses the boundary at that
    segment's X.  A horizontal jog onto the port marker is the segment *after*
    (approach) or *before* (departure) this leg, so it does not mask the true
    crossing X.  Returns ``None`` when no vertical leg touches the boundary.
    """
    segments = list(zip(points, points[1:]))
    if approach:
        segments = list(reversed(segments))
    for (x1, y1), (x2, y2) in segments:
        touches_boundary = min(abs(y1 - port_y), abs(y2 - port_y)) <= COORD_TOLERANCE
        if (
            touches_boundary
            and abs(x2 - x1) <= COORD_TOLERANCE
            and abs(y2 - y1) > COORD_TOLERANCE
        ):
            return x1
    return None


def check_perp_entry_boundary_consistent(
    graph: MetroGraph,
    routes: list[RoutedPath],
) -> list[PerpEntryBoundaryViolation]:
    """Return lines that reverse lateral direction crossing a perp entry port.

    For each TOP/BOTTOM entry port, a line that *approaches* via an
    inter-section route and *continues* via an intra-section drop must cross the
    section boundary at one consistent per-line X: the approach's vertical leg
    into the boundary and the departure's vertical leg out of it must share that
    X.  A mismatch is the boundary jitter where the line lands on the port
    marker, then re-fans off it.  Merge ports with more than one approach for a
    line are exempt: those feeders genuinely converge, so a single crossing X is
    not defined.
    """
    approaches: dict[tuple[str, str], list[RoutedPath]] = defaultdict(list)
    departures: dict[tuple[str, str], RoutedPath] = {}
    for r in routes:
        if len(r.points) < 2:
            continue
        approaches[(r.edge.target, r.line_id)].append(r)
        departures.setdefault((r.edge.source, r.line_id), r)

    violations: list[PerpEntryBoundaryViolation] = []
    for pid, port in graph.ports.items():
        if not port.is_entry or port.side not in (PortSide.TOP, PortSide.BOTTOM):
            continue
        pst = graph.stations.get(pid)
        if pst is None:
            continue
        for line_id in graph.station_lines(pid):
            feeders = approaches.get((pid, line_id), [])
            departure = departures.get((pid, line_id))
            if len(feeders) != 1 or departure is None:
                continue
            ax = _boundary_crossing_x(feeders[0].points, pst.y, approach=True)
            dx = _boundary_crossing_x(departure.points, pst.y, approach=False)
            if ax is None or dx is None:
                continue
            if abs(ax - dx) > COORD_TOLERANCE:
                violations.append(
                    PerpEntryBoundaryViolation(pid, line_id, ax, dx, pst.y)
                )
    return violations


def _segment_unit(
    p1: tuple[float, float], p2: tuple[float, float]
) -> tuple[float, float] | None:
    """Unit travel vector for an axis-aligned segment.

    Returns ``None`` for a sub-pixel segment or a diagonal one (both axes
    significant): concentric nesting is defined only for the orthogonal
    horizontal/vertical legs of a 90-degree bend, so a diagonal leg carries no
    corner to test.
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    if abs(dx) < _MIN_SEGMENT_LENGTH and abs(dy) < _MIN_SEGMENT_LENGTH:
        return None
    if min(abs(dx), abs(dy)) > COORD_TOLERANCE:
        return None  # diagonal: not an orthogonal corner leg
    if abs(dx) >= abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


# A merge branch endpoint farther than this from the converging structure
# (its trunk, the merge junction, or the entry port) reads as a stub hanging
# in open space rather than a route that joins the trunk.  Two corner radii
# of slack covers the branch's turn-in arc and per-line bundle offsets; a real
# desync between the branch drop level and the trunk channel is an order of
# magnitude larger.
_MERGE_BRANCH_HANG_TOL = 2 * CURVE_RADIUS


@dataclass
class MergeBranchHang:
    """A merge feeder route that terminates disconnected from the trunk.

    At a reconvergence merge, the non-trunk feeders ("branches") descend to
    the trunk's bypass channel and turn into it.  When the branch's drop
    level disagrees with the channel the trunk route actually runs, the
    branch ends in open space, hundreds of pixels from the trunk it should
    join.  ``gap`` is the distance from the branch endpoint to the nearest
    converging structure (a sibling feeder's path, the merge junction, or
    the entry port).
    """

    merge_id: str
    line_id: str
    source: str
    endpoint: tuple[float, float]
    gap: float

    def message(self) -> str:
        return (
            f"merge {self.merge_id!r}: feeder {self.line_id!r} from "
            f"{self.source!r} ends at "
            f"({self.endpoint[0]:.1f},{self.endpoint[1]:.1f}), {self.gap:.1f}px "
            f"from the trunk it should join -- a route hanging in open space"
        )


def _point_to_polyline_distance(
    p: tuple[float, float], pts: Sequence[tuple[float, float]]
) -> float:
    """Shortest distance from point *p* to a polyline through *pts*."""
    px, py = p
    best = float("inf")
    for k in range(len(pts) - 1):
        ax, ay = pts[k]
        bx, by = pts[k + 1]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq == 0.0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5)
    return best


def _merge_entry_port(graph: MetroGraph, merge_id: str) -> Station | None:
    """The entry-port successor of a merge junction, if any."""
    for e in graph.edges_from(merge_id):
        port = graph.ports.get(e.target)
        if port and port.is_entry:
            return graph.stations.get(e.target)
    return None


def check_merge_branches_meet_trunk(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[MergeBranchHang]:
    """Return merge feeders whose route ends disconnected from the trunk.

    A reconvergence merge junction has at least two predecessors and a single
    entry-port successor.  Each feeder route must terminate where it joins the
    converging structure: a branch lands on the trunk's channel, the trunk
    reaches the entry port.  A feeder whose endpoint is farther than
    :data:`_MERGE_BRANCH_HANG_TOL` from every sibling feeder's path, the merge
    junction, and the entry port is a stub hanging in open space -- the
    symptom of the branch drop level disagreeing with the trunk's real channel.
    """
    by_key = {(r.edge.source, r.edge.target, r.line_id): r for r in routes}
    violations: list[MergeBranchHang] = []
    for merge_id in graph.junctions:
        feeders = list(graph.edges_to(merge_id))
        if len(feeders) < 2:
            continue
        entry_port = _merge_entry_port(graph, merge_id)
        if entry_port is None:
            continue
        merge_st = graph.stations.get(merge_id)
        if merge_st is None:
            continue
        polylines: list[tuple[Edge, list[tuple[float, float]]]] = []
        for e in feeders:
            r = by_key.get((e.source, e.target, e.line_id))
            if r is not None and len(r.points) >= 2:
                polylines.append((e, _route_render_points(r, offsets)))
        for edge, pts in polylines:
            end = pts[-1]
            d_merge = ((end[0] - merge_st.x) ** 2 + (end[1] - merge_st.y) ** 2) ** 0.5
            d_entry = (
                (end[0] - entry_port.x) ** 2 + (end[1] - entry_port.y) ** 2
            ) ** 0.5
            d_sibling = min(
                (
                    _point_to_polyline_distance(end, other)
                    for other_edge, other in polylines
                    if other_edge is not edge
                ),
                default=float("inf"),
            )
            gap = min(d_merge, d_entry, d_sibling)
            if gap > _MERGE_BRANCH_HANG_TOL:
                violations.append(
                    MergeBranchHang(
                        merge_id=merge_id,
                        line_id=edge.line_id,
                        source=edge.source,
                        endpoint=end,
                        gap=gap,
                    )
                )
    return violations


# A routed endpoint farther than this from every real anchor (a station/
# port/junction marker or another route's path) reads as a path hanging in
# open space rather than one terminating on the structure it serves.  Two
# corner radii of slack covers the endpoint's turn-in arc and the per-line
# bundle offset that fans it off the marker; a genuine hang (a desynced drop
# level, a stub that never reaches its trunk) is an order of magnitude larger.
_HANGING_ROUTE_TOL = 2 * CURVE_RADIUS


@dataclass(frozen=True)
class HangingRoute:
    """A routed path whose endpoint terminates disconnected from any anchor.

    Every route must begin and end on a real anchor: a station, port, or
    junction marker, or a point on another route it legitimately joins (a
    bundle mate, a branch onto a trunk, a peel-off).  An endpoint farther than
    :data:`_HANGING_ROUTE_TOL` from all of those is a stub hanging in open
    space -- the universal symptom underneath the family-specific endpoint
    desyncs (merge branches, rail stubs).  ``which`` names the offending end
    (``"source"`` or ``"target"``); ``gap`` is the distance to the nearest
    anchor.
    """

    source: str
    target: str
    line_id: str
    which: str
    endpoint: tuple[float, float]
    gap: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"route {self.source!r}->{self.target!r} on {self.line_id!r}: "
            f"{self.which} endpoint "
            f"({self.endpoint[0]:.1f},{self.endpoint[1]:.1f}) is {self.gap:.1f}px "
            f"from the nearest station, port, junction, or joining route -- "
            f"a path hanging in open space"
        )


def check_no_hanging_routes(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> list[HangingRoute]:
    """Return routes whose endpoints terminate disconnected from any anchor.

    The general backstop for the hanging-path defect class: it asserts the
    property every family-specific endpoint guard encodes locally -- that a
    routed segment terminates at a real anchor -- once, over every route.  An
    endpoint is anchored when it lies within :data:`_HANGING_ROUTE_TOL` of a
    station/port/junction marker (all live in ``graph.stations``, including
    terminus/file-icon hosts) or of any *other* route's rendered path (the
    join point of a bundle mate, a branch onto its trunk, or a peel-off).

    Rail-mode endpoints are skipped: a rail stub terminates on its rail rather
    than a marker, a distinct idiom with its own dedicated stub tracking.  This
    complements -- and does not replace -- the precise family-specific checks
    such as :func:`check_merge_branches_meet_trunk`; they stay as sharper
    diagnostics.
    """
    anchor_xy = [(st.x, st.y) for st in graph.stations.values()]
    rendered = [(r, _route_render_points(r, offsets)) for r in routes]
    polylines = [(r, pts) for r, pts in rendered if len(pts) >= 2]

    violations: list[HangingRoute] = []
    for r, pts in rendered:
        if len(pts) < 2:
            continue
        ends = (("source", pts[0], r.edge.source), ("target", pts[-1], r.edge.target))
        for which, end, node in ends:
            if graph.station_is_rail(node):
                continue
            d_marker = min(
                (((end[0] - x) ** 2 + (end[1] - y) ** 2) ** 0.5 for x, y in anchor_xy),
                default=float("inf"),
            )
            if d_marker <= _HANGING_ROUTE_TOL:
                continue
            d_join = min(
                (
                    _point_to_polyline_distance(end, other_pts)
                    for other, other_pts in polylines
                    if other is not r
                ),
                default=float("inf"),
            )
            if d_join <= _HANGING_ROUTE_TOL:
                continue
            violations.append(
                HangingRoute(
                    source=r.edge.source,
                    target=r.edge.target,
                    line_id=r.line_id,
                    which=which,
                    endpoint=end,
                    gap=min(d_marker, d_join),
                )
            )
    return violations


@dataclass(frozen=True)
class PerpExitLeadInOverdip:
    """A cross-column perp-exit lead-in clears a section it never passes under.

    The exit-side down-leg of
    :func:`~nf_metro.layout.routing.inter_section_handlers._route_perp_exit_over`'s
    ``crosses_box`` shape drops at the exit X and runs only to the inter-column
    gap, so it should clear just the sections under that span.  A leg seated
    below (BOTTOM exit) or above (TOP exit) a section whose X extent it never
    overlaps loops needlessly to the canvas edge around a box it doesn't cross.
    """

    source: str
    target: str
    section_id: str
    leg_y: float
    section_edge: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"perp-exit lead-in {self.source!r}->{self.target!r} corridor at "
            f"y={self.leg_y:.1f} overshoots section {self.section_id!r} "
            f"(edge y={self.section_edge:.1f}) it never passes under"
        )


def check_perp_exit_over_leadin_clears_only_spanned_sections(
    graph: MetroGraph, routes: list[RoutedPath]
) -> list[PerpExitLeadInOverdip]:
    """Return crosses_box perp-exit lead-ins overshooting an un-spanned section.

    ``_route_perp_exit_over`` drops out of a TOP/BOTTOM exit, crosses to the
    inter-column gap, then climbs to the entry-side corridor.  The first leg --
    from the exit X to the gap -- need only clear the source column's sections;
    if it dips below (BOTTOM exit) or rises above (TOP exit) a section in a
    different column it never travels under, the route loops to the canvas edge.
    """
    tol = COORD_TOLERANCE
    violations: list[PerpExitLeadInOverdip] = []
    for r in routes:
        src_port = graph.ports.get(r.edge.source)
        tgt_port = graph.ports.get(r.edge.target)
        if (
            src_port is None
            or tgt_port is None
            or src_port.is_entry
            or not tgt_port.is_entry
            or src_port.side not in (PortSide.TOP, PortSide.BOTTOM)
            or tgt_port.side not in (PortSide.TOP, PortSide.BOTTOM)
            or not r.is_inter_section
            or len(r.points) != 6
        ):
            continue
        src_sec = resolve_section(graph, graph.stations.get(r.edge.source))
        if src_sec is None:
            continue
        (sx, _sy), (cx, cy), (gx, _gy), *_rest = r.points
        if abs(cx - sx) > tol:  # not the exit-drop-first shape
            continue
        is_bottom = src_port.side == PortSide.BOTTOM
        leg_lo, leg_hi = sorted((sx, gx))
        for s in graph.sections.values():
            if s.id == src_sec.id or s.grid_row != src_sec.grid_row or s.bbox_h <= 0:
                continue
            s_lo, s_hi = s.bbox_x, s.bbox_x + s.bbox_w
            if leg_hi > s_lo + tol and s_hi > leg_lo + tol:
                continue  # leg passes under this section; clearing it is expected
            if is_bottom and cy > s.bbox_y + s.bbox_h + tol:
                violations.append(
                    PerpExitLeadInOverdip(
                        r.edge.source, r.edge.target, s.id, cy, s.bbox_y + s.bbox_h
                    )
                )
            elif not is_bottom and cy < s.bbox_y - tol:
                violations.append(
                    PerpExitLeadInOverdip(
                        r.edge.source, r.edge.target, s.id, cy, s.bbox_y
                    )
                )
    return violations


@dataclass(frozen=True)
class RightEntryNeedlessDive:
    """A RIGHT entry fed from above loops below the box though a drop-in fits.

    :func:`~nf_metro.layout.routing.inter_section_handlers._route_right_entry_cross_row`
    drops straight down the source's outward side into the RIGHT port when that
    descent column is clear past the target's right edge, reserving the
    around-below loop for cases where the direct drop is obstructed.  A route
    that dives below the target box while the drop-in column was clear took the
    loop needlessly.
    """

    source: str
    target: str
    dive_y: float
    box_bottom: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"RIGHT-entry feed {self.source!r}->{self.target!r} dives to "
            f"y={self.dive_y:.1f} below target box bottom {self.box_bottom:.1f} "
            f"though a clear drop-in down the outward side reaches the entry Y"
        )


def check_right_entry_drop_in_when_clear(
    graph: MetroGraph, routes: list[RoutedPath]
) -> list[RightEntryNeedlessDive]:
    """Return RIGHT-entry-from-above routes that loop below a clear drop-in.

    A feed from a higher row into a RIGHT entry whose source already sits past
    the target's right edge can drop straight down the source's outward side to
    the entry Y; the descent X the route leads out to (``points[1].x``) and the
    router's own :func:`_right_entry_drop_in_is_clear` decide whether that drop
    is unobstructed.  A route diving below the target box while that drop-in was
    clear is a needless around-below loop.
    """
    from nf_metro.layout.routing.inter_section_handlers import (
        _right_entry_drop_in_is_clear,
    )

    tol = COORD_TOLERANCE
    violations: list[RightEntryNeedlessDive] = []
    for r in routes:
        tgt_port = graph.ports.get(r.edge.target)
        if (
            tgt_port is None
            or not tgt_port.is_entry
            or tgt_port.side is not PortSide.RIGHT
            or not r.is_inter_section
            or len(r.points) < 2
        ):
            continue
        src_sec = resolve_section(graph, graph.stations.get(r.edge.source))
        tgt_sec = resolve_section(graph, graph.stations.get(r.edge.target))
        if (
            src_sec is None
            or tgt_sec is None
            or src_sec.grid_row is None
            or tgt_sec.grid_row is None
            or src_sec.grid_row >= tgt_sec.grid_row
            or tgt_sec.bbox_h <= 0
        ):
            continue
        box_bottom = tgt_sec.bbox_y + tgt_sec.bbox_h
        dive_y = max(y for _, y in r.points)
        if dive_y <= box_bottom + tol:
            continue
        src_station = graph.stations.get(r.edge.source)
        tgt_station = graph.stations.get(r.edge.target)
        if src_station is None or tgt_station is None:
            continue
        corner_x = r.points[1][0]
        if _right_entry_drop_in_is_clear(graph, src_station, tgt_station, corner_x):
            violations.append(
                RightEntryNeedlessDive(r.edge.source, r.edge.target, dive_y, box_bottom)
            )
    return violations


@dataclass
class UndeclaredGapChannel:
    """A vertical inter-section leg sits in a gap with no :class:`GapSlot`.

    :func:`~nf_metro.layout.routing.normalize._materialize_gap_slots` only
    re-stacks legs a handler declared, so an in-gap leg without a covering slot
    is left at its raw routing X -- the overlap the materialization exists to
    resolve.  A handler that emits a gap channel must call
    :meth:`RoutedPath.declare_gap_slot` for it.
    """

    line_id: str
    edge: tuple[str, str]
    x: float
    lo_col: int
    down: bool

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        d = "down" if self.down else "up"
        return (
            f"undeclared gap channel: line {self.line_id!r} "
            f"({self.edge[0]}->{self.edge[1]}) runs {d} at x={self.x:.1f} in "
            f"gap (cols {self.lo_col},{self.lo_col + 1}) with no declared GapSlot"
        )


def check_gap_channels_materialized(
    graph: MetroGraph,
    routes: list[RoutedPath],
) -> list[UndeclaredGapChannel]:
    """Return inter-section gap channels a handler placed but did not declare.

    Every non-exempt inter-section route's vertical leg that lands inside an
    inter-column gap must carry a matching :class:`GapSlot`, so
    :func:`_materialize_gap_slots` re-stacks it concentrically.  A leg in a gap
    with no slot of the same low column and direction escaped materialization.
    """
    out: list[UndeclaredGapChannel] = []
    for rp in routes:
        if not rp.is_inter_section or rp.normalize_exempt:
            continue
        declared = {(s.gap_lo_col, s.direction is Direction.D) for s in rp.gap_slots}
        for _k, x, y_lo, y_hi, down in iter_vertical_segments(rp):
            match = gap_lo_for_x(graph, x, y_lo, y_hi)
            if match is None:
                continue
            lo, _row = match
            if (lo, down) not in declared:
                out.append(
                    UndeclaredGapChannel(
                        line_id=rp.line_id,
                        edge=(rp.edge.source, rp.edge.target),
                        x=x,
                        lo_col=lo,
                        down=down,
                    )
                )
    return out


@dataclass
class UndeclaredTrunk:
    """An inter-section route's horizontal bypass trunk carries no :class:`TrunkSlot`.

    :func:`~nf_metro.layout.routing.normalize._materialize_trunk_slots` only fans
    trunks a handler declared, so an undeclared trunk is excluded from its gap's
    band and left fused on a sibling at its raw routing Y -- the overlap the
    materialization exists to resolve.  A handler that emits a U-shaped bypass
    must route through :func:`_route_inter_section` (which calls
    :func:`_declare_trunk`) so its trunk is annotated.
    """

    line_id: str
    edge: tuple[str, str]
    y: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"undeclared trunk: line {self.line_id!r} "
            f"({self.edge[0]}->{self.edge[1]}) runs a horizontal bypass trunk at "
            f"y={self.y:.1f} with no declared TrunkSlot"
        )


def check_trunks_declared(routes: list[RoutedPath]) -> list[UndeclaredTrunk]:
    """Return inter-section horizontal trunks a handler placed but did not declare.

    Every inter-section route carrying a U-shaped bypass trunk must annotate it
    with a :class:`TrunkSlot` so :func:`_materialize_trunk_slots` can fan it into
    its gap's concentric band.  A trunk on a route with no slot escaped the
    declaration chokepoint and would be left at its raw Y.
    """
    out: list[UndeclaredTrunk] = []
    for rp in routes:
        if not rp.is_inter_section or rp.trunk_slot is not None:
            continue
        trunks = list(iter_horizontal_trunks(rp))
        if trunks:
            out.append(
                UndeclaredTrunk(
                    line_id=rp.line_id,
                    edge=(rp.edge.source, rp.edge.target),
                    y=trunks[0][1].y,
                )
            )
    return out


@dataclass
class PeeloffBundleCrossing:
    """A peel-off bundle into a LEFT entry port braids instead of nesting.

    Lines riding one shared bypass trunk that rise into a common LEFT entry port
    must turn in concentrically: the riser peel-x (and the port-slot Y) ordered
    by trunk depth.  A member whose realized peel-x is not the slot its trunk
    depth earns rises across the lines stacked with it, crossing them just
    before the port.
    """

    port_id: str
    line_id: str
    peel_x: float
    expected_peel_x: float

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        return (
            f"peel-off bundle into port {self.port_id!r}: line {self.line_id!r} "
            f"rises at peel-x {self.peel_x:.1f} but its trunk depth earns the "
            f"slot at {self.expected_peel_x:.1f} (the bundle braids into the port)"
        )


def check_peeloff_concentric(
    graph: MetroGraph, routes: list[RoutedPath]
) -> list[PeeloffBundleCrossing]:
    """Return peel-off bundles that braid into a LEFT entry port.

    Every contiguous concentric peel-off bundle - lines sharing one bypass trunk
    rising into a common LEFT entry port - must have its riser peel-x and
    port-slot Y ordered by trunk depth so the bundle nests crossing-free into the
    port.  A member off its depth-earned slot rises across the lines stacked with
    it, braiding the bundle just before the port.  The ordering is set up front by
    ``_convergence_line_order`` (riser peel-x) and ``_order_convergence_entry_ports``
    (port slots), so the bundle nests through the standard layout path.
    """
    out: list[PeeloffBundleCrossing] = []
    for bundle in iter_port_peeloff_bundles(routes, graph, OFFSET_STEP):
        targets = peeloff_target_slots(bundle)
        for line_id, tail in bundle.per_line.items():
            slot = targets[line_id]
            if not tail_on_slot(tail, slot):
                out.append(
                    PeeloffBundleCrossing(
                        port_id=bundle.port_id,
                        line_id=line_id,
                        peel_x=tail.peel_x,
                        expected_peel_x=slot.peel_x,
                    )
                )
    return out


class CurveInvariantError(RuntimeError):
    """A rendered route contains a bundle-curve defect.

    Covers a bundle flip (a line crosses its bundle-mate), a non-concentric
    wholesale corner (pinched arcs), or two distinct lines collapsed onto one
    channel.  Raised on the render path itself so a routing handler can never
    silently draw a defective curve, independent of ``compute_layout``'s
    ``validate`` flag.
    """


def assert_render_curve_invariants(
    graph: MetroGraph,
    routes: list[RoutedPath],
    offsets: dict[tuple[str, str], float],
) -> None:
    """Raise :class:`CurveInvariantError` if the final render routes are defective.

    Runs the bundle-curve correctness checks on the *final* ``route_edges``
    output -- the exact geometry the renderer is about to draw.  The
    stage-boundary guards in :func:`compute_layout` only run under
    ``validate=True`` (off for ``nf-metro render``), so a handler that builds a
    bundle with an inconsistent fan (flip), a hand-picked corner radius
    (non-concentric), or a collapsed channel could reach the canvas unchecked.
    This closes that gap: every render path routes through here, so such a
    curve aborts the render with a message naming the offending edge instead of
    being drawn.

    It is a **backstop**, not the primary correctness mechanism.  Every
    inter-section route is built from a centreline through the bundle builder
    (see ``docs/dev/inter_section_dispatch.md``), which makes a flip, a pinch,
    or a collinear overlay impossible by construction.  In normal operation
    these checks never fire; a failure means a genuinely new, un-tabled shape
    reached the renderer built some other way, and the fix is to route it
    through the builder too -- not to relax the check.

    Set ``NF_METRO_ALLOW_BAD_CURVES=1`` to downgrade to a warning (debugging a
    work-in-progress handler only; not a supported render mode).

    A layout that bridges a perpendicular connection across grid columns (a
    ``direction:`` override -- explicit or inferred -- that feeds a section's
    perpendicular entry/drop from outside its own column) also downgrades to a
    warning.  The run/trunk is held in its bbox, but the multi-line bundle
    through such a forced-perpendicular drop is an unsupported shape the builder
    cannot make clean; the render proceeds best-effort and the warning names the
    actionable fix rather than aborting.
    """
    named_checks: list[tuple[str, Sequence[_HasMessage]]] = [
        (
            "bundle order (line crosses its bundle-mate)",
            check_bundle_order_preserved(routes),
        ),
        (
            "non-concentric bundle corner",
            check_concentric_bundle_corners(graph, routes, offsets),
        ),
        (
            "collinear distinct lines",
            check_no_collinear_distinct_lines(graph, routes, offsets),
        ),
        (
            "intra-section collinear distinct lines",
            check_intra_section_collinear_distinct_lines(graph, routes, offsets),
        ),
        (
            "collinear distinct diagonals",
            check_no_collinear_distinct_diagonals(graph, routes, offsets),
        ),
        (
            "same-line parallel descents",
            check_no_same_line_parallel_descents(graph, routes, offsets),
        ),
        (
            "merge branch hanging in open space",
            check_merge_branches_meet_trunk(graph, routes, offsets),
        ),
        (
            "route hanging in open space",
            check_no_hanging_routes(graph, routes, offsets),
        ),
        (
            "undeclared gap channel",
            check_gap_channels_materialized(graph, routes),
        ),
        (
            "undeclared trunk",
            check_trunks_declared(routes),
        ),
        (
            "peel-off bundle braids into port",
            check_peeloff_concentric(graph, routes),
        ),
    ]
    messages: list[str] = []
    for label, violations in named_checks:
        if violations:
            first = violations[0]
            extra = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
            messages.append(f"[{label}] {first.message()}{extra}")
    if not messages:
        return

    detail = "\n  ".join(messages)
    msg = (
        "render aborted: a routing handler produced defective bundle curves. "
        "The handler for the named edge built the bundle with an inconsistent "
        "fan (flip), a hand-picked corner radius (non-concentric), or a "
        "collapsed channel. Size wholesale corners via "
        "concentric_corner_radius_at and fan every leg consistently.\n  "
        f"{detail}"
    )
    if os.environ.get("NF_METRO_ALLOW_BAD_CURVES"):
        warnings.warn(msg, stacklevel=2)
        return
    bridged = sorted(graph._cross_column_perp_bridges)
    if bridged:
        warnings.warn(
            f"section(s) {', '.join(bridged)} have a perpendicular connection "
            f"bridged across grid columns; routing draws a best-effort lead-in "
            f"and the bundle geometry through the drop may be imperfect. "
            f"{FLOW_ALIGNED_PORT_ADVICE}\n  {detail}",
            stacklevel=2,
        )
        return
    raise CurveInvariantError(msg)


__all__ = [
    "BundleOrderViolation",
    "CollinearOverlapViolation",
    "DiagonalOverlapViolation",
    "CurveInvariantError",
    "FanoutTailGap",
    "HangingRoute",
    "MergeBranchHang",
    "MergePortApproachViolation",
    "NonConcentricCornerViolation",
    "PartialBranchGapViolation",
    "PeeloffBundleCrossing",
    "PerpEntryBoundaryViolation",
    "PerpExitLeadInOverdip",
    "RightEntryNeedlessDive",
    "SameLineParallelRun",
    "Side",
    "StackedElbowGraze",
    "UndeclaredGapChannel",
    "UndeclaredTrunk",
    "check_bundle_order_preserved",
    "check_gap_channels_materialized",
    "check_trunks_declared",
    "check_concentric_bundle_corners",
    "check_fanout_tail_join",
    "check_merge_branches_meet_trunk",
    "check_merge_port_approach_side",
    "check_no_hanging_routes",
    "check_no_collinear_distinct_lines",
    "check_no_collinear_distinct_diagonals",
    "check_no_same_line_parallel_descents",
    "check_peeloff_concentric",
    "check_perp_entry_boundary_consistent",
    "check_perp_exit_over_leadin_clears_only_spanned_sections",
    "check_right_entry_drop_in_when_clear",
    "bypass_horizontal_targets",
    "check_stacked_elbow_clearance",
    "check_partial_branch_offset_gaps",
    "classify_merge_port_feeders",
    "distinct_offset_levels",
    "fanout_junctions",
    "is_independent_fan_branch",
]
