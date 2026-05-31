"""Declarative inter-section routing descriptors.

:data:`WRAP_TABLE` catalogues the corner sequence each handler in
``core.py`` produces, keyed by ``(exit_side, entry_side, drow_sign,
dcol_sign)``.  ``exit_side`` is ``None`` when the source is a
junction without a port side; ``drow_sign`` / ``dcol_sign`` are
``sign()`` values in ``{-1, 0, +1}``.

The table is a documentation reference: nothing in routing consumes
it at runtime.  Bundle ordering across complex paths is preserved
by the per-corner offset propagation inside the wrap-route
handlers (handedness-aware radii at each turn); the runtime
:func:`~nf_metro.layout.routing.invariants.check_bundle_order_preserved`
guard catches any regression.

Same-row L-shapes and the TB ``(BOTTOM, TOP, 1, 0)`` straight drop
are absent because they're degenerate cases the wrap handlers
don't fire on; the dispatcher's if-cascade handles them directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from functools import cached_property

from nf_metro.layout.routing.common import Direction
from nf_metro.parser.model import PortSide

# ---------------------------------------------------------------------------
# Corner / turn-sequence primitives
# ---------------------------------------------------------------------------


class CornerHandedness(Enum):
    """Direction of rotation at a corner.

    ``CW`` (clockwise): a route going RIGHT that turns DOWN, or DOWN
    that turns LEFT, etc.  ``CCW`` (counter-clockwise) is the mirror.

    Used to compute :attr:`TurnSequence.parity`: an odd number of
    handedness *changes* (CW->CCW or CCW->CW between consecutive
    corners) inverts bundle ordering across the route.
    """

    CW = "CW"
    CCW = "CCW"


_CW_TURNS: frozenset[tuple[Direction, Direction]] = frozenset(
    {
        (Direction.R, Direction.D),
        (Direction.D, Direction.L),
        (Direction.L, Direction.U),
        (Direction.U, Direction.R),
    }
)
_CCW_TURNS: frozenset[tuple[Direction, Direction]] = frozenset(
    {
        (Direction.R, Direction.U),
        (Direction.U, Direction.L),
        (Direction.L, Direction.D),
        (Direction.D, Direction.R),
    }
)


@dataclass(frozen=True)
class Corner:
    """A single right-angle corner in a routed path.

    Attributes
    ----------
    in_tangent:
        Direction the route is travelling as it enters the corner.
    out_tangent:
        Direction it travels as it leaves the corner.  Must be
        perpendicular to ``in_tangent`` (no straight-through, no
        180-degree turn).
    concentric:
        ``True`` when this corner is part of a concentric bundle - i.e.
        all lines in the bundle share the same arc centre and only
        differ in radius.  ``False`` for an isolated turn.
    handedness:
        Computed property: ``CW`` for ``R->D / D->L / L->U / U->R``
        (the four screen-clockwise quarter turns), ``CCW`` for their
        mirrors.  Derived from ``in_tangent`` / ``out_tangent`` so
        hand-written descriptor tables can't drift from the geometry.
    """

    in_tangent: Direction
    out_tangent: Direction
    concentric: bool = True

    def __post_init__(self) -> None:
        pair = (self.in_tangent, self.out_tangent)
        if pair not in _CW_TURNS and pair not in _CCW_TURNS:
            raise ValueError(
                f"Corner tangents {self.in_tangent.value}->"
                f"{self.out_tangent.value} are not a right-angle turn "
                f"(same axis or identical direction)"
            )

    @property
    def handedness(self) -> CornerHandedness:
        if (self.in_tangent, self.out_tangent) in _CW_TURNS:
            return CornerHandedness.CW
        return CornerHandedness.CCW


@dataclass(frozen=True)
class TurnSequence:
    """An ordered sequence of corners along a routed inter-section path.

    The sequence represents the geometric "shape" of the route ignoring
    straight runs.  For example, a standard L-shape (right->down->right)
    is a 2-corner sequence ``[CW, CCW]``; a left-entry wrap
    (right->down->left->down->right) is a 4-corner sequence
    ``[CW, CW, CCW, CCW]``.

    The cached :attr:`parity` property captures whether the route ends
    up flipping the bundle's outer/inner ordering: ``True`` iff there
    is an odd number of handedness changes between consecutive corners.
    """

    corners: tuple[Corner, ...]

    def __len__(self) -> int:
        return len(self.corners)

    def __iter__(self) -> Iterator[Corner]:
        return iter(self.corners)

    def __getitem__(self, idx: int) -> Corner:
        return self.corners[idx]

    @cached_property
    def parity(self) -> bool:
        """``True`` iff the sequence has an odd number of handedness changes.

        A "change" is a pair of consecutive corners whose handedness
        differs (CW->CCW or CCW->CW).  Recorded for descriptor-level
        documentation; runtime wrap handlers preserve bundle ordering
        via per-corner offset propagation regardless of this value.

        Examples
        --------
        * Standard L-shape ``[CW, CCW]``: one change -> parity = True.
        * 4-corner wrap ``[CW, CW, CCW, CCW]``: one change -> parity = True.
        * Straight line (no corners): zero changes -> parity = False.
        * 2-corner same-handedness ``[CW, CW]``: zero changes -> parity = False.
        """
        changes = 0
        for prev, curr in zip(self.corners, self.corners[1:]):
            if prev.handedness != curr.handedness:
                changes += 1
        return changes % 2 == 1


# ---------------------------------------------------------------------------
# Wrap descriptor
# ---------------------------------------------------------------------------


class RouteKind(Enum):
    """Which handler in ``core.py`` produces a given corner sequence.

    Names match the dispatch sites; one enum member per handler.
    """

    L_SHAPE = "l_shape"
    TOP_ENTRY_L_SHAPE = "top_entry_l_shape"
    LEFT_ENTRY_WRAP = "left_entry_wrap"
    RIGHT_ENTRY_WRAP = "right_entry_wrap"
    TB_BOTTOM_EXIT = "tb_bottom_exit"


class ChannelKind(Enum):
    """Coarse classification of a route's main vertical/horizontal channel.

    * ``L_SHAPE`` - single vertical channel in the inter-column gap.
    * ``WRAP``    - route exits then re-enters the same column or wraps
      around the target section.
    * ``BYPASS``  - route goes around an intervening section.
    * ``TB_EXIT`` - vertical drop from a TB BOTTOM port.
    * ``STRAIGHT``- degenerate same-X or same-Y route.
    """

    L_SHAPE = "L_SHAPE"
    WRAP = "WRAP"
    BYPASS = "BYPASS"
    TB_EXIT = "TB_EXIT"
    STRAIGHT = "STRAIGHT"


@dataclass(frozen=True)
class WrapDescriptor:
    """Describes the corner sequence a routing handler produces."""

    kind: RouteKind
    turn_sequence: TurnSequence
    channel_kind: ChannelKind

    @property
    def parity(self) -> bool:
        """Convenience: forward to :attr:`TurnSequence.parity`."""
        return self.turn_sequence.parity


# ---------------------------------------------------------------------------
# Pre-built turn sequences keyed by route shape
# ---------------------------------------------------------------------------
# Each constant captures the shape of the route the corresponding
# ``core.py`` handler builds in coordinate space, ignoring straight
# runs between corners.

_L_RIGHT_DOWN_RIGHT = TurnSequence(
    corners=(
        Corner(Direction.R, Direction.D),
        Corner(Direction.D, Direction.R),
    )
)

_L_LEFT_DOWN_LEFT = TurnSequence(
    corners=(
        Corner(Direction.L, Direction.D),
        Corner(Direction.D, Direction.L),
    )
)

_L_RIGHT_UP_RIGHT = TurnSequence(
    corners=(
        Corner(Direction.R, Direction.U),
        Corner(Direction.U, Direction.R),
    )
)

# Left-entry wrap (source row above, target row below, entry on LEFT):
# right -> down -> left -> down -> right.  Four corners with one
# handedness change (parity = True).
_LEFT_ENTRY_WRAP_DOWN = TurnSequence(
    corners=(
        Corner(Direction.R, Direction.D),
        Corner(Direction.D, Direction.L),
        Corner(Direction.L, Direction.D),
        Corner(Direction.D, Direction.R),
    )
)

# Right-entry wrap mirror.
_RIGHT_ENTRY_WRAP_DOWN = TurnSequence(
    corners=(
        Corner(Direction.L, Direction.D),
        Corner(Direction.D, Direction.R),
        Corner(Direction.R, Direction.D),
        Corner(Direction.D, Direction.L),
    )
)

# TOP-entry L-shape variants.
_TOP_ENTRY_L_DOWN_FROM_RIGHT = TurnSequence(
    corners=(
        Corner(Direction.R, Direction.D),
        Corner(Direction.D, Direction.R),
    )
)
_TOP_ENTRY_L_DOWN_FROM_LEFT = TurnSequence(
    corners=(
        Corner(Direction.L, Direction.D),
        Corner(Direction.D, Direction.L),
    )
)


# ---------------------------------------------------------------------------
# WRAP_TABLE: declarative mirror of _route_inter_section's if-cascade
# ---------------------------------------------------------------------------
# Key tuple: (exit_side, entry_side, drow_sign, dcol_sign).  ``exit_side``
# is ``None`` when the source is a junction without a port side; entry
# sides are always known (every inter-section edge terminates at a port).
WRAP_TABLE: dict[tuple[PortSide | None, PortSide, int, int], WrapDescriptor] = {
    # ------- Cross-row L-shapes (dispatcher fallback line 635) ----------
    # Descending L: source exits RIGHT (LR section), target entry on
    # LEFT, one row down.
    (PortSide.RIGHT, PortSide.LEFT, 1, 1): WrapDescriptor(
        kind=RouteKind.L_SHAPE,
        turn_sequence=_L_RIGHT_DOWN_RIGHT,
        channel_kind=ChannelKind.L_SHAPE,
    ),
    # Ascending L (serpentine return).
    (PortSide.RIGHT, PortSide.LEFT, -1, 1): WrapDescriptor(
        kind=RouteKind.L_SHAPE,
        turn_sequence=_L_RIGHT_UP_RIGHT,
        channel_kind=ChannelKind.L_SHAPE,
    ),
    # ------- TOP entry L-shape (dispatcher line 528-529) ----------------
    # TB section reached from an LR predecessor on the LEFT.
    (PortSide.RIGHT, PortSide.TOP, 1, 1): WrapDescriptor(
        kind=RouteKind.TOP_ENTRY_L_SHAPE,
        turn_sequence=_TOP_ENTRY_L_DOWN_FROM_RIGHT,
        channel_kind=ChannelKind.L_SHAPE,
    ),
    # TB section reached from an RL predecessor on the RIGHT.
    (PortSide.LEFT, PortSide.TOP, 1, -1): WrapDescriptor(
        kind=RouteKind.TOP_ENTRY_L_SHAPE,
        turn_sequence=_TOP_ENTRY_L_DOWN_FROM_LEFT,
        channel_kind=ChannelKind.L_SHAPE,
    ),
    # ------- LEFT-entry cross-row wrap (dispatcher line 608-617) --------
    # Source row above, target row below, entry on LEFT, source column
    # to the RIGHT of the target column (dx < 0).  Four-corner zigzag.
    (PortSide.RIGHT, PortSide.LEFT, 1, -1): WrapDescriptor(
        kind=RouteKind.LEFT_ENTRY_WRAP,
        turn_sequence=_LEFT_ENTRY_WRAP_DOWN,
        channel_kind=ChannelKind.WRAP,
    ),
    # Junction source (exit-chain wraps around): same shape.
    (None, PortSide.LEFT, 1, -1): WrapDescriptor(
        kind=RouteKind.LEFT_ENTRY_WRAP,
        turn_sequence=_LEFT_ENTRY_WRAP_DOWN,
        channel_kind=ChannelKind.WRAP,
    ),
    # ------- RIGHT-entry cross-row wrap (dispatcher line 598-599) -------
    # Source to the LEFT of the target column, entry on RIGHT.  Wraps
    # over the top of the target section and drops in from the right.
    (PortSide.LEFT, PortSide.RIGHT, 1, 1): WrapDescriptor(
        kind=RouteKind.RIGHT_ENTRY_WRAP,
        turn_sequence=_RIGHT_ENTRY_WRAP_DOWN,
        channel_kind=ChannelKind.WRAP,
    ),
    # ------- TB BOTTOM exit to side-entry (dispatcher line 521-522) -----
    # TB section exits via BOTTOM, target entry on LEFT side.  Vertical
    # drop with X offsets terminating in an L-shape into the side port.
    (PortSide.BOTTOM, PortSide.LEFT, 1, 1): WrapDescriptor(
        kind=RouteKind.TB_BOTTOM_EXIT,
        turn_sequence=_L_RIGHT_DOWN_RIGHT,
        channel_kind=ChannelKind.TB_EXIT,
    ),
    (PortSide.BOTTOM, PortSide.LEFT, 1, -1): WrapDescriptor(
        kind=RouteKind.TB_BOTTOM_EXIT,
        turn_sequence=_L_LEFT_DOWN_LEFT,
        channel_kind=ChannelKind.TB_EXIT,
    ),
}


__all__ = [
    "ChannelKind",
    "Corner",
    "CornerHandedness",
    "RouteKind",
    "TurnSequence",
    "WRAP_TABLE",
    "WrapDescriptor",
]
