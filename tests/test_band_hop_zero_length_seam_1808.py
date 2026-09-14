"""Regression lock for #1808: a band-hop that drops at the junction column.

When a boxed-in fan-out junction feeds a LEFT entry by hopping two inter-row
bands and no cell-mate gap resolves for its lead-out, the branch drops straight
down the junction column instead of leading out first. That makes the source
seam's run leg zero-length. The correct reading is a turn-less seam (the member
states only the drop it opens on), matching the coincident-column bottom-exit
precedent; a zero-length run leg is legitimate geometry, not an error.

This locks that the frozen nf-core/riboseq layout whose grid partition traps
such a junction renders without a bare, message-less ``AssertionError`` from
the seam's leg-direction reading.

The fixture is a frozen partition of the nf-core/riboseq map rather than the
shipped map itself so the lock tracks the exact geometry that reproduces the
defect, not a map that may drift.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro import render_string
from nf_metro.layout.routing.invariants import CurveInvariantError

# nf-core/riboseq with a grid partition (row cuts after preprocessing/alignment,
# novel_transcripts, and the orf_calling/psite/te_gene band) that seats a
# straddling fan-out junction's te_orf branch on the band-hop path.
RIBOSEQ_BAND_HOP = (
    Path(__file__).parent
    / "fixtures"
    / "curve_invariant_repros"
    / "riboseq_band_hop_zero_length_seam.mmd"
).read_text()


def test_band_hop_drop_at_junction_column_no_bare_assert() -> None:
    """A zero-length band-hop seam leg resolves to a turn-less seam, no bare assert.

    #1808 is scoped to seam construction only. The curve invariant that guards
    the final routes is downstream of it, so reaching that stage proves the seam
    was built without the assert: an unrelated fan-overlay curve defect
    (#1806/#1809) aborts this fixture there, and that abort is an accepted pass
    for #1808. What must never recur is the bare ``AssertionError`` from reading
    a heading off the zero-length run leg.
    """
    try:
        svg = render_string(RIBOSEQ_BAND_HOP)
    except CurveInvariantError:
        return
    assert svg.startswith("<")
