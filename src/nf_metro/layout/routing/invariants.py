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

Wired into :func:`compute_layout(graph, validate=True)` via
``_guard_bundle_order_preserved`` in ``engine.py``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.routing.common import Direction, RoutedPath

# Segments shorter than this are sub-pixel artefacts of per-line
# offsets and carry no meaningful direction of travel.
_MIN_SEGMENT_LENGTH = 1.0


class Side:
    """Sentinel-string namespace: ``LEFT`` / ``RIGHT`` / ``COINCIDENT``."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    COINCIDENT = "COINCIDENT"


def _segment_direction(
    p1: tuple[float, float], p2: tuple[float, float]
) -> Direction | None:
    """Cardinal direction (STRICT off-axis tolerance); helper for unit tests."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    horizontal = abs(dx) > abs(dy) and abs(dy) <= COORD_TOLERANCE_FINE
    vertical = abs(dy) > abs(dx) and abs(dx) <= COORD_TOLERANCE_FINE
    if horizontal:
        return Direction.R if dx > 0 else Direction.L
    if vertical:
        return Direction.D if dy > 0 else Direction.U
    return None


def _left_of(tangent: Direction) -> Direction:
    """Cardinal direction 90 deg CCW from *tangent* (screen coords)."""
    return {
        Direction.R: Direction.U,
        Direction.U: Direction.L,
        Direction.L: Direction.D,
        Direction.D: Direction.R,
    }[tangent]


def _relative_side(
    a_xy: tuple[float, float],
    b_xy: tuple[float, float],
    side_direction: Direction,
) -> str:
    """LEFT iff ``(a - b) . side_direction > 0``, else RIGHT / COINCIDENT."""
    ax, ay = a_xy
    bx, by = b_xy
    if side_direction is Direction.U:
        delta = by - ay
    elif side_direction is Direction.D:
        delta = ay - by
    elif side_direction is Direction.R:
        delta = ax - bx
    elif side_direction is Direction.L:
        delta = bx - ax
    else:  # pragma: no cover - exhausted by Direction
        return Side.COINCIDENT
    if abs(delta) <= COORD_TOLERANCE_FINE:
        return Side.COINCIDENT
    return Side.LEFT if delta > 0 else Side.RIGHT


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
    before: str
    after: str
    segment_index: int = -1

    def message(self) -> str:
        """Human-readable summary suitable for the engine error message."""
        cx, cy = self.corner_xy
        return (
            f"bundle {self.edge_source!r}->{self.edge_target!r} "
            f"corner ({cx:.1f},{cy:.1f}) "
            f"in={self.in_tangent.value} out={self.out_tangent.value} "
            f"segment={self.segment_index}: "
            f"expected line {self.line_a!r} on {self.before} of "
            f"line {self.line_b!r} (matching incoming run); "
            f"observed {self.line_a!r} on {self.after} of "
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
        return Direction.R if dx > 0 else Direction.L
    return Direction.D if dy > 0 else Direction.U


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
            return BundleOrderViolation(
                edge_source=src_id,
                edge_target=tgt_id,
                line_a=a_route.line_id,
                line_b=b_route.line_id,
                corner_xy=a_p1,
                in_tangent=last_dir if last_dir is not None else Direction.R,
                out_tangent=cur_dir if cur_dir is not None else Direction.R,
                before=Side.LEFT if last_sign > 0 else Side.RIGHT,
                after=Side.LEFT if sign > 0 else Side.RIGHT,
                segment_index=k,
            )
        last_sign = sign
        last_dir = cur_dir

    return None


__all__ = [
    "BundleOrderViolation",
    "Side",
    "check_bundle_order_preserved",
]
