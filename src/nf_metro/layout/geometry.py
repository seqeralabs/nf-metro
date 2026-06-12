"""Low-level geometric primitives shared by layout passes and validation guards."""

from __future__ import annotations

import bisect
from collections.abc import Iterator


def segment_intersects_quad(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    quad: list[tuple[float, float]],
) -> bool:
    """``True`` iff the segment touches or crosses the convex *quad*.

    *quad* is four corners in order (winding either way).  Exact for a convex
    polygon: the segment hits it when an endpoint lies inside or the segment
    crosses any edge.  Used for rotated (angled) label footprints, where an
    axis-aligned bbox would overstate the diagonal strip's extent.
    """
    n = len(quad)

    def _inside(px: float, py: float) -> bool:
        sign = 0
        for i in range(n):
            ax, ay = quad[i]
            bx, by = quad[(i + 1) % n]
            cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            if cross > 1e-9:
                if sign < 0:
                    return False
                sign = 1
            elif cross < -1e-9:
                if sign > 0:
                    return False
                sign = -1
        return True

    if _inside(x1, y1) or _inside(x2, y2):
        return True

    def _ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)

    for i in range(n):
        cx, cy = quad[i]
        dx, dy = quad[(i + 1) % n]
        if _ccw(x1, y1, cx, cy, dx, dy) != _ccw(x2, y2, cx, cy, dx, dy) and _ccw(
            x1, y1, x2, y2, cx, cy
        ) != _ccw(x1, y1, x2, y2, dx, dy):
            return True
    return False


def segments_cross(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """``True`` iff open segment ``p1-p2`` properly crosses ``p3-p4``.

    Proper crossing only: shared endpoints and collinear overlaps return
    ``False`` (the two segments must straddle each other).  Used to detect when
    one routed leg passes through another rather than merely touching it.
    """

    eps = 1e-6

    def _orient(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def _straddle(d1: float, d2: float) -> bool:
        return (d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)

    return _straddle(_orient(p3, p4, p1), _orient(p3, p4, p2)) and _straddle(
        _orient(p1, p2, p3), _orient(p1, p2, p4)
    )


def segment_intersects_bbox(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky test: ``True`` iff the segment touches or crosses *bbox*.

    Exact for any segment against an axis-aligned
    ``(x_min, y_min, x_max, y_max)`` bbox.
    """
    bx_min, by_min, bx_max, by_max = bbox
    if max(x1, x2) < bx_min or min(x1, x2) > bx_max:
        return False
    if max(y1, y2) < by_min or min(y1, y2) > by_max:
        return False
    dx, dy = x2 - x1, y2 - y1
    t_min, t_max = 0.0, 1.0
    for p, q in (
        (-dx, x1 - bx_min),
        (dx, bx_max - x1),
        (-dy, y1 - by_min),
        (dy, by_max - y1),
    ):
        if abs(p) < 1e-9:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0 and t > t_min:
            t_min = t
        elif p > 0 and t < t_max:
            t_max = t
        if t_min > t_max:
            return False
    return True


class BBoxXIndex:
    """X-sorted index over labelled bboxes for O(log N + k) range queries."""

    __slots__ = ("_items", "_x_mins")

    def __init__(
        self,
        boxes: list[tuple[str, tuple[float, float, float, float]]],
    ) -> None:
        self._items = sorted(boxes, key=lambda item: item[1][0])
        self._x_mins = [item[1][0] for item in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[tuple[str, tuple[float, float, float, float]]]:
        return iter(self._items)

    def query_x_range(
        self, qx_min: float, qx_max: float
    ) -> Iterator[tuple[str, tuple[float, float, float, float]]]:
        """Yield ``(key, bbox)`` for every item whose bbox X-extent
        overlaps ``[qx_min, qx_max]``.
        """
        upper = bisect.bisect_right(self._x_mins, qx_max)
        for i in range(upper):
            key, bbox = self._items[i]
            if bbox[2] >= qx_min:
                yield key, bbox
