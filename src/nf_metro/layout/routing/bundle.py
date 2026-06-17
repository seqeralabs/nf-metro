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
    n_legs = len(centerline) - 1
    if n_legs < 1:
        raise ValueError("centerline needs at least two vertices")
    return _fan_bundle(
        [(edge, line_id, [s] * n_legs) for edge, line_id, s in members],
        centerline,
        base_radius,
        min_radius=min_radius,
        is_inter_section=is_inter_section,
        normalize_exempt=normalize_exempt,
    )


def build_tapered_bundle(
    members: list[tuple[Edge, str, float, float]],
    centerline: list[_Vec],
    transition_leg: int,
    base_radius: float,
    *,
    min_radius: float | None = None,
    is_inter_section: bool = True,
    normalize_exempt: bool = True,
) -> list[RoutedPath]:
    """Fan a *tapering* bundle: a per-line source offset and target offset.

    A rigid bundle keeps one perpendicular offset on every leg; a tapering one
    carries a wider spread on its source side than its target side (or vice
    versa), so it cannot.  Each line holds two offsets -- ``src_offset`` on the
    legs before *transition_leg*, ``tgt_offset`` on the legs at and after it --
    and the offset switches at the single vertex where ``transition_leg``
    begins.  That vertex becomes a *transition corner*: one flanking leg is
    fanned by the source offset, the other by the target offset.  Its arcs do
    not share a centre (the two legs are fanned by different amounts), so it is
    sized to the fanned turning leg via
    :func:`~nf_metro.layout.routing.corners.concentric_corner_radius_at`.  A
    *wholesale* corner -- both flanking legs carrying one offset -- is genuinely
    concentric, as in :func:`build_concentric_bundle`.

    Parameters
    ----------
    members
        ``(edge, line_id, src_offset, tgt_offset)`` per line.  ``src_offset ==
        tgt_offset`` for every line reduces to the rigid bundle, byte-identical
        to :func:`build_concentric_bundle`.
    centerline
        ``>= 2`` axis-aligned vertices; each consecutive pair differs in exactly
        one axis.
    transition_leg
        Index of the first leg that carries ``tgt_offset`` (``1`` for the common
        inter-section L-shape: the source-side leg fans by ``src_offset``, the
        channel and target-side legs by ``tgt_offset``).
    base_radius
        Reference-line corner radius.
    min_radius
        Optional floor for inside-of-turn arcs.
    """
    n_legs = len(centerline) - 1
    if n_legs < 1:
        raise ValueError("centerline needs at least two vertices")
    if not 0 <= transition_leg <= n_legs:
        raise ValueError(f"transition_leg {transition_leg} out of range [0, {n_legs}]")
    return _fan_bundle(
        [
            (
                edge,
                line_id,
                [src if leg < transition_leg else tgt for leg in range(n_legs)],
            )
            for edge, line_id, src, tgt in members
        ],
        centerline,
        base_radius,
        min_radius=min_radius,
        is_inter_section=is_inter_section,
        normalize_exempt=normalize_exempt,
    )


def _fan_bundle(
    members: list[tuple[Edge, str, list[float]]],
    centerline: list[_Vec],
    base_radius: float,
    *,
    min_radius: float | None,
    is_inter_section: bool,
    normalize_exempt: bool,
) -> list[RoutedPath]:
    """Emit one route per member from explicit per-leg offsets.

    ``members`` is ``(edge, line_id, leg_offsets)`` with one signed
    perpendicular offset per centreline leg.  A constant offset across all legs
    is a rigid bundle; an offset that switches between legs tapers.  At every
    interior vertex only the *vertical* leg displaces X, so its signed X
    displacement is the input the concentric-radius helper derives the turn
    from -- a wholesale corner (both legs equal) lands genuinely concentric, a
    transition corner (legs differ) is sized to the turning leg.
    """
    legs = [
        _axis_unit(centerline[i], centerline[i + 1]) for i in range(len(centerline) - 1)
    ]
    normals = [_right_normal(t) for t in legs]

    routes: list[RoutedPath] = []
    for edge, line_id, offs in members:
        points: list[_Vec] = []
        radii: list[float] = []
        for vi, (cx, cy) in enumerate(centerline):
            if vi == 0:
                o, (nx, ny) = offs[0], normals[0]
                px, py = cx + o * nx, cy + o * ny
            elif vi == len(centerline) - 1:
                o, (nx, ny) = offs[-1], normals[-1]
                px, py = cx + o * nx, cy + o * ny
            else:
                # Interior corner: the incoming and outgoing legs meet here.
                # One leg is horizontal (its normal shifts Y) and one vertical
                # (its normal shifts X), so each axis takes the shift from the
                # leg that bends it.  ``vertical_dx`` is this line's signed X
                # displacement from the centreline at this corner -- the input
                # the concentric-radius helper derives the turn from.
                o_in, o_out = offs[vi - 1], offs[vi]
                n_in, n_out = normals[vi - 1], normals[vi]
                vertical_dx = o_in * n_in[0] + o_out * n_out[0]
                px = cx + vertical_dx
                py = cy + o_in * n_in[1] + o_out * n_out[1]
                radii.append(
                    concentric_corner_radius_at(
                        centerline[vi - 1],
                        centerline[vi],
                        centerline[vi + 1],
                        vertical_dx,
                        base_radius,
                        min_radius=min_radius,
                    )
                )
            points.append((px, py))

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
