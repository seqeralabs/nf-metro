"""Cardinal orientation helpers shared by route planners and emitters."""

from __future__ import annotations

from typing import TypeAlias

from nf_metro.layout.route_plan import DemandAxis, turn_handedness
from nf_metro.layout.routing.common import Direction

__all__ = (
    "direction_axis",
    "direction_vector",
    "get_point_coordinate",
    "lateral_axis",
    "turn_handedness",
)

Point: TypeAlias = tuple[float, float]
Vector: TypeAlias = tuple[int, int]


def direction_axis(direction: Direction) -> DemandAxis:
    """Return the canvas axis along which *direction* travels."""
    if direction in (Direction.R, Direction.L):
        return DemandAxis.X
    return DemandAxis.Y


def lateral_axis(direction: Direction) -> DemandAxis:
    """Return the canvas axis perpendicular to *direction*."""
    if direction_axis(direction) is DemandAxis.X:
        return DemandAxis.Y
    return DemandAxis.X


def direction_vector(direction: Direction) -> Vector:
    """Return the unit screen-space vector for *direction*."""
    return {
        Direction.R: (1, 0),
        Direction.L: (-1, 0),
        Direction.U: (0, -1),
        Direction.D: (0, 1),
    }[direction]


def get_point_coordinate(point: Point, axis: DemandAxis) -> float:
    """Read one X or Y coordinate from *point*."""
    if axis is DemandAxis.X:
        return point[0]
    if axis is DemandAxis.Y:
        return point[1]
    raise ValueError("a point coordinate requires the X or Y axis")
