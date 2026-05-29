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

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    horizontal_direction,
    vertical_direction,
)

# Segments shorter than this are sub-pixel artefacts of per-line
# offsets and carry no meaningful direction of travel.
_MIN_SEGMENT_LENGTH = 1.0


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


__all__ = [
    "BundleOrderViolation",
    "FanoutTailGap",
    "Side",
    "check_bundle_order_preserved",
    "check_fanout_tail_join",
    "fanout_junctions",
]
