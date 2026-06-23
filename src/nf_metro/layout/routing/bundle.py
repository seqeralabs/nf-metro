"""Constructive bundle-curve builder.

The single entry point for turning a multi-line bundle around corners.  A
caller describes only the bundle's **centreline** -- a polyline of axis-aligned
vertices -- plus each line's signed perpendicular offset from it.
:func:`build_concentric_bundle` emits each line as a rigid parallel offset of
that centreline, deriving every corner radius from the offsets it holds.

Why this exists: hand-assembling per-line ``points`` and ``curve_radii`` is the
most common source of broken renders.  A handler can offset one leg the wrong
way (the bundle flips and the lines cross), hand-pick a corner radius that
nests non-concentrically (the bundle pinches through the bend), or feed a base
radius that pulls an inside-of-turn arc below the floor.  All three are
*impossible* here by construction:

* Each line is the same centreline shifted by a constant perpendicular
  distance, so the lines keep a constant side-of-travel order -- no flip.
* Radii are derived from the turn geometry, never supplied by the caller -- so
  no hand-picked sign.
* Every corner is **anchored on the bundle's innermost-of-turn line**: the
  builder shifts the whole corner so the line deepest inside the turn lands at
  ``base_radius`` and every other line fans outward ``>= base_radius``.  The
  caller passes only the floor (``base_radius``), never a per-corner or
  half-width-bumped value -- the builder owns the anchor, derived from the
  offsets it already holds.

Prefer this over building bundle routes by hand: the render-path curve guard
in ``invariants`` is a backstop for paths built another way, not the mechanism
that keeps these correct.
"""

from __future__ import annotations

from collections.abc import Sequence

from nf_metro.layout.constants import COORD_TOLERANCE
from nf_metro.layout.routing.common import OffsetRegime, RoutedPath
from nf_metro.layout.routing.corners import reference_anchored_radius
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
    bundle_offsets: Sequence[float] | None = None,
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
        Floor corner radius: the radius the *innermost-of-turn* line takes at
        every corner.  Pass the global ``curve_radius`` -- never a value
        pre-bumped by the bundle's half-width; the builder derives the anchor
        itself (see module docstring).
    min_radius
        Optional hard floor for the resulting arcs (see
        :func:`~nf_metro.layout.routing.corners.reference_anchored_radius`).
    bundle_offsets
        The signed offsets of all lines in the co-travelling bundle, used to
        anchor each corner on the innermost line.  A handler that routes its
        siblings one at a time (so *members* holds a single line) passes the
        full fan here, so the lone member nests within the bundle's spread.
        ``None`` (the default) anchors on *members* themselves -- the right
        choice when the bundle is gathered whole.

    Returns
    -------
    One :class:`RoutedPath` per member, each a parallel offset of *centerline*
    with concentric corner radii.  Each is :attr:`OffsetRegime.BAKED`: the
    per-line offset is in the points, not left to the renderer's heuristic.
    """
    n_legs = len(centerline) - 1
    if n_legs < 1:
        raise ValueError("centerline needs at least two vertices")
    anchor = (
        [[s] * n_legs for s in bundle_offsets] if bundle_offsets is not None else None
    )
    return _fan_bundle(
        [(edge, line_id, [s] * n_legs) for edge, line_id, s in members],
        centerline,
        base_radius,
        min_radius=min_radius,
        anchor_offsets=anchor,
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
    bundle_offsets: Sequence[tuple[float, float]] | None = None,
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
    not share a centre (the two legs are fanned by different amounts), yet it is
    anchored on the innermost-of-turn line so no line falls below the floor.  A
    *wholesale* corner -- both flanking legs carrying one offset -- is genuinely
    concentric, as in :func:`build_concentric_bundle`.

    Parameters
    ----------
    members
        ``(edge, line_id, src_offset, tgt_offset)`` per line.  ``src_offset ==
        tgt_offset`` for every line reduces to the rigid bundle, equivalent to
        :func:`build_concentric_bundle`.
    centerline
        ``>= 2`` axis-aligned vertices; each consecutive pair differs in exactly
        one axis.
    transition_leg
        Index of the first leg that carries ``tgt_offset`` (``1`` for the common
        inter-section L-shape: the source-side leg fans by ``src_offset``, the
        channel and target-side legs by ``tgt_offset``).
    base_radius
        Floor corner radius: the radius the innermost-of-turn line takes.  Pass
        the global ``curve_radius``; the builder derives the per-corner anchor
        from the offsets (see :func:`build_concentric_bundle`).
    min_radius
        Optional floor for inside-of-turn arcs.
    bundle_offsets
        The ``(src_offset, tgt_offset)`` of every line in the co-travelling
        bundle, used to anchor each corner on the innermost line.  A handler that
        routes its siblings one at a time (so *members* holds a single line)
        passes the full fan here -- the source-region corners anchor on the
        source spread and the target-region corners on the target spread, so a
        tapering U whose two gaps carry different line counts nests correctly at
        both ends.  ``None`` anchors on *members* themselves.
    """
    n_legs = len(centerline) - 1
    if n_legs < 1:
        raise ValueError("centerline needs at least two vertices")
    if not 0 <= transition_leg <= n_legs:
        raise ValueError(f"transition_leg {transition_leg} out of range [0, {n_legs}]")

    def per_leg(src: float, tgt: float) -> list[float]:
        return [src if leg < transition_leg else tgt for leg in range(n_legs)]

    anchor = (
        [per_leg(src, tgt) for src, tgt in bundle_offsets]
        if bundle_offsets is not None
        else None
    )
    return _fan_bundle(
        [(edge, line_id, per_leg(src, tgt)) for edge, line_id, src, tgt in members],
        centerline,
        base_radius,
        min_radius=min_radius,
        anchor_offsets=anchor,
        is_inter_section=is_inter_section,
        normalize_exempt=normalize_exempt,
    )


def build_offset_bundle(
    members: list[tuple[Edge, str, list[float]]],
    centerline: list[_Vec],
    base_radius: float,
    *,
    min_radius: float | None = None,
    bundle_offsets: Sequence[Sequence[float]] | None = None,
    is_inter_section: bool = True,
    normalize_exempt: bool = True,
) -> list[RoutedPath]:
    """Fan a bundle whose per-leg offsets are given explicitly, leg by leg.

    The most general builder.  :func:`build_concentric_bundle` holds one offset
    on every leg and :func:`build_tapered_bundle` switches between two at a
    single transition; a shape that fans by a *different* amount on more than two
    legs -- an H-V-H staircase that departs a shared port at offset zero, fans
    out only in its vertical channel, then lands at a third offset -- needs all
    of its per-leg offsets stated directly.  Each member carries one signed
    perpendicular offset per centreline leg, and every corner anchors on the
    bundle's innermost-of-turn line, so no arc falls below the floor.

    Parameters
    ----------
    members
        ``(edge, line_id, leg_offsets)`` per line, ``leg_offsets`` one signed
        perpendicular offset per centreline leg (``len(centerline) - 1`` of
        them).
    centerline
        ``>= 2`` axis-aligned vertices; each consecutive pair differs in exactly
        one axis.
    base_radius
        Floor corner radius for the innermost-of-turn line.
    min_radius
        Optional floor for inside-of-turn arcs.
    bundle_offsets
        The per-leg offsets of every line in the co-travelling bundle, anchoring
        each corner on the innermost line.  A handler routing its siblings one at
        a time passes the full fan here; ``None`` anchors on *members*.
    """
    n_legs = len(centerline) - 1
    if n_legs < 1:
        raise ValueError("centerline needs at least two vertices")
    return _fan_bundle(
        [(edge, line_id, list(offs)) for edge, line_id, offs in members],
        centerline,
        base_radius,
        min_radius=min_radius,
        anchor_offsets=[list(o) for o in bundle_offsets]
        if bundle_offsets is not None
        else None,
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
    anchor_offsets: list[list[float]] | None = None,
) -> list[RoutedPath]:
    """Emit one route per member from explicit per-leg offsets.

    ``members`` is ``(edge, line_id, leg_offsets)`` with one signed
    perpendicular offset per centreline leg.  A constant offset across all legs
    is a rigid bundle; an offset that switches between legs tapers.  At every
    interior vertex only the *vertical* leg displaces X, so its signed X
    displacement, projected onto the turn, gives each line a *signed offset*:
    positive on the outside of the bend (larger radius), negative on the inside.

    Each corner is anchored on the innermost-of-turn line of the whole bundle:
    the smallest signed offset across the bundle is subtracted from every line's,
    so the innermost lands at ``base_radius`` and the rest fan outward.  This
    derives the anchor from the offsets alone, so no caller pre-bumps the base by
    the bundle's half-width.  ``anchor_offsets`` is the per-leg offsets of the
    full bundle; ``None`` anchors on *members* themselves (the bundle is whole).
    """
    legs = [
        _axis_unit(centerline[i], centerline[i + 1]) for i in range(len(centerline) - 1)
    ]
    normals = [_right_normal(t) for t in legs]

    def signed_offset(offs: list[float], vi: int) -> float:
        """This line's corner-radius offset at interior vertex *vi*.

        The line's signed X displacement from the centreline at the corner,
        projected onto the turn (``turn_out_x - turn_in_x``) so an outside-of-turn
        line is positive and an inside one negative.  This is the ``-dx * ux``
        projection :func:`~nf_metro.layout.routing.corners.concentric_corner_radius`
        feeds to ``reference_anchored_radius``; here it is taken raw so the
        per-corner anchor can be subtracted before sizing.
        """
        vertical_dx = offs[vi - 1] * normals[vi - 1][0] + offs[vi] * normals[vi][0]
        return -vertical_dx * (legs[vi][0] - legs[vi - 1][0])

    anchor_bundle = (
        anchor_offsets
        if anchor_offsets is not None
        else [offs for _e, _l, offs in members]
    )
    corner_anchor = {
        vi: min(signed_offset(offs, vi) for offs in anchor_bundle)
        for vi in range(1, len(centerline) - 1)
    }

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
                # leg that bends it.
                o_in, o_out = offs[vi - 1], offs[vi]
                n_in, n_out = normals[vi - 1], normals[vi]
                px = cx + o_in * n_in[0] + o_out * n_out[0]
                py = cy + o_in * n_in[1] + o_out * n_out[1]
                sm = signed_offset(offs, vi)
                # The anchor is the innermost line of the declared bundle, so an
                # emitted member can only land at/above it.  A member below the
                # anchor means the bundle passed for anchoring did not include
                # this line (a single-member caller's bundle_offsets is wrong),
                # which would re-introduce a sub-floor arc.
                assert sm >= corner_anchor[vi] - COORD_TOLERANCE, (
                    f"bundle member {line_id!r} offset {sm:.2f} lies inside the "
                    f"anchor {corner_anchor[vi]:.2f} at corner {vi}; the bundle "
                    "passed for anchoring must include every emitted line"
                )
                radii.append(
                    reference_anchored_radius(
                        sm - corner_anchor[vi], base_radius, min_radius=min_radius
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
                offset_regime=OffsetRegime.BAKED,
            )
        )
    return routes
