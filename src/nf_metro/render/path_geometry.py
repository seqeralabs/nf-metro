"""Shared render-time path geometry derived from frozen route decisions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from nf_metro.layout.phases.guards import SettledRouteValidationError
from nf_metro.layout.routing.common import (
    Direction,
    RoutedPath,
    SourceTurnout,
    segment_direction,
)
from nf_metro.layout.routing.corners import desired_curve_radii

Point = tuple[float, float]

_INCOMING_X_SIGNS: dict[Direction, float] = {Direction.R: 1.0, Direction.L: -1.0}


def _incoming_x_sign(incoming: Direction) -> float:
    """The X direction a turnout's incoming leg runs along.

    A turnout is only drawn from a horizontal approach, so its incoming tangent
    moves along X alone and the corner's Y carries through unchanged.
    """
    sign = _INCOMING_X_SIGNS.get(incoming)
    if sign is None:
        raise SettledRouteValidationError(
            f"source turnout cannot be approached along {incoming.name}"
        )
    return sign


def materialize_source_turnout_paths(
    routes: Sequence[RoutedPath],
    polylines: Sequence[Sequence[Point]],
    *,
    default_radius: float,
) -> tuple[list[list[Point]], list[list[float] | None], list[int]]:
    """Materialize every source turn and its terminal incoming tangent."""
    if len(routes) != len(polylines):
        raise ValueError("routes and polylines must be aligned")
    materialized = [list(points) for points in polylines]
    route_indices: defaultdict[tuple[str, str, str], list[int]] = defaultdict(list)
    for index, route in enumerate(routes):
        route_indices[route.line_id, route.edge.source, route.edge.target].append(index)

    terminal_tangents: dict[int, Point] = {}
    for route, points in zip(routes, materialized, strict=True):
        turnout = route.source_turnout
        if turnout is None or turnout.continuing_target_id is not None:
            continue
        incoming_indices = route_indices[
            route.line_id,
            turnout.incoming_source_id,
            route.edge.source,
        ]
        if len(incoming_indices) != 1:
            raise SettledRouteValidationError(
                "terminal source turnout requires one incoming member"
            )
        incoming = materialized[incoming_indices[0]]
        if len(incoming) < 2 or not points:
            raise SettledRouteValidationError(
                "terminal source turnout requires drawable runways"
            )
        dx = _incoming_x_sign(turnout.incoming_direction)
        tangent = (points[0][0] - dx * turnout.radius, points[0][1])
        if (
            segment_direction(incoming[-2], incoming[-1])
            is not turnout.incoming_direction
        ):
            raise SettledRouteValidationError(
                "terminal source turnout incoming direction disagrees"
            )
        if (
            abs(incoming[-1][0] - points[0][0]) > 1e-6
            or abs(incoming[-1][1] - points[0][1]) > 1e-6
        ):
            raise SettledRouteValidationError(
                "terminal source turnout members do not meet"
            )
        prior_tangent = terminal_tangents.get(incoming_indices[0])
        if prior_tangent is not None and prior_tangent != tangent:
            raise SettledRouteValidationError(
                "terminal source turnouts require one incoming tangent"
            )
        terminal_tangents[incoming_indices[0]] = tangent

    for incoming_index, tangent in terminal_tangents.items():
        materialized[incoming_index][-1] = tangent

    radii: list[list[float] | None] = []
    shifts: list[int] = []
    for index, route in enumerate(routes):
        points, route_radii, shift = materialize_source_turnout(
            materialized[index],
            route.curve_radii,
            route.source_turnout,
            corner_index=0,
            default_radius=default_radius,
        )
        materialized[index] = points
        radii.append(route_radii)
        shifts.append(shift)
    return materialized, radii, shifts


def materialize_source_turnout(
    points: Sequence[Point],
    curve_radii: Sequence[float] | None,
    turnout: SourceTurnout | None,
    *,
    corner_index: int,
    default_radius: float,
) -> tuple[list[Point], list[float] | None, int]:
    """Apply one frozen cross-member source curve to drawable geometry.

    ``corner_index`` is zero for a separately rendered member, where the
    incoming tangent is synthetic, and an interior index for an animation
    chain that already contains the preceding member. The returned integer is
    the segment-rank shift introduced by the synthetic prefix.
    """
    materialized = list(points)
    if turnout is None:
        return materialized, None if curve_radii is None else list(curve_radii), 0
    if not materialized or corner_index < 0 or corner_index >= len(materialized):
        raise SettledRouteValidationError(
            "source turnout corner is outside its drawable path"
        )

    segment_shift = 0
    if corner_index == 0:
        corner = materialized[0]
        dx = _incoming_x_sign(turnout.incoming_direction)
        materialized.insert(0, (corner[0] - dx * turnout.radius, corner[1]))
        corner_index = 1
        segment_shift = 1

    if corner_index <= 0 or corner_index >= len(materialized) - 1:
        raise SettledRouteValidationError(
            "source turnout requires incoming and outgoing legs"
        )
    if (
        segment_direction(materialized[corner_index - 1], materialized[corner_index])
        is not turnout.incoming_direction
        or segment_direction(materialized[corner_index], materialized[corner_index + 1])
        is not turnout.outgoing_direction
    ):
        raise SettledRouteValidationError(
            "source turnout directions disagree with drawable geometry"
        )

    desired = desired_curve_radii(materialized, default_radius=default_radius)
    if curve_radii is not None:
        start = segment_shift
        if start + len(curve_radii) > len(desired):
            raise SettledRouteValidationError(
                "source turnout leaves fewer corners than the route declares radii"
            )
        desired[start : start + len(curve_radii)] = curve_radii
    desired[corner_index - 1] = turnout.radius
    return materialized, desired, segment_shift
