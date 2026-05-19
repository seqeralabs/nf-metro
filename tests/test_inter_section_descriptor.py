"""Tests for the inter-section routing descriptor scaffolding.

These tests pin down the parity contract of :class:`TurnSequence`
and check that every entry in :data:`WRAP_TABLE` agrees with the
parity ``_propagate_wrap_flip_parity`` would assign to a section
reached by the corresponding edge.

The parity alignment test is the load-bearing one: it is the early
warning that catches descriptor/propagator drift before C1/C2
unifies them into a single source of truth.
"""

from __future__ import annotations

from nf_metro.layout.routing.common import Direction
from nf_metro.layout.routing.inter_section import (
    WRAP_TABLE,
    Corner,
    CornerHandedness,
    TurnSequence,
    WrapDescriptor,
)

# ---------------------------------------------------------------------------
# TurnSequence.parity unit tests
# ---------------------------------------------------------------------------


def test_left_entry_wrap_parity_is_true():
    """The 4-corner left-entry wrap has odd handedness changes.

    Shape: right -> down -> left -> down -> right.

    Corner handednesses: ``[CW, CW, CCW, CCW]``.  Adjacent pairs:
    CW->CW (no change), CW->CCW (change), CCW->CCW (no change).
    One change overall -> parity = True.
    """
    seq = TurnSequence(
        corners=(
            Corner(Direction.R, Direction.D, CornerHandedness.CW),
            Corner(Direction.D, Direction.L, CornerHandedness.CW),
            Corner(Direction.L, Direction.D, CornerHandedness.CCW),
            Corner(Direction.D, Direction.R, CornerHandedness.CCW),
        )
    )
    assert seq.parity is True
    # Sanity: len works and iteration yields the corners in order
    assert len(seq) == 4
    assert [c.handedness for c in seq] == [
        CornerHandedness.CW,
        CornerHandedness.CW,
        CornerHandedness.CCW,
        CornerHandedness.CCW,
    ]


def test_l_shape_parity_is_true():
    """Standard L-shape (right -> down -> right) has one handedness change.

    Shape: ``[CW, CCW]`` -> exactly one change -> parity = True.
    """
    seq = TurnSequence(
        corners=(
            Corner(Direction.R, Direction.D, CornerHandedness.CW),
            Corner(Direction.D, Direction.R, CornerHandedness.CCW),
        )
    )
    assert seq.parity is True


def test_empty_turn_sequence_has_parity_false():
    """A degenerate straight route has no corners and parity = False."""
    seq = TurnSequence(corners=())
    assert seq.parity is False
    assert len(seq) == 0


def test_same_handedness_pair_parity_is_false():
    """Two consecutive corners with the same handedness: zero changes.

    A spiral-like ``[CW, CW]`` (the front half of a four-corner wrap)
    has zero handedness changes -> parity False.
    """
    seq = TurnSequence(
        corners=(
            Corner(Direction.R, Direction.D, CornerHandedness.CW),
            Corner(Direction.D, Direction.L, CornerHandedness.CW),
        )
    )
    assert seq.parity is False


def test_wrap_descriptor_parity_delegates_to_turn_sequence():
    """WrapDescriptor.parity is a thin shim over TurnSequence.parity."""
    seq = TurnSequence(
        corners=(
            Corner(Direction.R, Direction.D, CornerHandedness.CW),
            Corner(Direction.D, Direction.R, CornerHandedness.CCW),
        )
    )
    desc = WrapDescriptor(kind="test", turn_sequence=seq, channel_kind="L_SHAPE")
    assert desc.parity == seq.parity is True


# ---------------------------------------------------------------------------
# WRAP_TABLE alignment check
# ---------------------------------------------------------------------------
#
# The propagator's rule (see ``_propagate_wrap_flip_parity`` in
# ``engine.py``): each section's flip parity is
#
#     flip[tgt] = flip[src] XOR is_wrap
#
# where ``is_wrap`` is ``True`` iff the connecting edge spans
# different grid rows (drow_sign != 0).  Roots default to
# ``flip = False``.
#
# For a fresh section reached via a single edge from a root predecessor,
# the propagator's parity contribution is therefore exactly
# ``drow_sign != 0``.  Each descriptor's :attr:`TurnSequence.parity`
# must agree with that contribution: a route that crosses rows must
# emit a corner sequence with odd handedness changes, and a same-row
# route must emit one with even changes.  Drift in either direction
# will eventually manifest as visible line crossings at the entry-port
# quarter-circles.


def test_wrap_table_parity_matches_propagator_contribution():
    """Every WRAP_TABLE entry's parity matches what the propagator yields.

    For an edge with ``drow_sign = D``, the propagator contributes
    ``D != 0`` to the target section's flip flag.  Each descriptor's
    ``TurnSequence.parity`` must equal that contribution.
    """
    mismatches: list[str] = []
    for key, descriptor in WRAP_TABLE.items():
        _exit_side, _entry_side, drow_sign, _dcol_sign = key
        propagator_contribution = drow_sign != 0
        if descriptor.parity != propagator_contribution:
            mismatches.append(
                f"{key} ({descriptor.kind}): descriptor.parity = "
                f"{descriptor.parity}, propagator_contribution = "
                f"{propagator_contribution}"
            )
    assert not mismatches, "WRAP_TABLE / propagator drift:\n  " + "\n  ".join(
        mismatches
    )


def test_wrap_table_keys_are_well_formed():
    """Sanity: keys use legal exit/entry sides and -1/0/+1 signs."""
    legal_exit = {"LEFT", "RIGHT", "TOP", "BOTTOM", "JUNCTION"}
    legal_entry = {"LEFT", "RIGHT", "TOP", "BOTTOM"}
    for key in WRAP_TABLE:
        exit_side, entry_side, drow_sign, dcol_sign = key
        assert exit_side in legal_exit, f"bad exit_side: {key}"
        assert entry_side in legal_entry, f"bad entry_side: {key}"
        assert drow_sign in (-1, 0, 1), f"bad drow_sign: {key}"
        assert dcol_sign in (-1, 0, 1), f"bad dcol_sign: {key}"


def test_wrap_table_is_non_empty():
    """WRAP_TABLE must cover at least the genuine wrap cases.

    Catches accidental deletions during refactors.  If you legitimately
    want to remove every entry (e.g. moving the table elsewhere), update
    this test to reflect the new home.
    """
    assert len(WRAP_TABLE) >= 4, "WRAP_TABLE shrank below the wrap-handler floor"
