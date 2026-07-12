"""Concentric corner geometry for bundled metro lines.

When multiple metro lines travel as a parallel bundle and turn a corner,
each line follows a different-radius arc to maintain the bundle's visual
ordering without crossings.  Lines on the OUTSIDE of the turn get larger
radii (wider arcs), lines on the INSIDE get smaller radii (tighter arcs).

Key invariant
-------------
A line on the left of a downward-going bundle must be on TOP of the
following horizontal if the bundle turns left, but on the BOTTOM if it
turns right.  Equivalently, the line on the outside of every corner
always gets the largest radius.

All radii are computed as::

    radius = base_radius + k * offset_step

where *k* ranges from 0 (innermost) to n-1 (outermost).  The radius
is NEVER variable beyond the line's position within the bundle.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import NamedTuple

from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP
from nf_metro.layout.routing.common import Direction

# ---------------------------------------------------------------------------
# Primitive: reversed (inner/outer) offset
# ---------------------------------------------------------------------------


def resolve_curve_radii(
    points: list[tuple[float, float]],
    desired_radii: list[float] | None,
    default_radius: float = CURVE_RADIUS,
) -> list[float]:
    """Compute effective curve radii after segment-budget clamping.

    For each corner (intermediate waypoint), the desired radius is clamped
    to the available segment length on each side.  When adjacent corners
    share a segment, space is allocated proportionally based on their
    desired radii so concentric geometry is preserved.

    This is the single source of truth for radius resolution, used by both
    the routing layer (for validation) and the rendering layer (for SVG
    path construction).

    Parameters
    ----------
    points : list of (x, y) tuples
        The waypoints of the routed path.
    desired_radii : list of float or None
        Desired radius at each corner (length must equal ``len(points) - 2``
        when not None).  Falls back to *default_radius* for missing entries.
    default_radius : float
        Fallback radius when *desired_radii* is None or too short.

    Returns
    -------
    list of float
        Effective (clamped) radius for each corner.
    """
    n_corners = len(points) - 2
    if n_corners <= 0:
        return []

    effective: list[float] = []
    for i in range(1, len(points) - 1):
        corner_idx = i - 1
        desired_r = (
            desired_radii[corner_idx]
            if desired_radii and corner_idx < len(desired_radii)
            else default_radius
        )

        prev, curr, nxt = points[i - 1], points[i], points[i + 1]
        len1 = math.hypot(curr[0] - prev[0], curr[1] - prev[1])
        len2 = math.hypot(nxt[0] - curr[0], nxt[1] - curr[1])

        # Proportional allocation for segments shared between adjacent corners.
        if i > 1:
            prev_r = (
                desired_radii[i - 2]
                if desired_radii and (i - 2) < len(desired_radii)
                else default_radius
            )
            total = prev_r + desired_r
            max_len1 = len1 * desired_r / total if total > 0 else len1 / 2
        else:
            max_len1 = len1

        if i < len(points) - 2:
            next_r = (
                desired_radii[i]
                if desired_radii and i < len(desired_radii)
                else default_radius
            )
            total = desired_r + next_r
            max_len2 = len2 * desired_r / total if total > 0 else len2 / 2
        else:
            max_len2 = len2

        effective.append(min(desired_r, max_len1, max_len2))

    return effective


class CornerTangent(NamedTuple):
    """Rounded-corner tangent points for one interior vertex.

    ``before``/``after`` are where the smoothing curve leaves the incoming
    leg and rejoins the outgoing leg; ``corner`` is the vertex the curve
    bends around.  ``curved`` is False for a degenerate vertex (a
    zero-length neighbouring leg), where all three points collapse to the
    vertex itself and no curve is drawn.
    """

    curved: bool
    before: tuple[float, float]
    corner: tuple[float, float]
    after: tuple[float, float]


def curve_tangents(
    points: list[tuple[float, float]],
    resolved: list[float],
) -> list[CornerTangent]:
    """Per-interior-vertex rounded-corner tangent points.

    For each interior vertex ``i`` (``1..len(points)-2``) the smoothing
    curve leaves the incoming leg at ``before`` and rejoins the outgoing leg
    at ``after``, each a distance ``resolved[i-1]`` from the corner along the
    respective unit leg vector.

    This is the single source of tangent geometry shared by the static SVG
    renderer and the animation renderer, so a corner is rounded identically
    in both.  ``resolved`` is the clamped radius list from
    :func:`resolve_curve_radii` (length ``len(points) - 2``).
    """
    tangents: list[CornerTangent] = []
    for i in range(1, len(points) - 1):
        prev, curr, nxt = points[i - 1], points[i], points[i + 1]
        dx1, dy1 = curr[0] - prev[0], curr[1] - prev[1]
        len1 = (dx1**2 + dy1**2) ** 0.5
        dx2, dy2 = nxt[0] - curr[0], nxt[1] - curr[1]
        len2 = (dx2**2 + dy2**2) ** 0.5
        r = resolved[i - 1]
        if len1 > 0 and len2 > 0:
            before = (curr[0] - dx1 / len1 * r, curr[1] - dy1 / len1 * r)
            after = (curr[0] + dx2 / len2 * r, curr[1] + dy2 / len2 * r)
            tangents.append(CornerTangent(True, before, curr, after))
        else:
            tangents.append(CornerTangent(False, curr, curr, curr))
    return tangents


def reversed_offset(offset: float, max_offset: float) -> float:
    """Flip a line's offset within a bundle.

    Maps the outermost line (offset == max_offset) to 0 and the
    innermost line (offset == 0) to max_offset.  Used whenever a
    concentric corner swaps the spatial ordering of lines in a bundle.
    """
    return max_offset - offset


def corner_radius(
    offset: float,
    max_offset: float,
    *,
    outside: bool,
    base_radius: float = CURVE_RADIUS,
) -> float:
    """Compute the concentric arc radius for one line at a corner.

    Every concentric corner radius in the system follows::

        radius = base_radius + effective_offset

    where *effective_offset* is either the raw offset (line is on the
    outside of the turn) or the reversed offset (line is on the inside).

    Parameters
    ----------
    offset : float
        This line's offset within the bundle (0 to *max_offset*).
    max_offset : float
        Maximum offset across all lines in the bundle.
    outside : bool
        ``True`` when the line at *offset* is on the **outside** of the
        turn and therefore needs the larger radius.  ``False`` when it
        is on the inside.
    base_radius : float
        Minimum curve radius (innermost line).

    Returns
    -------
    float
        ``base_radius + offset`` when *outside* is True,
        ``base_radius + (max_offset - offset)`` when False.
    """
    eff = offset if outside else reversed_offset(offset, max_offset)
    return reference_anchored_radius(eff, base_radius)


def concentric_corner_radius(
    turn_in: tuple[float, float],
    turn_out: tuple[float, float],
    dx: float,
    base_radius: float = CURVE_RADIUS,
    *,
    min_radius: float | None = None,
) -> float:
    """Concentric arc radius for one line of a wholesale-translated 90° corner.

    When a bundle of parallel lines turns a 90° corner and the *entire* corner
    is translated per line so this line's vertical leg sits *dx* to the side of
    the reference line, the arcs share a common centre iff::

        radius = base_radius - dx * (turn_out_x - turn_in_x)

    (derived by equating every line's arc centre ``corner + radius *
    (turn_out - turn_in)``; ``turn_out_x - turn_in_x`` is always +/-1 for a real
    90° turn, and a valid bundle must fan in X since a pure-Y translation would
    overlap the vertical legs - so the X displacement alone fixes the radius).
    This is the single direction-driven entry point for nestable corners: any
    orientation - right-then-down, down-then-left, etc. - is mapped to the
    correct signed offset purely from the two travel vectors, so the same
    routine sizes every such corner regardless of compass direction.

    The arcs are genuinely concentric only when the *whole* corner translates
    together.  At a *transition* corner - one flanking leg fanned by *dx*, the
    other pinned at a fixed (e.g. port) offset - this still returns a sensibly
    nested radius (sized to the fanned leg) but the arcs do not share a centre.

    Parameters
    ----------
    turn_in, turn_out : tuple of float
        Unit travel-direction vectors into and out of the corner (axis-aligned;
        ``turn_out - turn_in`` has both components +/-1 for a real 90° turn).
    dx : float
        This line's signed X displacement from the bundle's reference line.
    base_radius : float
        Reference-line radius.
    min_radius : float or None
        Optional floor (see :func:`reference_anchored_radius`); inside-of-turn
        lines fall below *base_radius* and a deep bundle can drive it to zero.

    Returns
    -------
    float
        The concentric radius via :func:`reference_anchored_radius`.
    """
    ux = turn_out[0] - turn_in[0]
    return reference_anchored_radius(-dx * ux, base_radius, min_radius=min_radius)


def _corner_travel_units(
    prev: tuple[float, float],
    corner: tuple[float, float],
    nxt: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Axis-aligned unit travel vectors into and out of an axis-aligned corner.

    Each leg snaps to its dominant axis, so a segment carrying sub-pixel
    off-axis drift resolves to a clean cardinal unit.
    """

    def unit(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
        sx, sy = b[0] - a[0], b[1] - a[1]
        if abs(sx) >= abs(sy):
            return (math.copysign(1.0, sx) if sx else 0.0, 0.0)
        return (0.0, math.copysign(1.0, sy) if sy else 0.0)

    return unit(prev, corner), unit(corner, nxt)


def concentric_corner_radius_at(
    prev: tuple[float, float],
    corner: tuple[float, float],
    nxt: tuple[float, float],
    dx: float,
    base_radius: float = CURVE_RADIUS,
    *,
    min_radius: float | None = None,
) -> float:
    """Concentric radius for one bundle line, deriving the turn from the points.

    Geometry-keyed entry point for a wholesale-translated 90° corner: it reads
    the two travel vectors from the corner's own three waypoints (*prev* ->
    *corner* -> *nxt*) and forwards them to :func:`concentric_corner_radius`, so
    a caller supplies only the corner geometry and this line's signed
    displacement *dx* from the bundle reference - never a hand-picked sign.  Use
    this at every wholesale-translated bundle corner; reserve raw
    :func:`reference_anchored_radius` for transition corners (one leg pinned,
    e.g. a converging port jog) where non-concentric is intended.
    """
    turn_in, turn_out = _corner_travel_units(prev, corner, nxt)
    return concentric_corner_radius(
        turn_in, turn_out, dx, base_radius, min_radius=min_radius
    )


def widest_coincident_radius(radii: Iterable[float]) -> float:
    """The outermost of several corner radii meeting at one shared vertex.

    Same-line legs a coincidence pass fuses onto one channel each arrive with
    the radius their own handler produced; where they share a turn vertex the
    fused stroke must draw the widest so it nests outside any concentric bundle
    sharing that corner.  Selecting among radii the helper family already
    produced keeps corner sizing centralised in this module rather than
    scattered as inline arithmetic at the fusion site.
    """
    return max(radii)


def reference_anchored_radius(
    signed_offset: float,
    base_radius: float = CURVE_RADIUS,
    *,
    min_radius: float | None = None,
) -> float:
    """Concentric arc radius anchored on a *reference* line, not the innermost.

    ``corner_radius`` anchors the bundle's innermost line at *base_radius* so
    every radius is ``>= base_radius``.  The TOP-entry staircase into a port
    instead anchors a chosen **reference line** (the one kept continuous with
    its bundle-mates at the upstream junction) at *base_radius*, then offsets
    every other line by its signed perpendicular displacement from that
    reference.  Because that reference is interior to the bundle, lines on the
    inside of a turn fall **below** the base radius (``signed_offset < 0``).

    Both forms encode the same concentricity invariant ``radius - displacement
    = const``; they differ only in which line is pinned to *base_radius*.  This
    helper is the single source of truth for the reference-anchored variant.

    Parameters
    ----------
    signed_offset : float
        Perpendicular displacement of this line from the reference line at the
        corner, signed by the inside/outside sense of the turn (positive on the
        outside, negative on the inside).  The reference line itself has
        ``signed_offset == 0`` and therefore radius *base_radius*.
    base_radius : float
        Reference-line radius (the bundle-wide concentric centre offset).
    min_radius : float or None
        Optional floor.  Tight converging jogs onto a shared port point can
        drive ``base_radius + signed_offset`` to zero or below; pass a small
        positive floor (e.g. ``COORD_TOLERANCE``) to keep the arc renderable.
        ``None`` (default) applies no floor.

    Returns
    -------
    float
        ``base_radius + signed_offset`` (clamped up to *min_radius* if given).
    """
    r = base_radius + signed_offset
    if min_radius is not None:
        return max(min_radius, r)
    return r


# ---------------------------------------------------------------------------
# Standard inter-section L-shape (horizontal -> vertical -> horizontal)
# ---------------------------------------------------------------------------


def l_shape_stagger(
    i: int, n: int, vertical: Direction, offset_step: float = OFFSET_STEP
) -> float:
    """Signed lateral offset of line *i* in an *n*-line L-shape vertical channel.

    The ``delta`` half of :func:`l_shape_radii` -- where line *i* sits relative
    to the channel centre -- with no radius computation.  A centreline builder
    derives every corner radius from the geometry it lays out, so a handler that
    only needs to place the channel (not size a hand-rolled arc) takes this.

    Going DOWN, ``i = 0`` is rightmost (positive delta); going UP it is leftmost
    (negative delta), matching :func:`l_shape_radii`'s inside/outside convention.
    """
    if vertical is Direction.D:
        return ((n - 1) / 2 - i) * offset_step
    return (i - (n - 1) / 2) * offset_step


def l_shape_radii(
    i: int,
    n: int,
    vertical: Direction,
    offset_step: float = OFFSET_STEP,
    base_radius: float = CURVE_RADIUS,
) -> tuple[float, float, float]:
    """Compute offset and radii for a standard inter-section L-shape.

    An L-shape routes ``horizontal -> vertical -> horizontal`` with two
    corners.  The bundle of *n* parallel lines fans out in the vertical
    channel, and each line gets a different radius at each corner so
    the arcs are concentric (nested) rather than overlapping.

    Parameters
    ----------
    i : int
        This line's index within the bundle (0 to n-1), as assigned by
        ``compute_bundle_info()``.
    n : int
        Total number of lines in the bundle.
    vertical : Direction
        ``Direction.D`` if the vertical segment goes downward (dy > 0),
        ``Direction.U`` if it goes upward.
    offset_step : float
        Spacing between adjacent lines in the bundle.
    base_radius : float
        Minimum curve radius (innermost line).

    Returns
    -------
    delta : float
        X offset from the vertical channel center for this line.
    r_first : float
        Corner radius at the first turn (horizontal -> vertical).
    r_second : float
        Corner radius at the second turn (vertical -> horizontal).

    Geometry
    --------
    Going DOWN (right -> down -> right):
        * Corner 1 is a CW turn.  i=0 is placed rightmost (positive
          delta), on the outside, so it gets the largest radius.
        * Corner 2 is a CCW turn.  The rightmost line is now on the
          inside, so it gets the smallest radius.

    Going UP (right -> up -> right):
        * Corner 1 is a CCW turn.  i=0 is placed leftmost (negative
          delta), on the inside, so it gets the smallest radius.
        * Corner 2 is a CW turn.  The leftmost line is now on the
          outside, so it gets the largest radius.
    """
    off = (n - 1 - i) * offset_step
    max_off = (n - 1) * offset_step
    delta = l_shape_stagger(i, n, vertical, offset_step)

    if vertical is Direction.D:
        # Corner 1 (CW):  rightmost = outside -> largest radius
        r_first = corner_radius(off, max_off, outside=True, base_radius=base_radius)
        # Corner 2 (CCW): rightmost = inside  -> smallest radius
        r_second = corner_radius(off, max_off, outside=False, base_radius=base_radius)
    else:
        # Corner 1 (CCW): leftmost = inside  -> smallest radius
        r_first = corner_radius(off, max_off, outside=False, base_radius=base_radius)
        # Corner 2 (CW):  leftmost = outside -> largest radius
        r_second = corner_radius(off, max_off, outside=True, base_radius=base_radius)

    return delta, r_first, r_second


# ---------------------------------------------------------------------------
# Bypass (two back-to-back L-shapes)
# ---------------------------------------------------------------------------


def bypass_stagger(
    g1_i: int,
    g1_n: int,
    g2_i: int,
    g2_n: int,
    horizontal: Direction,
    offset_step: float = OFFSET_STEP,
    gap1_vertical: Direction = Direction.D,
    gap2_vertical: Direction = Direction.U,
) -> tuple[float, float]:
    """Per-line lateral offsets at a U-shaped bypass's two vertical channels.

    A bypass is two back-to-back L-shapes (corners 1-2 at gap1, corners 3-4 at
    gap2).  The centreline builder derives every corner radius from the geometry,
    so a handler needs only where each line sits in the two channels: this
    returns ``(delta1, delta2)``, the :func:`l_shape_stagger` of each gap.

    Parameters
    ----------
    g1_i, g1_n : int
        Line index and bundle size at gap1.
    g2_i, g2_n : int
        Line index and bundle size at gap2.
    horizontal : Direction
        ``Direction.R`` when the bypass travels rightward (dx > 0),
        ``Direction.L`` when leftward.  Left-going bypasses mirror the
        inside/outside assignment.
    gap1_vertical : Direction
        Vertical direction at gap1.  ``Direction.D`` when the trunk is below the
        source (standard case), ``Direction.U`` when the source is below it.
    gap2_vertical : Direction
        Vertical direction at gap2.  Almost always ``Direction.U``.
    """
    # For left-going bypasses, reverse indices so the outside/inside
    # assignment matches the mirrored corner geometry.
    going_right = horizontal is Direction.R
    g1_idx = g1_i if going_right else g1_n - 1 - g1_i
    g2_idx = g2_i if going_right else g2_n - 1 - g2_i
    delta1 = l_shape_stagger(g1_idx, g1_n, gap1_vertical, offset_step)
    delta2 = l_shape_stagger(g2_idx, g2_n, gap2_vertical, offset_step)
    return delta1, delta2
