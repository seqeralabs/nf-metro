"""Constructive bundle-curve builder.

The single entry point for turning a multi-line bundle around corners.  A
caller describes only the bundle's **centreline** -- a polyline of axis-aligned
vertices -- plus each line's signed perpendicular offset from it.
:func:`build_concentric_bundle` emits each line as a rigid parallel offset of
that centreline, sizing every corner via :func:`concentric_corner_radius_at`.

Why this exists: hand-assembling per-line ``points`` and ``curve_radii`` is the
most common source of broken renders.  A handler can offset one leg the wrong
way (the bundle flips and the lines cross) or hand-pick a corner radius that
nests non-concentrically (the bundle pinches through the bend).  Both are
*impossible* here by construction:

* Each line is the same centreline shifted by a constant perpendicular
  distance, so the lines keep a constant side-of-travel order -- no flip.
* Radii are derived from the turn geometry, never supplied by the caller -- so
  no hand-picked sign.

Prefer this over building bundle routes by hand: the render-path curve guard
in ``invariants`` is a backstop for paths built another way, not the mechanism
that keeps these correct.
"""

from __future__ import annotations

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.corners import concentric_corner_radius_at
from nf_metro.parser.model import Edge

_Vec = tuple[float, float]


def _axis_unit(a: _Vec, b: _Vec) -> _Vec:
    """Axis-aligned unit travel vector from *a* to *b*.

    Raises ``ValueError`` for a diagonal leg: the builder only fans
    axis-aligned centrelines, where a parallel offset is itself axis-aligned.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) > COORD_TOLERANCE and abs(dy) > COORD_TOLERANCE:
        raise ValueError(
            f"centreline leg {a}->{b} is diagonal; build_concentric_bundle "
            "fans axis-aligned centrelines only"
        )
    if abs(dx) >= abs(dy):
        return (1.0 if dx >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy >= 0 else -1.0)


def _right_normal(t: _Vec) -> _Vec:
    """Right-hand normal of travel *t* in screen coords (y grows downward)."""
    return (-t[1], t[0])


def build_concentric_bundle(
    members: list[tuple[Edge, str, float]],
    centerline: list[_Vec],
    base_radius: float,
    *,
    min_radius: float | None = None,
    is_inter_section: bool = True,
    normalize_exempt: bool = True,
) -> list[RoutedPath]:
    """Fan a bundle of lines along a shared axis-aligned *centerline*.

    Parameters
    ----------
    members
        ``(edge, line_id, signed_offset)`` per line.  ``signed_offset`` is the
        line's perpendicular displacement from the centreline (right-hand
        normal positive).  Centre the bundle on the centreline so each line's
        endpoints land on the offsets the rest of the layout expects.
    centerline
        ``>= 2`` axis-aligned vertices; each consecutive pair must differ in
        exactly one axis.
    base_radius
        Reference-line corner radius (the centreline's own radius).
    min_radius
        Optional floor for inside-of-turn arcs (see
        :func:`~nf_metro.layout.routing.corners.reference_anchored_radius`).

    Returns
    -------
    One :class:`RoutedPath` per member, each a parallel offset of *centerline*
    with concentric corner radii.  ``offsets_applied`` is set: the per-line
    offset is baked into the points, not left to the renderer's heuristic.
    """
    if len(centerline) < 2:
        raise ValueError("centerline needs at least two vertices")

    legs = [
        _axis_unit(centerline[i], centerline[i + 1]) for i in range(len(centerline) - 1)
    ]
    normals = [_right_normal(t) for t in legs]

    routes: list[RoutedPath] = []
    for edge, line_id, s in members:
        points: list[_Vec] = []
        radii: list[float] = []
        for vi, (cx, cy) in enumerate(centerline):
            if vi == 0:
                nx, ny = normals[0]
            elif vi == len(centerline) - 1:
                nx, ny = normals[-1]
            else:
                # Interior corner: the parallel offset of the incoming leg and
                # of the outgoing leg meet here.  One leg is horizontal (its
                # normal shifts Y) and one vertical (its normal shifts X), so
                # summing the two normals selects the right shift per axis.
                rn_in, rn_out = normals[vi - 1], normals[vi]
                nx, ny = rn_in[0] + rn_out[0], rn_in[1] + rn_out[1]
                # ``s * nx`` is this line's signed X displacement from the
                # centreline at this corner -- the input the concentric-radius
                # helper derives the turn from.
                radii.append(
                    concentric_corner_radius_at(
                        centerline[vi - 1],
                        centerline[vi],
                        centerline[vi + 1],
                        s * nx,
                        base_radius,
                        min_radius=min_radius,
                    )
                )
            points.append((cx + s * nx, cy + s * ny))

        routes.append(
            RoutedPath(
                edge=edge,
                line_id=line_id,
                points=points,
                is_inter_section=is_inter_section,
                curve_radii=radii or None,
                normalize_exempt=normalize_exempt,
                offsets_applied=True,
            )
        )
    return routes
