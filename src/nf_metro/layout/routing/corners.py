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

from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP

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
    return base_radius + eff


# ---------------------------------------------------------------------------
# Standard inter-section L-shape (horizontal -> vertical -> horizontal)
# ---------------------------------------------------------------------------


def l_shape_radii(
    i: int,
    n: int,
    going_down: bool,
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
    going_down : bool
        ``True`` if the vertical segment goes downward (dy > 0).
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

    if going_down:
        # i=0 -> rightmost (positive delta)
        delta = ((n - 1) / 2 - i) * offset_step
        # Corner 1 (CW):  rightmost = outside -> largest radius
        r_first = corner_radius(off, max_off, outside=True, base_radius=base_radius)
        # Corner 2 (CCW): rightmost = inside  -> smallest radius
        r_second = corner_radius(off, max_off, outside=False, base_radius=base_radius)
    else:
        # i=0 -> leftmost (negative delta)
        delta = (i - (n - 1) / 2) * offset_step
        # Corner 1 (CCW): leftmost = inside  -> smallest radius
        r_first = corner_radius(off, max_off, outside=False, base_radius=base_radius)
        # Corner 2 (CW):  leftmost = outside -> largest radius
        r_second = corner_radius(off, max_off, outside=True, base_radius=base_radius)

    return delta, r_first, r_second


# ---------------------------------------------------------------------------
# Bypass (two back-to-back L-shapes)
# ---------------------------------------------------------------------------


def bypass_radii(
    g1_i: int,
    g1_n: int,
    g2_i: int,
    g2_n: int,
    going_right: bool,
    offset_step: float = OFFSET_STEP,
    base_radius: float = CURVE_RADIUS,
    gap1_going_down: bool = True,
    gap2_going_down: bool = False,
) -> tuple[float, float, float, float, float, float]:
    """Compute deltas and radii for a U-shaped bypass route.

    A bypass is two back-to-back L-shapes with corners 1-2 at gap1 and
    corners 3-4 at gap2.  This function wraps two ``l_shape_radii``
    calls so that all four corners satisfy the same ``delta + r = const``
    concentricity invariant used everywhere else.

    Parameters
    ----------
    g1_i, g1_n : int
        Line index and bundle size at gap1.
    g2_i, g2_n : int
        Line index and bundle size at gap2.
    going_right : bool
        ``True`` when the bypass travels rightward (dx > 0).
        Left-going bypasses mirror the inside/outside assignment.
    offset_step, base_radius : float
        Passed through to ``l_shape_radii``.
    gap1_going_down : bool
        Vertical direction at gap1.  ``True`` when the trunk is below
        the source (standard case), ``False`` when the source is below
        the trunk (e.g. bottom of a tall section bypassing a shorter
        neighbour).
    gap2_going_down : bool
        Vertical direction at gap2.  Almost always ``False`` (trunk
        below target).

    Returns
    -------
    delta1 : float
        X offset from gap1 channel center for this line.
    delta2 : float
        X offset from gap2 channel center for this line.
    r1, r2, r3, r4 : float
        Corner radii at each of the four corners.
    """
    # For left-going bypasses, reverse indices so the outside/inside
    # assignment matches the mirrored corner geometry.
    g1_idx = g1_i if going_right else g1_n - 1 - g1_i
    g2_idx = g2_i if going_right else g2_n - 1 - g2_i

    # Gap1 L-shape (corners 1 and 2)
    delta1, r1, r2 = l_shape_radii(
        g1_idx,
        g1_n,
        going_down=gap1_going_down,
        offset_step=offset_step,
        base_radius=base_radius,
    )
    # Gap2 L-shape (corners 3 and 4)
    delta2, r3, r4 = l_shape_radii(
        g2_idx,
        g2_n,
        going_down=gap2_going_down,
        offset_step=offset_step,
        base_radius=base_radius,
    )
    return delta1, delta2, r1, r2, r3, r4


# ---------------------------------------------------------------------------
# TB section LEFT/RIGHT exit L-shape (vertical drop -> horizontal)
# ---------------------------------------------------------------------------


def tb_exit_corner(
    src_off: float,
    max_src_off: float,
    exit_right: bool,
    base_radius: float = CURVE_RADIUS,
) -> tuple[float, float, float]:
    """Compute offsets and radius for a TB section exit L-shape.

    Routes: vertical drop from last station -> corner -> horizontal to
    the LEFT or RIGHT exit port.

    Parameters
    ----------
    src_off : float
        This line's X offset within the TB section.
    max_src_off : float
        Maximum X offset across all lines at this station.
    exit_right : bool
        ``True`` for a RIGHT exit port, ``False`` for LEFT.
    base_radius : float
        Minimum curve radius (innermost line).

    Returns
    -------
    vert_x_off : float
        X offset for the vertical segment.
    horiz_y_off : float
        Y offset for the horizontal segment.
    corner_radius : float
        Concentric arc radius at the corner.

    Geometry
    --------
    The horizontal Y offset always uses the reversed offset so that the
    outermost vertical line (furthest from center) maps to the largest
    radius.

    RIGHT exit (DOWN -> RIGHT, CCW turn):
        Vertical X uses the non-reversed offset.  The line with the
        largest non-reversed offset is on the outside of the CCW turn.

    LEFT exit (DOWN -> LEFT, CW turn):
        Vertical X uses the reversed offset.  The line with the largest
        reversed offset is on the outside of the CW turn.
    """
    rev = reversed_offset(src_off, max_src_off)
    horiz_y_off = rev
    # The line at src_off is on the INSIDE of both LEFT and RIGHT
    # exit turns, so the radius uses the reversed offset.
    r = corner_radius(src_off, max_src_off, outside=False, base_radius=base_radius)

    if exit_right:
        vert_x_off = src_off
    else:
        vert_x_off = rev

    return vert_x_off, horiz_y_off, r


# ---------------------------------------------------------------------------
# TB section LEFT/RIGHT entry L-shape (horizontal -> vertical drop)
# ---------------------------------------------------------------------------


def tb_entry_corner(
    tgt_off: float,
    max_tgt_off: float,
    entry_right: bool,
    base_radius: float = CURVE_RADIUS,
) -> tuple[float, float, float]:
    """Compute offsets and radius for a TB section entry L-shape.

    Routes: horizontal from LEFT or RIGHT entry port -> corner ->
    vertical drop to the first internal station.

    Parameters
    ----------
    tgt_off : float
        This line's X offset at the target station in the TB section.
    max_tgt_off : float
        Maximum X offset across all lines at the target station.
    entry_right : bool
        ``True`` for a RIGHT entry port, ``False`` for LEFT.
    base_radius : float
        Minimum curve radius (innermost line).

    Returns
    -------
    vert_x_off : float
        X offset for the vertical segment.
    corner_radius : float
        Concentric arc radius at the corner.

    Geometry
    --------
    RIGHT entry (LEFT -> DOWN, CW turn):
        Vertical X uses the non-reversed offset.

    LEFT entry (RIGHT -> DOWN, CCW turn):
        Vertical X uses the reversed offset.

    The corner radius always uses the reversed target offset so that
    the outermost vertical line gets the largest radius.
    """
    rev = reversed_offset(tgt_off, max_tgt_off)
    # The line at tgt_off is on the INSIDE of both LEFT and RIGHT
    # entry turns, so the radius uses the reversed offset.
    r = corner_radius(tgt_off, max_tgt_off, outside=False, base_radius=base_radius)

    if entry_right:
        vert_x_off = tgt_off
    else:
        vert_x_off = rev

    return vert_x_off, r
