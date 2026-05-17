"""Low-level geometric primitives shared by layout passes and validation guards."""

from __future__ import annotations

import bisect
from collections.abc import Iterator


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
