"""Low-level geometric primitives shared by layout passes and validation guards."""

from __future__ import annotations

import bisect
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


class _HasXY(Protocol):
    x: float
    y: float


@dataclass(frozen=True)
class Axis:
    """A coordinate axis (``"x"`` or ``"y"``) and its spacing unit."""

    name: str
    step: float

    def get(self, station: _HasXY) -> float:
        return getattr(station, self.name)

    def set(self, station: _HasXY, value: float) -> None:
        setattr(station, self.name, value)


@dataclass(frozen=True)
class AxisFrame:
    """A section's layer (``primary``) and track (``secondary``) axes.

    LR/RL place layers along X and stack lines along Y; TB transposes the two.
    ``primary_sign`` is ``-1`` for RL, which runs the primary axis in reverse
    (mirrored by ``single_section._mirror_primary``), else ``+1``.
    """

    primary: Axis
    secondary: Axis
    primary_sign: float

    @staticmethod
    def axes_for_direction(direction: str) -> tuple[str, str]:
        """``(primary, secondary)`` axis names for *direction*, spacing-free.

        A vertical flow (TB/BT) runs its layers down Y and stacks lines along
        X; a horizontal flow (LR/RL) does the reverse.  Exposed separately from
        :meth:`for_direction` so passes can ask which axis is the flow axis (or
        the lane axis) without having spacings to hand.
        """
        return ("y", "x") if direction in ("TB", "BT") else ("x", "y")

    @classmethod
    def for_direction(
        cls, direction: str, x_spacing: float, y_spacing: float
    ) -> AxisFrame:
        primary, secondary = cls.axes_for_direction(direction)
        step = {"x": x_spacing, "y": y_spacing}
        sign = -1.0 if direction == "RL" else 1.0
        return cls(Axis(primary, step[primary]), Axis(secondary, step[secondary]), sign)


def lanes_run_along_y(direction: str) -> bool:
    """``True`` when a section stacks its lines (the secondary/lane axis) on Y.

    Row-level inter-section passes align the Y axis: row trunk-Y alignment, the
    shared row Y-grid, top-aligning row-mates.  A horizontal (LR/RL) section's
    lanes are Y-separated, so it is a first-class member of that machinery.  A
    vertical (TB/BT) section runs its flow down Y and separates lines along X,
    so it has no row-Y lane grid to share and the row passes leave its Y alone.
    """
    return AxisFrame.axes_for_direction(direction)[1] == "y"


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
